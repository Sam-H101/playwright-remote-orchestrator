"""Signing people in to the Grid UI with an OpenID Connect identity provider: settings, discovery, signing keys,
the authorize URL, redeeming the code and checking the ID token.

Nothing here touches the database or Flask: app.py runs the flow and accounts.py keeps the users and sessions.
Only the ID token is used, and only once its signature, issuer, audience, times and nonce check out: the access
and refresh tokens the provider sends with it are dropped at once, and its userinfo endpoint is never asked.
Every address fetched comes from GRID_OIDC_ISSUER or the provider's discovery document, never from a request,
and none of them is followed through a redirect.
"""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

import jwt

from . import store

log = logging.getLogger(__name__)

DISCOVERY_PATH = "/.well-known/openid-configuration"
SCOPES = ("openid", "email", "profile")
# Asymmetric algorithms only. none and the HS* family are refused outright: an HS256 token "signed" with the
# provider's public key is the classic forgery.
ALLOWED_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
_CURVES = {"ES256": ("P-256",), "ES384": ("P-384",), "ES512": ("P-521",), "EdDSA": ("Ed25519", "Ed448")}
# Entra's multi-tenant addresses: their discovery documents state a template, never an issuer a token carries.
MULTI_TENANT = ("common", "organizations", "consumers")
CLIENT_AUTH = ("auto", "basic", "post")

# The back channel: how long each read from the provider may wait, and how much it may answer.
FETCH_TIMEOUT_S = 5
FETCH_LIMIT = 256 * 1024
TOKEN_TIMEOUT_S = 10
TOKEN_LIMIT = 1024 * 1024
# Discovery and the key set are kept this long. While the provider can't give fresh ones, the last good copy
# stands in for up to STALE_S, asking again at most every RETRY_S. A key id nobody has heard of sends for the key
# set again at most every KEY_REFETCH_S, so made-up ids can't make the Grid UI hammer the provider.
CACHE_S = 3600
STALE_S = 86400
RETRY_S = 60
KEY_REFETCH_S = 60
MAX_ID_TOKEN_BYTES = 64 * 1024
MAX_TEXT = 255
MAX_GROUPS = 500
# Unicode categories no claim kept as text may hold: control characters, and lone surrogates (Cs), which JSON can
# carry but no database can store.
_UNWANTED = ("Cc", "Cs")

Settings = namedtuple("Settings", "issuer client_id client_secret client_auth public_url public_path public_host label "
                                  "scopes groups_claims admins end_session ca_file allow_http skew session_hours "
                                  "idle_minutes")
# The cookies' names and attributes. host_only: the names carry __Host-, so no sibling subdomain can set them.
CookieNames = namedtuple("CookieNames", "session login path secure host_only")
# A sign-in about to start: the browser value the login cookie carries, the state and nonce sent to the
# provider, and the PKCE verifier with its S256 challenge.
Login = namedtuple("Login", "browser state nonce verifier challenge")
# A session the lookup found but won't accept, and why: unknown, revoked, disabled, expired or idle.
Ended = namedtuple("Ended", "reason")
# What the cache holds: whose it is (the issuer, or the key set's address), what, when it was fetched, and the
# earliest a failed refresh may be tried again.
_Cached = namedtuple("_Cached", "source value fetched retry_at")

