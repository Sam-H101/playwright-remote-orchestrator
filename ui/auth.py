"""Who may do what on the grid: one gate, pluggable sign-in providers.

Two providers come with it. The shared admin token (GRID_ADMIN_TOKEN, sent as X-Grid-Token) is the
break-glass way in, and holds every permission. Single sign-on (GRID_OIDC_ISSUER) signs people in
through an OpenID Connect identity provider; their browser then sends the session cookie the sign-in
set, and they may do what their roles allow (accounts.py works that out on every request). Another
provider plugs in by adding an object with the same methods to PROVIDERS: configured(),
attempted(request) (did the request carry this provider's credentials at all?), authenticate(request)
-> Principal or None, and describe() -> {id, kind, label}. Routes never look at headers or cookies
themselves; they are wrapped in permission_required() (or admin_required(), for what needs every
permission), which leaves the principal in g.principal, and the grid's reads ask reader_principal()
where they need sign-in. Who the request's credentials belong to is worked out once per request.
"""
import collections
import functools
import hmac
import inspect
import os
import threading
import time
from collections import namedtuple

from flask import abort, g, jsonify, make_response, request

from . import oidc, permissions, store

# name: who it is, as the page shows it; method: the provider id that vouched for them; user_id: their users.id
# (None for the admin token and for client tokens without an owner); permissions: what they may do; role_ids:
# their effective role ids, for items shared with a role; email: for the account menu. A provider that says
# nothing about permissions vouches for an admin, as every principal was before roles existed. scope: a client
# token's scope (None: no limit; people and the bootstrap token have none). It is kept apart from permissions on
# purpose: what a token reaches is judged by its owner's permissions, and a smaller set there would reach more
# shared sign-ins, not fewer.
Principal = namedtuple("Principal", "name method user_id permissions role_ids email scope",
                       defaults=(None, permissions.ALL, (), None, None))

# What anyone but an admin is told about single sign-on that can't work as configured: the problem itself may
# name a file path, so it goes to the log, the admin token and people who may see the settings (users.view).
MISCONFIGURED = ("Single sign-on isn't set up correctly. The Grid UI's log says what's wrong; an admin can sign in "
                 "with the admin token meanwhile.")

# Why a session cookie stopped working, as the page tells it (accounts.session_access says which).
ENDED = {
    "expired": "Your sign-in has expired. Sign in again.",
    "idle": "You were signed out after a while without using the grid. Sign in again.",
    "revoked": "Your session was ended, by signing out elsewhere or by an admin. Sign in again.",
    "disabled": "Your account on this grid is disabled. Ask an admin to enable it.",
}


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

    def describe(self, detail=False):
        return {"id": self.id, "kind": "secret", "label": "Admin token"}


class OidcSessionProvider:
    """People signed in through the OpenID Connect identity provider: their browser sends the session cookie the
    sign-in set. A session token can't be guessed, so a dead one never counts towards the guessing limit
    (guessable = False); the app's after_request hook drops it instead."""

    id = "oidc"
    guessable = False
    uses_database = True  # known_principal() leaves it out

    def __init__(self, lookup=None):
        self.lookup = lookup  # token -> user access with session_id, oidc.Ended(reason) or None (set by configure())

    def configured(self):
        return oidc.configured()  # GRID_OIDC_ISSUER is set, even when the rest is wrong: the grid stays protected

    def attempted(self, req):
        return bool(req.cookies.get(oidc.cookie_names().session))

    def authenticate(self, req):
        token = req.cookies.get(oidc.cookie_names().session, "")
        if not token or self.lookup is None:
            return None
        found = self.lookup(token)  # DatabaseUnavailable propagates (the route answers 503, not "signed out")
        if found is None or isinstance(found, oidc.Ended):
            g.session_ended = found.reason if found else "unknown"  # the app's after_request hook drops the cookie
            return None
        g.signed_in_user = found  # for session_info: who they are and where their roles came from
        return principal_for_user(found, self.id)

    def describe(self, detail=False):
        d = {"id": self.id, "kind": "redirect", "label": oidc.settings().label, "start": "auth/login"}
        problem = sign_in_problem()
        if problem:
            d["problem"] = problem if detail else MISCONFIGURED
        return d


