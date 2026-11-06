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
import hashlib
import hmac
import html
import json
import secrets
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
    "token_500": "the token endpoint answers 500",
    "token_slow": "the token endpoint takes delay_s to answer",
    "discovery_down": "discovery answers 503",
    "discovery_issuer_mismatch": "discovery states another issuer",
    "discovery_redirect": "discovery redirects elsewhere",
    "jwks_oversize": "the key set is over 256 KiB",
    "oversize_id_token": "the ID token is over 64 KiB",
    "html_content_type": "discovery and the key set answer as text/html",
}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _segment(obj):
    return _b64(json.dumps(obj, separators=(",", ":")).encode())


def _challenge(verifier):
    return _b64(hashlib.sha256(verifier.encode()).digest())


class _Quiet(WSGIRequestHandler):
    """No access log: what the Grid UI logs is what the tests look at."""

    def log_request(self, code="-", size="-"):
        pass


class FakeIdp:
    """The provider. Its knobs are plain attributes a test sets:

    user          the claims of whoever signs in next: sub, email, email_verified, name, groups (a list, a str or
                  left out), any other claim, and overage (True: the groups are left out as Entra does)
    auth_methods  token_endpoint_auth_methods_supported, which the token endpoint also enforces (None: not
                  listed, and only client_secret_basic is taken, the standard's default)
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
        self.logouts = []
        self._lock = threading.Lock()
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
            "token_endpoint": self._url("/token"),
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

    def _client(self):
        """The client_id the token request authenticated as, or None (wrong credentials, a method this provider
        doesn't take, or two methods at once)."""
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
        client = self._client()
        if client is None:
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


def serve(host="127.0.0.1", path=""):
    """A FakeIdp answering at http://<host>:<a free port><path> until stop(). Unit tests use 127.0.0.1; browser
    tests use localhost, so the provider is another site than a grid at 127.0.0.1."""
    idp = FakeIdp(path)
    idp.server = make_server(host, 0, idp.app, threaded=True, request_handler=_Quiet)
    idp.origin = "http://{}:{}".format(host, idp.server.server_port)
    # Polled often, so stop() returns at once rather than half a second later in every test.
    threading.Thread(target=idp.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return idp
