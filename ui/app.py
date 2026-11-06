"""Grid-style management console for the Playwright orchestrator.

Run locally:  python -m ui.app   (from the repo root)
"""
import ipaddress
import logging
import mimetypes
import os
import socket
import time
import urllib.parse

from flask import Flask, abort, g, jsonify, render_template, request

from . import auth, mfa, recorder, store

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
# Read at call time, so the setting can change while the app runs (the tests rely on it).
auth.configure(lambda: ADMIN_TOKEN)
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


def _host_allowed(name):
    if "*" in ALLOWED_HOSTS or name in ALLOWED_HOSTS or name in _LOCAL_NAMES:
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
        # Behind a proxy that rewrites Host, list the public name in GRID_UI_ALLOWED_HOSTS.
        origin_netloc = urllib.parse.urlsplit(origin).netloc.lower()
        if origin_netloc != request.host.lower() and _hostname(origin_netloc) not in ALLOWED_HOSTS:
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
    always has it."""
    _log_db_error(error)
    try:
        signed_in = auth.current_principal() is not None
    except Exception:  # refused by the guessing limit: certainly not signed in
        signed_in = False
    return str(error) if signed_in else getattr(error, "public", "The database can't be reached.")


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


@app.get("/")
@app.get("/ui")
def index():
    return render_template("index.html")


@app.get("/status")
@app.get("/api/status")
def status():
    RECORDER.ensure_running()
    data, _ = RECORDER.observe()  # an outage goes to _db_error_response, which answers ?soft=1 with data
    data = dict(data)  # the recorder keeps the snapshot it was given
    data["grid_name"] = GRID_NAME or None
    data["public_gateway_url"] = PUBLIC_GATEWAY_URL or None
    data["poll_ms"] = POLL_MS
    # ?events=<cursor>: also what the recorder saw since then (0: all of the last 15 minutes).
    if request.args.get("events") is not None:
        data.update(RECORDER.export(request.args.get("events")))
    return jsonify(data)


@app.get("/api/events")
def events():
    """The recorder's events since ?since=<epoch ms> (default: all it holds), for scripts and tools."""
    RECORDER.ensure_running()
    try:
        RECORDER.observe()
    except store.DatabaseUnavailable:
        pass  # what was recorded before the outage is still worth having
    return _private(jsonify(RECORDER.export(request.args.get("since", "0"))))


def _observe_before_action():
    # A fresh "before" snapshot, so the one after the action differs from it only by the action
    # (and whatever the checks did in the same instant).
    try:
        RECORDER.observe()
    except Exception as e:
        log.debug("recorder: no snapshot before the action: %s", e)


def _attribute(match):
    """Record the action's effect and mark it as this admin's. Returns the event ids."""
    principal = getattr(g, "principal", None)
    try:
        _, ids = RECORDER.observe(attribute=(match, principal.name if principal else ""))
    except Exception as e:
        log.debug("recorder: no snapshot after the action: %s", e)
        return []
    return ids


@app.get("/api/session")
def session():
    """Sign-in state for the page: who is signed in (judged from the credentials sent, if any),
    whether admin sign-in is configured at all, and which providers to offer."""
    return _private(jsonify(auth.session_info()))


@app.post("/api/slots/<int:slot_id>/release")
@auth.admin_required()
def release_slot(slot_id):
    body = request.get_json(silent=True)
    claimed_at = body.get("claimed_at") if isinstance(body, dict) else None
    _observe_before_action()
    if claimed_at is None:
        updated = store.release_slot(slot_id)
    else:
        # The page sends the claim its confirm described; anything else is a different check's.
        result, updated = store.release_claim(slot_id, str(claimed_at))
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
@auth.admin_required()
def reset_slot(slot_id):
    _observe_before_action()
    updated = store.reset_slot_failures(slot_id)
    ids = _attribute(lambda e: e.get("slot") == slot_id and e["type"] == "reset")
    return jsonify(updated=updated, events=ids)


@app.post("/api/nodes/reset-failures")
@auth.admin_required()
def reset_node():
    host = _host_arg()
    _observe_before_action()
    updated = store.reset_node_failures(host)
    ids = _attribute(lambda e: e.get("node") == host and e["type"] == "reset")
    return jsonify(updated=updated, events=ids)