PROVIDERS = [AdminTokenProvider(), OidcSessionProvider()]
_tables_problem = None  # accounts.tables_problem, set by configure()


def configure(admin_secret, session_lookup=None, problem=None):
    """Point the providers at the app's settings: the admin token (a callable returning the secret), the session
    lookup (accounts.session_access) and what says the sign-in tables are missing (accounts.tables_problem).
    They are handed in because auth.py never imports accounts."""
    global _tables_problem
    for provider in PROVIDERS:
        if isinstance(provider, AdminTokenProvider):
            provider.secret = admin_secret
        elif isinstance(provider, OidcSessionProvider):
            provider.lookup = session_lookup
    _tables_problem = problem


def sign_in_problem():
    """Why nobody can sign in with single sign-on, as a sentence for the log and for admins, or None: the settings
    (oidc.problem()), or a database without the users tables."""
    return oidc.problem() or (_tables_problem() if _tables_problem else None)


def admin_configured():
    return any(p.configured() for p in PROVIDERS)


def principal_for_user(access, method, name=None):
    """The principal of a user, from what accounts.user_access says they may do now."""
    return Principal(name or access["name"], method, access["id"], access["permissions"],
                     tuple(r["id"] for r in access["roles"]), access["email"])


def is_admin(principal):
    """Whether the principal may do everything: the Admin role, the admin token, a client token without an owner,
    or a provider that says nothing about permissions."""
    return principal is not None and principal.permissions == permissions.ALL


def can(principal, permission):
    """Whether the principal may do this. None is a grid with no sign-in configured, where only grid actions are
    open."""
    if principal is None:
        return permission in permissions.OPEN_GRID
    return permission in principal.permissions


def access(principal):
    """What the principal may see of MFA secrets, saved sign-ins and tokens (store.Access). Whoever may do
    everything counts as holding the Admin role, so what is shared with Admin reaches them all as it reaches the
    admins: an MFA secret always, a saved sign-in while store._reaches says it does."""
    roles = tuple(principal.role_ids)
    if is_admin(principal) and permissions.ADMIN not in roles:
        roles += (permissions.ADMIN,)
    return store.Access(principal.user_id, roles, principal.permissions)


def actor(principal):
    """Who makes a change, for the audit log (store.Actor), or None on a grid with no sign-in configured."""
    if principal is None:
        return None
    return store.Actor(principal.user_id, principal.name, principal.method, request.remote_addr, principal.permissions)


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


def _guessable(provider):
    # Whether a wrong credential of this provider is a guess: a secret someone could type, not a session token.
    return hasattr(provider, "attempted") and getattr(provider, "guessable", True)


def _principal_from(providers):
    configured = [p for p in providers if p.configured()]
    # A provider without attempted() is asked every time (and its refusals don't count as guesses).
    attempted = [p for p in configured if getattr(p, "attempted", lambda req: True)(request)]
    if not attempted:
        return None
    if any(_guessable(p) for p in attempted):
        wait = retry_after()
        if wait:
            _too_many(wait)
    found, failed = None, False
    try:
        for provider in attempted:
            principal = provider.authenticate(request)
            if principal is not None:
                found = principal
                break
            failed = failed or _guessable(provider)
    finally:
        # Counted even when a later provider raises: a session lookup that meets an outage must not make a wrong
        # admin token beside it a free guess.
        if failed:
            _note_failure()
            g.secret_refused = True  # the app's after_request hook tells the page, which stops sending it
    return found


