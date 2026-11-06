"""Grid-style management console for the Playwright orchestrator.

Run locally:  python -m ui.app   (from the repo root)
"""
import collections
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import threading
import time
import urllib.parse

from flask import Flask, abort, g, jsonify, redirect, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

from . import accounts, auth, mfa, oidc, permissions, recorder, store

log = logging.getLogger(__name__)

# The Windows registry can map .js to text/plain; with nosniff the browser would then refuse the modules.
mimetypes.add_type("text/javascript", ".js")

ADMIN_TOKEN = os.environ.get("GRID_ADMIN_TOKEN", "")
# The hub address runner hosts and checks outside the compose network use (GATEWAY_URL is often
# "ws://hub:4500", a name only the containers can resolve). Unset: the page derives one.
PUBLIC_GATEWAY_URL = (os.environ.get("GATEWAY_PUBLIC_URL") or "").rstrip("/")
GRID_NAME = os.environ.get("GRID_NAME", "")
# How often a visible page polls /api/status. Each poll is one snapshot of the database, so a grid
# watched by many people on a busy MySQL server may want a slower page.
try:
    POLL_MS = max(1000, int(os.environ.get("GRID_UI_POLL_MS", "1500")))
except ValueError:
    POLL_MS = 1500
# Extra host names the UI may be managed through (besides IP addresses, localhost and this
# machine's names), comma separated; "*" allows any. Guards the actions against DNS rebinding.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("GRID_UI_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _proxy_hops():
    text = (os.environ.get("GRID_UI_PROXY_HOPS") or "0").strip()
    try:
        if int(text) >= 0:
            return int(text)
    except ValueError:
        pass
    log.warning("GRID_UI_PROXY_HOPS is how many reverse proxies are in front of the Grid UI, 0 or more, not %r: "
                "taking 0", text)
    return 0


# How many trusted reverse proxies are in front of the UI. Above 0, X-Forwarded-For is believed that many hops
# deep, for the addresses the guessing and sign-in limits count and that sessions and the audit log keep. Nothing
# else is taken from forwarded headers: single sign-on's addresses and cookie flags come from GRID_UI_PUBLIC_URL.
PROXY_HOPS = _proxy_hops()

# Where checks ask for MFA codes and saved sign-ins (with their client token rather than the admin's).
CHECK_CODE_PATH = "/api/mfa/code"
CHECK_CHECKOUT_PATH = "/api/signins/checkout"
CHECK_SAVE_PATH = "/api/signins/save"
CHECK_RELEASE_PATH = "/api/signins/release"
CHECK_PATHS = {CHECK_CODE_PATH, CHECK_CHECKOUT_PATH, CHECK_SAVE_PATH, CHECK_RELEASE_PATH}

app = Flask(__name__)
# How far any request body is read, chunked ones too (they have no Content-Length to refuse them by): one
# byte past the largest a request may send, a saved sign-in's, so a body cut off here tells from one that fits.
app.config["MAX_CONTENT_LENGTH"] = store.SIGNIN_MAX_BODY_BYTES + 1
# Read at call time, so the setting can change while the app runs (the tests rely on it). auth.py never imports
# accounts, so the session lookup and the check for the sign-in tables are handed to it.
auth.configure(lambda: ADMIN_TOKEN, session_lookup=accounts.session_access, problem=accounts.tables_problem)


class _TrustedProxies:
    """The app behind PROXY_HOPS trusted reverse proxies: request.remote_addr is the address the outermost of
    them was asked from (X-Forwarded-For), and nothing else changes. Reads PROXY_HOPS on each request, so a test
    can change it."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if PROXY_HOPS > 0:
            fixed = ProxyFix(self.wsgi_app, x_for=PROXY_HOPS, x_proto=0, x_host=0, x_port=0, x_prefix=0)
            return fixed(environ, start_response)
        return self.wsgi_app(environ, start_response)


app.wsgi_app = _TrustedProxies(app.wsgi_app)

# One per process. Every status poll feeds it the snapshot it reads anyway; a background thread
# (GRID_RECORDER_INTERVAL seconds, 0 to turn it off) keeps it going while no page is open.
RECORDER = recorder.Recorder(lambda: store.snapshot(), lambda: store.db_identity(), recorder.interval_from_env())

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                               "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                               "frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _local_names():
    names = {"localhost"}
    for lookup in (socket.gethostname, socket.getfqdn):
        try:
            names.add(lookup().lower())
        except OSError:
            pass
    return names


_LOCAL_NAMES = _local_names()


def _hostname(netloc):
    try:
        return (urllib.parse.urlsplit("//" + netloc).hostname or "").lower()
    except ValueError:
        return ""


def _named_hosts():
    """GRID_UI_ALLOWED_HOSTS, plus the host of GRID_UI_PUBLIC_URL (read now, like the rest of single sign-on's
    settings): the address people are told to open is always one to manage the grid through."""
    public = _hostname(oidc.settings().public_host)
    return ALLOWED_HOSTS | {public} if public else ALLOWED_HOSTS


def _host_allowed(name):
    named = _named_hosts()
    if "*" in named or name in named or name in _LOCAL_NAMES:
        return True
    try:
        ipaddress.ip_address(name)  # an address can't be rebound to another server
        return True
    except ValueError:
        return False


def _refuse(message):
    return jsonify(error=message), 403


@app.before_request
def _same_origin_actions_only():
    """Management actions must come from this page, even when no admin token is set.

    Stops cross-site form posts (which need no CORS preflight) and DNS rebinding."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.path in CHECK_PATHS:
        # Checks, not browsers: these routes take nothing but a client token in the Authorization header,
        # which a page on another site can't send without a CORS preflight (never approved here). Checks
        # reach the UI by its compose name (grid_ui), which the host rule below would refuse.
        return None
    host = _hostname(request.host)
    if not _host_allowed(host):
        return _refuse("Actions aren't accepted through the host name {!r}. "
                       "Add it to GRID_UI_ALLOWED_HOSTS to manage the grid through it.".format(host))
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        return _refuse("Cross-site request refused.")
    origin = request.headers.get("Origin")
    if origin is not None:
        # Behind a proxy that rewrites Host, list the public name in GRID_UI_ALLOWED_HOSTS (or GRID_UI_PUBLIC_URL).
        origin_netloc = urllib.parse.urlsplit(origin).netloc.lower()
        if origin_netloc != request.host.lower() and _hostname(origin_netloc) not in _named_hosts():
            return _refuse("Cross-origin request refused.")
    # A custom header or a JSON body can't be sent cross-site without a CORS preflight.
    if "X-Grid-Token" not in request.headers and not request.is_json:
        return _refuse("Send actions from the grid page (X-Grid-Token header or a JSON body required).")
    return None


def _private(response, status=200):
    """A token response: never cached (one of them holds a plaintext token)."""
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    return response


def _host_arg():
    host = (request.get_json(silent=True) or {}).get("host")
    if not isinstance(host, str) or not host.strip():
        abort(400)
    return host


@app.after_request
def _security_headers(response):
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _db_error_text(error):
    """The full error (it may name a file path or a server) only for a signed-in admin; the log
    always has it. Whoever the request already turned out to be, or the admin token says: the
    session lookup may be what failed, and it isn't tried again."""
    _log_db_error(error)
    admin = auth.is_admin(auth.known_principal())
    return str(error) if admin else getattr(error, "public", "The database can't be reached.")


_last_db_error = [None]


def _log_db_error(error):
    if str(error) != _last_db_error[0]:  # once per distinct error, not once per poll
        _last_db_error[0] = str(error)
        log.warning("database unavailable: %s", error)


def _db_error_response(error, status):
    # ?soft=1 (the page's own status polls): the outage as data, not an HTTP error the browser logs every
    # few seconds. Without it, and on every other route, the 503 (or 403) it has always been.
    if request.endpoint == "status" and request.args.get("soft") == "1":
        return _private(jsonify(error=_db_error_text(error), unavailable=True))
    return jsonify(error=_db_error_text(error)), status


