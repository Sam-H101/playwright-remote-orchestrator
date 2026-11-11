"""A fake OpenID Connect identity provider for the Grid UI's tests: discovery, a key set, an authorize page, a
token endpoint and sign-out, with a knob for each way a real one can go wrong.

    idp = fake_idp.serve("127.0.0.1")              # a real HTTP server on a free port, in a daemon thread
    idp.redirect_uri = "http://localhost/auth/callback"
    callback = idp.approve(authorize_url)           # where /authorize would send the browser back to
    idp.stop()

Unit tests drive the Grid UI with Flask's test client, which fetches discovery, keys and tokens from this server
and uses approve() in place of a browser. Browser tests serve it at localhost while the grid is at 127.0.0.1, so
the two are cross-site. Not collected by pytest (no test_ in its name), and like the rest of ui/tests, left out of
the images by .dockerignore.
"""
import base64
import collections
import datetime
import hashlib
import hmac
import html
import http.server
import ipaddress
import json
import secrets
import socket
import threading
import time
import urllib.parse

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from flask import Flask, Response, redirect, request
from jwt.algorithms import ECAlgorithm, RSAAlgorithm
from werkzeug.serving import WSGIRequestHandler, make_server

CLIENT_ID = "grid-ui"
CLIENT_SECRET = "s3cret-client"
USER = {"sub": "00u-dana-lee", "email": "dana@example.com", "email_verified": True, "name": "Dana Lee",
        "groups": ["grid-testers"]}
# What each fault does, so a test can say which way the provider goes wrong.
FAULTS = {
    "wrong_iss": "the ID token's iss is another provider's",
    "wrong_aud": "the ID token is for another client",
    "multi_aud_no_azp": "the ID token has two audiences and no azp",
    "azp_other": "the ID token's azp is another client",
    "expired": "the ID token's exp was 600 s ago",
    "iat_future": "the ID token's iat is 600 s ahead",
    "nbf_future": "the ID token's nbf is 600 s ahead",
    "bad_signature": "the ID token's payload was changed after signing",
    "alg_none": "the ID token is unsigned (alg none)",
    "hs256_public_key": "the ID token is HS256 with the RSA public key as the secret",
    "unknown_kid": "the ID token names a key that isn't published",
    "kid_missing_many_keys": "the ID token names no key, and two RSA keys are published",
    "crit_header": "the ID token's header has crit",
    "no_nonce": "the ID token has no nonce",
    "wrong_nonce": "the ID token's nonce isn't the sign-in's",
    "missing_sub": "the ID token has no sub",
    "long_sub": "the ID token's sub is 256 characters",
    "surrogate_sub": "the ID token's sub holds a lone surrogate (JSON can carry one; no database can store it)",
    "token_400_invalid_grant": "the token endpoint answers 400 invalid_grant",
    "token_401_invalid_client": "the token endpoint answers 401 invalid_client",
    "token_401_entra_70021": "the token endpoint answers 401 invalid_client as Entra does: error_codes [70021] and a "
                             "description",
    "token_400_grant_with_number": "the token endpoint answers 400 invalid_grant with error_codes [700024]",
    "token_500": "the token endpoint answers 500",
    "token_slow": "the token endpoint takes delay_s to answer",
    "discovery_down": "discovery answers 503",
    "discovery_issuer_mismatch": "discovery states another issuer",
    "discovery_redirect": "discovery redirects elsewhere",
    "jwks_oversize": "the key set is over 256 KiB",
    "oversize_id_token": "the ID token is over 64 KiB",
    "html_content_type": "discovery and the key set answer as text/html",
}
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
# What every refusal's description holds, as Entra's holds its own words: a test looks for it where none may be.
DESCRIPTION_MARKER = "description-marker"
TRACE_ID = "00000000-0000-0000-0000-000000000001"
CORRELATION_ID = "00000000-0000-0000-0000-000000000002"


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _segment(obj):
    return _b64(json.dumps(obj, separators=(",", ":")).encode())


def _unsegment(segment):
    """The JSON object a JWT's part holds, or None."""
    try:
        found = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def entra_refusal(error, codes=(), quoted=""):
    """An error answer as Entra writes one: the OAuth error, a description that quotes what it was sent, its error
    numbers, and the ids of its own trace."""
    number = "AADSTS{}: ".format(codes[0]) if codes else ""
    body = {"error": error,
            "error_description": "{}{} '{}'. Trace ID: {}".format(number, DESCRIPTION_MARKER, quoted, TRACE_ID),
            "timestamp": "2026-11-09 12:00:00Z", "trace_id": TRACE_ID, "correlation_id": CORRELATION_ID}
    if codes:
        body["error_codes"] = list(codes)
    return body


def _challenge(verifier):
    return _b64(hashlib.sha256(verifier.encode()).digest())


class _Quiet(WSGIRequestHandler):
    """No access log: what the Grid UI logs is what the tests look at."""

    def log_request(self, code="-", size="-"):
        pass


