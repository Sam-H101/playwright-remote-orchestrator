"""Signing people in to the Grid UI with an OpenID Connect identity provider: settings, discovery, signing keys,
the authorize URL, redeeming the code and checking the ID token.

Nothing here touches the database or Flask: app.py runs the flow and accounts.py keeps the users and sessions.
Only the ID token is used, and only once its signature, issuer, audience, times and nonce check out: the access
and refresh tokens the provider sends with it are dropped at once, and its userinfo endpoint is never asked.
The Grid UI proves to the token endpoint that it is the client with a client secret, or with a token the platform
issues: read from a file, or asked from the platform's Azure managed identity endpoint when it is sent. None of
them is logged, answered, kept or written anywhere.
Every address fetched comes from GRID_OIDC_ISSUER or the provider's discovery document, or is the managed identity
endpoint the platform set in the environment, never from a request, and none of them is followed through a
redirect. Plain http never goes through a proxy.
"""
import base64
import datetime
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
import socket
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
CLIENT_AUTH = ("auto", "basic", "post", "federated")
# What the settings come to. secret is auto with a client secret: basic or post, by the provider's discovery.
CLIENT_METHODS = ("secret", "basic", "post", "federated")
# A client assertion (RFC 7523 §2.2): how the platform's token travels in place of a secret.
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

# The back channel: how long each read from the provider may wait, and how much it may answer.
FETCH_TIMEOUT_S = 5
FETCH_LIMIT = 256 * 1024
TOKEN_TIMEOUT_S = 10
TOKEN_LIMIT = 1024 * 1024
# The platform's token file, and an error answer: how much of each is read at most.
TOKEN_FILE_LIMIT = 16 * 1024
ERROR_LIMIT = 16 * 1024
MAX_ERROR_CODES = 4
MAX_TOKEN_AUDIENCES = 5
MAX_EXPIRY = 10 ** 11  # seconds since 1970 beyond which an exp is no date (the year 5138)
# A managed identity's token: where an Azure VM hands it out (IMDS), the API versions of the two endpoints, how
# long each may take, how much of an answer is read, and how long what the endpoint last answered is remembered
# (whether it gave a token, never the token).
IMDS_URL = "http://169.254.169.254/metadata/identity/oauth2/token"
IMDS_API_VERSION = "2018-02-01"
APP_SERVICE_API_VERSION = "2019-08-01"
IDENTITY_TIMEOUT_S = 5
IDENTITY_LIMIT = 64 * 1024
IDENTITY_MEMO_S = 60
# The addresses IDENTITY_ENDPOINT may have: loopback, link-local and private. The platform sets the variable and
# it is trusted like any other; this only keeps a request that carries IDENTITY_HEADER off public networks.
_LOCAL_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "::1/128", "fe80::/10",
    "fc00::/7"))
# Entra ID's sign-in hosts, each with the audience a federated identity credential takes in that cloud: a
# managed identity's token is asked for with it, and is sent to these hosts only.
ENTRA_AUDIENCES = {
    "login.microsoftonline.com": "api://AzureADTokenExchange",
    "login.microsoftonline.us": "api://AzureADTokenExchangeUSGov",
    "login.chinacloudapi.cn": "api://AzureADTokenExchangeChina",
}
PLAIN_HTTP_WARNING = (
    "GRID_OIDC_ALLOW_HTTP=1 and GRID_OIDC_CLIENT_AUTH=federated: the platform's token goes to the identity provider "
    "over plain http, where anyone on the network between them can read it and use it as the Grid UI's credential "
    "until it expires. For development only.")
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

Settings = namedtuple("Settings", "issuer client_id client_secret client_auth federated_token_file azure_token_file "
                                  "managed_identity public_url public_path public_host label scopes groups_claims admins end_session "
                                  "ca_file allow_http skew session_hours idle_minutes")
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
_JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_ENDPOINT_PATH = re.compile(r"[A-Za-z0-9/._~%-]*")
_HEADER_VALUE = re.compile(r"[\x21-\x7e]{1,4096}")
# The three ways to prove the client, of which one may be set.
_CREDENTIALS = ("GRID_OIDC_CLIENT_SECRET", "GRID_OIDC_FEDERATED_TOKEN_FILE", "GRID_OIDC_MANAGED_IDENTITY")
# Where the platform hands out its managed identity's token. kind: imds (an Azure VM's Instance Metadata Service)
# or app-service (App Service, Functions, Container Apps). url: rebuilt from the parts that were checked. header:
# the request header the endpoint checks; its value is read from the environment as the request is sent, and kept
# nowhere.
IdentityEndpoint = namedtuple("IdentityEndpoint", "kind url api_version header")
# What the endpoint last answered, per process: never the token, never IDENTITY_HEADER. at: time.monotonic(). key:
# (identity, audience, endpoint url) asked for. claims: the token's iss, sub, aud and exp, or None. sentence and
# transient: why no token was given, or None and False.
_IdentityMemo = namedtuple("_IdentityMemo", "at key claims sentence transient")
_SHOWN_CLAIMS = ("iss", "sub", "aud", "exp")

_cache_lock = threading.Lock()
_discovered = None
_identity_memo = None
_key_set = None
_keys_refetched = None  # time.monotonic() of the last refetch an unknown key id asked for