@app.errorhandler(store.DatabaseUnavailable)
def database_unavailable(error):
    return _db_error_response(error, 503)


@app.errorhandler(store.TokensUnavailable)
def tokens_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(store.DatabasePermissionDenied)
def database_permission_denied(error):
    return _db_error_response(error, 403)


@app.errorhandler(store.MfaUnavailable)
def mfa_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(store.SigninsUnavailable)
def signins_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(mfa.SecretsKeyMissing)
def secrets_key_missing(error):
    # secrets_key_unset: the page explains the setting instead of offering a form that can't work.
    return jsonify(error=str(error), secrets_key_unset=True), 503


@app.errorhandler(mfa.CannotDecrypt)
def cannot_decrypt(error):
    return jsonify(error=str(error), cannot_decrypt=True), 409


@app.errorhandler(store.NotShared)
def not_shared(error):
    # A check asked by name for an MFA secret or a sign-in someone else has: names are shared by everyone on the
    # grid, so saying so tells nothing that "the name is taken" doesn't, and the check's author learns why.
    return _private(jsonify(error=str(error), not_shared=True), 403)


@app.errorhandler(store.NotPermitted)
def not_permitted(error):
    # Seen, but not theirs to change. A refusal by one of the rules that stop people giving themselves or others
    # more than they have is recorded, in a transaction of its own: the change it refused rolled back.
    if error.rule:
        _audit_denied(error)
    return _private(jsonify(error=str(error), forbidden=True), 403)


@app.errorhandler(accounts.InvalidRequest)
def invalid_request(error):
    # A built-in role, a reserved name or a role that manages people for everyone is refused by a rule too
    # (InvalidRole.rule), and recorded like a NotPermitted one.
    if getattr(error, "rule", None):
        _audit_denied(error)
    return _private(jsonify(error=str(error)), 400)


@app.errorhandler(accounts.AccessBusy)
def access_busy(error):
    return _private(jsonify(error=str(error)), 409)


@app.errorhandler(accounts.NotTransferable)
def not_transferable(error):
    return _private(jsonify(error=str(error)), 409)


@app.errorhandler(accounts.UsersUnavailable)
def users_unavailable(error):
    return _private(jsonify(error=str(error)), 503)


# ---- audit rows written alone ---------------------------------------------------------
#
# A few audit rows record a read or a refusal rather than a change, so they are written in a transaction of their
# own (store.audit_alone), and at most once per key within a window, in memory and per process: the page asks
# for a code it shows again every time window, and a refused request can come as fast as someone clicks.
# Duplicates across the gunicorn workers are fine.

AUDIT_WINDOWS_S = {"auth.breakglass": 3600, "mfa.code_viewed": 300, "access.denied": 60}
_AUDIT_KEYS_KEPT = 10000  # past this many, the key written longest ago is forgotten
_audited = collections.OrderedDict()  # (action, key) -> when its row was last written
_audited_lock = threading.Lock()


def _audit_due(action, key):
    """Whether the row of this action for this key is due (and if so, it counts as written now)."""
    now = time.monotonic()
    with _audited_lock:
        last = _audited.get((action, key))
        if last is not None and now - last < AUDIT_WINDOWS_S[action]:
            return False
        _audited[(action, key)] = now
        _audited.move_to_end((action, key))
        while len(_audited) > _AUDIT_KEYS_KEPT:
            _audited.popitem(last=False)
        return True


def reset_audit_throttles():
    """Forget which throttled audit rows were written."""
    with _audited_lock:
        _audited.clear()


def _actor_key(actor):
    # A person by their id; the admin token and client tokens without an owner by name and address.
    return actor.user_id if actor.user_id is not None else (actor.name, actor.address)


def _audit_denied(error):
    """access.denied for a refusal by an anti-escalation rule: the route and rule, and what the actor lacked."""
    actor = g.get("actor")
    route = request.url_rule.rule if request.url_rule else request.path
    if actor is None or not _audit_due("access.denied", (_actor_key(actor), request.method, route, error.rule)):
        return
    details = {"route": "{} {}".format(request.method, request.path), "rule": error.rule}
    if error.permission:
        details["permission"] = error.permission
    store.audit_alone(actor, "access.denied", details=details)


@app.get("/")
@app.get("/ui")
def index():
    return render_template("index.html")


@app.get("/status")
@app.get("/api/status")
def status():
    # An outage, here (the session lookup) or in the snapshot, goes to _db_error_response, which answers
    # ?soft=1 with data.
    names = _reader()
    RECORDER.ensure_running()
    data, _ = RECORDER.observe()
    data = dict(data)  # the recorder keeps the snapshot it was given
    data["grid_name"] = GRID_NAME or None
    data["public_gateway_url"] = PUBLIC_GATEWAY_URL or None
    data["poll_ms"] = POLL_MS
    # ?events=<cursor>: also what the recorder saw since then (0: all of the last 15 minutes).
    if request.args.get("events") is not None:
        data.update(_exported(request.args.get("events"), names))
    return jsonify(data)


@app.get("/api/events")
def events():
    """The recorder's events since ?since=<epoch ms> (default: all it holds), for scripts and tools. Where reading
    the grid needs sign-in, a script sends a client token as checks do."""
    names = _reader()
    RECORDER.ensure_running()
    try:
        RECORDER.observe()
    except store.DatabaseUnavailable:
        pass  # what was recorded before the outage is still worth having
    return _private(jsonify(_exported(request.args.get("since", "0"), names)))


def _observe_before_action():
    # A fresh "before" snapshot, so the one after the action differs from it only by the action
    # (and whatever the checks did in the same instant).
    try:
        RECORDER.observe()
    except Exception as e:
        log.debug("recorder: no snapshot before the action: %s", e)


def _attribute(match):
    """Record the action's effect and mark it as taken by whoever signed in (the name the page shows them by:
    "Admin" for the admin token, a person's own with single sign-on). Returns the event ids."""
    principal = getattr(g, "principal", None)
    try:
        _, ids = RECORDER.observe(attribute=(match, principal.name if principal else ""))
    except Exception as e:
        log.debug("recorder: no snapshot after the action: %s", e)
        return []
    return ids


def reads_open():
    """Whether the overview, activity and sessions are open to anyone (GRID_UI_READS=open) or need sign-in
    (signed-in). Unset, they need it with single sign-on and are open without. Read now, so it can change while
    the app runs; any other value counts as signed-in. Without any sign-in configured they are open whatever it
    says: nobody could sign in to read them, and grid actions are open then too."""
    if not auth.admin_configured():
        return True
    value = os.environ.get("GRID_UI_READS", "").strip().lower()
    if not value:
        return not oidc.configured()
    if value not in ("open", "signed-in"):
        store.warn_once("GRID_UI_READS is open or signed-in, not {!r}: reads need sign-in meanwhile.".format(value))
    return value == "open"


def _reader():
    """Let the request read the grid, or refuse it (auth.reader_principal) where reading needs sign-in. Returns
    whether the events exported to it keep who took each action: with single sign-on those are people's names,
    which nobody gets without signing in, even where reading is open. Asking who it is also keeps a signed-in
    person's session from going idle while their page polls."""
    if not reads_open():
        # ?soft=1, the page's status poll: a refusal comes back as data, like an outage (_db_error_response).
        auth.reader_principal(accounts.client_token_access,
                              soft=request.endpoint == "status" and request.args.get("soft") == "1")
        return True
    if not oidc.configured():
        return True  # "Admin", or "" where nobody signs in: never a person's name
    try:
        return auth.current_principal() is not None
    except Exception:  # too many wrong admin tokens, or an outage in the session lookup: read anonymously
        return False