def current_principal():
    """Who the request's credentials belong to, or None. A wrong guessable credential (the admin token) counts
    towards the guessing limit even when another credential vouches for the request; past the limit, a request
    that sends one is refused with 429 before anything is compared. Session cookies can't be guessed and never
    touch the limit. Worked out once per request: later calls get the same answer, or the same refusal, without
    another lookup or another counted guess."""
    if "auth_outcome" in g:
        principal, error = g.auth_outcome
        if error is not None:
            raise error
        return principal
    try:
        principal = _principal_from(PROVIDERS)
    except Exception as e:  # the 429 abort, or DatabaseUnavailable from the session lookup
        g.auth_outcome = (None, e)
        raise
    g.auth_outcome = (principal, None)
    return principal


def known_principal():
    """For error texts: the principal this request already worked out, else whoever the providers that never
    touch the database vouch for. Never raises, and never starts a database lookup."""
    try:
        if "auth_outcome" in g:
            return current_principal()
        return _principal_from([p for p in PROVIDERS if not getattr(p, "uses_database", False)])
    except Exception:
        return None


def _refuse(**body):
    abort(make_response(jsonify(**body), 403))


def _signed_out_body():
    if not oidc.configured():  # the admin token is the only way in: exactly as before single sign-on
        return {"error": "The admin token is missing or wrong.", "token_required": True}
    reason = g.get("session_ended")
    body = {"error": ENDED.get(reason, "Sign in to continue."), "token_required": True}
    if reason in ENDED:
        body["reason"] = reason
    return body


def check_admin(refuse_when_unconfigured=False, unconfigured_error=None):
    """The gate. Without any sign-in configured, grid actions stay open (as they always were), but
    access tokens are refused: their metadata is sensitive and only an admin may manage them."""
    if not admin_configured():
        if refuse_when_unconfigured:
            # admin_token_unset: the page explains the setting instead of asking anyone to sign in.
            _refuse(error=unconfigured_error or "set GRID_ADMIN_TOKEN to manage access tokens", admin_token_unset=True)
        return None
    principal = current_principal()
    if principal is None:
        # token_required tells the page to ask for sign-in, unlike other 403s (proxies, origin checks).
        _refuse(**_signed_out_body())
    return principal


def _forbidden_body(permission):
    # forbidden, unlike token_required, tells the page not to ask anyone to sign in again.
    return {"error": "Your roles don't include “{}”, which this needs. Ask an admin for a role that does."
                     .format(permissions.PERMISSIONS[permission][1]), "forbidden": True, "permission": permission}


def _forbid(permission):
    _refuse(**_forbidden_body(permission))


def permission_required(*perms, refuse_when_unconfigured=False, unconfigured_error=None):
    """The gate, with what the principal must be allowed: any one of perms (none: anyone signed in). Without any
    sign-in configured, grid actions stay open as before (the principal is None). Leaves the principal in
    g.principal and who it is for the audit log in g.actor."""
    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            g.principal = check_admin(refuse_when_unconfigured, unconfigured_error)
            g.actor = actor(g.principal)
            if g.principal is not None and perms and not any(p in g.principal.permissions for p in perms):
                _forbid(perms[0])
            return view(*args, **kwargs)
        return wrapper
    return decorate


def require(*perms):
    """Past the gate, what only the request says it needs (the kind of token it makes): refused as the gate
    refuses unless the principal may do one of perms."""
    principal = g.get("principal")
    if principal is not None and not any(p in principal.permissions for p in perms):
        _forbid(perms[0])