class Unavailable(Exception):
    """The identity provider, or the platform's managed identity endpoint, can't be reached or answered something
    other than what was asked for. The message says which, for the log; only the managed identity endpoint's
    sentences (ours) are also shown to admins on the Single sign-on tab."""


class HttpRefused(Exception):
    """The identity provider answered an HTTP error. error is the OAuth error code it gave ("invalid_client") or
    None, and codes the numbers of its error_codes, which Entra gives (70021): nothing else of the answer is kept."""

    def __init__(self, status, error=None, codes=()):
        super().__init__("HTTP {}{}{}".format(status, " " + error if error else "", " (error codes {})".format(
            ", ".join(str(code) for code in codes)) if codes else ""))
        self.status = status
        self.error = error
        self.codes = tuple(codes)


class Misconfigured(Exception):
    """The provider's discovery document doesn't fit the settings (another issuer, an address that isn't https,
    no algorithm the Grid UI accepts, no client assertions), or the Grid UI can't authenticate as its client as
    configured (then the message is problem()'s sentence), or the platform's managed identity endpoint won't give a
    token as configured: the message says how, for the log."""


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
        federated_token_file=_env("GRID_OIDC_FEDERATED_TOKEN_FILE"),
        azure_token_file=_env("AZURE_FEDERATED_TOKEN_FILE"),
        managed_identity=_env("GRID_OIDC_MANAGED_IDENTITY").lower(),
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


def _client_method(s):
    """How the Grid UI proves to the token endpoint that it is the client, by the settings alone (no file is
    opened): (one of CLIENT_METHODS, None), or (None, what is wrong with them). A secret and a token file are a
    problem, not a choice, and the platform's token is sent only when GRID_OIDC_CLIENT_AUTH=federated says so:
    auto never picks it, whatever the platform offers."""
    if s.client_auth not in CLIENT_AUTH:
        return None, "GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated."
    given = [name for name, value in zip(_CREDENTIALS, (s.client_secret, s.federated_token_file, s.managed_identity))
             if value]
    if len(given) > 1:
        return None, ("Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and "
                      "GRID_OIDC_MANAGED_IDENTITY: {} and {} are set, and the Grid UI won't pick one.".format(
                          ", ".join(given[:-1]), given[-1]))
    if s.client_auth == "federated":
        if s.client_secret:
            return None, ("GRID_OIDC_CLIENT_AUTH=federated doesn't go with GRID_OIDC_CLIENT_SECRET, which is set: "
                          "unset one of them.")
        if s.managed_identity:
            if s.managed_identity != "system" and not _GUID.fullmatch(s.managed_identity):
                return None, ("GRID_OIDC_MANAGED_IDENTITY is system, or the client id of a user-assigned managed "
                              "identity (a GUID such as aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee).")
            if s.azure_token_file:
                return None, ("GRID_OIDC_MANAGED_IDENTITY doesn't go with the AZURE_FEDERATED_TOKEN_FILE that Azure "
                              "workload identity sets: in a Kubernetes pod that file is the Grid UI's own identity, "
                              "and the managed identity endpoint gives out the node's. Unset GRID_OIDC_MANAGED_IDENTITY.")
            if _entra_audience(s.issuer) is None:
                return None, ("GRID_OIDC_MANAGED_IDENTITY needs an Entra ID GRID_OIDC_ISSUER over https "
                              "(https://login.microsoftonline.com/<tenant id>/v2.0, or login.microsoftonline.us or "
                              "login.chinacloudapi.cn in those clouds): a managed identity's token is good only there, "
                              "and is sent nowhere else.")
            return "federated", None
        if not s.federated_token_file and not s.azure_token_file:
            return None, ("GRID_OIDC_CLIENT_AUTH=federated needs a token to send: GRID_OIDC_FEDERATED_TOKEN_FILE, "
                          "GRID_OIDC_MANAGED_IDENTITY, or the AZURE_FEDERATED_TOKEN_FILE that Azure workload identity "
                          "sets in a Kubernetes pod.")
        return "federated", None
    for name, value in (("GRID_OIDC_FEDERATED_TOKEN_FILE", s.federated_token_file),
                        ("GRID_OIDC_MANAGED_IDENTITY", s.managed_identity)):
        if value:
            return None, "{0} is used only with GRID_OIDC_CLIENT_AUTH=federated: set that, or unset {0}.".format(name)
    if not s.client_secret:
        if s.client_auth == "auto":
            return None, ("Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the "
                          "platform issues, so the Grid UI can prove to the identity provider that it is the client.")
        return None, "GRID_OIDC_CLIENT_AUTH={} needs GRID_OIDC_CLIENT_SECRET.".format(s.client_auth)
    return ("secret" if s.client_auth == "auto" else s.client_auth), None


def _entra_audience(issuer):
    """The federated credential audience of the Entra ID cloud the issuer is in, or None when the issuer isn't
    one of ENTRA_AUDIENCES' hosts over https (http only to a loopback host, which no Entra host is: the tests'
    fake, added to the map, is the one such)."""
    if not _allowed_url(issuer, False):
        return None
    return ENTRA_AUDIENCES.get((_split(issuer).hostname or "").lower())


def client_method():
    """How the Grid UI authenticates to the token endpoint as the settings stand: one of CLIENT_METHODS, or None
    while single sign-on is off or the settings don't say (problem() says why)."""
    s = settings()
    return _client_method(s)[0] if s.issuer else None


