"""Grid-style management console for the Playwright orchestrator.

Run locally:  python -m ui.app   (from the repo root)
"""
import ipaddress
import logging
import mimetypes
import os
import socket
import urllib.parse

from flask import Flask, abort, g, jsonify, render_template, request

from . import auth, recorder, store

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

app = Flask(__name__)
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


@app.errorhandler(store.DatabaseUnavailable)
def database_unavailable(error):
    return jsonify(error=_db_error_text(error)), 503


@app.errorhandler(store.TokensUnavailable)
def tokens_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(store.DatabasePermissionDenied)
def database_permission_denied(error):
    return jsonify(error=_db_error_text(error)), 403


@app.get("/")
@app.get("/ui")
def index():
    return render_template("index.html")


@app.get("/status")
@app.get("/api/status")
def status():
    RECORDER.ensure_running()
    try:
        data, _ = RECORDER.observe()
    except store.DatabaseUnavailable as e:
        # ?soft=1 (the page's own polls): the outage as data, not an HTTP error the browser logs every
        # few seconds. Without it the answer is the 503 (or 403) it has always been.
        if request.args.get("soft") != "1":
            raise
        return _private(jsonify(error=_db_error_text(e), unavailable=True))
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
    try:
        token = store.create_token(body.get("name"), body.get("kind"), body.get("nodeName"), body.get("expiresInDays"))
    except store.InvalidToken as e:
        return jsonify(error=str(e)), 400
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


if __name__ == "__main__":
    app.run(host=os.environ.get("GRID_UI_HOST", "0.0.0.0"), port=int(os.environ.get("GRID_UI_PORT", "4444")))
