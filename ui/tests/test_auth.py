"""The admin gate, the sign-in session endpoint, the security headers and the additive status keys."""
import json
import os
import random
import re
import sqlite3

import pytest

from ui import app as grid_app, auth, demo, store

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
TEMPLATE = os.path.join(os.path.dirname(STATIC), "templates", "index.html")


@pytest.fixture
def client(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with sqlite3.connect(path) as con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    return grid_app.app.test_client()


def test_every_response_carries_the_security_headers(client):
    for url in ("/", "/ui", "/api/status", "/status", "/api/session", "/static/js/main.js", "/static/grid.css", "/nope"):
        r = client.get(url)
        assert r.headers["Content-Security-Policy"] == (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
            "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"), url
        assert r.headers["Referrer-Policy"] == "no-referrer"
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
    r = client.post("/api/slots/1/release", data="x")  # refused by the origin guard: still headed
    assert r.status_code == 403 and r.headers["X-Frame-Options"] == "DENY"


def test_scripts_are_served_as_javascript(client):
    # With nosniff, a module served as text/plain (a Windows registry quirk) would not run at all.
    for name in ("js/main.js", "js/live.js", "theme.js"):
        assert client.get("/static/" + name).headers["Content-Type"].startswith("text/javascript")


def test_the_page_has_no_inline_script_or_style(client):
    html = client.get("/").get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html)  # every script has a src
    assert " style=" not in html and "<style" not in html
    assert not re.search(r"\son[a-z]+=", html)  # no inline handlers
    assert '<script type="module" src="/static/js/main.js">' in html
    assert "data-token-required" not in html  # the page asks /api/session instead


def test_session_reports_sign_in_state(client, monkeypatch):
    r = client.get("/api/session")
    assert r.headers["Cache-Control"] == "no-store"
    assert r.get_json() == {"signedIn": False, "name": None, "method": None, "adminConfigured": False, "providers": []}

    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    body = client.get("/api/session").get_json()
    assert body["adminConfigured"] is True and body["signedIn"] is False
    assert body["providers"] == [{"id": "token", "kind": "secret", "label": "Admin token"}]
    assert client.get("/api/session", headers={"X-Grid-Token": "wrong"}).get_json()["signedIn"] is False
    body = client.get("/api/session", headers={"X-Grid-Token": "s3cret"}).get_json()
    assert body["signedIn"] is True and body["name"] == "Admin" and body["method"] == "token"


def test_the_one_gate_answers_exactly_as_before(client, monkeypatch):
    """Byte-identical refusals: token_required for a missing or wrong token, admin_token_unset on the token
    routes without GRID_ADMIN_TOKEN; grid actions stay open without one, as they always were."""
    unset = json.dumps({"admin_token_unset": True, "error": "set GRID_ADMIN_TOKEN to manage access tokens"}, sort_keys=True)
    r = client.get("/api/tokens")
    assert r.status_code == 403 and json.dumps(r.get_json(), sort_keys=True) == unset
    assert client.post("/api/slots/1/reset-failures", json={}).status_code == 200

    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    for headers in ({}, {"X-Grid-Token": ""}, {"X-Grid-Token": "s3cre"}):
        for r in (client.post("/api/slots/1/release", json={}, headers=headers), client.get("/api/tokens", headers=headers)):
            assert r.status_code == 403
            assert r.get_json() == {"error": "The admin token is missing or wrong.", "token_required": True}
    assert client.post("/api/slots/1/release", json={}, headers={"X-Grid-Token": "s3cret"}).status_code == 200


def test_a_second_provider_plugs_in_without_touching_the_routes(client, monkeypatch):
    class Header:
        id = "sso"

        def configured(self):
            return True

        def authenticate(self, req):
            user = req.headers.get("X-Test-User")
            return auth.Principal(user, self.id) if user else None

        def describe(self):
            return {"id": self.id, "kind": "redirect", "label": "Company sign-in"}

    monkeypatch.setattr(auth, "PROVIDERS", auth.PROVIDERS + [Header()])
    assert client.post("/api/slots/1/release", json={}).status_code == 403
    r = client.post("/api/slots/1/release", json={}, headers={"X-Test-User": "dana"})
    assert r.status_code == 200
    body = client.get("/api/session", headers={"X-Test-User": "dana"}).get_json()
    assert body["signedIn"] is True and body["name"] == "dana" and body["method"] == "sso"


def test_status_gains_the_grid_name_and_public_hub_address(client, monkeypatch):
    body = client.get("/api/status").get_json()
    assert body["grid_name"] is None and body["public_gateway_url"] is None
    monkeypatch.setattr(grid_app, "GRID_NAME", "lab grid")
    monkeypatch.setattr(grid_app, "PUBLIC_GATEWAY_URL", "wss://grid.example.com")
    body = client.get("/status").get_json()
    assert body["grid_name"] == "lab grid" and body["public_gateway_url"] == "wss://grid.example.com"
    assert body["gateway_url"] == store.GATEWAY_URL  # the existing key is unchanged


def test_only_the_auth_module_knows_how_admin_credentials_are_kept():
    owners = {}
    for folder, _, files in os.walk(STATIC):
        for name in files:
            if name.endswith(".js"):
                text = open(os.path.join(folder, name), encoding="utf-8").read()
                for word in ("X-Grid-Token", "sessionStorage", "gridToken"):
                    if word in text:
                        owners.setdefault(word, set()).add(name)
    assert owners == {"X-Grid-Token": {"auth.js"}, "sessionStorage": {"auth.js"}, "gridToken": {"auth.js"}}


def test_markup_is_built_only_through_the_escaping_template():
    for folder, _, files in os.walk(os.path.join(STATIC, "js")):
        for name in files:
            text = open(os.path.join(folder, name), encoding="utf-8").read()
            assert 'style="' not in text and "style='" not in text and "setAttribute(\"style\"" not in text, name
            if name != "html.js":
                assert ".innerHTML" not in text and "outerHTML" not in text and "insertAdjacentHTML" not in text, name
            assert not re.search(r"\braw\(", text) or name == "html.js", name  # raw() exists, and no view uses it


def test_demo_activity_claims_and_releases_like_the_checks(tmp_path):
    path = str(tmp_path / "demo.db")
    with sqlite3.connect(path) as con:
        demo.seed(con)
        before = con.execute("SELECT id, inUse, LastRunDate, failCount FROM portUsage ORDER BY id").fetchall()
    rng = random.Random(4)
    with sqlite3.connect(path) as con:
        claimed = demo.burst(con, rng)
        assert 3 <= len(claimed) <= 7
        assert all(r[0] == 1 for r in con.execute("SELECT inUse FROM portUsage WHERE id IN ({})".format(
            ",".join("?" * len(claimed))), claimed))
        demo.finish(con, claimed, rng)
        rows = con.execute("SELECT inUse, LastRunDate FROM portUsage WHERE id IN ({})".format(",".join("?" * len(claimed))), claimed).fetchall()
    assert all(in_use == 0 and stamp for in_use, stamp in rows)
    assert len(before) == 22


def test_a_soft_status_poll_reports_an_outage_as_data(client, monkeypatch, tmp_path):
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))  # a directory: every query fails
    r = client.get("/api/status")
    assert r.status_code == 503 and "error" in r.get_json() and "unavailable" not in r.get_json()
    r = client.get("/api/status?soft=1")
    assert r.status_code == 200 and r.get_json()["unavailable"] is True and r.get_json()["error"]
    assert r.headers["Cache-Control"] == "no-store"