def platform_token_unused():
    """Whether the platform offers a token (AZURE_FEDERATED_TOKEN_FILE) that the settings leave alone: single
    sign-on is on, and GRID_OIDC_CLIENT_AUTH isn't federated."""
    s = settings()
    return bool(s.issuer and s.azure_token_file) and s.client_auth != "federated"


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


def _small_file(path, limit):
    """(A file's bytes, None), or (None, why not): admins read the reason on the page, so nothing of the file and
    no exception's text goes into it."""
    if not os.path.isfile(path):
        return None, "there is no file {}".format(path)
    if not os.access(path, os.R_OK):
        return None, "the Grid UI's user may not read {}".format(path)
    try:
        with open(path, "rb") as f:
            data = f.read(limit + 1)
    except OSError:
        return None, "{} can't be opened".format(path)
    if not data.strip():
        return None, "{} is empty".format(path)
    if len(data) > limit:
        return None, "{} holds more than {} bytes".format(path, limit)
    return data, None


def _federated_file(s):
    """(The setting that says where the platform's token is, the path): ours, else the one Azure workload identity
    sets in a Kubernetes pod."""
    if s.federated_token_file:
        return "GRID_OIDC_FEDERATED_TOKEN_FILE", s.federated_token_file
    return "AZURE_FEDERATED_TOKEN_FILE", s.azure_token_file


def _token_claims(token):
    """A JWT's claims, decoded without verifying anything: for its expiry and for showing an admin whom the
    platform says the Grid UI is, never for trusting. None when it isn't a JWT."""
    if not _JWT_SHAPE.fullmatch(token):
        return None
    payload = token.split(".")[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, RecursionError):
        return None
    return claims if isinstance(claims, dict) else None


def _expiry(claims):
    """A token's exp as a number, or None when it states none that is a date: a string, a boolean, NaN, an
    infinity, a negative number, or one past MAX_EXPIRY. Comparing is all that is done to it, which no number
    makes raise (math.isfinite does, for an integer too large for a float)."""
    exp = claims.get("exp")
    if isinstance(exp, bool) or not isinstance(exp, (int, float)):
        return None
    return exp if 0 < exp < MAX_EXPIRY else None


def _utc(seconds):
    """Seconds since 1970 as a naive UTC datetime of whole seconds (what store._iso takes), or None when no
    datetime holds it."""
    try:
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).replace(tzinfo=None, microsecond=0)
    except (OverflowError, OSError, ValueError):
        return None


def _federated_token(s):
    """The platform's token, read from its file now: (the token, its claims, None), or (None, its claims when they
    could be read, the sentence that says why it can't be used). Never kept: the platform replaces the file, and
    the next sign-in must send the new one."""
    name, path = _federated_file(s)
    data, reason = _small_file(path, TOKEN_FILE_LIMIT)
    token = claims = None
    if not reason:
        try:
            token = data.decode("ascii").strip()
        except UnicodeDecodeError:
            token = ""
        claims = _token_claims(token)
        exp = _expiry(claims) if claims else None
        if claims is None:
            reason = "{} doesn't hold a token (three base64url parts)".format(path)
        elif exp is not None and exp + s.skew <= time.time():
            reason = "the token in {} has expired; the platform should have replaced it".format(path)
    if reason:
        return None, claims, "{} can't be used: {}.".format(name, reason)
    return token, claims, None


# ---- the managed identity endpoint ----------------------------------------------------------


def _local_address(url):
    """(host, port, path) of url when it is plain http to a local address: localhost, or a loopback, link-local or
    private IP address (_LOCAL_NETWORKS), with no user name or password, no query or fragment, and a path of plain
    characters; else None. The only addresses a managed identity's token is asked from; GRID_OIDC_ALLOW_HTTP has
    no say here. Local means not on a public network, not this machine: 169.254/16 and the private ranges are
    neighbours too, which is why the platform's word (the environment) is what is trusted."""
    parts = _split(url) if isinstance(url, str) else None
    if not parts or parts.scheme != "http" or "@" in parts.netloc or "?" in url or "#" in url \
            or not _ENDPOINT_PATH.fullmatch(parts.path):  # "?" and "#" also catch an empty query or fragment
        return None
    try:
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return None
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not any(address in network for network in _LOCAL_NETWORKS):
            return None
    return host, port, parts.path