def admin_required(refuse_when_unconfigured=False):
    """The gate for what only an admin may do: every permission (the Admin role, the admin token, or a provider
    that says nothing about permissions). permission_required() is for anything less."""
    def decorate(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            g.principal = check_admin(refuse_when_unconfigured)
            g.actor = actor(g.principal)
            if g.principal is not None and not is_admin(g.principal):
                _refuse(error="Only an admin can do this. Ask one for the Admin role.", forbidden=True)
            return view(*args, **kwargs)
        return wrapper
    return decorate


# ---- checks -------------------------------------------------------------------------
#
# Checks ask for MFA codes and saved sign-ins with the client token they already connect to the hub
# with, sent as "Authorization: Bearer <token>": a managed client token from the Access page, or the
# hub's bootstrap GATEWAY_CLIENT_TOKEN when the UI is given its value. Wrong tokens count towards the
# same guessing limit as wrong admin tokens. A token someone made while signed in acts as them, with
# what they may do at the time of each request; one made with the admin token, before there were
# users, or the bootstrap token, acts as an admin.

# A bootstrap value shorter than this is ignored: older compose files gave the UI the word "set" in place
# of the token, and that must never work as one.
MIN_BOOTSTRAP_TOKEN_LENGTH = 16


def _bearer():
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _owner_refused(message):
    # Not a guess: the token is right, its owner just may not use it. owner_disabled tells checks so.
    abort(make_response(jsonify(error=message, owner_disabled=True), 403))


def _token_principal(lookup):
    token = _bearer()
    if not token:
        abort(make_response(jsonify(error="Send the check's client token as 'Authorization: Bearer <token>'.",
                                    client_token_required=True), 401))
    wait = retry_after()
    if wait:
        _too_many(wait)
    bootstrap = os.environ.get("GATEWAY_CLIENT_TOKEN", "")
    if len(bootstrap) >= MIN_BOOTSTRAP_TOKEN_LENGTH and hmac.compare_digest(token.encode(), bootstrap.encode()):
        return Principal("GATEWAY_CLIENT_TOKEN", "client-bootstrap")
    found = lookup(token)
    if found is None:
        _note_failure()
        abort(make_response(jsonify(error="The client token is wrong, revoked or expired.", client_token_required=True), 401))
    name = "client token '{}'".format(found["name"])
    if found["ownerId"] is None:
        return Principal(name, "client-token", scope=found["scope"])
    owner = found["owner"]
    if owner is None:
        _owner_refused("This client token belongs to a person this grid no longer knows, so it gets nothing from the "
                       "Grid UI. An admin can revoke it.")
    if owner["disabled"]:
        _owner_refused("This client token belongs to {}, whose account is disabled, so it gets nothing from the Grid "
                       "UI. An admin can enable them, or revoke the token.".format(owner["name"]))
    return principal_for_user(owner, "client-token",
                              name="{} ({})".format(name, owner["name"])[:255])._replace(scope=found["scope"])


def in_scope(principal, scope):
    """Whether a client token's scope leaves `scope` in. People, the bootstrap token and tokens made without
    a scope have none, and everything is in."""
    return principal.scope is None or scope in principal.scope


def out_of_scope(scope, error=None):
    """The 403 for a client token whose scope leaves `scope` out: not the same as its owner's roles lacking it."""
    return make_response(jsonify(
        error=error or "This client token's scope doesn't include “{}”. A token's scope can't be changed: "
                       "make another on the Access page.".format(permissions.TOKEN_SCOPES.get(scope, scope)),
        out_of_scope=True, scope=scope), 403)


def check_principal(lookup, scope, permission=None):
    """The check a request comes from, judged by its client token, or a 401/403/429 abort.

    lookup(token) -> the managed client token as {"id", "name", "ownerId", "owner", "scope"}, or None
    (accounts.client_token_access). Whether the token acts as a person goes by ownerId, never by whether
    the owner could be found: a token whose owner this grid no longer knows gets nothing, never an
    admin's access. scope is the scope id the request needs (permissions.TOKEN_SCOPES): a token whose scope
    leaves it out is refused first, with out_of_scope. permission is what the owner's roles must include as
    well (mfa.use, signins.use): a token whose owner's roles don't include it is refused as forbidden."""
    principal = _token_principal(lookup)
    if not in_scope(principal, scope):
        abort(out_of_scope(scope))
    if permission is not None and permission not in principal.permissions:
        abort(make_response(jsonify(error="This client token's owner's roles don't include “{}”.".format(
            permissions.PERMISSIONS[permission][1]), forbidden=True, permission=permission), 403))
    return principal


# ---- reading the grid -----------------------------------------------------------------
#
# The overview, activity and sessions (/status, /api/status, /api/events) are open to anyone unless the app
# says they need sign-in (GRID_UI_READS). Then whoever may "See the grid" reads them: a person, the admin
# token, or a client token sent as checks send theirs, for scripts that watch the grid. Nothing else but the
# check routes takes a client token.


def _refuse_read(soft, why, body):
    if soft:
        # The page's own status poll: the refusal as data, holding nothing of the grid, rather than an HTTP
        # error the browser logs every few seconds.
        response = make_response(jsonify(refused=why, error=body["error"]))
        response.headers["Cache-Control"] = "no-store"
        abort(response)
    _refuse(**body)


def reader_principal(lookup, soft=False):
    """Who reads the grid where reading needs sign-in, or a refusal as the gate's: token_required for nobody,
    forbidden without grid.view. A client token sent as "Authorization: Bearer <token>" is looked at only when
    nothing else vouches for the request, with check_principal's lookup, so a wrong one is a 401 (or the
    guessing limit's 429), one whose owner can't use it a 403 owner_disabled, and one whose scope leaves out
    grid.view a 403 out_of_scope. soft answers those two
    refusals with 200 {"refused": "signedout" | "forbidden", "error"} instead; the 401, 403 and 429 of a
    client token never are. Without any sign-in configured, nobody could sign in to read, so reading is as
    open as grid actions (the principal is None)."""
    if not admin_configured():
        return None
    principal = current_principal()
    if principal is None and _bearer():
        principal = check_principal(lookup, "grid.view")
    if principal is None:
        _refuse_read(soft, "signedout", _signed_out_body())
    if not can(principal, "grid.view"):
        _refuse_read(soft, "forbidden", _forbidden_body("grid.view"))
    return principal


# ---- the page's sign-in state ---------------------------------------------------------


def _describe(provider, detail):
    # Plug-in providers may have a describe() that takes nothing: they have no problem to tell.
    try:
        takes_detail = bool(inspect.signature(provider.describe).parameters)
    except (TypeError, ValueError):
        takes_detail = False
    return provider.describe(detail) if takes_detail else provider.describe()


def session_info(reads_open=True):
    """What the page needs to present sign-in state. Validates the credentials sent, if any.

    permissions is what the request may do now (on a grid with no sign-in configured, the grid actions anyone
    may take); user and roles are a signed-in person's. unavailable: the database can't be reached, so a session
    cookie couldn't be looked at (the page keeps what it knew). reads_open is app.reads_open()."""
    unavailable = False
    try:
        principal = current_principal()
    except store.DatabaseUnavailable:
        # The admin token is asked before the cookie and never touches the database, so it didn't vouch.
        principal, unavailable = None, True
    detail = principal is not None and (principal.method == AdminTokenProvider.id
                                        or "users.view" in principal.permissions)
    providers = [_describe(p, detail) for p in PROVIDERS if p.configured()]
    unavailable = unavailable or bool(g.get("db_unavailable"))  # set by accounts.tables_problem()
    found = g.get("signed_in_user")
    if principal is None or principal.user_id is None or not found or found["id"] != principal.user_id:
        found = None
    if not admin_configured():
        allowed = sorted(permissions.OPEN_GRID)
    else:
        allowed = sorted(principal.permissions) if principal else []
    return {
        "signedIn": principal is not None,
        "name": principal.name if principal else None,
        "method": principal.method if principal else None,
        "adminConfigured": admin_configured(),
        "providers": providers,
        "user": {"id": found["id"], "email": found["email"], "emailVerified": found["emailVerified"],
                 "subject": found["subject"], "issuer": found["issuer"]} if found else None,
        "permissions": allowed,
        "roles": list(found["roles"]) if found else [],
        "admin": is_admin(principal),
        "readsOpen": reads_open,
        "unavailable": unavailable,
    }