class FakeCluster:
    """A cluster that issues service-account tokens, as a Kubernetes cluster's OIDC issuer does (AKS's among them):
    an issuer URL (made up, trailing / and all) and an RSA key."""

    def __init__(self, issuer="https://oidc.cluster.example/11111111-2222-3333-4444-555555555555/"):
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def token(self, subject="system:serviceaccount:grid:grid-ui", audience="api://AzureADTokenExchange",
              lifetime_s=3600, **changes):
        """A projected service-account token: RS256, aud as a list, as Kubernetes writes it."""
        now = int(time.time())
        claims = {"iss": self.issuer, "sub": subject, "aud": [audience], "iat": now, "nbf": now, "exp": now + lifetime_s}
        claims.update(changes)
        return jwt.encode({k: v for k, v in claims.items() if v is not None}, self.key, algorithm="RS256",
                          headers={"kid": "cluster-1"})


class FakeIdp:
    """The provider. Its knobs are plain attributes a test sets:

    user          the claims of whoever signs in next: sub, email, email_verified, name, groups (a list, a str or
                  left out), any other claim, and overage (True: the groups are left out as Entra does)
    auth_methods  token_endpoint_auth_methods_supported, which the token endpoint also enforces (None: not
                  listed, and only client_secret_basic is taken, the standard's default). private_key_jwt among
                  them: a platform's token is taken as a client assertion
    federated     the (cluster, subject, audience) triples whose tokens are taken in place of a secret, added
                  with trust(): the provider's federated identity credentials
    assertions    each client assertion received, decoded without verifying: header, claims and raw
    token_endpoint  the token_endpoint discovery publishes in place of the provider's own (None: its own)
    algs          id_token_signing_alg_values_supported (None: not listed); alg is what signs (RS256 or ES256)
    fault         one of FAULTS, or None
    delay_s       how long the token_slow fault keeps the token endpoint waiting
    clock_offset_s  how far ahead the provider's clock is (negative: behind)
    auto          discovery's authorization_endpoint carries ?auto=1, and /authorize then sends the browser
                  straight back as user instead of showing its page
    counts        requests answered, per endpoint
    """

    def __init__(self, path=""):
        self.path = path  # "" or e.g. "/tenant/": the issuer is the origin plus this, trailing / and all
        self.prefix = "/" + path.strip("/") if path.strip("/") else ""
        self.origin = None  # set by serve()
        self.server = None
        self.client_id, self.client_secret = CLIENT_ID, CLIENT_SECRET
        self.redirect_uri = None  # the one registered redirect URI; /authorize refuses any other
        self.post_logout_redirect_uri = None
        self.user = dict(USER)
        self.auth_methods = ["client_secret_basic", "client_secret_post"]
        self.algs = ["RS256", "ES256"]
        self.alg = "RS256"
        self.fault = None
        self.delay_s = 3.0
        self.clock_offset_s = 0
        self.auto = False
        self.extra_keys = []  # JWKs published besides the provider's own
        self.counts = collections.Counter()
        self.authorize_requests = []  # each authorization request's query, as received
        self.auth_used = []  # how the client authenticated at each token request
        self.federated = []
        self.assertions = []
        self.token_endpoint = None
        self.logouts = []
        self._lock = threading.Lock()
        self._refused = threading.local()  # the error numbers of the request's refused assertion
        self._codes = {}
        self._pending = {}
        self._private = {"r1": rsa.generate_private_key(public_exponent=65537, key_size=2048),
                         "r2": rsa.generate_private_key(public_exponent=65537, key_size=2048),
                         "e1": ec.generate_private_key(ec.SECP256R1())}
        self._published = ["r1", "e1"]
        self._signing = {"RS256": "r1", "ES256": "e1"}
        self.app = self._make_app()

    @property
    def fault(self):
        return self._fault

    @fault.setter
    def fault(self, fault):
        if fault is not None and fault not in FAULTS:  # a misspelt fault would otherwise test nothing
            raise ValueError("no fault called {!r}".format(fault))
        self._fault = fault

    @property
    def base_url(self):
        """The issuer, exactly as discovery and the ID tokens state it."""
        return self.origin + self.path

    def _url(self, endpoint):
        return self.origin + self.prefix + endpoint

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def trust(self, cluster, subject="system:serviceaccount:grid:grid-ui", audience="api://AzureADTokenExchange"):
        """A federated identity credential: that cluster's tokens for that subject and audience stand for the
        client."""
        with self._lock:
            self.federated.append((cluster, subject, audience))

    def rotate(self):
        """Publish the second RSA key and sign with it from now on, as a provider rolling its keys over does."""
        with self._lock:
            self._published = ["r1", "r2", "e1"]
            self._signing["RS256"] = "r2"

    # ---- keys and tokens ------------------------------------------------------------

    def jwk(self, kid):
        """The public JWK of one of the provider's keys."""
        rsa_key = kid.startswith("r")
        algorithm = RSAAlgorithm if rsa_key else ECAlgorithm
        found = algorithm.to_jwk(self._private[kid].public_key(), as_dict=True)
        found.update(kid=kid, use="sig", alg="RS256" if rsa_key else "ES256")
        return found

    def public_pem(self, kid="r1"):
        return self._private[kid].public_key().public_bytes(serialization.Encoding.PEM,
                                                            serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, claims, alg=None, kid=None, headers=None, signer=None):
        """An ID token with these claims, signed with alg by the provider's key for it (or by its key signer). The
        header names kid, by default the signing key's id (kid=False names none), plus any headers."""
        alg = alg or self.alg
        with self._lock:
            signer = signer or self._signing[alg]
        header = dict(headers or {})
        if kid is not False:
            header["kid"] = signer if kid is None else kid
        return jwt.encode(claims, self._private[signer], algorithm=alg, headers=header)

    def claims(self, nonce, user=None):
        """The claims the provider puts in an ID token for user (the user knob), before any fault."""
        now = int(time.time()) + self.clock_offset_s
        user = dict(self.user if user is None else user)
        overage = user.pop("overage", False)
        claims = {"iss": self.base_url, "aud": self.client_id, "iat": now, "exp": now + 300, "auth_time": now,
                  "nonce": nonce}
        claims.update(user)
        if overage:
            claims.pop("groups", None)
            claims["_claim_names"] = {"groups": "src1"}
            claims["_claim_sources"] = {"src1": {"endpoint": self._url("/groups")}}
        return claims

    def id_token(self, nonce, user=None):
        """The ID token the token endpoint gives for a sign-in with that nonce, fault included."""
        claims, fault = self.claims(nonce, user), self.fault
        now = claims["iat"]
        changes = {
            "wrong_iss": {"iss": "https://impostor.example.com"},
            "wrong_aud": {"aud": "someone-else"},
            "multi_aud_no_azp": {"aud": [self.client_id, "someone-else"]},
            "azp_other": {"azp": "someone-else"},
            "expired": {"iat": now - 900, "exp": now - 600},
            "iat_future": {"iat": now + 600, "exp": now + 900},
            "nbf_future": {"nbf": now + 600},
            "wrong_nonce": {"nonce": "not-this-sign-ins-nonce"},
            "long_sub": {"sub": "s" * 256},
            "surrogate_sub": {"sub": "00u-\ud800"},
            "oversize_id_token": {"padding": "x" * (70 * 1024)},
        }
        claims.update(changes.get(fault, {}))
        for name, gone in (("nonce", "no_nonce"), ("sub", "missing_sub")):
            if fault == gone:
                claims.pop(name, None)
        if fault == "alg_none":
            return _segment({"alg": "none", "typ": "JWT"}) + "." + _segment(claims) + "."
        if fault == "hs256_public_key":
            signing_input = _segment({"alg": "HS256", "typ": "JWT", "kid": "r1"}) + "." + _segment(claims)
            mac = hmac.new(self.public_pem("r1"), signing_input.encode(), hashlib.sha256).digest()
            return signing_input + "." + _b64(mac)
        if fault == "unknown_kid":
            return self.sign(claims, kid="r-unknown")
        if fault == "kid_missing_many_keys":
            return self.sign(claims, alg="RS256", kid=False)
        if fault == "crit_header":
            # Signed by hand: jwt.encode drops b64 when it is true. b64 is an extension PyJWT knows, so it is the
            # Grid UI's own check that refuses it.
            header = {"alg": "RS256", "typ": "JWT", "kid": "r1", "b64": True, "crit": ["b64"]}
            signing_input = _segment(header) + "." + _segment(claims)
            signature = RSAAlgorithm(RSAAlgorithm.SHA256).sign(signing_input.encode(), self._private["r1"])
            return signing_input + "." + _b64(signature)
        signed = self.sign(claims)
        if fault == "bad_signature":
            header, _, signature = signed.split(".")
            return header + "." + _segment(dict(claims, name="Mallory")) + "." + signature
        return signed

    # ---- signing in without a browser -----------------------------------------------

    def _refusal(self, query):
        """Why the provider refuses an authorization request, or None."""
        if query.get("client_id") != self.client_id:
            return "unknown client_id"
        if not self.redirect_uri or query.get("redirect_uri") != self.redirect_uri:
            return "redirect_uri isn't registered"
        if query.get("response_type") != "code" or "openid" not in query.get("scope", "").split():
            return "only the code flow with the openid scope"
        if not query.get("code_challenge") or query.get("code_challenge_method") != "S256":
            return "PKCE with S256 is required"
        return None

    def _back(self, params):
        # The registered redirect URI, never the request's: what follows the ? is all the request can change.
        return self.redirect_uri + "?" + urllib.parse.urlencode(params)

    def _grant(self, query):
        """The callback URL with a fresh code for this request, as user."""
        code = secrets.token_urlsafe(24)
        with self._lock:
            self._codes[code] = {"client_id": query["client_id"], "redirect_uri": query["redirect_uri"],
                                 "challenge": query["code_challenge"], "nonce": query.get("nonce"),
                                 "user": dict(self.user)}
        return self._back({"code": code, "state": query.get("state", "")})

    def _denial(self, query):
        return self._back({"error": "access_denied", "error_description": "<b>no</b>",
                           "state": query.get("state", "")})

    def _query(self, authorize_url):
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query, keep_blank_values=True))
        self.authorize_requests.append(dict(query))
        refusal = self._refusal(query)
        if refusal:
            raise ValueError(refusal)
        return query

    def approve(self, authorize_url):
        """Where /authorize would send the browser back to once user signs in: the redirect URI with a code."""
        return self._grant(self._query(authorize_url))

    def deny(self, authorize_url):
        """Where /authorize would send the browser back to after Cancel: the redirect URI with access_denied."""
        return self._denial(self._query(authorize_url))

    # ---- endpoints ------------------------------------------------------------------

    def _make_app(self):
        app = Flask(__name__)
        for endpoint, view, methods in (
                ("/.well-known/openid-configuration", self._discovery, ["GET"]),
                ("/jwks", self._jwks, ["GET"]),
                ("/authorize", self._authorize, ["GET"]),
                ("/continue", self._continue, ["GET"]),
                ("/cancel", self._cancel, ["GET"]),
                ("/token", self._token, ["POST"]),
                ("/logout", self._logout, ["GET"])):
            app.add_url_rule(self.prefix + endpoint, view.__name__.strip("_"), view, methods=methods)

        @app.before_request
        def count():
            with self._lock:
                self.counts[request.endpoint] += 1
        return app

    @staticmethod
    def _json(body, status=200, mimetype="application/json"):
        return Response(json.dumps(body), status=status, mimetype=mimetype, headers={"Cache-Control": "no-store"})

    def _discovery(self):
        if self.fault == "discovery_down":
            return self._json({"error": "temporarily_unavailable"}, 503)
        if self.fault == "discovery_redirect":
            return redirect(self._url("/elsewhere"))
        document = {
            "issuer": self.origin + "/other" if self.fault == "discovery_issuer_mismatch" else self.base_url,
            "authorization_endpoint": self._url("/authorize") + ("?auto=1" if self.auto else ""),
            "token_endpoint": self.token_endpoint or self._url("/token"),
            "jwks_uri": self._url("/jwks"),
            "end_session_endpoint": self._url("/logout"),
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "code_challenge_methods_supported": ["S256"],
        }
        if self.algs is not None:
            document["id_token_signing_alg_values_supported"] = list(self.algs)
        if self.auth_methods is not None:
            document["token_endpoint_auth_methods_supported"] = list(self.auth_methods)
        return self._json(document, mimetype="text/html" if self.fault == "html_content_type" else "application/json")

    def _jwks(self):
        with self._lock:
            kids = list(self._published)
        if self.fault == "kid_missing_many_keys" and "r2" not in kids:
            kids.append("r2")
        body = {"keys": [self.jwk(kid) for kid in kids] + list(self.extra_keys)}
        if self.fault == "jwks_oversize":
            body["padding"] = "x" * (300 * 1024)
        # Served as Google serves its key set, with a charset parameter.
        kind = "text/html; charset=utf-8" if self.fault == "html_content_type" else "application/json; charset=UTF-8"
        return Response(json.dumps(body), content_type=kind)

    def _authorize(self):
        query = request.args.to_dict()
        self.authorize_requests.append(dict(query))
        refusal = self._refusal(query)
        if refusal:
            return Response(refusal, status=400, mimetype="text/plain")
        if query.get("auto") == "1":
            return redirect(self._grant(query), 303)
        pending = secrets.token_urlsafe(12)
        with self._lock:
            self._pending[pending] = query
        page = ("<!doctype html><meta charset=utf-8><title>Fake identity provider</title>"
                "<form method=get action={continue_to}><input type=hidden name=pending value={pending}>"
                "<p>Sign in to the grid?</p><button id=continue>Continue as {name}</button> "
                "<button id=cancel formaction={cancel}>Cancel</button></form>").format(
            continue_to=html.escape(self._url("/continue")), cancel=html.escape(self._url("/cancel")),
            pending=pending, name=html.escape(str(self.user.get("name", "")), quote=True))
        return Response(page, mimetype="text/html")

    def _take_pending(self):
        with self._lock:
            return self._pending.pop(request.args.get("pending", ""), None)

    def _continue(self):
        query = self._take_pending()
        if query is None:
            return Response("unknown sign-in", status=400, mimetype="text/plain")
        return redirect(self._grant(query), 303)

    def _cancel(self):
        query = self._take_pending()
        if query is None:
            return Response("unknown sign-in", status=400, mimetype="text/plain")
        return redirect(self._denial(query), 303)

    def _refuse(self, *codes, quoted=""):
        """Refuses the request's assertion: _token() answers with these error numbers and a description that
        quotes quoted."""
        self._refused.codes, self._refused.quoted = codes, str(quoted)
        return None

    def _asserted_client(self):
        """The client_id a token request with a client assertion authenticated as, or None. The checks run in
        this order and the first that fails decides the answer: the request's shape, the client, any federated
        credential at all, the token's issuer, its signature, its expiry, its subject, its audience."""
        form = request.form
        raw = form.get("client_assertion", "")
        with self._lock:
            self.auth_used.append("federated")
            trusted = list(self.federated)
        parts = raw.split(".")
        header, claims = (_unsegment(parts[0]), _unsegment(parts[1])) if len(parts) == 3 else (None, None)
        if header is not None and claims is not None:
            with self._lock:
                self.assertions.append({"header": header, "claims": claims, "raw": raw})
        if (form.get("client_assertion_type") != ASSERTION_TYPE or "Authorization" in request.headers
                or "client_secret" in form or "private_key_jwt" not in (self.auth_methods or [])
                or header is None or claims is None):
            return self._refuse()
        if form.get("client_id") != self.client_id:
            return self._refuse(700016, quoted=form.get("client_id", ""))
        if not trusted:
            return self._refuse(70025)
        issuer = claims.get("iss")
        triples = [triple for triple in trusted if triple[0].issuer == issuer]
        if not isinstance(issuer, str) or not triples:
            return self._refuse(700211, quoted=issuer)
        if header.get("alg") != "RS256":
            return self._refuse()
        verified = None
        for cluster in {id(triple[0]): triple[0] for triple in triples}.values():
            try:
                # The signature and nothing else: the checks after this one each have an answer of their own.
                verified = jwt.decode(raw, cluster.key.public_key(), algorithms=["RS256"], options={
                    "verify_signature": True, "verify_exp": False, "verify_nbf": False, "verify_iat": False,
                    "verify_aud": False, "verify_iss": False})
            except jwt.InvalidTokenError:
                continue
            triples = [triple for triple in triples if triple[0] is cluster]
            break
        if verified is None:
            return self._refuse()
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or exp <= time.time() + self.clock_offset_s:
            return self._refuse(700024, quoted=exp)
        subject = claims.get("sub")
        triples = [triple for triple in triples if isinstance(subject, str) and triple[1] == subject]
        if not triples:
            return self._refuse(700213, quoted=subject)
        audience = claims.get("aud")
        audience = [audience] if isinstance(audience, str) else audience if isinstance(audience, list) else []
        if not any(triple[2] in audience for triple in triples):
            return self._refuse(700212, quoted=claims.get("aud"))
        return self.client_id

    def _client(self):
        """The client_id the token request authenticated as, or None (wrong credentials, a method this provider
        doesn't take, or two methods at once). With a client assertion: whether it is a trusted cluster's token
        for the client."""
        self._refused.codes = self._refused.quoted = None
        if "client_assertion" in request.form or "client_assertion_type" in request.form:
            return self._asserted_client()
        header = request.headers.get("Authorization", "")
        if header.startswith("Basic "):
            if "client_secret" in request.form:
                return None
            try:
                pair = base64.b64decode(header[6:], validate=True).decode()
            except ValueError:  # bad base64, or not UTF-8
                return None
            name, _, given = pair.partition(":")
            # RFC 6749 2.3.1: each part was form-encoded before they were joined.
            client, given = urllib.parse.unquote_plus(name), urllib.parse.unquote_plus(given)
            method = "client_secret_basic"
            if request.form.get("client_id", client) != client:
                return None
        else:
            client, given = request.form.get("client_id", ""), request.form.get("client_secret", "")
            method = "client_secret_post"
        with self._lock:
            self.auth_used.append(method)
        accepted = self.auth_methods if self.auth_methods is not None else ["client_secret_basic"]
        if method not in accepted or client != self.client_id:
            return None
        return client if hmac.compare_digest(given.encode(), self.client_secret.encode()) else None

    def _token(self):
        if self.fault == "token_slow":
            time.sleep(self.delay_s)
        refusals = {"token_400_invalid_grant": (400, "invalid_grant"), "token_401_invalid_client": (401, "invalid_client"),
                    "token_500": (500, "server_error")}
        if self.fault in refusals:
            status, error = refusals[self.fault]
            return self._json({"error": error}, status)
        numbered = {"token_401_entra_70021": (401, "invalid_client", 70021),
                    "token_400_grant_with_number": (400, "invalid_grant", 700024)}
        if self.fault in numbered:
            status, error, number = numbered[self.fault]
            return self._json(entra_refusal(error, (number,), "system:serviceaccount:grid:grid-ui"), status)
        client = self._client()
        if client is None:
            if self._refused.codes is not None:  # an assertion: refused as Entra refuses one
                return self._json(entra_refusal("invalid_client", self._refused.codes, self._refused.quoted), 401)
            return self._json({"error": "invalid_client"}, 401)
        form = request.form
        with self._lock:
            grant = self._codes.pop(form.get("code", ""), None)  # a code works once, right or wrong
        verifier = form.get("code_verifier", "")
        if (grant is None or form.get("grant_type") != "authorization_code" or grant["client_id"] != client
                or form.get("redirect_uri") != grant["redirect_uri"]
                or not hmac.compare_digest(_challenge(verifier).encode(), grant["challenge"].encode())):
            return self._json({"error": "invalid_grant"}, 400)
        return self._json({"access_token": "at-" + secrets.token_urlsafe(24), "token_type": "Bearer",
                           "expires_in": 3600, "refresh_token": "rt-" + secrets.token_urlsafe(24),
                           "id_token": self.id_token(grant["nonce"], grant["user"])})

    def _logout(self):
        query = request.args.to_dict()
        self.logouts.append(dict(query))
        if self.post_logout_redirect_uri and query.get("post_logout_redirect_uri") == self.post_logout_redirect_uri:
            return redirect(self.post_logout_redirect_uri, 303)
        return Response("<!doctype html><title>Signed out</title><p>You're signed out of the fake identity provider.",
                        mimetype="text/html")