# name -> (default, lowest, highest, what it counts, and the (value, meaning) it may take outside that range)
_NUMBERS = {
    "GRID_OIDC_CLOCK_SKEW_S": (60, 0, 300, "seconds", None),
    "GRID_SESSION_HOURS": (12, 1, 168, "hours", None),
    "GRID_SESSION_IDLE_MINUTES": (120, 5, 1440, "minutes", (0, "never")),
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
_BROWSER_VALUE = re.compile(r"[A-Za-z0-9_-]{43}")
_ERROR_CODE = re.compile(r"[A-Za-z0-9_.-]{1,64}")
_USER_AGENT = "playwright-grid-ui"

_cache_lock = threading.Lock()
_discovered = None
_key_set = None
_keys_refetched = None  # time.monotonic() of the last refetch an unknown key id asked for


class Unavailable(Exception):
    """The identity provider can't be reached, or answered something other than the JSON asked for. The message
    says which, for the log; it never reaches a page."""


class HttpRefused(Exception):
    """The identity provider answered an HTTP error. error is the OAuth error code it gave ("invalid_client"),
    or None: nothing else of the answer is kept."""

    def __init__(self, status, error=None):
        super().__init__("HTTP {}{}".format(status, " " + error if error else ""))
        self.status = status
        self.error = error


class Misconfigured(Exception):
    """The provider's discovery document doesn't fit the settings (another issuer, an address that isn't https,
    no algorithm the Grid UI accepts): the message says how, for the log."""


class SignInFailed(Exception):
    """An ID token, or the token endpoint's answer, that can't be trusted. reason is a short code for the audit
    log, the message says more for the server's log, and subject is the token's sub when it was read and usable
    (else None)."""

    def __init__(self, reason, detail=None, subject=None):
        super().__init__(detail or reason)
        self.reason = reason
        self.subject = subject


# ---- settings -----------------------------------------------------------------------


def _env(name, default=""):
    return os.environ.get(name, "").strip() or default


def _number(name):
    """A whole-number setting, or None when it isn't one in its range. Unset means its default."""
    default, low, high, _, other = _NUMBERS[name]
    text = _env(name)
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return None
    return value if low <= value <= high or (other and value == other[0]) else None


def _number_or_default(name):
    value = _number(name)
    return _NUMBERS[name][0] if value is None else value


def _split(url):
    try:
        return urllib.parse.urlsplit(url)
    except ValueError:  # e.g. an unclosed [ in an IPv6 address
        return None


def normal_host(netloc):
    """hostname[:port] of a URL's netloc or a Host header, lower case, without :80 or :443: the form the public
    URL's host and a request's are compared in. "" when it doesn't parse."""
    try:
        parts = urllib.parse.urlsplit("//" + netloc)
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return ""
    if ":" in host:
        host = "[" + host + "]"
    return host if port in (None, 80, 443) else "{}:{}".format(host, port)


def settings():
    """Every single sign-on setting, read now: tests change them between requests. A number out of range reads
    as its default here; problem() is what reports it."""
    public_url = _env("GRID_UI_PUBLIC_URL")
    if public_url and not public_url.endswith("/"):
        public_url += "/"
    public = _split(public_url)
    scopes = []
    for scope in _env("GRID_OIDC_SCOPES").split():
        if scope not in SCOPES and scope not in scopes:
            scopes.append(scope)
    claims = [c.strip() for c in _env("GRID_OIDC_GROUPS_CLAIM", "groups").split(",") if c.strip()]
    return Settings(
        issuer=_env("GRID_OIDC_ISSUER"),
        client_id=_env("GRID_OIDC_CLIENT_ID"),
        client_secret=_env("GRID_OIDC_CLIENT_SECRET"),
        client_auth=_env("GRID_OIDC_CLIENT_AUTH", "auto").lower(),
        public_url=public_url,
        public_path=(public.path if public else "") or "/",
        public_host=normal_host(public.netloc) if public else "",
        label=_env("GRID_OIDC_LABEL", "single sign-on"),
        scopes=tuple(scopes),
        groups_claims=tuple(dict.fromkeys(claims)) or ("groups",),
        admins=tuple(a.strip() for a in _env("GRID_OIDC_ADMINS").split(",") if a.strip()),
        end_session=_env("GRID_OIDC_END_SESSION") == "1",
        ca_file=_env("GRID_OIDC_CA_FILE"),
        allow_http=_env("GRID_OIDC_ALLOW_HTTP") == "1",
        skew=_number_or_default("GRID_OIDC_CLOCK_SKEW_S"),
        session_hours=_number_or_default("GRID_SESSION_HOURS"),
        idle_minutes=_number_or_default("GRID_SESSION_IDLE_MINUTES"),
    )


def configured():
    """Whether single sign-on is on: GRID_OIDC_ISSUER is set, even when the rest is wrong (the grid then stays
    protected, and problem() says what to fix)."""
    return bool(_env("GRID_OIDC_ISSUER"))


def cookie_names():
    """The session and login cookies' names, path and flags. __Host- names (which no sibling subdomain can set)
    need https and the path /; a public URL with a path prefix, or plain http, gets plain names on that path."""
    s = settings()
    https = s.public_url.lower().startswith("https://")
    if https and s.public_path == "/":
        return CookieNames("__Host-grid-session", "__Host-grid-login", "/", True, True)
    return CookieNames("grid-session", "grid-login", s.public_path, https, False)


def redirect_uri():
    """Where the provider sends the browser back to; register it at the provider."""
    return settings().public_url + "auth/callback"


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allowed_url(url, allow_http):
    """An absolute https URL; http only to a loopback host, or anywhere with GRID_OIDC_ALLOW_HTTP=1."""
    parts = _split(url) if isinstance(url, str) else None
    if not parts or not parts.hostname:
        return False
    if parts.scheme == "https":
        return True
    return parts.scheme == "http" and (allow_http or _loopback(parts.hostname))


def _ca_file_problem(path):
    """Why the extra CA certificates can't be used, in words of our own, or None. Admins read this on the page,
    so no exception's text goes into it."""
    if not os.path.isfile(path):
        return "there is no file {}".format(path)
    if not os.access(path, os.R_OK):
        return "the Grid UI's user may not read {}".format(path)
    try:
        ssl.create_default_context().load_verify_locations(cafile=path)
    except ssl.SSLError:
        return "{} holds no PEM certificates".format(path)
    except OSError:
        return "{} can't be opened".format(path)
    return None


def _first_problem(s):
    if not s.client_id or not s.client_secret:
        return "Set GRID_OIDC_CLIENT_ID and GRID_OIDC_CLIENT_SECRET to the Grid UI's registration at the identity provider."
    public = _split(s.public_url)
    if not public or public.scheme not in ("http", "https") or not public.hostname:
        return "Set GRID_UI_PUBLIC_URL to the address people open the Grid UI at, e.g. https://grid.example.com/."
    for name, url in (("GRID_OIDC_ISSUER", s.issuer), ("GRID_UI_PUBLIC_URL", s.public_url)):
        if not _allowed_url(url, s.allow_http):
            return "{} must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development).".format(name)
    if _split(s.issuer).path.strip("/").split("/")[0].lower() in MULTI_TENANT:
        return ("GRID_OIDC_ISSUER is a multi-tenant address (common, organizations or consumers), which no ID token "
                "states as its issuer. Use your tenant's: https://login.microsoftonline.com/<tenant id>/v2.0.")
    if s.client_auth not in CLIENT_AUTH:
        return "GRID_OIDC_CLIENT_AUTH is auto, basic or post."
    for entry in s.admins:
        if not (entry.startswith("sub:") and len(entry) > 4) and not _EMAIL.fullmatch(entry):
            return "GRID_OIDC_ADMINS entry {!r} is neither an email address nor sub:<subject>.".format(entry)
    for name, (_, low, high, unit, other) in _NUMBERS.items():
        if _number(name) is None:
            return "{} is a number of {} from {} to {}{}.".format(
                name, unit, low, high, ", or {} for {}".format(*other) if other else "")
    if s.ca_file:
        reason = _ca_file_problem(s.ca_file)
        if reason:
            return "GRID_OIDC_CA_FILE can't be read: {}.".format(reason)
    return None


def problem():
    """Why single sign-on can't work as configured, as a sentence for the log and for admins, or None (also when
    GRID_OIDC_ISSUER is unset). Worked out on every call, like mfa.key_problem(); each distinct one is logged
    once."""
    s = settings()
    if not s.issuer:
        return None
    text = _first_problem(s)
    if text:
        store.warn_once(text, log)
    elif s.allow_http and not all(u.lower().startswith("https://") for u in (s.issuer, s.public_url)):
        store.warn_once("GRID_OIDC_ALLOW_HTTP=1: single sign-on runs over plain http, which is for development only.", log)
    return text


# ---- the back channel ---------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The provider's redirects are refused, never followed: every address comes from the settings or the
    discovery document."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the 3xx as an HTTPError


def _opener(s):
    context = ssl.create_default_context()
    if s.ca_file:
        try:
            context.load_verify_locations(cafile=s.ca_file)  # on top of the system's
        except OSError:
            raise Unavailable("GRID_OIDC_CA_FILE can't be used (see the warning about it)") from None
    return urllib.request.build_opener(_NoRedirects, urllib.request.HTTPSHandler(context=context))


def _error_code(answer):
    """The OAuth error code of an error answer ("invalid_grant"), or None."""
    try:
        body = json.loads(answer.read(4096))
    except (ValueError, RecursionError, OSError, http.client.HTTPException):
        return None
    finally:
        answer.close()
    error = body.get("error") if isinstance(body, dict) else None
    return error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else None


def _fetch(url, data=None, headers=None, timeout=None, limit=None):
    """The JSON object a GET of url answers (a POST of data, form-encoded bytes, when given).

    https only, except to a loopback host or with GRID_OIDC_ALLOW_HTTP=1; no redirects; at most limit bytes
    (FETCH_LIMIT); application/json or a +json type, whatever its parameters. Raises Unavailable, or HttpRefused
    for an HTTP error. The one way out to the provider, so tests replace it here."""
    s = settings()
    limit = limit or FETCH_LIMIT
    if not _allowed_url(url, s.allow_http):
        raise Unavailable("the identity provider gave an address that isn't https")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}, Accept="application/json"))
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with _opener(s).open(request, timeout=timeout or FETCH_TIMEOUT_S) as answer:
            body = answer.read(limit + 1)
            media = answer.headers.get_content_type()
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            e.close()
            raise Unavailable("the identity provider answered with a redirect (HTTP {}), which isn't followed".format(
                e.code)) from None
        raise HttpRefused(e.code, _error_code(e)) from None
    except urllib.error.URLError as e:
        raise Unavailable("the identity provider can't be reached: {}".format(e.reason)) from None
    except (OSError, http.client.HTTPException) as e:  # timeouts and dropped connections while reading
        raise Unavailable("the identity provider can't be reached: {}".format(str(e) or type(e).__name__)) from None
    except ValueError:
        raise Unavailable("the identity provider gave an address that can't be fetched") from None
    if len(body) > limit:
        raise Unavailable("the identity provider answered more than {} bytes".format(limit))
    if media != "application/json" and not media.endswith("+json"):
        raise Unavailable("the identity provider answered {} where JSON was expected".format(media))
    try:
        document = json.loads(body)
    except (ValueError, RecursionError):
        raise Unavailable("the identity provider's answer isn't valid JSON") from None
    if not isinstance(document, dict):
        raise Unavailable("the identity provider's answer isn't a JSON object")
    return document


