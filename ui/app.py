"""Grid-style management console for the Playwright orchestrator.

Run locally:  python -m ui.app   (from the repo root)
"""
import hmac
import ipaddress
import os
import socket
import urllib.parse

from flask import Flask, abort, jsonify, make_response, render_template, request

from . import store

ADMIN_TOKEN = os.environ.get("GRID_ADMIN_TOKEN", "")
# Extra host names the UI may be managed through (besides IP addresses, localhost and this
# machine's names), comma separated; "*" allows any. Guards the actions against DNS rebinding.
ALLOWED_HOSTS = {h.strip().lower() for h in os.environ.get("GRID_UI_ALLOWED_HOSTS", "").split(",") if h.strip()}

app = Flask(__name__)


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


def _require_admin():
    if not ADMIN_TOKEN:
        return
    supplied = request.headers.get("X-Grid-Token", "")
    if not hmac.compare_digest(supplied.encode(), ADMIN_TOKEN.encode()):
        # token_required tells the page to ask for the token, unlike other 403s (proxies, origin checks).
        abort(make_response(jsonify(error="The admin token is missing or wrong.", token_required=True), 403))


def _require_token_admin():
    """Access tokens are managed with the admin token only. Their metadata is sensitive too, so
    without GRID_ADMIN_TOKEN even the list is refused."""
    if not ADMIN_TOKEN:
        # admin_token_unset: the page explains the setting instead of asking for a token.
        abort(make_response(jsonify(error="set GRID_ADMIN_TOKEN to manage access tokens", admin_token_unset=True), 403))
    _require_admin()


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


@app.errorhandler(store.DatabaseUnavailable)
def database_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(store.TokensUnavailable)
def tokens_unavailable(error):
    return jsonify(error=str(error)), 503


@app.errorhandler(store.DatabasePermissionDenied)
def database_permission_denied(error):
    return jsonify(error=str(error)), 403


@app.get("/")
@app.get("/ui")
def index():
    return render_template("index.html", token_required=bool(ADMIN_TOKEN))


@app.get("/status")
@app.get("/api/status")
def status():
    return jsonify(store.snapshot())


@app.post("/api/slots/<int:slot_id>/release")
def release_slot(slot_id):
    _require_admin()
    return jsonify(updated=store.release_slot(slot_id))


@app.post("/api/slots/<int:slot_id>/reset-failures")
def reset_slot(slot_id):
    _require_admin()
    return jsonify(updated=store.reset_slot_failures(slot_id))


@app.post("/api/nodes/reset-failures")
def reset_node():
    _require_admin()
    return jsonify(updated=store.reset_node_failures(_host_arg()))


@app.post("/api/nodes/forget")
def forget_node():
    _require_admin()
    host = _host_arg()
    result, count = store.forget_node(host)
    if result == "forgotten":
        return jsonify(forgotten=True, host=host, slots=count)
    if result == "not_found":
        return jsonify(error="{} is not a registered gateway node.".format(host)), 404
    if result == "online":
        return jsonify(error="{} is online. Stop its agent first.".format(host)), 409
    return jsonify(error="{} has {} slot(s) in use. Release them first.".format(host, count)), 409


@app.get("/api/tokens")
def list_tokens():
    _require_token_admin()
    result = store.list_tokens()
    # Whether the env bootstrap tokens are set, if the UI gets the same variables as the hub.
    # Informational only; their values never leave the server.
    result["bootstrap"] = {"agent": bool(os.environ.get("GATEWAY_AGENT_TOKEN")),
                           "client": bool(os.environ.get("GATEWAY_CLIENT_TOKEN"))}
    return _private(jsonify(result))


@app.post("/api/tokens")
def create_token():
    _require_token_admin()
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Send the token's settings as a JSON object."), 400
    try:
        token = store.create_token(body.get("name"), body.get("kind"), body.get("nodeName"), body.get("expiresInDays"))
    except store.InvalidToken as e:
        return jsonify(error=str(e)), 400
    return _private(jsonify(token), 201)


@app.post("/api/tokens/<int:token_id>/revoke")
def revoke_token(token_id):
    _require_token_admin()
    token = store.revoke_token(token_id)
    if token is None:
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    return _private(jsonify(revoked=True, token=token))


@app.post("/api/tokens/<int:token_id>/delete")
def delete_token(token_id):
    _require_token_admin()
    result = store.delete_token(token_id)
    if result == "not_found":
        return jsonify(error="There is no access token {}.".format(token_id)), 404
    if result == "active":
        return jsonify(error="This token is still active. Revoke it before deleting it."), 409
    return _private(jsonify(deleted=True, id=token_id))


if __name__ == "__main__":
    app.run(host=os.environ.get("GRID_UI_HOST", "0.0.0.0"), port=int(os.environ.get("GRID_UI_PORT", "4444")))
