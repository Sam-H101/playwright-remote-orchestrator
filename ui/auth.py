"""Who may change the grid: one gate, pluggable sign-in providers.

Today the only provider is the shared admin token (GRID_ADMIN_TOKEN, sent as X-Grid-Token). A
second provider (OAuth, SSO) plugs in by adding an object with the same methods to PROVIDERS:
configured(), attempted(request) (did the request carry this provider's credentials at all?),
authenticate(request) -> Principal or None, and describe() -> {id, kind, label}. Routes never look
at headers themselves; they are wrapped in admin_required(), which leaves the principal in g.principal.
"""
import collections
import functools
import hmac
import os
import threading
import time
from collections import namedtuple

from flask import abort, g, jsonify, make_response, request

# name: who signed in, as the page shows it; method: the provider id that vouched for them.
Principal = namedtuple("Principal", "name method")


class AdminTokenProvider:
    """A shared secret, kept by the page for one browser tab."""

    id = "token"
    header = "X-Grid-Token"

    def __init__(self, secret=None):
        # A callable, so a test (or a reload of the settings) can change the secret at runtime.
        self.secret = secret or (lambda: os.environ.get("GRID_ADMIN_TOKEN", ""))

    def configured(self):
        return bool(self.secret())

    def attempted(self, req):
        # The page sends the header empty when it has no secret (it proves same-origin): not a guess.
        return bool(req.headers.get(self.header, ""))

    def authenticate(self, req):
        secret = self.secret()
        if not secret or self.header not in req.headers:
            return None
        supplied = req.headers.get(self.header, "")
        if hmac.compare_digest(supplied.encode(), secret.encode()):
            return Principal("Admin", self.id)
        return None

    def describe(self):
        return {"id": self.id, "kind": "secret", "label": "Admin token"}


PROVIDERS = [AdminTokenProvider()]


def configure(admin_secret):
    """Point the admin-token provider at the app's setting (a callable returning the secret)."""
    for provider in PROVIDERS:
        if isinstance(provider, AdminTokenProvider):
            provider.secret = admin_secret


def admin_configured():
    return any(p.configured() for p in PROVIDERS)


# Guessing the admin secret: after FAILURE_LIMIT wrong credentials from one address within
# FAILURE_WINDOW_S, that address is refused (429) until the oldest failure ages out. In memory and per
# process: it slows a guesser down, it isn't an account lockout.
FAILURE_LIMIT = 20
FAILURE_WINDOW_S = 300
_failures = collections.defaultdict(collections.deque)
_failures_lock = threading.Lock()


def _client():
    return request.remote_addr or "?"


def _recent_failures(addr, now):
    q = _failures.get(addr)
    if not q:
        return q
    while q and now - q[0] > FAILURE_WINDOW_S:
        q.popleft()
    if not q:
        del _failures[addr]
    return q


def retry_after():
    """Seconds this client must wait before its credentials are looked at again (0: go ahead)."""
    now = time.monotonic()
    with _failures_lock:
        q = _recent_failures(_client(), now)
        if not q or len(q) < FAILURE_LIMIT:
            return 0
        return max(1, int(FAILURE_WINDOW_S - (now - q[0])) + 1)


def _note_failure():
    with _failures_lock:
        _failures[_client()].append(time.monotonic())


def reset_failures():
    with _failures_lock:
        _failures.clear()


def _too_many(wait):
    response = make_response(jsonify(error="Too many wrong sign-in attempts from this address. Try again in {} s.".format(wait),
                                      retry_after_s=wait), 429)
    response.headers["Retry-After"] = str(wait)
    abort(response)


def current_principal():
    """Who the request's credentials belong to, or None. Wrong credentials count towards the
    guessing limit; past it, the request is refused with 429 before any credential is compared."""
    configured = [p for p in PROVIDERS if p.configured()]
    # A provider without attempted() is asked every time (and its refusals don't count as guesses).
    attempted = [p for p in configured if getattr(p, "attempted", lambda req: True)(request)]
    if not attempted:
        return None
    wait = retry_after()
    if wait:
        _too_many(wait)
    for provider in attempted:
        principal = provider.authenticate(request)
        if principal is not None:
            return principal
    if any(hasattr(p, "attempted") for p in attempted):
        _note_failure()
    return None


def _refuse(**body):
    abort(make_response(jsonify(**body), 403))


def check_admin(refuse_when_unconfigured=False):
    """The gate. Without any sign-in configured, grid actions stay open (as they always were), but
    access tokens are refused: their metadata is sensitive and only an admin may manage them."""
    if not admin_configured():
        if refuse_when_unconfigured:
            # admin_token_unset: the page explains the setting instead of asking anyone to sign in.
            _refuse(error="set GRID_ADMIN_TOKEN to manage access tokens", admin_token_unset=True)
        return None
    principal = current_principal()
    if principal is None:
        # token_required tells the page to ask for sign-in, unlike other 403s (proxies, origin checks).
        _refuse(error="The admin token is missing or wrong.", token_required=True)
    return principal


def admin_required(refuse_when_unconfigured=False):
    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            g.principal = check_admin(refuse_when_unconfigured)
            return view(*args, **kwargs)
        return wrapper
    return decorate


def session_info():
    """What the page needs to present sign-in state. Validates the credentials sent, if any."""
    principal = current_principal()
    return {
        "signedIn": principal is not None,
        "name": principal.name if principal else None,
        "method": principal.method if principal else None,
        "adminConfigured": admin_configured(),
        "providers": [p.describe() for p in PROVIDERS if p.configured()],
    }