def _endpoint_text(url):
    """An endpoint's address to show: scheme, host, port and path, cleaned and cut like a claim. The host is
    rebuilt from hostname and port, never copied from the netloc, so a user name or password in it is never
    shown. None when the address can't be read (an unclosed [, a port that isn't a number from 0 to 65535), so
    that nothing but the real address is ever shown as it."""
    parts = _split(url)
    if not parts:
        return None
    try:
        host, port = parts.hostname or "", parts.port
    except ValueError:
        return None
    if ":" in host:
        host = "[" + host + "]"
    netloc = host if port is None else "{}:{}".format(host, port)
    return _clean_text(urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", "")))


def _identity_endpoint():
    """Where the platform hands out its managed identity's token, by what it put in the environment:
    (IdentityEndpoint, None), or (None, why not, a clause of our own). IDENTITY_ENDPOINT with IDENTITY_HEADER is
    App Service's, Functions' and Container Apps' endpoint; with neither, an Azure VM's Instance Metadata Service,
    unless the environment says this is a platform whose identity is elsewhere. Never falls back from one to the
    other. Service Fabric, Azure Arc and the older MSI_ENDPOINT aren't supported."""
    endpoint = _env("IDENTITY_ENDPOINT")
    header = os.environ.get("IDENTITY_HEADER", "")
    if _env("IDENTITY_SERVER_THUMBPRINT"):
        return None, ("IDENTITY_SERVER_THUMBPRINT is set, as Service Fabric sets it, and Service Fabric's managed "
                      "identity isn't supported")
    if endpoint:
        if not header:
            if _env("IMDS_ENDPOINT"):
                return None, ("IDENTITY_ENDPOINT and IMDS_ENDPOINT are set without IDENTITY_HEADER, as Azure Arc sets "
                              "them, and an Azure Arc server's managed identity isn't supported")
            return None, ("IDENTITY_ENDPOINT is set without the IDENTITY_HEADER that App Service, Functions and "
                          "Container Apps set beside it")
        if not _HEADER_VALUE.fullmatch(header):
            return None, "IDENTITY_HEADER isn't a usable header value"
        local = _local_address(endpoint)
        if not local:
            shown = _endpoint_text(endpoint)
            if not shown:
                return None, ("IDENTITY_ENDPOINT isn't an address the Grid UI can read (check the host, any [ ] around "
                              "an IPv6 address, and the port)")
            return None, ("IDENTITY_ENDPOINT is {}, and the Grid UI asks for a managed identity's token only over plain "
                          "http at a local address (localhost, or a loopback, link-local or private IP address), with "
                          "no query").format(shown)
        host, port, path = local
        netloc = ("[" + host + "]" if ":" in host else host) + ("" if port is None else ":{}".format(port))
        return IdentityEndpoint("app-service", "http://" + netloc + path, APP_SERVICE_API_VERSION,
                                "X-IDENTITY-HEADER"), None
    if _env("MSI_ENDPOINT") or _env("MSI_SECRET"):
        return None, ("MSI_ENDPOINT or MSI_SECRET is set without IDENTITY_ENDPOINT, and that older managed identity "
                      "endpoint isn't supported")
    if _env("WEBSITE_SITE_NAME") or _env("CONTAINER_APP_NAME"):
        return None, ("this is App Service, Functions or Container Apps, but IDENTITY_ENDPOINT isn't set: turn the "
                      "app's managed identity on, then restart it")
    if _env("KUBERNETES_SERVICE_HOST"):
        return None, ("this is a Kubernetes pod, where the Instance Metadata Service gives out the node's identity, "
                      "not the Grid UI's: use the pod's token file instead")
    return IdentityEndpoint("imds", IMDS_URL, IMDS_API_VERSION, "Metadata"), None


def _where(endpoint):
    if endpoint.kind == "imds":
        return "the Azure Instance Metadata Service (http://169.254.169.254)"
    return "the managed identity endpoint in IDENTITY_ENDPOINT ({})".format(_endpoint_text(endpoint.url))


def _identity_fetch(endpoint, query, secret):
    """The JSON object the platform's managed identity endpoint answers a GET with: (it, None, False), or (None,
    why not, whether asking again in a minute may help). Its own way out, apart from _fetch: plain http to a local
    address only, never through a proxy (Azure says not to, and a proxy would see the token), no redirects, at
    most IDENTITY_LIMIT bytes, IDENTITY_TIMEOUT_S. Of an error answer, the status and the error code only. Tests
    point IMDS_URL or IDENTITY_ENDPOINT at a fake on loopback."""
    where = _where(endpoint)
    if not _local_address(endpoint.url):  # _identity_endpoint said so already; this is where it is relied on
        return None, "{} isn't at a local address".format(where), False
    url = endpoint.url + "?" + urllib.parse.urlencode(query)  # the checked address, and our parameters only
    request = urllib.request.Request(url, headers={endpoint.header: secret, "Accept": "application/json"})
    request.add_header("User-Agent", _USER_AGENT)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects)
    try:
        with opener.open(request, timeout=IDENTITY_TIMEOUT_S) as answer:
            body = answer.read(IDENTITY_LIMIT + 1)
            media = answer.headers.get_content_type()
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            e.close()
            return None, "{} answered with a redirect (HTTP {}), which isn't followed".format(where, e.code), False
        error = _error_codes(e)[0]
        status = "HTTP {}{}".format(e.code, " " + error if error else "")
        if e.code in (404, 410, 429) or e.code >= 500:
            return None, ("{} answered {}, which Azure says it does while it updates or is busy: try again in a "
                          "minute, and if it keeps happening, check that the identity is turned on and "
                          "assigned").format(where, status), True
        return None, "{} refused to give a token: {}. Check that this VM or app has {}".format(
            where, status, _identity_named(settings())), False
    except urllib.error.URLError as e:
        return None, _unreached(where, e.reason), True
    except (OSError, http.client.HTTPException) as e:
        return None, _unreached(where, e), True
    except ValueError:  # the address and the header were checked, so this isn't expected; no exception text kept
        return None, "{} couldn't be asked: urllib refused to make the request".format(where), False
    if len(body) > IDENTITY_LIMIT:
        return None, "{} answered more than {} bytes".format(where, IDENTITY_LIMIT), False
    try:
        document = json.loads(body) if media == "application/json" or media.endswith("+json") else None
    except (ValueError, RecursionError):
        document = None
    if not isinstance(document, dict):
        return None, "{} didn't answer with a JSON object".format(where), False
    return document, None, False