def _listed(document, field, default):
    value = document.get(field, default)
    return value if isinstance(value, list) else []


def _algorithms(document):
    """The algorithms an ID token from this provider may use: ours that its discovery document lists."""
    listed = _listed(document, "id_token_signing_alg_values_supported", ["RS256"])
    return [alg for alg in ALLOWED_ALGS if alg in listed]


def _check_discovery(document, s):
    if document.get("issuer") != s.issuer:
        raise Misconfigured("the discovery document's issuer is {!r}, but GRID_OIDC_ISSUER is {!r}: they must be "
                            "equal, trailing / and all".format(document.get("issuer"), s.issuer))
    for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not _allowed_url(document.get(field), s.allow_http):
            raise Misconfigured("the discovery document's {} isn't an https URL".format(field))
    if "code" not in _listed(document, "response_types_supported", ["code"]):
        raise Misconfigured("the identity provider doesn't offer the authorization code flow (response type code)")
    if "S256" not in _listed(document, "code_challenge_methods_supported", ["S256"]):
        raise Misconfigured("the identity provider doesn't take S256 PKCE challenges")
    if not _algorithms(document):
        raise Misconfigured("the identity provider signs ID tokens with none of {}".format(", ".join(ALLOWED_ALGS)))


def _cached_or_fetch(cached, source, refresh, fetch, what):
    """cached's value while it is fresh (and no refresh is asked for), else fetch()'s. When fetching fails, a copy
    younger than STALE_S stands in, and the next try waits RETRY_S. Returns (the value, a new _Cached to keep or
    None)."""
    now = time.monotonic()
    if cached and (now < cached.retry_at or (not refresh and now - cached.fetched < CACHE_S)):
        return cached.value, None
    try:
        value = fetch()
    except (Unavailable, HttpRefused, Misconfigured) as e:
        if not cached or now - cached.fetched >= STALE_S:
            raise
        log.warning("couldn't refresh %s, so the copy from %d minutes ago stands in: %s",
                    what, (now - cached.fetched) // 60, e)
        return cached.value, cached._replace(retry_at=now + RETRY_S)
    return value, _Cached(source, value, now, now)


def discovery(refresh=False):
    """The provider's discovery document (OpenID Connect Discovery 1.0 §4), checked against the settings. Kept for
    CACHE_S (refresh asks again), and while a fresh one can't be had, the last good one stands in. Raises
    Unavailable or HttpRefused when there is none to use, Misconfigured when the provider's doesn't fit."""
    global _discovered
    s = settings()
    with _cache_lock:
        cached = _discovered if _discovered and _discovered.source == s.issuer else None

    def fetch():
        # A trailing / on the issuer is dropped before the path is added (§4), as Auth0's issuers need.
        document = _fetch(s.issuer.rstrip("/") + DISCOVERY_PATH)
        _check_discovery(document, s)
        return document

    document, keep = _cached_or_fetch(cached, s.issuer, refresh, fetch, "the identity provider's discovery document")
    if keep:
        with _cache_lock:
            _discovered = keep
    return document


def _keys(refresh=False):
    """The provider's signing keys (its JWKS), less any marked for another use. Kept like discovery; refresh asks
    again (the provider may have rotated its keys), at most every KEY_REFETCH_S."""
    global _key_set, _keys_refetched
    address = discovery()["jwks_uri"]
    now = time.monotonic()
    with _cache_lock:
        cached = _key_set if _key_set and _key_set.source == address else None
        if refresh and cached and _keys_refetched is not None and now - _keys_refetched < KEY_REFETCH_S:
            return cached.value  # asked again too soon: the set already held
        if refresh:
            _keys_refetched = now

    def fetch():
        found = _fetch(address).get("keys")
        if not isinstance(found, list):
            raise Unavailable("the identity provider's key set has no list of keys")
        return [k for k in found if isinstance(k, dict) and k.get("use", "sig") == "sig"]

    found, keep = _cached_or_fetch(cached, address, refresh, fetch, "the identity provider's key set")
    if keep:
        with _cache_lock:
            _key_set = keep
    return found


def reset_caches():
    """Forget the discovery document, the keys and the refetch clock: one test's provider must not answer for
    another's."""
    global _discovered, _key_set, _keys_refetched
    with _cache_lock:
        _discovered = _key_set = _keys_refetched = None


# ---- signing in ---------------------------------------------------------------------


def pkce_challenge(verifier):
    """The S256 code challenge of a PKCE verifier (RFC 7636 §4.2): base64url of its sha256, without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_login(browser=None):
    """Fresh values for a sign-in about to start. The browser value is the login cookie's: the one the browser
    sent is kept (so two tabs can sign in at once) only when the cookie is __Host- and the value is one we could
    have made; a cookie without __Host- can be planted by a sibling subdomain, so it gets a new one."""
    keep = cookie_names().host_only and isinstance(browser, str) and _BROWSER_VALUE.fullmatch(browser)
    verifier = secrets.token_urlsafe(48)  # 64 characters, within RFC 7636's 43 to 128
    return Login(browser if keep else secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(32),
                 verifier, pkce_challenge(verifier))


def authorize_url(login):
    """Where the browser goes to sign in: the provider's authorization endpoint, with the query it already has,
    plus the authorization request (RFC 6749 §4.1.1) with PKCE. No response_mode: query is already the default
    for code, and some providers check the parameter against their response_modes_supported."""
    s = settings()
    parts = urllib.parse.urlsplit(discovery()["authorization_endpoint"])
    ours = {"response_type": "code", "client_id": s.client_id, "redirect_uri": redirect_uri(),
            "scope": " ".join(SCOPES + s.scopes), "state": login.state, "nonce": login.nonce,
            "code_challenge": login.challenge, "code_challenge_method": "S256"}
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k not in ours]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query + list(ours.items())),
                                                  fragment=""))


def _client_auth_method(document, s):
    """basic or post. auto: basic when discovery lists client_secret_basic or lists nothing (its default), else
    post. Discovery lists what the provider supports, not what the client is registered for, hence the setting."""
    if s.client_auth in ("basic", "post"):
        return s.client_auth
    listed = document.get("token_endpoint_auth_methods_supported")
    if not isinstance(listed, list) or not listed or "client_secret_basic" in listed:
        return "basic"
    return "post"


def redeem(code, verifier):
    """The ID token the token endpoint gives for an authorization code (RFC 6749 §4.1.3), the Grid UI signing in as
    its client with HTTP Basic or client_secret in the body. Everything else the endpoint answers, the access and
    refresh tokens included, is dropped here. Raises Unavailable, HttpRefused, or SignInFailed("no_id_token")."""
    s = settings()
    document = discovery()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "code_verifier": verifier, "client_id": s.client_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if _client_auth_method(document, s) == "basic":
        # RFC 6749 §2.3.1: each part is encoded before they're joined, so a ':' in either survives.
        pair = urllib.parse.quote(s.client_id, safe="") + ":" + urllib.parse.quote(s.client_secret, safe="")
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    else:
        form["client_secret"] = s.client_secret
    answer = _fetch(document["token_endpoint"], data=urllib.parse.urlencode(form).encode(), headers=headers,
                    timeout=TOKEN_TIMEOUT_S, limit=TOKEN_LIMIT)
    signed = answer.get("id_token")
    if not isinstance(signed, str) or not signed:
        raise SignInFailed("no_id_token", "the token endpoint answered without an ID token")
    return signed


def end_session_url():
    """Where to send the browser to sign out of the provider too (RP-initiated logout), or None: only with
    GRID_OIDC_END_SESSION=1 and an end_session_endpoint in discovery. No id_token_hint, since the ID token isn't
    kept, so a provider that needs one (Okta) can't do this. Signing out of the grid never waits on it."""
    s = settings()
    if not s.end_session:
        return None
    try:
        endpoint = discovery().get("end_session_endpoint")
    except (Unavailable, HttpRefused, Misconfigured):
        return None
    if not _allowed_url(endpoint, s.allow_http):
        return None
    parts = urllib.parse.urlsplit(endpoint)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query += [("client_id", s.client_id), ("post_logout_redirect_uri", s.public_url)]
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query), fragment=""))