def _exported(cursor, names):
    """RECORDER.export(cursor), with who took each action blanked unless names."""
    found = RECORDER.export(cursor)
    if not names:
        for event in found["events"]:
            if "by" in event:
                event["by"] = ""
    return found


@app.get("/api/session")
def session():
    """Sign-in state for the page: who is signed in (judged from the credentials sent, if any),
    whether admin sign-in is configured at all, which providers to offer, and what the request may do.
    Someone signing in with the admin token (break-glass) is recorded in the audit log."""
    info = auth.session_info(reads_open())
    principal = auth.known_principal()
    if (principal is not None and principal.method == auth.AdminTokenProvider.id
            and _audit_due("auth.breakglass", request.remote_addr)):
        store.audit_alone(auth.actor(principal), "auth.breakglass")
    return _private(jsonify(info))


@app.post("/api/slots/<int:slot_id>/release")
@auth.permission_required("grid.operate")
def release_slot(slot_id):
    body = request.get_json(silent=True)
    claimed_at = body.get("claimed_at") if isinstance(body, dict) else None
    _observe_before_action()
    if claimed_at is None:
        updated = store.release_slot(slot_id, g.actor)
    else:
        # The page sends the claim its confirm described; anything else is a different check's.
        result, updated = store.release_claim(slot_id, str(claimed_at), g.actor)
        if result == "not_found":
            return jsonify(error="There is no slot {}.".format(slot_id)), 404
        if result == "free":
            return jsonify(error="That check already finished: the slot is free, so nothing was released.",
                           released=False), 409
        if result == "reclaimed":
            return jsonify(error="Another check claimed this slot since you looked, so nothing was released.",
                           reclaimed=True, released=False), 409
    ids = _attribute(lambda e: e.get("slot") == slot_id and e["type"] == "finished")
    return jsonify(updated=updated, events=ids)


@app.post("/api/slots/<int:slot_id>/reset-failures")
@auth.permission_required("grid.operate")
def reset_slot(slot_id):
    _observe_before_action()
    updated = store.reset_slot_failures(slot_id, g.actor)
    ids = _attribute(lambda e: e.get("slot") == slot_id and e["type"] == "reset")
    return jsonify(updated=updated, events=ids)


@app.post("/api/nodes/reset-failures")
@auth.permission_required("grid.operate")
def reset_node():
    host = _host_arg()
    _observe_before_action()
    updated = store.reset_node_failures(host, g.actor)
    ids = _attribute(lambda e: e.get("node") == host and e["type"] == "reset")
    return jsonify(updated=updated, events=ids)


@app.post("/api/nodes/forget")
@auth.permission_required("grid.operate")
def forget_node():
    host = _host_arg()
    _observe_before_action()
    result, count = store.forget_node(host, g.actor)
    if result == "forgotten":
        ids = _attribute(lambda e: e.get("node") == host and e["type"] == "node-left")
        return jsonify(forgotten=True, host=host, slots=count, events=ids)
    if result == "not_found":
        return jsonify(error="{} is not a registered gateway node.".format(host)), 404
    if result == "online":
        return jsonify(error="{} is online. Stop its agent first.".format(host)), 409
    return jsonify(error="{} has {} slot(s) in use. Release them first.".format(host, count)), 409


@app.errorhandler(store.InvalidToken)
def invalid_token(error):
    return jsonify(error=str(error)), 400


# Who may see a token: everyone's with tokens.all, else their own with tokens.own and every agent token with
# tokens.agents (store._token_rights). Everyone the gate lets in gets the counts of every kind, though.
TOKEN_PERMISSIONS = ("tokens.own", "tokens.agents", "tokens.all")
# What making each kind of token needs.
TOKEN_KIND_PERMISSIONS = {"client": "tokens.own", "agent": "tokens.agents"}


@app.get("/api/tokens")
@auth.permission_required(*TOKEN_PERMISSIONS, refuse_when_unconfigured=True)
def list_tokens():
    result = store.list_tokens(auth.access(g.principal))
    accounts.annotate("token", result["tokens"])
    # Whether the env bootstrap tokens are set, if the UI gets the same variables as the hub.
    # Informational only; their values never leave the server.
    result["bootstrap"] = {"agent": bool(os.environ.get("GATEWAY_AGENT_TOKEN")),
                           "client": bool(os.environ.get("GATEWAY_CLIENT_TOKEN"))}
    return _private(jsonify(result))


@app.post("/api/tokens")
@auth.permission_required("tokens.own", "tokens.agents", refuse_when_unconfigured=True)
def create_token():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send the token's settings as a JSON object."), 400
    kind = body.get("kind")
    if isinstance(kind, str) and kind in TOKEN_KIND_PERMISSIONS:
        auth.require(TOKEN_KIND_PERMISSIONS[kind])
    token = store.create_token(body.get("name"), kind, body.get("nodeName"), body.get("expiresInDays"),
                               owner_id=g.principal.user_id, access=auth.access(g.principal), actor=g.actor)
    return _private(jsonify(accounts.annotate("token", [token])[0]), 201)


@app.post("/api/tokens/<int:token_id>/revoke")
@auth.permission_required(*TOKEN_PERMISSIONS, refuse_when_unconfigured=True)
def revoke_token(token_id):
    token = store.revoke_token(token_id, auth.access(g.principal), g.actor)
    if token is None:
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    return _private(jsonify(revoked=True, token=accounts.annotate("token", [token])[0]))


@app.post("/api/tokens/<int:token_id>/delete")
@auth.permission_required(*TOKEN_PERMISSIONS, refuse_when_unconfigured=True)
def delete_token(token_id):
    result = store.delete_token(token_id, auth.access(g.principal), g.actor)
    if result == "not_found":
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    if result == "active":
        return jsonify(error="This token is still active. Revoke it before deleting it."), 409
    return _private(jsonify(deleted=True, id=token_id))


# ---- signing in -----------------------------------------------------------------------
#
# Single sign-on: oidc.py speaks to the identity provider, accounts.py keeps the users and sessions. /auth/login
# sends the browser to the provider with a pending sign-in kept in the database, so whichever worker the callback
# reaches can finish it, and bound to the browser by the login cookie. /auth/callback redeems the code, checks the
# ID token and sets the session cookie. Both end in a redirect back to the page, relative so that it works behind
# a path prefix, with ?signin=<why> when signing in didn't work. Nothing the identity provider says is ever put in
# an answer.

# A sign-in may start at most LOGIN_START_LIMIT times per address within LOGIN_START_WINDOW_S, in memory and per
# process, so cookieless starts can't fill oidcLogins up to accounts.MAX_PENDING_LOGINS and keep everyone else
# from signing in. Generous, because an office behind one address (or a proxy without GRID_UI_PROXY_HOPS) shares it.
# An IPv6 address counts as its /64: one host commonly holds a whole /64, and could start from a new address each
# time.
LOGIN_START_LIMIT = 60
LOGIN_START_WINDOW_S = 600
_START_ADDRESSES_KEPT = 10000  # past this many, the address seen longest ago is forgotten
# Where the page goes back to after signing in (?return=): one of its own hashes, never a URL.
_RETURN_HASH = re.compile(r"#/[A-Za-z0-9/_\-.~?=&%:,+]{0,500}")
DEFAULT_RETURN = "#/overview"
# The identity provider's error codes that mean the person didn't agree, or may not sign in: denied. Any other
# is failed.
_DENIED = ("access_denied", "login_required", "interaction_required", "consent_required")
MAX_CODE_LENGTH = 2048
_MOVED_HOSTS_LOGGED = 100
_login_starts = collections.OrderedDict()  # start key -> its starts' times; the key used longest ago first
_login_starts_lock = threading.Lock()
_moved_hosts = set()  # hosts sign-in was still started at after being sent to GRID_UI_PUBLIC_URL's