def _unreached(where, reason):
    """Why an endpoint wasn't reached, in fixed words: no exception's text goes to the page."""
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "{} didn't answer within {} s".format(where, IDENTITY_TIMEOUT_S)
    if isinstance(reason, ConnectionRefusedError):
        return "{} refused the connection".format(where)
    return "{} can't be reached".format(where)


def _identity_named(s):
    if s.managed_identity == "system":
        return "its system-assigned managed identity turned on"
    return "the user-assigned managed identity with client id {} assigned to it".format(s.managed_identity)


def _managed_identity_token(s):
    """The managed identity's token for Entra ID, asked for now from the platform's endpoint: (the token, its
    claims, None, False), or (None, its claims when they could be read, the sentence that says why it can't be
    used, whether asking again in a minute may help). The token is never kept; the outcome is remembered
    (_remember)."""
    endpoint, wrong = _identity_endpoint()
    if wrong:
        return None, None, "GRID_OIDC_MANAGED_IDENTITY can't be used: {}.".format(wrong), False
    query = {"resource": _entra_audience(s.issuer), "api-version": endpoint.api_version}
    if s.managed_identity != "system":
        query["client_id"] = s.managed_identity
    secret = "true" if endpoint.kind == "imds" else os.environ.get("IDENTITY_HEADER", "")
    answer, reason, transient = _identity_fetch(endpoint, query, secret)
    token = claims = None
    if not reason:
        token = answer.get("access_token")
        claims = _token_claims(token) if isinstance(token, str) and len(token) <= TOKEN_FILE_LIMIT else None
        exp = _expiry(claims) if claims else None
        if claims is None:
            reason = "{} answered without a token (no access_token of three base64url parts, at most {} bytes)".format(
                _where(endpoint), TOKEN_FILE_LIMIT)
        elif exp is not None and exp + s.skew <= time.time():
            reason = "the token {} gave has already expired; check this machine's clock".format(_where(endpoint))
    sentence = "GRID_OIDC_MANAGED_IDENTITY can't be used: {}.".format(reason) if reason else None
    _remember(_identity_key(s, endpoint), claims, sentence, transient)
    if sentence:
        return None, claims, sentence, transient
    return token, claims, None, False


def _identity_key(s, endpoint):
    return s.managed_identity, _entra_audience(s.issuer), endpoint.url


def _remember(key, claims, sentence, transient):
    """Keeps what the endpoint answered for IDENTITY_MEMO_S: whether it gave a token, and the claims the Admin
    page shows. Never the token."""
    global _identity_memo
    shown = {name: claims[name] for name in _SHOWN_CLAIMS if name in claims} if claims else None
    with _cache_lock:
        _identity_memo = _IdentityMemo(time.monotonic(), key, shown, sentence, transient)


def _remembered(s):
    """What the endpoint answered for these settings less than IDENTITY_MEMO_S ago, or None."""
    endpoint = _identity_endpoint()[0]
    with _cache_lock:
        memo = _identity_memo
    if memo and endpoint and memo.key == _identity_key(s, endpoint) and time.monotonic() - memo.at < IDENTITY_MEMO_S:
        return memo
    return None


def _identity_outcome(s):
    """(claims, sentence, transient) for the start of a sign-in and the Admin page: the memo while it is fresh,
    else asked for now (and the token dropped)."""
    memo = _remembered(s)
    if memo:
        return memo.claims, memo.sentence, memo.transient
    _, claims, sentence, transient = _managed_identity_token(s)
    return claims, sentence, transient


def _platform_token(s):
    """The platform's token now, from whichever source the settings name: (token, claims, sentence, transient). A
    managed identity is asked unless it failed less than IDENTITY_MEMO_S ago."""
    if s.managed_identity:
        memo = _remembered(s)
        if memo and memo.sentence:
            return None, memo.claims, memo.sentence, memo.transient
        return _managed_identity_token(s)
    return _federated_token(s) + (False,)


def _first_problem(s):
    if not s.client_id:
        return "Set GRID_OIDC_CLIENT_ID to the Grid UI's client id at the identity provider."
    method, wrong = _client_method(s)
    if wrong:
        return wrong
    public = _split(s.public_url)
    if not public or public.scheme not in ("http", "https") or not public.hostname:
        return "Set GRID_UI_PUBLIC_URL to the address people open the Grid UI at, e.g. https://grid.example.com/."
    for name, url in (("GRID_OIDC_ISSUER", s.issuer), ("GRID_UI_PUBLIC_URL", s.public_url)):
        if not _allowed_url(url, s.allow_http):
            return "{} must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development).".format(name)
    if _split(s.issuer).path.strip("/").split("/")[0].lower() in MULTI_TENANT:
        return ("GRID_OIDC_ISSUER is a multi-tenant address (common, organizations or consumers), which no ID token "
                "states as its issuer. Use your tenant's: https://login.microsoftonline.com/<tenant id>/v2.0.")
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
    if method != "federated":
        return None
    if s.managed_identity:
        reason = _identity_endpoint()[1]
        return "GRID_OIDC_MANAGED_IDENTITY can't be used: {}.".format(reason) if reason else None
    return _federated_token(s)[2]