# ---- the ID token -------------------------------------------------------------------


def _fits(jwk, alg):
    """Whether a key of jwk's type (and curve) can check a signature made with alg."""
    if alg[:2] in ("RS", "PS"):
        return jwk.get("kty") == "RSA"
    return jwk.get("kty") == ("OKP" if alg == "EdDSA" else "EC") and jwk.get("crv") in _CURVES[alg]


def _signing_key(kid, alg):
    """The provider's key an ID token names, or without a kid, its one key of the right type."""
    if kid is not None:
        found = [k for k in _keys() if k.get("kid") == kid]
        if not found:  # the provider may have rotated its keys since the set was fetched
            found = [k for k in _keys(refresh=True) if k.get("kid") == kid]
        if len(found) != 1:
            raise SignInFailed("unknown_key", "the identity provider doesn't publish exactly one key with the kid the "
                                              "ID token names")
    else:
        found = [k for k in _keys() if _fits(k, alg)]
        if len(found) != 1:
            raise SignInFailed("unknown_key", "the ID token names no key, and the identity provider doesn't have "
                                              "exactly one that fits {}".format(alg))
    jwk = found[0]
    if not _fits(jwk, alg):
        raise SignInFailed("key_mismatch", "the key the ID token names doesn't fit {}".format(alg))
    if "alg" in jwk and jwk["alg"] != alg:
        raise SignInFailed("key_mismatch", "the key the ID token names is for another algorithm than {}".format(alg))
    return jwk


