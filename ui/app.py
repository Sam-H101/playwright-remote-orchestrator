"""Grid-style management console for the Playwright orchestrator.

Run locally:  python -m ui.app   (from the repo root)
"""
import hmac
import os

from flask import Flask, abort, jsonify, render_template, request

from . import store

ADMIN_TOKEN = os.environ.get("GRID_ADMIN_TOKEN", "")

app = Flask(__name__)


def _require_admin():
    if not ADMIN_TOKEN:
        return
    supplied = request.headers.get("X-Grid-Token", "")
    if not hmac.compare_digest(supplied, ADMIN_TOKEN):
        abort(403)


@app.get("/")
@app.get("/ui")
def index():
    return render_template("index.html", token_required=bool(ADMIN_TOKEN))


@app.get("/status")
@app.get("/api/status")
def status():
    snap = store.snapshot()
    snap["ready"] = snap["totals"]["free"] - snap["totals"]["failing"] > 0
    return jsonify(snap)


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
    host = (request.get_json(silent=True) or {}).get("host")
    if host is None:
        abort(400)
    return jsonify(updated=store.reset_node_failures(host))


if __name__ == "__main__":
    app.run(host=os.environ.get("GRID_UI_HOST", "0.0.0.0"), port=int(os.environ.get("GRID_UI_PORT", "4444")))