def problem():
    """Why single sign-on can't work as configured, as a sentence for the log and for admins, or None (also when
    GRID_OIDC_ISSUER is unset). Worked out on every call, like mfa.key_problem(); each distinct one is logged
    once. Never asks the managed identity endpoint."""
    s = settings()
    if not s.issuer:
        return None
    text = _first_problem(s)
    if text:
        store.warn_once(text, log)
    elif s.allow_http:
        warning = plain_http_warning() or (
            None if all(u.lower().startswith("https://") for u in (s.issuer, s.public_url))
            else "GRID_OIDC_ALLOW_HTTP=1: single sign-on runs over plain http, which is for development only.")
        if warning:
            store.warn_once(warning, log)
    return text


def plain_http_warning():
    """The warning for when the platform's token goes over plain http to a host that isn't loopback, which only
    GRID_OIDC_ALLOW_HTTP=1 allows; else None. Looks at the settings and at a discovery document already held, and
    never fetches one. Never for a managed identity, whose token goes only to Entra ID over https."""
    s = settings()
    if not s.allow_http or s.managed_identity or client_method() != "federated":
        return None
    with _cache_lock:
        held = _discovered.value if _discovered and _discovered.source == s.issuer else {}
    if _allowed_url(s.issuer, False) and _allowed_url(held.get("token_endpoint", s.issuer), False):
        return None
    return PLAIN_HTTP_WARNING


def federated_token_facts():
    """The platform's token for the Admin page, or None when another method is in use: its source (a file and the
    setting that names it, or a managed identity and the endpoint it comes from), and the issuer, subject,
    audience and expiry it states, decoded without verifying. For a token file they are what the identity
    provider's federated credential must name; for a managed identity, credential says what it must name. A
    managed identity's outcome comes from the memo, or is asked for once, only while the settings have no problem;
    problem is then what kept a token from being given, else None. Never the token, never IDENTITY_HEADER."""
    s = settings()
    if client_method() != "federated":
        return None
    sentence = claims = credential = None
    if s.managed_identity:
        endpoint = _identity_endpoint()[0]
        facts = {"source": "managed-identity", "setting": "GRID_OIDC_MANAGED_IDENTITY", "file": None,
                 "identity": s.managed_identity, "host": endpoint.kind if endpoint else None,
                 "endpoint": _endpoint_text(endpoint.url) if endpoint else None}
        if not problem():
            claims, sentence, _ = _identity_outcome(s)
        credential = {"issuer": _clean_text(s.issuer), "subject": _clean_text((claims or {}).get("sub")),
                      "audience": _entra_audience(s.issuer)}
    else:
        name, path = _federated_file(s)
        facts = {"source": "file", "setting": name, "file": path, "identity": None, "host": None, "endpoint": None}
        claims = _federated_token(s)[1]
    decoded, claims = claims is not None, claims or {}
    audience = claims.get("aud")
    audience = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
    exp = _expiry(claims)
    facts.update({"decoded": decoded, "issuer": _clean_text(claims.get("iss")),
                  "subject": _clean_text(claims.get("sub")),
                  "audience": [a for a in map(_clean_text, audience[:MAX_TOKEN_AUDIENCES]) if a],
                  "expiresAt": store._iso(_utc(exp)) if exp is not None else None,
                  "expired": exp is not None and exp + s.skew <= time.time(),
                  "credential": credential, "problem": sentence})
    return facts


def provider_problem():
    """With the platform's token: what in the provider's discovery document keeps the Grid UI from authenticating,
    as a sentence for the Admin page, or None. None too while the settings have a problem of their own (problem()
    says that one) and while the document can't be had (a sign-in says so). Uses discovery()'s cache, so it
    fetches only when a sign-in would, and never asks the managed identity endpoint."""
    if client_method() != "federated" or problem():
        return None
    try:
        _check_provider(settings())
    except Misconfigured as e:
        text = str(e)
        return text[:1].upper() + text[1:] + "."
    except (Unavailable, HttpRefused):
        return None
    return None


# ---- the back channel ---------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """The provider's redirects are refused, never followed: every address comes from the settings or the
    discovery document."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises the 3xx as an HTTPError


def _opener(s, url):
    """How a request to the provider goes out: no redirects, TLS checked against the system's CAs and
    GRID_OIDC_CA_FILE's. Plain http goes straight to its host, never through the environment's proxy: a proxy
    would read what is sent, the client's credential among it. https keeps the proxy, through which TLS runs end
    to end."""
    context = ssl.create_default_context()
    if s.ca_file:
        try:
            context.load_verify_locations(cafile=s.ca_file)  # on top of the system's
        except OSError:
            raise Unavailable("GRID_OIDC_CA_FILE can't be used (see the warning about it)") from None
    handlers = [_NoRedirects, urllib.request.HTTPSHandler(context=context)]
    if _split(url).scheme == "http":  # _fetch has already checked url with _allowed_url
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers)