def serve(host="127.0.0.1", path="", tls=None, public_host=None):
    """A FakeIdp answering at http://<host>:<a free port><path> until stop(). Unit tests use 127.0.0.1; browser
    tests use localhost, so the provider is another site than a grid at 127.0.0.1. tls: (certificate file, key
    file) to answer over https instead (write_server_certificate makes a pair for 127.0.0.1). public_host: the
    host its addresses name, when it listens on 0.0.0.0."""
    idp = FakeIdp(path)
    idp.server = make_server(host, 0, idp.app, threaded=True, request_handler=_Quiet, ssl_context=tls)
    idp.origin = "{}://{}:{}".format("https" if tls else "http", public_host or host, idp.server.server_port)
    # Polled often, so stop() returns at once rather than half a second later in every test.
    threading.Thread(target=idp.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return idp


def write_server_certificate(folder):
    """A CA and a certificate it signed for 127.0.0.1 and localhost, written to folder: (the CA's file, the
    certificate's file, its key's file). GRID_OIDC_CA_FILE takes the CA's."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test identity provider CA")])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
          .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
          .not_valid_after(now + datetime.timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                       crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
          .sign(ca_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])).issuer_name(ca_name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                        x509.DNSName("localhost")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    paths = [folder / "ca.pem", folder / "server.pem", folder / "server-key.pem"]
    paths[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    paths[1].write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    paths[2].write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return tuple(str(p) for p in paths)


# ---- the platform's managed identity endpoint ----------------------------------------------------

# Made-up names of one user-assigned managed identity, the tenant that issues its tokens, and the application a
# token's aud may name in place of the resource asked for.
CLIENT_ID_MI = "aaaaaaaa-1111-2222-3333-444444444444"
OBJECT_ID_MI = "bbbbbbbb-5555-6666-7777-888888888888"
TENANT_ID = "ffffffff-0000-1111-2222-333333333333"
TENANT_ISSUER = "https://login.microsoftonline.com/{}/v2.0".format(TENANT_ID)
RESOURCE_APP_ID = "cccccccc-9999-aaaa-bbbb-dddddddddddd"
IMDS_PATH = "/metadata/identity/oauth2/token"
APP_SERVICE_PATH = "/msi/token"
# What each fault does.
IDENTITY_FAULTS = {
    "slow": "the endpoint waits delay_s before it answers",
    "hang_up": "the endpoint closes the connection without answering",
    "garbage": "the endpoint writes a line that isn't HTTP and closes the connection",
    "redirect": "the endpoint answers 302 to /elsewhere",
    # IMDS answers an error with an OAuth error code; App Service with statusCode and message, and no code.
    "status_400": "the endpoint answers 400 (IMDS: invalid_request)",
    "status_401": "the endpoint answers 401 (IMDS: unauthorized)",
    "status_403": "the endpoint answers 403 (IMDS: forbidden)",
    "status_404": "the endpoint answers 404 (IMDS: not_found)",
    "status_410": "the endpoint answers 410 (IMDS: gone)",
    "status_429": "the endpoint answers 429 (IMDS: too_many_requests)",
    "status_500": "the endpoint answers 500 (IMDS: unknown)",
    "status_503": "the endpoint answers 503 (IMDS: service_unavailable)",
    "html": "the token's answer comes as text/html",
    "not_json": "the answer is application/json that isn't JSON",
    "json_list": "the answer is a JSON list",
    "no_token": "the answer has no access_token",
    "not_a_jwt": "the access_token isn't a JWT",
    "huge_token": "the access_token is a JWT of 16385 bytes",
    "oversize": "the answer is 65537 bytes of JSON",
    "expired": "the token's exp was an hour ago",
}
_STATUS_ERRORS = {400: "invalid_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 410: "gone",
                  429: "too_many_requests", 500: "unknown", 503: "service_unavailable"}


def sized_token(issuer, size, **claims):
    """A token of issuer's (a FakeCluster) with these claims, padded to exactly size bytes."""
    for extra in range(4):
        low, high = 0, size
        while low <= high:
            pad = (low + high) // 2
            token = issuer.token(padding="p" * pad, **claims)
            header, payload, signature = token.split(".")
            if extra:  # base64url skips one length in four: a longer kid moves the whole token by a character
                token = jwt.encode(_unsegment(payload), issuer.key, algorithm="RS256",
                                   headers={"kid": "cluster-1" + "x" * extra})
            if len(token) == size:
                return token
            if len(token) < size:
                low = pad + 1
            else:
                high = pad - 1
    raise ValueError("no token of {} bytes".format(size))


class FakeIdentityEndpoint:
    """This machine's managed identity endpoint, both ways Azure offers it: an Azure VM's Instance Metadata Service
    (GET /metadata/identity/oauth2/token, header Metadata: true, api-version 2018-02-01 or later) and App
    Service's (GET /msi/token, header X-IDENTITY-HEADER, api-version 2019-08-01). Its tokens are signed by tenant,
    a FakeCluster whose issuer is an Entra tenant's v2.0 address, with sub the identity's object id and aud the
    resource asked for, as a string. Its knobs are plain attributes:

    system       the system-assigned identity's object id, or None: none turned on
    assigned     user-assigned identities, client id to object id
    header       the IDENTITY_HEADER the App Service path takes
    fault        one of IDENTITY_FAULTS, or None
    delay_s      how long slow waits
    lifetime_s   the token's life
    aud_form     resource: aud is the resource asked for; guid: RESOURCE_APP_ID, as Entra is reported to do
    iss_form     v2: tokens signed by tenant; v1: by a second issuer, https://sts.windows.net/<tenant>/
    token_size   the access_token padded to this many bytes, or None
    answer_size  the answer padded to this many bytes, or None
    requests     each request: path, query (a dict), header names, header_ok (never the value), forwarded
    elsewhere    requests to /elsewhere, where redirect points
    issued       every access_token given, for tests that look for it where it may not be
    """

    def __init__(self):
        self.tenant = FakeCluster(TENANT_ISSUER)
        self.tenant_v1 = FakeCluster("https://sts.windows.net/{}/".format(TENANT_ID))
        self.system = None
        self.assigned = {CLIENT_ID_MI: OBJECT_ID_MI}
        self.header = secrets.token_urlsafe(24)
        self.fault = None
        self.delay_s = 1.5
        self.lifetime_s = 3600
        self.aud_form = "resource"
        self.iss_form = "v2"
        self.token_size = None
        self.answer_size = None
        self.requests = []
        self.elsewhere = 0
        self.issued = []
        self.origin = None
        self.server = None
        self._lock = threading.Lock()
        self.app = self._make_app()

    @property
    def fault(self):
        return self._fault

    @fault.setter
    def fault(self, fault):
        if fault is not None and fault not in IDENTITY_FAULTS:
            raise ValueError("no fault called {!r}".format(fault))
        self._fault = fault

    @property
    def imds_url(self):
        return self.origin + IMDS_PATH

    @property
    def app_service_url(self):
        return self.origin + APP_SERVICE_PATH

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def _make_app(self):
        app = Flask(__name__)
        app.add_url_rule(IMDS_PATH, "imds", lambda: self._token("imds"), methods=["GET"])
        app.add_url_rule(APP_SERVICE_PATH, "app_service", lambda: self._token("app-service"), methods=["GET"])
        app.add_url_rule("/elsewhere", "elsewhere", self._elsewhere, methods=["GET"])
        return app

    def _elsewhere(self):
        with self._lock:
            self.elsewhere += 1
        return self._refusal(404, "not_found")

    @staticmethod
    def _refusal(status, error, description="", kind="imds"):
        """An error answer in the shape that kind of endpoint uses: IMDS's OAuth error and error_description, or
        App Service's statusCode, message and correlationId, which carry no error code. Each holds
        DESCRIPTION_MARKER in its words."""
        words = "{} {}".format(description, DESCRIPTION_MARKER).strip()
        if kind == "app-service":
            body = {"statusCode": status, "message": words, "correlationId": CORRELATION_ID}
        else:
            body = {"error": error, "error_description": words}
        return Response(json.dumps(body), status=status, mimetype="application/json")

    @staticmethod
    def _drop(line=b""):
        """Writes line, which isn't HTTP, and closes the connection: the caller gets no answer. What the handler
        writes after this goes nowhere."""
        connection = request.environ["werkzeug.socket"]
        if line:
            connection.sendall(line)
        connection.shutdown(socket.SHUT_RDWR)

    def _token(self, kind):
        query = request.args.to_dict()
        if kind == "imds":
            header_ok = request.headers.get("Metadata") == "true"
        else:
            header_ok = hmac.compare_digest(request.headers.get("X-IDENTITY-HEADER", "").encode(), self.header.encode())
        forwarded = "X-Forwarded-For" in request.headers
        with self._lock:
            self.requests.append({"path": request.path, "query": dict(query), "headers": sorted(request.headers.keys()),
                                  "header_ok": header_ok, "forwarded": forwarded})
        fault = self.fault
        if fault == "slow":
            time.sleep(self.delay_s)
        if not header_ok:
            return self._refusal(400, "bad_request_102", "Required metadata header not specified", kind)
        if forwarded:
            return self._refusal(400, "bad_request", "X-Forwarded-For isn't taken", kind)
        if not query.get("api-version") or not query.get("resource"):
            return self._refusal(400, "invalid_request", "api-version and resource are required", kind)
        if sum(1 for name in ("client_id", "object_id", "msi_res_id", "mi_res_id") if name in query) > 1:
            return self._refusal(400, "invalid_request", "only one identity may be named", kind)
        if "client_id" in query:
            object_id = self.assigned.get(query["client_id"])
        elif any(name in query for name in ("object_id", "msi_res_id", "mi_res_id")):
            object_id = None
        else:
            object_id = self.system
        if object_id is None:
            return self._refusal(400, "invalid_request", "Identity not found", kind)
        if fault in ("hang_up", "garbage"):
            self._drop(b"garbage\r\n" if fault == "garbage" else b"")
            return Response("", status=204)
        if fault == "redirect":
            return redirect("/elsewhere", 302)
        if fault and fault.startswith("status_"):
            status = int(fault[7:])
            return self._refusal(status, _STATUS_ERRORS[status], kind=kind)
        resource = query["resource"]
        now = int(time.time())
        # aud as a string, as Entra writes it; uti makes each token another, as Entra's are.
        claims = {"subject": object_id, "oid": object_id, "tid": TENANT_ID, "uti": secrets.token_urlsafe(16),
                  "aud": RESOURCE_APP_ID if self.aud_form == "guid" else resource, "nbf": now, "iat": now,
                  "exp": now - 3600 if fault == "expired" else now + self.lifetime_s}
        signer = self.tenant_v1 if self.iss_form == "v1" else self.tenant
        size = 16385 if fault == "huge_token" else self.token_size
        token = sized_token(signer, size, **claims) if size else signer.token(**claims)
        if fault == "not_a_jwt":
            token = "not-a-jwt-" + secrets.token_urlsafe(16)
        with self._lock:
            self.issued.append(token)
        expires_on = str(now + self.lifetime_s)
        if kind == "imds":
            body = {"access_token": token, "refresh_token": "", "expires_in": str(self.lifetime_s - 1),
                    "expires_on": expires_on, "not_before": str(now), "resource": resource, "token_type": "Bearer"}
        else:
            body = {"access_token": token, "expires_on": expires_on, "resource": resource, "token_type": "Bearer",
                    "client_id": query.get("client_id") or "dddddddd-0000-0000-0000-000000000000"}
        if fault == "no_token":
            del body["access_token"]
        text = json.dumps(body)
        size = 65537 if fault == "oversize" else self.answer_size
        if size:
            body["padding"] = ""
            text = json.dumps(body)
            body["padding"] = "x" * (size - len(text))
            text = json.dumps(body)
        if fault == "json_list":
            text = json.dumps([body])
        if fault == "not_json":
            text = "{" + text
        return Response(text, mimetype="text/html" if fault == "html" else "application/json")


def serve_identity(host="127.0.0.1"):
    """A FakeIdentityEndpoint answering at http://<host>:<a free port> until stop()."""
    endpoint = FakeIdentityEndpoint()
    endpoint.server = make_server(host, 0, endpoint.app, threaded=True, request_handler=_Quiet)
    endpoint.origin = "http://{}:{}".format(host, endpoint.server.server_port)
    threading.Thread(target=endpoint.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return endpoint


class Recorder:
    """An HTTP server that answers 502 to every request and counts them: it stands in for a proxy and for the VM's
    managed identity address, where no request may go. seen: (the request line, the source address) of each;
    never a header's value."""

    def __init__(self, host="127.0.0.1"):
        self.seen = []
        lock, seen = threading.Lock(), self.seen

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def __getattr__(self, name):
                if not name.startswith("do_"):
                    raise AttributeError(name)
                return self._answer

            def _answer(self):
                with lock:
                    seen.append((self.requestline, self.client_address[0]))
                body = b'{"error":"bad_gateway"}'
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Connection", "close")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)
                self.close_connection = True

        self.server = http.server.ThreadingHTTPServer((host, 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://{}:{}".format(host, self.server.server_port)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    @property
    def count(self):
        return len(self.seen)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def serve_recorder(host="127.0.0.1"):
    return Recorder(host)