@app.post("/api/nodes/forget")
@auth.admin_required()
def forget_node():
    host = _host_arg()
    _observe_before_action()
    result, count = store.forget_node(host)
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


@app.get("/api/tokens")
@auth.admin_required(refuse_when_unconfigured=True)
def list_tokens():
    result = store.list_tokens()
    # Whether the env bootstrap tokens are set, if the UI gets the same variables as the hub.
    # Informational only; their values never leave the server.
    result["bootstrap"] = {"agent": bool(os.environ.get("GATEWAY_AGENT_TOKEN")),
                           "client": bool(os.environ.get("GATEWAY_CLIENT_TOKEN"))}
    return _private(jsonify(result))


@app.post("/api/tokens")
@auth.admin_required(refuse_when_unconfigured=True)
def create_token():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send the token's settings as a JSON object."), 400
    token = store.create_token(body.get("name"), body.get("kind"), body.get("nodeName"), body.get("expiresInDays"))
    return _private(jsonify(token), 201)


@app.post("/api/tokens/<int:token_id>/revoke")
@auth.admin_required(refuse_when_unconfigured=True)
def revoke_token(token_id):
    token = store.revoke_token(token_id)
    if token is None:
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    return _private(jsonify(revoked=True, token=token))


@app.post("/api/tokens/<int:token_id>/delete")
@auth.admin_required(refuse_when_unconfigured=True)
def delete_token(token_id):
    result = store.delete_token(token_id)
    if result == "not_found":
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    if result == "active":
        return jsonify(error="This token is still active. Revoke it before deleting it."), 409
    return _private(jsonify(deleted=True, id=token_id))


# ---- MFA secrets ----------------------------------------------------------------------
#
# Admins store authenticator seeds here; checks ask for codes with their client token. Seeds go in and
# never come out: every answer carries codes or metadata only, and none is cached.

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
@auth.admin_required(refuse_when_unconfigured=True)
def list_mfa():
    result = store.list_mfa()
    result["keyProblem"] = mfa.key_problem()
    # The page counts down to the next code by this clock, not the viewer's.
    result["serverTime"] = time.time()
    return _private(jsonify(result))


@app.post("/api/mfa")
@auth.admin_required(refuse_when_unconfigured=True)
def create_mfa():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send the secret as a JSON object."), 400
    secret = store.create_mfa(body.get("name"), body.get("secret"), body.get("issuer"), body.get("account"),
                              body.get("algorithm"), body.get("digits"), body.get("period"))
    # The name as sent (create_mfa checked it): code scanning takes anything create_mfa returns for a password.
    log.info("mfa secret %r added by %s", body["name"].strip(), g.principal.name if g.principal else "an open grid")
    return _private(jsonify(secret), 201)


@app.post("/api/mfa/<int:entry_id>/delete")
@auth.admin_required(refuse_when_unconfigured=True)
def delete_mfa(entry_id):
    if not store.delete_mfa(entry_id):
        return jsonify(error="There is no MFA secret {}.".format(entry_id)), 404
    log.info("mfa secret %d deleted by %s", entry_id, g.principal.name if g.principal else "an open grid")
    return _private(jsonify(deleted=True, id=entry_id))


@app.get("/api/mfa/<int:entry_id>/code")
@auth.admin_required(refuse_when_unconfigured=True)
def peek_mfa_code(entry_id):
    """The current code, for an admin to read; hands nothing out."""
    code = store.peek_mfa_code(entry_id)
    if code is None:
        return jsonify(error="There is no MFA secret {}.".format(entry_id)), 404
    return _private(jsonify(code))


@app.post("/api/mfa/<int:entry_id>/use")
@auth.admin_required(refuse_when_unconfigured=True)
def use_mfa_code(entry_id):
    """A code for an admin to type somewhere, handed out like a check's so no check gets the same one."""
    code = store.issue_mfa_code(g.principal.name if g.principal else "Admin", secret_id=entry_id, label="Grid UI")
    if code is None:
        return jsonify(error="There is no MFA secret {}.".format(entry_id)), 404
    return _private(jsonify(code))