def _error_codes(answer):
    """What is kept of an error answer: (its OAuth error code ("invalid_grant") or None, the numbers in its
    error_codes, which Entra gives ([70021]), at most MAX_ERROR_CODES). Never its description: that is the
    provider's text, and may quote what it was sent."""
    try:
        body = json.loads(answer.read(ERROR_LIMIT))
    except (ValueError, RecursionError, OSError, http.client.HTTPException):
        return None, ()
    finally:
        answer.close()
    if not isinstance(body, dict):
        return None, ()
    error, codes = body.get("error"), body.get("error_codes")
    numbers = tuple(code for code in codes if isinstance(code, int) and not isinstance(code, bool)
                    and 0 < code < 10 ** 9) if isinstance(codes, list) else ()
    return error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else None, numbers[:MAX_ERROR_CODES]


def _fetch(url, data=None, headers=None, timeout=None, limit=None):
    """The JSON object a GET of url answers (a POST of data, form-encoded bytes, when given).

    https only, except to a loopback host or with GRID_OIDC_ALLOW_HTTP=1; plain http never through a proxy; no
    redirects; at most limit bytes (FETCH_LIMIT); application/json or a +json type, whatever its parameters.
    Raises Unavailable, or HttpRefused for an HTTP error. The one way out to the provider, so tests replace it
    here."""
    s = settings()
    limit = limit or FETCH_LIMIT
    if not _allowed_url(url, s.allow_http):
        raise Unavailable("the identity provider gave an address that isn't https")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}, Accept="application/json"))
    request.add_header("User-Agent", _USER_AGENT)
    try:
        with _opener(s, url).open(request, timeout=timeout or FETCH_TIMEOUT_S) as answer:
            body = answer.read(limit + 1)
            media = answer.headers.get_content_type()
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            e.close()
            raise Unavailable("the identity provider answered with a redirect (HTTP {}), which isn't followed".format(
                e.code)) from None
        raise HttpRefused(e.code, *_error_codes(e)) from None
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
    """Forget the discovery document, the keys, the refetch clock and what the managed identity endpoint last
    answered: one test's provider must not answer for another's."""
    global _discovered, _key_set, _keys_refetched, _identity_memo
    with _cache_lock:
        _discovered = _key_set = _keys_refetched = _identity_memo = None


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
    """basic, post or federated: how the token request authenticates. With a client secret and auto: basic when
    discovery lists client_secret_basic or lists nothing (its default), else post; discovery lists what the
    provider supports, not what the client is registered for, hence the setting. The platform's token travels as
    a client assertion, which discovery calls private_key_jwt: a provider that lists methods and not that one is
    not sent one, nor one over plain http to a host that isn't loopback, unless GRID_OIDC_ALLOW_HTTP=1; and a
    managed identity's token goes only to the issuer's own host over https. Raises Misconfigured, with problem()'s
    sentence when the settings don't say."""
    method, wrong = _client_method(s)
    if wrong:
        raise Misconfigured(wrong)
    if method in ("basic", "post"):
        return method
    listed = document.get("token_endpoint_auth_methods_supported")
    if method == "secret":
        if not isinstance(listed, list) or not listed or "client_secret_basic" in listed:
            return "basic"
        return "post"
    endpoint = document.get("token_endpoint")
    if s.managed_identity:
        issuer, sent_to = _split(s.issuer), _split(endpoint) if isinstance(endpoint, str) else None
        try:
            same = bool(sent_to) and (sent_to.hostname, sent_to.port) == (issuer.hostname, issuer.port)
        except ValueError:
            same = False
        if not same or not _allowed_url(endpoint, False):
            raise Misconfigured("the discovery document's token_endpoint isn't https on {}, GRID_OIDC_ISSUER's host, "
                                "and a managed identity's token is sent nowhere else".format(issuer.hostname))
    elif not _allowed_url(endpoint, s.allow_http):
        raise Misconfigured("the identity provider's token_endpoint is plain http on a host that isn't loopback, and "
                            "the platform's token goes there only with GRID_OIDC_ALLOW_HTTP=1")
    if isinstance(listed, list) and listed and "private_key_jwt" not in listed:
        raise Misconfigured("the identity provider doesn't take client assertions: private_key_jwt isn't among the "
                            "token_endpoint_auth_methods_supported its discovery document lists")
    return method


def _check_provider(s):
    """The discovery document, and the method against it: raises what discovery() raises, and Misconfigured."""
    _client_auth_method(discovery(), s)


def check_client():
    """Whether the Grid UI can authenticate to this provider's token endpoint as configured, asked before anyone
    is sent off to sign in: the discovery document and the method against it, and with a managed identity, that
    its endpoint gives a token (remembered for IDENTITY_MEMO_S, else asked now and the token dropped). Raises what
    discovery() raises, Misconfigured, and Unavailable when the endpoint may give one in a minute."""
    s = settings()
    _check_provider(s)
    if s.managed_identity and _client_method(s)[0] == "federated":
        _, wrong, transient = _identity_outcome(s)
        if wrong:
            raise (Unavailable if transient else Misconfigured)(wrong)