def _control_free(text):
    return not any(unicodedata.category(ch) in _UNWANTED for ch in text)


def _usable_subject(sub):
    return sub if isinstance(sub, str) and 0 < len(sub) <= MAX_TEXT and _control_free(sub) else None


def _time(claims, name):
    value = claims[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SignInFailed("malformed", "the ID token's {} isn't a time".format(name))
    return value


def _claims(signed, key, alg, s):
    # PyJWT checks the signature, aud, iss and that the claims are there. The times are checked by the caller
    # against its own now, so the log can say how far off they are.
    try:
        return jwt.decode(signed, key, algorithms=[alg], audience=s.client_id, issuer=s.issuer,
                          options={"require": ["exp", "iat", "iss", "aud", "sub"], "verify_exp": False,
                                   "verify_nbf": False, "verify_iat": False})
    except jwt.InvalidSignatureError:
        raise SignInFailed("signature", "the ID token's signature doesn't check out") from None
    except jwt.MissingRequiredClaimError as e:
        raise SignInFailed("missing_claim", "the ID token has no {}".format(e.claim)) from None
    except jwt.InvalidAudienceError:
        raise SignInFailed("audience", "the ID token is for another client than GRID_OIDC_CLIENT_ID") from None
    except jwt.InvalidIssuerError:
        raise SignInFailed("issuer", "the ID token's issuer isn't GRID_OIDC_ISSUER") from None
    except jwt.PyJWTError:
        raise SignInFailed("malformed", "the ID token can't be read") from None


def verify_id_token(signed, nonce, now=None):
    """The claims of an ID token from the token endpoint, once all of it checks out: signed with an algorithm the
    provider lists (never none or HS*) by a key it publishes, this issuer byte for byte, this client as the
    audience (and as azp when there are others), times within GRID_OIDC_CLOCK_SKEW_S of now (this server's
    clock unless given), this sign-in's nonce, and a sub that is usable as a name.

    Raises SignInFailed; Unavailable, HttpRefused or Misconfigured when discovery or the keys can't be had."""
    s = settings()
    now = time.time() if now is None else now
    if not isinstance(signed, str) or len(signed) > MAX_ID_TOKEN_BYTES or signed.count(".") != 2:
        raise SignInFailed("malformed", "the ID token isn't a signed JWT of at most 64 KiB")
    try:
        header = jwt.get_unverified_header(signed)
    except jwt.PyJWTError:
        raise SignInFailed("malformed", "the ID token's header can't be read") from None
    alg = header.get("alg")
    allowed = _algorithms(discovery())
    if alg not in allowed:
        raise SignInFailed("algorithm", "the ID token is signed with {!r}; from this provider the Grid UI takes "
                                        "{}".format(alg, ", ".join(allowed)))
    if "crit" in header:  # newer PyJWTs refuse a crit they don't know as malformed; this refuses the rest
        raise SignInFailed("crit", "the ID token's header has crit, extensions the Grid UI doesn't understand")
    jwk = _signing_key(header.get("kid"), alg)
    try:
        key = jwt.PyJWK(jwk, algorithm=alg).key
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise SignInFailed("key_mismatch", "the identity provider's key for the ID token can't be used") from None
    claims = _claims(signed, key, alg, s)
    subject = _usable_subject(claims.get("sub"))
    if claims["iss"] != s.issuer:  # PyJWT compares it too; this is the comparison relied on
        raise SignInFailed("issuer", "the ID token's issuer isn't GRID_OIDC_ISSUER", subject)
    if isinstance(claims["aud"], list) and len(claims["aud"]) > 1 and "azp" not in claims:
        raise SignInFailed("azp", "the ID token has several audiences and no azp to say which one it is for",
                           subject)
    if "azp" in claims and claims["azp"] != s.client_id:
        raise SignInFailed("azp", "the ID token was issued to another client (azp)", subject)
    exp, iat = _time(claims, "exp"), _time(claims, "iat")
    if exp <= now - s.skew:
        raise SignInFailed("expired", "the ID token's exp was {:.0f} s ago by this server's clock".format(now - exp),
                           subject)
    if "nbf" in claims and _time(claims, "nbf") > now + s.skew:
        raise SignInFailed("not_yet_valid", "the ID token's nbf is {:.0f} s ahead of this server's clock".format(
            claims["nbf"] - now), subject)
    # No lower bound on iat: it would compare the provider's clock with the database's, and the nonce and the
    # single-use, browser-bound state already stop a replay.
    if iat > now + s.skew:
        raise SignInFailed("issued_in_future", "the ID token's iat is {:.0f} s ahead of this server's clock".format(
            iat - now), subject)
    # surrogatepass: a nonce holding a lone surrogate is just another nonce that isn't this sign-in's.
    claimed = str(claims.get("nonce", "")).encode("utf-8", "surrogatepass")
    if not nonce or not hmac.compare_digest(claimed, nonce.encode()):
        raise SignInFailed("nonce", "the ID token's nonce isn't this sign-in's", subject)
    if subject is None:
        raise SignInFailed("subject", "the ID token's sub is empty, longer than {} characters or holds control "
                                      "characters or lone surrogates".format(MAX_TEXT))
    return claims


def _clean_text(value):
    """A claim as text to show: control characters and lone surrogates stripped, cut to MAX_TEXT; None when
    nothing is left."""
    if not isinstance(value, str):
        return None
    text = "".join(ch for ch in value if unicodedata.category(ch) not in _UNWANTED).strip()[:MAX_TEXT]
    return text or None


def identity(claims):
    """Who a verified ID token says signed in: {"subject", "email", "emailVerified", "name", "groups",
    "groupsOverage"}. groups is the union of the GRID_OIDC_GROUPS_CLAIM claims' values (a string is one value),
    unique, in order, at most MAX_GROUPS, leaving out any that holds a control character or a lone surrogate (no
    group mapping could name it). When the provider says it left them out (Entra's overage), there are none and
    groupsOverage is set."""
    s = settings()
    email = claims.get("email")
    if not (isinstance(email, str) and 0 < len(email) <= MAX_TEXT and _control_free(email)):
        email = None
    verified = claims.get("email_verified")
    name = next(filter(None, (_clean_text(claims.get("name")), _clean_text(claims.get("preferred_username")),
                              email)), None)
    left_out = claims.get("_claim_names")
    overage = isinstance(left_out, dict) and any(claim in left_out for claim in s.groups_claims)
    groups = []
    if overage:
        store.warn_once("the identity provider left the groups out of the ID token (too many); map Entra app roles "
                        "with GRID_OIDC_GROUPS_CLAIM=roles, or limit the claim to the groups assigned to the application",
                        log)
    else:
        for claim in s.groups_claims:
            value = claims.get(claim)
            for group in [value] if isinstance(value, str) else value if isinstance(value, list) else []:
                if isinstance(group, str) and 0 < len(group) <= MAX_TEXT and _control_free(group) \
                        and group not in groups:
                    groups.append(group)
        groups = groups[:MAX_GROUPS]
    return {"subject": claims["sub"], "email": email, "emailVerified": verified is True or verified == "true",
            "name": name, "groups": groups, "groupsOverage": overage}


def admin_match(person, admins):
    """Whether GRID_OIDC_ADMINS names this person (an identity() or a users row): sub:<subject> exactly, or their
    email ignoring case, but only an email the provider said it verified."""
    for entry in admins:
        if entry.startswith("sub:"):
            if person.get("subject") == entry[4:]:
                return True
        elif person.get("emailVerified") and isinstance(person.get("email"), str) \
                and person["email"].casefold() == entry.casefold():
            return True
    return False