@app.post(CHECK_CODE_PATH)
def check_mfa_code():
    """A code for a check, which sends its client token and {"name", "label"?, "minValidS"?}.

    The answer's waitS is how long to wait before typing the code (when it comes from the next window)
    and validForS how long it works from now."""
    principal = auth.check_principal(store.client_token_name)
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("name"), str) or not body["name"].strip():
        return _private(jsonify(error='Send {"name": "<secret name>"} as JSON.'), 400)
    min_valid = body.get("minValidS")
    if min_valid is not None and (isinstance(min_valid, bool) or not isinstance(min_valid, (int, float)) or min_valid < 0):
        return _private(jsonify(error="minValidS is a number of seconds, 0 or more."), 400)
    code = store.issue_mfa_code(principal.name, name=body["name"].strip(), label=body.get("label"),
                                min_valid_s=min_valid)
    if code is None:
        return _private(jsonify(error="There is no MFA secret called {!r}.".format(body["name"].strip())), 404)
    return _private(jsonify(code))


# ---- saved sign-ins -------------------------------------------------------------------
#
# Checks keep the browser state of a sign-in here and reuse it on later runs, so most runs skip the sign-in
# and its MFA prompt. States go in and out with the checks' client tokens only: admins see what is saved
# and who used it, never a cookie, and no answer is cached.

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
    """(principal, body) of a check's sign-in request; a 401/429 for a missing or wrong client token, 413 for
    a body over SIGNIN_MAX_BODY_BYTES, 400 for one that isn't a JSON object."""
    principal = auth.check_principal(store.client_token_name)
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
@auth.admin_required(refuse_when_unconfigured=True)
def list_signins():
    result = store.list_signins()
    result["keyProblem"] = mfa.key_problem("signin")
    result["serverTime"] = time.time()
    return _private(jsonify(result))


@app.post("/api/signins/<int:signin_id>/forget")
@auth.admin_required(refuse_when_unconfigured=True)
def forget_signin(signin_id):
    signin = store.forget_signin(signin_id)
    if signin is None:
        return jsonify(error="There is no saved sign-in {}.".format(signin_id)), 404
    log.info("saved sign-in %r forgotten by %s", signin["name"], g.principal.name if g.principal else "an open grid")
    return _private(jsonify(forgotten=True, signin=signin))


@app.post("/api/signins/<int:signin_id>/delete")
@auth.admin_required(refuse_when_unconfigured=True)
def delete_signin(signin_id):
    if not store.delete_signin(signin_id):
        return jsonify(error="There is no saved sign-in {}.".format(signin_id)), 404
    log.info("saved sign-in %d deleted by %s", signin_id, g.principal.name if g.principal else "an open grid")
    return _private(jsonify(deleted=True, id=signin_id))


@app.post(CHECK_CHECKOUT_PATH)
def checkout_signin():
    """A check's saved sign-in, or its turn to sign in. It sends its client token and {"name",
    "rejectedVersion"?, "id"?, "leaseS"?}; the answer's status is "saved" (with the state), "sign-in" (with a
    lease token) or "wait" (with retryAfterS). rejectedVersion is the version of a state the site refused,
    and id the id it came with."""
    principal, body = _check_signin_request()
    return _private(jsonify(store.checkout_signin(principal.name, body.get("name"), body.get("rejectedVersion"),
                                                  body.get("leaseS"), body.get("id"))))


@app.post(CHECK_SAVE_PATH)
def save_signin():
    """The state a check is signed in with: {"name", "state", "maxAgeS"?} and either the "leaseToken" of the
    sign-in it just made or the "baseVersion" and "id" of the state it reused (cookies roll over)."""
    principal, body = _check_signin_request()
    answer = store.save_signin(principal.name, body.get("name"), body.get("state"), body.get("leaseToken"),
                               body.get("baseVersion"), body.get("maxAgeS"), body.get("id"))
    if answer is None:
        return _no_signin(body["name"])
    return _private(jsonify(answer))


@app.post(CHECK_RELEASE_PATH)
def release_signin():
    """{"name", "leaseToken"}: the lease of a sign-in that failed, so the next check signs in at once."""
    _, body = _check_signin_request()
    released = store.release_signin(body.get("name"), body.get("leaseToken"))
    if released is None:
        return _no_signin(body["name"])
    return _private(jsonify(released=released))


if __name__ == "__main__":
    app.run(host=os.environ.get("GRID_UI_HOST", "0.0.0.0"), port=int(os.environ.get("GRID_UI_PORT", "4444")))