def redeem(code, verifier):
    """The ID token the token endpoint gives for an authorization code (RFC 6749 §4.1.3), the Grid UI signing in as
    its client with its secret (HTTP Basic or client_secret in the body) or with the platform's token, read from
    its file or asked from the managed identity endpoint at this moment (not asked when the endpoint failed less
    than IDENTITY_MEMO_S ago). Everything else the endpoint answers, the access and refresh tokens included, is
    dropped here. Raises Unavailable, HttpRefused, Misconfigured, or SignInFailed("no_id_token")."""
    s = settings()
    document = discovery()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
            "code_verifier": verifier, "client_id": s.client_id}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    method = _client_auth_method(document, s)
    if method == "basic":
        # RFC 6749 §2.3.1: each part is encoded before they're joined, so a ':' in either survives.
        pair = urllib.parse.quote(s.client_id, safe="") + ":" + urllib.parse.quote(s.client_secret, safe="")
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode("ascii")
    elif method == "post":
        form["client_secret"] = s.client_secret
    else:
        token, _, wrong, transient = _platform_token(s)
        if wrong:
            raise (Unavailable if transient else Misconfigured)(wrong)
        form["client_assertion_type"] = ASSERTION_TYPE
        form["client_assertion"] = token
    answer = _fetch(document["token_endpoint"], data=urllib.parse.urlencode(form).encode(), headers=headers,
                    timeout=TOKEN_TIMEOUT_S, limit=TOKEN_LIMIT)
    signed = answer.get("id_token")
    if not isinstance(signed, str) or not signed:
        raise SignInFailed("no_id_token", "the token endpoint answered without an ID token")
    return signed


# Entra's error numbers that concern a federated identity credential, each with what to check, in words of our
# own. Advice for the log; nothing is decided on it.
_ENTRA_HINTS = {
    70021: "no federated identity credential on the app registration matches the platform's token. For a managed "
           "identity it must name GRID_OIDC_ISSUER, the identity's principal id and the audience of GRID_OIDC_ISSUER's "
           "cloud (api://AzureADTokenExchange in the public cloud), as the Admin page's Single sign-on tab shows them "
           "in Credential issuer, Credential subject and Credential audience; for a token file, the token's issuer, "
           "subject and audience exactly, as the tab shows them. One added a moment ago can take a few minutes to "
           "spread.",
    700211: "no federated identity credential on the app registration names the token's issuer. For a managed "
            "identity it is GRID_OIDC_ISSUER, https://login.microsoftonline.com/<tenant id>/v2.0; for a token file "
            "it is the token's issuer exactly, trailing / and all (a cluster's OIDC issuer URL for a Kubernetes "
            "token), as the Admin page's Single sign-on tab shows it.",
    700212: "no federated identity credential on the app registration names the token's audience. It must be "
            "api://AzureADTokenExchange (api://AzureADTokenExchangeUSGov in Azure Government, "
            "api://AzureADTokenExchangeChina in Azure operated by 21Vianet), for a managed identity whatever audience "
            "the token itself states; a pod that uses an AKS identity binding gets api://AKSIdentityBinding instead.",
    700213: "no federated identity credential on the app registration names the token's subject. It must be the "
            "token's subject letter for letter: system:serviceaccount:<namespace>:<service account> for a Kubernetes "
            "token, the object (principal) id for a managed identity (the Admin page's Single sign-on tab shows it).",
    70025: "the app registration has no federated identity credential. Add one: for a managed identity with "
           "GRID_OIDC_ISSUER, the identity's principal id and the audience of GRID_OIDC_ISSUER's cloud "
           "(api://AzureADTokenExchange in the public cloud), as the Admin page's Single sign-on tab shows them in "
           "Credential issuer, Credential subject and Credential audience; for a token file with the issuer, subject "
           "and audience the tab shows for the token.",
    700024: "the platform's token was outside its valid time. Check this machine's clock, and that the platform "
            "still gives out fresh tokens.",
    700016: "the tenant in GRID_OIDC_ISSUER has no application with the id in GRID_OIDC_CLIENT_ID.",
    700238: "the tenant doesn't take tokens from this issuer for federated identity credentials: ask whoever "
            "administers the tenant.",
}
_METHOD_HINTS = {
    "federated": "The provider's federated identity credential must name the token's issuer, subject and audience "
                 "exactly: the Admin page's Single sign-on tab shows them.",
    # A managed identity's token states its issuer and audience in its own way: the credential names Entra's.
    "managed-identity": "The app registration's federated identity credential must name GRID_OIDC_ISSUER, the managed "
                        "identity's principal id and the audience of GRID_OIDC_ISSUER's cloud "
                        "(api://AzureADTokenExchange in the public cloud), whatever the token itself states: the "
                        "Admin page's Single sign-on tab shows them as Credential issuer, Credential subject and "
                        "Credential audience.",
    "secret": "Check GRID_OIDC_CLIENT_SECRET, and set GRID_OIDC_CLIENT_AUTH when the client is registered for one "
              "method only.",
}
_METHOD_HINTS["basic"] = _METHOD_HINTS["post"] = _METHOD_HINTS["secret"]


def refusal_hint(codes, method=None):
    """What to check after the token endpoint refused a code, in words of our own, or "": by the first of Entra's
    error numbers known here, whatever OAuth error came with it; else, when the Grid UI itself was refused as the
    client (the caller then names the method: hint_method()), by the method. For the log; nothing is decided on
    it."""
    for code in codes:
        if code in _ENTRA_HINTS:
            return "AADSTS{}: {}".format(code, _ENTRA_HINTS[code])
    return _METHOD_HINTS.get(method, "")


def hint_method():
    """What refusal_hint() gives its hint by, as the settings stand: client_method(), but managed-identity when
    the platform's token comes from a managed identity, whose credential names Entra's issuer and audience, not
    the token's."""
    method = client_method()
    return "managed-identity" if method == "federated" and settings().managed_identity else method


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
