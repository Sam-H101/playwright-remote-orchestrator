"""Who may change the grid: one gate, pluggable sign-in providers.

Today the only provider is the shared admin token (GRID_ADMIN_TOKEN, sent as X-Grid-Token). A
second provider (OAuth, SSO) plugs in by adding an object with the same three methods to
PROVIDERS: configured(), authenticate(request) -> Principal or None, and describe(). Routes never
look at headers themselves; they are wrapped in admin_required().
"""
import functools
import hmac
import os
from collections import namedtuple

from flask import abort, jsonify, make_response, request

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


def current_principal():
    for provider in PROVIDERS:
        if provider.configured():
            principal = provider.authenticate(request)
            if principal is not None:
                return principal
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
            check_admin(refuse_when_unconfigured)
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