def _start_key(addr):
    """What an address's sign-in starts count towards: an IPv4 address (an IPv4-mapped IPv6 one too) as it is, an
    IPv6 address as its /64."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network((ip, 64), strict=False))


def _start_allowed(addr):
    """Whether this address may start another sign-in now; counts the start when it may. Each call costs the same
    however many addresses are counted: past _START_ADDRESSES_KEPT, the one used longest ago is forgotten, which
    at worst lets it start a few more."""
    now = time.monotonic()
    key = _start_key(addr)
    with _login_starts_lock:
        q = _login_starts.setdefault(key, collections.deque())
        _login_starts.move_to_end(key)
        while len(_login_starts) > _START_ADDRESSES_KEPT:
            _login_starts.popitem(last=False)
        while q and now - q[0] > LOGIN_START_WINDOW_S:
            q.popleft()
        if len(q) >= LOGIN_START_LIMIT:
            return False
        q.append(now)
        return True


def reset_login_starts():
    """Forget the sign-in starts counted, and the hosts logged for starting sign-in at the wrong address."""
    with _login_starts_lock:
        _login_starts.clear()
        _moved_hosts.clear()


def _note_moved(host, public_url):
    # A proxy that rewrites Host sends the browser here again after the redirect to the public address: go on
    # (rather than loop), and say once per host how to fix it.
    with _login_starts_lock:
        if host in _moved_hosts or len(_moved_hosts) >= _MOVED_HOSTS_LOGGED:
            return
        _moved_hosts.add(host)
    log.warning("sign-in was started at %s, but GRID_UI_PUBLIC_URL is %s; have the proxy pass the Host header on",
                host, public_url)


def _return_hash(value):
    return value if isinstance(value, str) and _RETURN_HASH.fullmatch(value) else DEFAULT_RETURN


def _back_to_page(return_to, failure=None):
    """The 303 back to the page, at return_to, with ?signin=<failure> when signing in didn't work. Relative to
    /auth/, so it reaches the page wherever the UI is served."""
    return redirect("../" + ("?signin=" + failure if failure else "") + return_to, 303)


# The cookies are HttpOnly, Secure whenever GRID_UI_PUBLIC_URL is https, and __Host- when it is https with no path
# (oidc.cookie_names()). The login cookie is SameSite=Lax, since a Strict one isn't sent on the identity provider's
# redirect back; the session cookie is Strict, since only the page's own requests need it.

def _set_login_cookie(response, value):
    names = oidc.cookie_names()
    response.set_cookie(names.login, value, max_age=accounts.LOGIN_TTL_S, path=names.path, secure=names.secure,
                        httponly=True, samesite="Lax")


def _expire_login_cookie(response):
    names = oidc.cookie_names()
    response.delete_cookie(names.login, path=names.path, secure=names.secure, httponly=True, samesite="Lax")


def _set_session_cookie(response, value):
    names = oidc.cookie_names()
    response.set_cookie(names.session, value, max_age=oidc.settings().session_hours * 3600, path=names.path,
                        secure=names.secure, httponly=True, samesite="Strict")


def _expire_session_cookie(response):
    names = oidc.cookie_names()
    response.delete_cookie(names.session, path=names.path, secure=names.secure, httponly=True, samesite="Strict")


@app.after_request
def _drop_dead_session_cookie(response):
    # A session cookie that no longer works (expired, idle, revoked, disabled or unknown), or one just signed out:
    # the browser forgets it, rather than send it with every request.
    if g.get("session_ended"):
        _expire_session_cookie(response)
    return response


@app.after_request
def _say_the_admin_token_was_wrong(response):
    # A wrong admin token doesn't stop a request a session cookie lets through, or a read that's open to anyone, but
    # each one counts towards the guessing limit. A tab still holding the token from before GRID_ADMIN_TOKEN changed
    # would lock its address out within a minute of polls: the page is told, and forgets it.
    if g.get("secret_refused"):
        response.headers["X-Grid-Token-Refused"] = "1"
    return response


@app.after_request
def _accounts_never_cached(response):
    # Sign-in redirects, sessions and people; refusals and errors too.
    if request.path.startswith(("/auth/", "/api/admin", "/api/directory", "/api/session")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/login")
def start_sign_in():
    """Sign in with single sign-on: off to the identity provider, then back to the page at ?return=<its hash>."""
    if not oidc.configured():
        abort(404)
    return_to = _return_hash(request.args.get("return"))
    if auth.sign_in_problem():
        return _back_to_page(return_to, "misconfigured")
    s = oidc.settings()
    host = oidc.normal_host(request.host)
    if host != s.public_host:
        if request.args.get("moved") != "1":
            # The login cookie would be this host's and the callback the public one's, so the sign-in would end
            # expired. The scheme isn't compared: behind a proxy that ends TLS, every request looks like http.
            return redirect(s.public_url + "auth/login?" + urllib.parse.urlencode({"return": return_to, "moved": "1"}),
                            303)
        _note_moved(host, s.public_url)
    if not _start_allowed(request.remote_addr or "?"):
        return _back_to_page(return_to, "busy")
    try:
        oidc.discovery()
    except (oidc.Unavailable, oidc.HttpRefused) as e:
        log.warning("couldn't start a sign-in: %s", e)
        return _back_to_page(return_to, "unavailable")
    except oidc.Misconfigured as e:
        log.warning("couldn't start a sign-in: %s", e)
        return _back_to_page(return_to, "misconfigured")
    login = oidc.new_login(request.cookies.get(oidc.cookie_names().login))
    try:
        accounts.start_login(store.token_hash(login.browser), store.token_hash(login.state), login.nonce,
                             login.verifier, return_to)
    except accounts.LoginsBusy:
        log.warning("%d sign-ins are on their way through the identity provider already: refused another",
                    accounts.MAX_PENDING_LOGINS)
        return _back_to_page(return_to, "busy")
    except accounts.UsersUnavailable:
        return _back_to_page(return_to, "misconfigured")
    except store.DatabaseUnavailable as e:
        _log_db_error(e)
        return _back_to_page(return_to, "unavailable")
    response = redirect(oidc.authorize_url(login), 303)
    _set_login_cookie(response, login.browser)
    return response


def _refused_sign_in(reason, subject=None):
    # Written alone: no change commits with it.
    details = {"reason": reason}
    if subject:
        details["subject"] = subject
    store.audit_alone(store.Actor(None, subject or "?", "oidc", request.remote_addr, frozenset()),
                      "auth.signin_refused", details=details)


def _redeem(login):
    """The ID token for the code the identity provider sent back, or (None, why not)."""
    try:
        return oidc.redeem(request.args["code"], login["verifier"]), None
    except oidc.HttpRefused as e:
        # Only the status and the OAuth error code: nothing else of the answer is kept.
        log.warning("the identity provider's token endpoint refused a sign-in's code: HTTP %d %s", e.status,
                    e.error or "")
        if e.status >= 500:
            return None, "unavailable"
        if e.status in (400, 401) and e.error in ("invalid_client", "unauthorized_client"):
            return None, "misconfigured"
        return None, "failed"
    except oidc.Unavailable as e:
        log.warning("couldn't redeem a sign-in's code: %s", e)
        return None, "unavailable"
    except oidc.Misconfigured as e:
        log.warning("couldn't redeem a sign-in's code: %s", e)
        return None, "misconfigured"
    except oidc.SignInFailed as e:
        log.warning("refused a sign-in: %s", e)
        return None, "failed"


def _session_for(login):
    """The session cookie's value for whoever the identity provider signed in, or (None, why not): one of the
    ?signin= codes."""
    s = oidc.settings()
    error = request.args.get("error")
    if error is not None:
        log.info("the identity provider signed nobody in: %r %s", error[:64],
                 repr(request.args.get("error_description", ""))[:200])
        return None, "denied" if error in _DENIED else "failed"
    if request.args.get("iss", s.issuer) != s.issuer:
        log.warning("a sign-in came back from another issuer than GRID_OIDC_ISSUER")
        return None, "failed"
    if not 0 < len(request.args.get("code", "")) <= MAX_CODE_LENGTH:
        log.warning("a sign-in came back without a usable code")
        return None, "failed"
    signed, failure = _redeem(login)
    if failure:
        return None, failure
    try:
        claims = oidc.verify_id_token(signed, login["nonce"])
    except oidc.SignInFailed as e:
        log.warning("refused a sign-in's ID token (%s): %s", e.reason, e)
        _refused_sign_in(e.reason, e.subject)
        return None, "failed"
    except (oidc.Unavailable, oidc.HttpRefused) as e:
        log.warning("couldn't check a sign-in's ID token: %s", e)
        return None, "unavailable"
    except oidc.Misconfigured as e:
        log.warning("couldn't check a sign-in's ID token: %s", e)
        return None, "misconfigured"
    try:
        value, person = accounts.sign_in(s.issuer, oidc.identity(claims), request.remote_addr,
                                         request.headers.get("User-Agent"))
    except accounts.UserDisabled:
        log.info("refused a sign-in: the user is disabled on this grid")  # recorded in the audit log
        return None, "disabled"
    except accounts.UsersUnavailable:
        log.warning("couldn't sign someone in: %s", accounts.NO_USER_TABLES)
        return None, "unavailable"
    except store.DatabaseUnavailable as e:
        _log_db_error(e)
        return None, "unavailable"
    log.info("%s signed in with single sign-on (subject %s)", person["name"], person["subject"])
    return value, None


@app.get("/auth/callback")
def finish_sign_in():
    """Where the identity provider sends the browser back to: finish the sign-in this browser started, set the
    session cookie, and go back to the page."""
    if not oidc.configured():
        abort(404)
    browser = request.cookies.get(oidc.cookie_names().login, "")
    state = request.args.get("state", "")
    if not browser or not state:
        return _back_to_page(DEFAULT_RETURN, "expired")
    try:
        login = accounts.take_login(store.token_hash(state), store.token_hash(browser))
    except accounts.UsersUnavailable:
        return _back_to_page(DEFAULT_RETURN, "misconfigured")
    except store.DatabaseUnavailable as e:
        _log_db_error(e)
        return _back_to_page(DEFAULT_RETURN, "unavailable")
    if login is None:  # none, used up, too old, or another browser's
        return _back_to_page(DEFAULT_RETURN, "expired")
    value, failure = _session_for(login)
    response = _back_to_page(_return_hash(login["return_to"]), failure)
    if value is not None:
        _set_session_cookie(response, value)
    if not login["others"]:  # other tabs of this browser may still be signing in with the login cookie
        _expire_login_cookie(response)
    return response


@app.post("/api/session/signout")
def sign_out():
    """Sign this browser out ({"everywhere": true}: every session of its user's), and drop its session cookie.
    endSessionUrl, with GRID_OIDC_END_SESSION=1, is where to sign out of the identity provider too."""
    body = request.get_json(silent=True)
    everywhere = isinstance(body, dict) and body.get("everywhere") is True
    g.session_ended = "signed out"  # _drop_dead_session_cookie expires it whatever happens next, an outage too
    accounts.end_session(request.cookies.get(oidc.cookie_names().session, ""), everywhere, request.remote_addr)
    answer = {"signedOut": True}
    url = oidc.end_session_url() if oidc.configured() else None
    if url:
        answer["endSessionUrl"] = url
    return _private(jsonify(answer))


# ---- people and roles -----------------------------------------------------------------
#
# What people may do comes from their roles (permissions.py); accounts.py works out who has which.

# What the routes about people answer where no sign-in is configured (with admin_token_unset).
PEOPLE_UNSET = "Set GRID_OIDC_ISSUER (single sign-on) or GRID_ADMIN_TOKEN to manage people."


@app.get("/api/permissions")
def permission_catalogue():
    """Every permission, in the role editor's order, and what each built-in role holds (with what that implies),
    so the page can say what a role brings and what a refusal was missing. Open: it says nothing about anyone."""
    built_in = [{"id": role_id, "name": name, "description": description,
                 "permissions": sorted(permissions.expand(perms))}
                for role_id, (name, description, perms) in permissions.BUILTIN_ROLES.items()]
    return _private(jsonify(permissions=permissions.catalogue(), builtInRoles=built_in))


@app.errorhandler(accounts.LastAdmin)
def last_admin(error):
    return _private(jsonify(error=str(error), last_admin=True), 409)


@app.errorhandler(accounts.HeldElsewhere)
def held_elsewhere(error):
    return _private(jsonify(error=str(error)), 409)


def _people_gate(*perms):
    """The gate of the Admin page's routes: any one of perms, and refused where no sign-in is configured."""
    return auth.permission_required(*perms, refuse_when_unconfigured=True, unconfigured_error=PEOPLE_UNSET)


_WHOLE_NUMBER = re.compile(r"-?[0-9]{1,12}")


def _arg_int(name, default, low, high):
    """?name= as a whole number from low to high, default when it isn't sent; 400 otherwise."""
    text = request.args.get(name, "")
    if text == "":
        return default
    value = int(text) if _WHOLE_NUMBER.fullmatch(text) else None
    if value is None or not low <= value <= high:
        raise accounts.InvalidRequest("{} is a whole number from {} to {}.".format(name, low, high))
    return value


def _arg_choice(name, choices):
    value = request.args.get(name) or choices[0]
    if value not in choices:
        raise accounts.InvalidRequest("{} is {} or {}.".format(name, ", ".join(choices[:-1]), choices[-1]))
    return value


def _body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise accounts.InvalidRequest("Send a JSON object.")
    return body


def _flag(body, name):
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise accounts.InvalidRequest("{} is true or false.".format(name))
    return value


def _number(body, name, nullable=False):
    """body[name] as a whole number (null too, with nullable); 400 when it isn't one, or isn't there."""
    value = body.get(name)
    if (value is None and nullable and name in body) or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    raise accounts.InvalidRequest('Send {{"{}": <{}>}}.'.format(name, "id, or null" if nullable else "id"))


def _no_user(user_id):
    return _private(jsonify(error="There is no user {}.".format(user_id)), 404)


def _no_role(role_id):
    return _private(jsonify(error="There is no role {}.".format(role_id)), 404)


def _user_answer(user_id, answer):
    return _no_user(user_id) if answer is None else _private(jsonify(answer))


@app.get("/api/admin/users")
@_people_gate("users.view")
def admin_users():
    """The people who signed in: ?q= (part of a name, email or subject), ?status=active|disabled|all,
    ?role=<id>|none, ?sort=recent|name, ?offset= and ?limit= (1 to 200). problem says why nobody can sign in
    with single sign-on, in full: whoever gets this far may see the settings."""
    role = request.args.get("role") or None
    if role not in (None, "none"):
        role = _arg_int("role", None, -len(permissions.BUILTIN_ROLES), store._MAX_TOKEN_ID)
    result = accounts.list_users(g.actor, request.args.get("q", "")[:255],
                                 _arg_choice("status", accounts.USER_STATUSES), role,
                                 _arg_choice("sort", accounts.USER_SORTS),
                                 _arg_int("offset", 0, 0, store._MAX_TOKEN_ID),
                                 _arg_int("limit", 50, 1, accounts.USERS_LIMIT))
    result["problem"] = auth.sign_in_problem()
    result["ssoConfigured"] = oidc.configured()
    return _private(jsonify(result))


@app.get("/api/admin/users/<int:user_id>")
@_people_gate("users.view")
def admin_user(user_id):
    """One person: what they may do and why, their sessions, what they own, and with audit.view what they did and
    what was done to them."""
    session_id = (g.get("signed_in_user") or {}).get("session_id")
    return _user_answer(user_id, accounts.user_detail(user_id, g.actor, auth.can(g.principal, "audit.view"),
                                                      session_id))


@app.post("/api/admin/users/<int:user_id>/roles/add")
@_people_gate("users.manage")
def admin_give_role(user_id):
    """Give {"role": <id>}: a built-in role's negative id, or a custom role's."""
    return _user_answer(user_id, accounts.give_role(user_id, _number(_body(), "role"), g.actor))


@app.post("/api/admin/users/<int:user_id>/roles/remove")
@_people_gate("users.manage")
def admin_take_role(user_id):
    """Take {"role": <id>} given on this page. activeClientTokens counts their client tokens that still work when
    they may no longer make them, which the page offers to revoke."""
    return _user_answer(user_id, accounts.take_role(user_id, _number(_body(), "role"), g.actor))


@app.post("/api/admin/users/<int:user_id>/disable")
@_people_gate("users.manage")
def admin_disable_user(user_id):
    """Disable someone; {"revokeClientTokens": true, "revokeAgentTokens": true} revokes their tokens too."""
    body = _body()
    return _user_answer(user_id, accounts.disable_user(user_id, g.actor, _flag(body, "revokeClientTokens"),
                                                       _flag(body, "revokeAgentTokens")))


@app.post("/api/admin/users/<int:user_id>/enable")
@_people_gate("users.manage")
def admin_enable_user(user_id):
    return _user_answer(user_id, accounts.enable_user(user_id, g.actor))


@app.post("/api/admin/users/<int:user_id>/sessions/revoke")
@_people_gate("users.manage")
def admin_end_sessions(user_id):
    """End {"sessionId": <id>}, or without it every session of theirs."""
    body = _body()
    session_id = _number(body, "sessionId") if "sessionId" in body else None
    if session_id is not None and not 1 <= session_id <= store._MAX_TOKEN_ID:
        session_id = 0  # no session's
    ended = accounts.end_user_sessions(user_id, g.actor, session_id)
    return _user_answer(user_id, None if ended is None else {"ended": ended})


@app.post("/api/admin/users/<int:user_id>/tokens/revoke")
@_people_gate("users.manage")
def admin_revoke_tokens(user_id):
    """Revoke their working tokens: {"client": true} the client tokens, {"agent": true} the agent tokens."""
    body = _body()
    revoked = accounts.revoke_user_tokens(user_id, g.actor, _flag(body, "client"), _flag(body, "agent"))
    return _user_answer(user_id, None if revoked is None else {"revoked": revoked})


@app.post("/api/admin/users/<int:user_id>/move-items")
@_people_gate("users.manage")
def admin_move_items(user_id):
    """Give everything they own of MFA secrets and saved sign-ins to {"toUserId": <id>}, or to the admins
    ({"toUserId": null}). Whoever does it may see every one of both."""
    auth.require("mfa.all")
    auth.require("signins.all")
    return _user_answer(user_id, accounts.move_items(user_id, _number(_body(), "toUserId", nullable=True), g.actor))


@app.get("/api/admin/roles")
@_people_gate("users.view")
def admin_roles():
    return _private(jsonify(accounts.list_roles(g.actor)))


def _role_fields(body):
    # Whatever the request leaves out is left as it is (create_role checks what a new role needs).
    return {key: body[key] for key in ("name", "description", "permissions") if key in body}


@app.post("/api/admin/roles")
@_people_gate("users.manage")
def admin_create_role():
    """A custom role: {"name", "description"?, "permissions": [permission ids]}."""
    body = _body()
    role = accounts.create_role(body.get("name"), body.get("description"), body.get("permissions"), g.actor)
    return _private(jsonify(role=role), 201)


@app.post("/api/admin/roles/<int(signed=True):role_id>")
@_people_gate("users.manage")
def admin_update_role(role_id):
    """Change a custom role: any of {"name", "description", "permissions"}."""
    role = accounts.update_role(role_id, _role_fields(_body()), g.actor)
    return _no_role(role_id) if role is None else _private(jsonify(role=role))


@app.post("/api/admin/roles/<int(signed=True):role_id>/delete")
@_people_gate("users.manage")
def admin_delete_role(role_id):
    removed = accounts.delete_role(role_id, g.actor)
    return _no_role(role_id) if removed is None else _private(jsonify(deleted=True, id=role_id, removed=removed))


@app.get("/api/admin/groups")
@_people_gate("users.view")
def admin_groups():
    return _private(jsonify(accounts.list_mappings()))


@app.post("/api/admin/groups")
@_people_gate("users.manage")
def admin_map_group():
    """Map {"group": <a value of the groups claim, or "*" for everyone>, "roleId", "note"?}. A mapping that is
    there already answers 200 with it."""
    body = _body()
    mapping, made = accounts.add_mapping(body.get("group"), _number(body, "roleId"), body.get("note"), g.actor)
    return _private(jsonify(mapping=mapping), 201 if made else 200)


@app.post("/api/admin/groups/<int:mapping_id>/delete")
@_people_gate("users.manage")
def admin_unmap_group(mapping_id):
    if not accounts.delete_mapping(mapping_id, g.actor):
        return _private(jsonify(error="There is no group mapping {}.".format(mapping_id)), 404)
    return _private(jsonify(deleted=True))


def _audit_actor():
    actor = request.args.get("actor", "")
    if actor in ("", "token", "checks"):
        return actor or None
    return _arg_int("actor", None, 1, store._MAX_TOKEN_ID)


def _audit_target():
    """?target=<kind>:<id>, as (kind, id)."""
    kind, _, target_id = request.args.get("target", "").partition(":")
    if not kind:
        return None
    if kind not in accounts.AUDIT_TARGETS or not _WHOLE_NUMBER.fullmatch(target_id) \
            or not 1 <= int(target_id) <= store._MAX_TOKEN_ID:
        raise accounts.InvalidRequest("target is <kind>:<id>, the kind one of {}.".format(
            ", ".join(accounts.AUDIT_TARGETS)))
    return kind, int(target_id)


@app.get("/api/admin/audit")
@_people_gate("audit.view")
def admin_audit():
    """The audit log, newest first: ?before=<id> (next of the page before), ?limit= (1 to 200),
    ?actor=<user id>|token|checks, ?category= and ?target=<kind>:<id>."""
    category = request.args.get("category") or None
    if category is not None and category not in accounts.AUDIT_CATEGORIES:
        raise accounts.InvalidRequest("category is one of {}.".format(", ".join(accounts.AUDIT_CATEGORIES)))
    return _private(jsonify(accounts.audit_log(_arg_int("before", None, 1, store._MAX_TOKEN_ID),
                                               _arg_int("limit", 50, 1, accounts.AUDIT_LIMIT), _audit_actor(),
                                               category, _audit_target())))


@app.get("/api/admin/sign-in")
@_people_gate("users.view")
def admin_sign_in():
    """Single sign-on's settings as the Grid UI reads them, and what is wrong with them: never the client secret."""
    s = oidc.settings()
    return _private(jsonify(
        configured=oidc.configured(), problem=auth.sign_in_problem(), label=s.label, issuer=s.issuer or None,
        clientId=s.client_id or None, clientAuth=s.client_auth,
        redirectUri=oidc.redirect_uri() if s.public_url else None, scopes=list(oidc.SCOPES + s.scopes),
        groupsClaims=list(s.groups_claims), admins=accounts.named_admins(),
        session={"hours": s.session_hours, "idleMinutes": s.idle_minutes}, readsOpen=reads_open(),
        adminToken=bool(ADMIN_TOKEN), endSession=s.end_session))


# ---- MFA secrets ----------------------------------------------------------------------
#
# People store authenticator seeds here and share them; checks ask for codes with their client token, for
# the secrets its owner may use. Seeds go in and never come out: every answer carries codes or metadata
# only, and none is cached. A secret someone may not see answers exactly as one that isn't there.

@app.after_request
def _mfa_never_cached(response):
    # Refusals and errors too (the admin gate's, the guessing limit's, the error handlers'), not only codes.
    if request.path == "/api/mfa" or request.path.startswith("/api/mfa/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.errorhandler(mfa.InvalidSecret)
def invalid_secret(error):
    return _private(jsonify(error=str(error)), 400)


@app.errorhandler(store.MfaBusy)
def mfa_busy(error):
    response = jsonify(error=str(error), retry_after_s=round(error.retry_after_s, 1))
    response.headers["Retry-After"] = str(max(1, int(error.retry_after_s + 0.999)))
    return _private(response, 429)


@app.get("/api/mfa")
@auth.permission_required("mfa.use", refuse_when_unconfigured=True)
def list_mfa():
    result = store.list_mfa(auth.access(g.principal))
    accounts.annotate("mfa", result["secrets"])
    result["keyProblem"] = mfa.key_problem()
    # The page counts down to the next code by this clock, not the viewer's.
    result["serverTime"] = time.time()
    return _private(jsonify(result))


@app.post("/api/mfa")
@auth.permission_required("mfa.add", refuse_when_unconfigured=True)
def create_mfa():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send the secret as a JSON object."), 400
    access = auth.access(g.principal)
    secret = store.create_mfa(body.get("name"), body.get("secret"), body.get("issuer"), body.get("account"),
                              body.get("algorithm"), body.get("digits"), body.get("period"),
                              owner_id=g.principal.user_id, actor=g.actor, access=access)
    # The name as sent (create_mfa checked it): code scanning takes anything create_mfa returns for a password.
    log.info("mfa secret %r added by %s", body["name"].strip(), g.principal.name if g.principal else "an open grid")
    # The listing's shape, so the page can show it as it will be listed.
    return _private(jsonify(accounts.annotate("mfa", store.ownership("mfa", [secret], access))[0]), 201)


def _no_secret(entry_id):
    # The same for a secret that isn't there and one the viewer may not see: ids can be counted through.
    return jsonify(error="There is no MFA secret {}.".format(entry_id)), 404


@app.post("/api/mfa/<int:entry_id>/delete")
@auth.permission_required("mfa.add", refuse_when_unconfigured=True)
def delete_mfa(entry_id):
    if not store.delete_mfa(entry_id, auth.access(g.principal), g.actor):
        return _no_secret(entry_id)
    log.info("mfa secret %d deleted by %s", entry_id, g.principal.name if g.principal else "an open grid")
    return _private(jsonify(deleted=True, id=entry_id))


@app.get("/api/mfa/<int:entry_id>/code")
@auth.permission_required("mfa.use", refuse_when_unconfigured=True)
def peek_mfa_code(entry_id):
    """The current code, for someone who may use the secret to read; hands nothing out. Recorded in the audit
    log at most once every few minutes per person and secret: the page asks again every time window."""
    code = store.peek_mfa_code(entry_id, access=auth.access(g.principal))
    if code is None:
        return _no_secret(entry_id)
    if _audit_due("mfa.code_viewed", (_actor_key(g.actor), entry_id)):
        store.audit_alone(g.actor, "mfa.code_viewed", "mfa", entry_id, code["name"])
    return _private(jsonify(code))


@app.post("/api/mfa/<int:entry_id>/use")
@auth.permission_required("mfa.use", refuse_when_unconfigured=True)
def use_mfa_code(entry_id):
    """A code for someone to type somewhere, handed out like a check's so no check gets the same one."""
    code = store.issue_mfa_code(g.principal.name if g.principal else "Admin", secret_id=entry_id, label="Grid UI",
                                access=auth.access(g.principal), actor=g.actor)
    if code is None:
        return _no_secret(entry_id)
    return _private(jsonify(code))


@app.post(CHECK_CODE_PATH)
def check_mfa_code():
    """A code for a check, which sends its client token and {"name", "label"?, "minValidS"?}.

    The answer's waitS is how long to wait before typing the code (when it comes from the next window)
    and validForS how long it works from now. A secret the token's owner may not use is refused with
    not_shared."""
    principal = auth.check_principal(accounts.client_token_access, "mfa.use")
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"].strip():
        return _private(jsonify(error='Send {"name": "<secret name>"} as JSON.'), 400)
    min_valid = body.get("minValidS")
    if min_valid is not None and (isinstance(min_valid, bool) or not isinstance(min_valid, (int, float)) or min_valid < 0):
        return _private(jsonify(error="minValidS is a number of seconds, 0 or more."), 400)
    code = store.issue_mfa_code(principal.name, name=body["name"].strip(), label=body.get("label"),
                                min_valid_s=min_valid, access=auth.access(principal))
    if code is None:
        return _private(jsonify(error="There is no MFA secret called {!r}.".format(body["name"].strip())), 404)
    return _private(jsonify(code))


# ---- saved sign-ins -------------------------------------------------------------------
#
# Checks keep the browser state of a sign-in here and reuse it on later runs, so most runs skip the sign-in
# and its MFA prompt. States go in and out with the checks' client tokens only: people see what is saved
# and who used it, never a cookie, and no answer is cached. A check uses only the sign-ins its token's owner
# is a steward of or has been given (store.py says why), and only stewards' checks sign in fresh and save.

@app.after_request
def _signins_never_cached(response):
    # Refusals and errors too, not only states.
    if request.path == "/api/signins" or request.path.startswith("/api/signins/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def _too_large(message):
    # error is a code here, as with lease_lost: checks tell these apart from mistakes in the request.
    return _private(jsonify(error="too_large", message=message), 413)


@app.errorhandler(store.InvalidSignin)
def invalid_signin(error):
    return jsonify(error=str(error)), 400


@app.errorhandler(store.SigninTooLarge)
def signin_too_large(error):
    return _too_large(str(error))


@app.errorhandler(413)
def request_too_large(error):
    # A Content-Length past MAX_CONTENT_LENGTH, refused before the body is read.
    return _too_large("The request is over {} bytes.".format(store.SIGNIN_MAX_BODY_BYTES))


@app.errorhandler(store.LeaseLost)
def lease_lost(error):
    return jsonify(error="lease_lost", message=str(error)), 409


@app.errorhandler(store.SigninsFull)
def signins_full(error):
    return jsonify(error=str(error), signins_full=True), 409


def _check_signin_request():
    """(principal, body) of a check's sign-in request; a 401/429 for a missing or wrong client token, 403 for one
    whose owner may not use sign-ins, 413 for a body over SIGNIN_MAX_BODY_BYTES, 400 for one that isn't a JSON
    object."""
    principal = auth.check_principal(accounts.client_token_access, "signins.use")
    # By its Content-Length before it is read; a chunked one, by what MAX_CONTENT_LENGTH let through.
    limit = store.SIGNIN_MAX_BODY_BYTES
    if (request.content_length or 0) > limit or len(request.get_data()) > limit:
        abort(_too_large("The request is over {} bytes.".format(limit)))
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(_private(jsonify(error='Send {"name": "<sign-in name>", ...} as a JSON object.'), 400))
    return principal, body


def _no_signin(name):
    return _private(jsonify(error="There is no saved sign-in called {!r}.".format(name.strip())), 404)


@app.get("/api/signins")
@auth.permission_required("signins.use", refuse_when_unconfigured=True)
def list_signins():
    result = store.list_signins(auth.access(g.principal))
    accounts.annotate("signin", result["signins"])
    result["keyProblem"] = mfa.key_problem("signin")
    result["serverTime"] = time.time()
    return _private(jsonify(result))


def _no_signin_id(signin_id):
    # The same for a sign-in that isn't there and one the viewer may not see: ids can be counted through.
    return jsonify(error="There is no saved sign-in {}.".format(signin_id)), 404


@app.post("/api/signins/<int:signin_id>/forget")
@auth.permission_required("signins.add", refuse_when_unconfigured=True)
def forget_signin(signin_id):
    signin = store.forget_signin(signin_id, auth.access(g.principal), g.actor)
    if signin is None:
        return _no_signin_id(signin_id)
    log.info("saved sign-in %r forgotten by %s", signin["name"], g.principal.name if g.principal else "an open grid")
    return _private(jsonify(forgotten=True, signin=accounts.annotate("signin", [signin])[0]))


@app.post("/api/signins/<int:signin_id>/delete")
@auth.permission_required("signins.add", refuse_when_unconfigured=True)
def delete_signin(signin_id):
    if not store.delete_signin(signin_id, auth.access(g.principal), g.actor):
        return _no_signin_id(signin_id)
    log.info("saved sign-in %d deleted by %s", signin_id, g.principal.name if g.principal else "an open grid")
    return _private(jsonify(deleted=True, id=signin_id))


@app.post(CHECK_CHECKOUT_PATH)
def checkout_signin():
    """A check's saved sign-in, or its turn to sign in. It sends its client token and {"name",
    "rejectedVersion"?, "id"?, "leaseS"?}; the answer's status is "saved" (with the state), "sign-in" (with a
    lease token) or "wait" (with retryAfterS). rejectedVersion is the version of a state the site refused,
    and id the id it came with. A sign-in the token's owner may not use is refused with not_shared, and one
    shared with them that has nothing usable saved with owner_signs_in: only its owner's checks sign in for it."""
    principal, body = _check_signin_request()
    answer = store.checkout_signin(principal.name, body.get("name"), body.get("rejectedVersion"), body.get("leaseS"),
                                   body.get("id"), access=auth.access(principal), actor=auth.actor(principal))
    if answer["status"] == "missing":  # a name nobody added, and this check may not add names
        return _no_signin(body["name"])
    if answer["status"] == "owner-signs-in":
        return _private(jsonify(error="Nothing usable is saved for {!r}, and only its owner's checks may sign in for "
                                      "it. It is shared with this client token's owner, who can use it once their own "
                                      "check has signed in.".format(answer["name"]), owner_signs_in=True), 409)
    return _private(jsonify(answer))


@app.post(CHECK_SAVE_PATH)
def save_signin():
    """The state a check is signed in with: {"name", "state", "maxAgeS"?} and either the "leaseToken" of the
    sign-in it just made or the "baseVersion" and "id" of the state it reused (cookies roll over). Only the
    checks of a sign-in's stewards save a reused state again: anyone else's is answered with the reason
    "shared"."""
    principal, body = _check_signin_request()
    answer = store.save_signin(principal.name, body.get("name"), body.get("state"), body.get("leaseToken"),
                               body.get("baseVersion"), body.get("maxAgeS"), body.get("id"),
                               access=auth.access(principal))
    if answer is None:
        return _no_signin(body["name"])
    return _private(jsonify(answer))


@app.post(CHECK_RELEASE_PATH)
def release_signin():
    """{"name", "leaseToken"}: the lease of a sign-in that failed, so the next check signs in at once."""
    principal, body = _check_signin_request()
    released = store.release_signin(body.get("name"), body.get("leaseToken"), access=auth.access(principal))
    if released is None:
        return _no_signin(body["name"])
    return _private(jsonify(released=released))


# ---- sharing --------------------------------------------------------------------------
#
# Whoever may manage an MFA secret or a saved sign-in shares it with people and roles, who may then use it (never
# manage it); whoever may see every one gives it to someone else; whoever may see every token gives one nobody
# owns to the person whose checks use it. accounts.py decides who may, under the access lock, and records it.
# Every answer carries the item as its listing shows it, so the page can show it at once.

@app.get("/api/directory")
@_people_gate("mfa.add", "signins.add", "tokens.all", "users.view")
def directory():
    """Whom something can be shared with or given to: every role, and the enabled people whose name or email holds
    ?q= (2 characters or more), at most accounts.DIRECTORY_LIMIT of them."""
    return _private(jsonify(accounts.directory(request.args.get("q", ""))))


def _grantee_arg():
    """(kind, id) of whom a share is for: {"user": <users.id>} or {"role": <role id>}."""
    body = _body()
    kinds = body.keys() & {"user", "role"}
    if len(kinds) != 1:
        raise accounts.InvalidRequest('Send {"user": <id>} or {"role": <id>}.')
    kind = kinds.pop()
    return kind, _number(body, kind)


def _item_answer(kind, item_id, row):
    # Hidden or not there, the same 404: ids can be counted through.
    if row is None:
        return _no_secret(item_id) if kind == "mfa" else _no_signin_id(item_id)
    return _private(jsonify(accounts.annotate(kind, [row])[0]))


def _share(kind, item_id, change):
    grantee_kind, grantee_id = _grantee_arg()
    row = change(kind, item_id, grantee_kind, grantee_id, auth.access(g.principal), g.actor)
    return _item_answer(kind, item_id, row)


def _give(kind, item_id):
    # {"userId": null} gives it to the admins.
    row = accounts.set_item_owner(kind, item_id, _number(_body(), "userId", nullable=True), auth.access(g.principal),
                                  g.actor)
    return _item_answer(kind, item_id, row)


@app.post("/api/mfa/<int:entry_id>/shares/add")
@auth.permission_required("mfa.add", refuse_when_unconfigured=True)
def share_mfa(entry_id):
    """Share a secret with {"user": <id>} or {"role": <id>}: they get its codes, here and through their checks."""
    return _share("mfa", entry_id, accounts.share_item)


@app.post("/api/mfa/<int:entry_id>/shares/remove")
@auth.permission_required("mfa.add", refuse_when_unconfigured=True)
def unshare_mfa(entry_id):
    return _share("mfa", entry_id, accounts.unshare_item)


@app.post("/api/mfa/<int:entry_id>/owner")
@auth.permission_required("mfa.all", refuse_when_unconfigured=True)
def give_mfa(entry_id):
    """Give a secret to {"userId": <id>}, or to the admins ({"userId": null}). It keeps its shares."""
    return _give("mfa", entry_id)


@app.post("/api/signins/<int:signin_id>/shares/add")
@auth.permission_required("signins.add", refuse_when_unconfigured=True)
def share_signin(signin_id):
    """Share a sign-in with {"user": <id>} or {"role": <id>}: their checks reuse what its owner's checks save. Only
    with someone who has no permission the sharer lacks, unless the sharer sees every sign-in: so the Admin role,
    whose checks don't use a sign-in just because admins see it, is given one by an admin."""
    return _share("signin", signin_id, accounts.share_item)


@app.post("/api/signins/<int:signin_id>/shares/remove")
@auth.permission_required("signins.add", refuse_when_unconfigured=True)
def unshare_signin(signin_id):
    return _share("signin", signin_id, accounts.unshare_item)


@app.post("/api/signins/<int:signin_id>/owner")
@auth.permission_required("signins.all", refuse_when_unconfigured=True)
def give_signin(signin_id):
    """Give a sign-in to {"userId": <id>}, or to the admins ({"userId": null}). Its shares go, and so do any
    sign-in under way and what was saved: the new owner decides who has it, and their checks sign in afresh."""
    return _give("signin", signin_id)


@app.post("/api/tokens/<int:token_id>/owner")
@auth.permission_required("tokens.all", refuse_when_unconfigured=True)
def give_token(token_id):
    """Give a client token nobody owns to {"userId": <id>}: checks using it then act as them, not as an admin."""
    token = accounts.set_token_owner(token_id, _number(_body(), "userId"), g.actor)
    if token is None:
        return _private(jsonify(error="There is no access token {}.".format(token_id)), 404)
    return _private(jsonify(token=accounts.annotate("token", [token])[0]))


if __name__ == "__main__":
    app.run(host=os.environ.get("GRID_UI_HOST", "0.0.0.0"), port=int(os.environ.get("GRID_UI_PORT", "4444")))
