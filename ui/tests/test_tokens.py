"""Managed access tokens: store.py and the API against sqlite.  Run from the repo root: python -m pytest ui/tests"""
import datetime
import hashlib
import re
import sqlite3

import pytest

from ui import app as grid_app, demo, store

ADMIN = {"X-Grid-Token": "s3cret"}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with sqlite3.connect(path) as con:
        demo.create_schema(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.delenv("GATEWAY_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return path


def run(path, sql, params=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, params).fetchall()


def add_token(path, name, kind="agent", expires=None, revoked=None, node_name=None):
    """expires / revoked: seconds ago (negative: in the future) or None."""
    with sqlite3.connect(path) as con:
        cur = con.execute(
            "INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, revokedAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (name, kind, hashlib.sha256(name.encode()).hexdigest(), "pwga_xxxxxxx", node_name, ts(3600),
             None if expires is None else ts(expires), None if revoked is None else ts(revoked)))
        return cur.lastrowid


def add_node(path, name, token_id, online=1, seen_ago=1):
    run(path, "INSERT INTO nodes (name, online, slots, lastSeen, tokenId) VALUES (?, ?, 1, ?, ?)",
        (name, online, ts(seen_ago), token_id))


def by_name(listing):
    return {t["name"]: t for t in listing["tokens"]}


def test_create_returns_the_plaintext_once_and_stores_only_its_hash(db):
    agent = store.create_token("  Build farm  ", "agent", "WIN-Build-07", 30)
    client = store.create_token("Checks", "client")
    assert re.fullmatch(r"pwga_[A-Za-z0-9_-]{43}", agent["token"])
    assert re.fullmatch(r"pwgc_[A-Za-z0-9_-]{43}", client["token"])
    assert agent["name"] == "Build farm" and agent["nodeName"] == "win-build-07"
    assert agent["prefix"] == agent["token"][:12] and agent["status"] == "active"
    expires = store._parse_time(agent["expiresAt"])
    assert abs((expires - utcnow()).total_seconds() - 30 * 86400) < 10
    assert client["expiresAt"] is None and client["nodeName"] is None

    rows = run(db, "SELECT * FROM accessTokens")
    assert len(rows) == 2
    for token in (agent["token"], client["token"]):
        assert not any(token in str(value) for row in rows for value in row)  # no plaintext anywhere
    assert run(db, "SELECT tokenHash, prefix FROM accessTokens WHERE id = ?", (agent["id"],)) == [
        (hashlib.sha256(agent["token"].encode()).hexdigest(), agent["token"][:12])]

    listing = store.list_tokens()
    assert listing["ready"] is True
    assert all("token" not in t for t in listing["tokens"])
    assert agent["token"] not in str(listing) and client["token"] not in str(listing)
    assert [t["name"] for t in listing["tokens"]] == ["Checks", "Build farm"]  # newest first
    assert store.create_token("x", "agent")["token"] != store.create_token("x", "agent")["token"]


@pytest.mark.parametrize("args", [
    ("", "agent", None, None),
    ("   ", "agent", None, None),
    ("x" * 129, "agent", None, None),
    ("tab\there", "agent", None, None),
    ("del\x7f", "agent", None, None),
    ("lone \ud800 surrogate", "agent", None, None),  # JSON can send one; no database can store it
    (None, "agent", None, None),
    ("ok", "admin", None, None),
    ("ok", ["agent"], None, None),          # not hashable: must be a 400, not a TypeError
    ("ok", {"agent": 1}, None, None),
    ("ok", "client", "node-1", None),       # only agent tokens are bound to nodes
    ("ok", "agent", "bad name", None),
    ("ok", "agent", "x" * 129, None),
    ("ok", "agent", 12, None),
    ("ok", "agent", None, 0),
    ("ok", "agent", None, 3651),
    ("ok", "agent", None, 1.5),
    ("ok", "agent", None, "30"),
    ("ok", "agent", None, True),
])
def test_create_validation(db, args):
    with pytest.raises(store.InvalidToken):
        store.create_token(*args)
    assert run(db, "SELECT COUNT(*) FROM accessTokens") == [(0,)]


def test_create_accepts_the_limits(db):
    assert store.create_token("x" * 128, "agent", "a" * 128, 3650)["nodeName"] == "a" * 128
    assert store.create_token("y", "agent", "", 1)["nodeName"] is None  # an empty field means any node


def test_status_and_the_nodes_using_each_token(db):
    active = add_token(db, "active", expires=-86400)
    expired = add_token(db, "expired", expires=60)
    revoked = add_token(db, "revoked", revoked=120)
    both = add_token(db, "revoked and expired", expires=600, revoked=60)
    add_token(db, "client", kind="client")
    add_node(db, "zeta", active)
    add_node(db, "Alpha", active, online=0)
    add_node(db, "stale", active, seen_ago=600)
    add_node(db, "old", revoked, online=0)
    add_node(db, "env-token", None)
    tokens = by_name(store.list_tokens())

    assert {n: t["status"] for n, t in tokens.items()} == {
        "active": "active", "expired": "expired", "revoked": "revoked", "revoked and expired": "revoked", "client": "active"}
    assert tokens["active"]["nodes"] == ["Alpha", "stale", "zeta"]
    assert tokens["active"]["nodesOnline"] == ["zeta"]  # online needs the flag and a recent heartbeat
    assert tokens["revoked"]["nodes"] == ["old"] and tokens["revoked"]["nodesOnline"] == []
    assert tokens["expired"]["nodes"] == [] and tokens["client"]["nodes"] == []
    for t in tokens.values():
        for key in ("createdAt", "expiresAt", "revokedAt", "lastUsedAt"):
            assert t[key] is None or t[key].endswith("Z")
    assert tokens["revoked"]["revokedAt"] and tokens["active"]["revokedAt"] is None
    assert both and expired


def test_revoke_is_idempotent(db):
    token_id = add_token(db, "t")
    first = store.revoke_token(token_id)
    assert first["status"] == "revoked" and first["revokedAt"].endswith("Z")
    run(db, "UPDATE accessTokens SET revokedAt = '2026-01-01 00:00:00' WHERE id = ?", (token_id,))
    assert store.revoke_token(token_id)["revokedAt"] == "2026-01-01T00:00:00Z"  # the first time stays
    assert store.revoke_token(9999) is None


def test_delete_only_revoked_or_expired_tokens(db):
    active = add_token(db, "active", expires=-600)
    expired = add_token(db, "expired", expires=600)
    revoked = add_token(db, "revoked", revoked=60)
    add_node(db, "n1", revoked)
    add_node(db, "n2", active)
    assert store.delete_token(active) == "active"
    assert store.delete_token(9999) == "not_found"
    assert store.delete_token(revoked) == "deleted"
    assert store.delete_token(expired) == "deleted"
    assert store.delete_token(revoked) == "not_found"
    assert [t["name"] for t in store.list_tokens()["tokens"]] == ["active"]
    assert run(db, "SELECT name, tokenId FROM nodes ORDER BY name") == [("n1", None), ("n2", active)]


def test_databases_from_before_managed_tokens(db):
    token_id = add_token(db, "t", revoked=5)
    add_node(db, "n1", token_id)
    run(db, "ALTER TABLE nodes DROP COLUMN tokenId")  # nodes without tokenId: no mapping, no error
    assert by_name(store.list_tokens())["t"]["nodes"] == []
    assert store.delete_token(token_id) == "deleted"

    run(db, "DROP TABLE accessTokens")  # an older demo database: an empty state, not an error
    listing = store.list_tokens()
    assert listing["ready"] is False and listing["tokens"] == []
    with pytest.raises(store.TokensUnavailable):
        store.create_token("t", "agent")
    assert store.revoke_token(1) is None and store.delete_token(1) == "not_found"

    client = grid_app.app.test_client()
    r = client.get("/api/tokens", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["ready"] is False
    r = client.post("/api/tokens", json={"name": "t", "kind": "agent"}, headers=ADMIN)
    assert r.status_code == 503 and "accessTokens" in r.get_json()["error"]


def test_the_demo_database_has_every_kind_of_token(tmp_path, monkeypatch):
    path = str(tmp_path / "demo.db")
    with sqlite3.connect(path) as con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    tokens = store.list_tokens()["tokens"]
    assert {t["status"] for t in tokens} == {"active", "expired", "revoked"}
    assert {t["kind"] for t in tokens} == {"agent", "client"}
    assert any(t["nodeName"] and t["status"] == "active" for t in tokens)
    assert any(t["kind"] == "agent" and not t["nodeName"] and t["status"] == "active" for t in tokens)
    assert sum(len(t["nodes"]) for t in tokens) >= 3


# ---- API -------------------------------------------------------------------------


def test_every_token_route_needs_grid_admin_token_to_be_set(db, monkeypatch):
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    token_id = add_token(db, "t", revoked=5)
    client = grid_app.app.test_client()
    responses = [
        client.get("/api/tokens"),
        client.get("/api/tokens", headers={"X-Grid-Token": "anything"}),
        client.post("/api/tokens", json={"name": "t", "kind": "agent"}, headers={"X-Grid-Token": ""}),
        client.post("/api/tokens/{}/revoke".format(token_id), json={}),
        client.post("/api/tokens/{}/delete".format(token_id), json={}),
    ]
    for r in responses:
        assert r.status_code == 403
        assert r.get_json()["error"] == "set GRID_ADMIN_TOKEN to manage access tokens"
        assert r.get_json()["admin_token_unset"] is True and "token_required" not in r.get_json()
    assert run(db, "SELECT COUNT(*) FROM accessTokens") == [(1,)]


def test_token_routes_check_the_admin_token(db):
    token_id = add_token(db, "t")
    client = grid_app.app.test_client()
    for headers in ({}, {"X-Grid-Token": "wrong"}, {"X-Grid-Token": "s3cretX"}):
        for r in (client.get("/api/tokens", headers=headers),
                  client.post("/api/tokens", json={"name": "n", "kind": "agent"}, headers=headers),
                  client.post("/api/tokens/{}/revoke".format(token_id), json={}, headers=headers)):
            assert r.status_code == 403 and r.get_json()["token_required"] is True
    assert run(db, "SELECT COUNT(*) FROM accessTokens WHERE revokedAt IS NULL") == [(1,)]

    r = client.get("/api/tokens", headers=ADMIN)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    body = r.get_json()
    assert body["ready"] is True and [t["name"] for t in body["tokens"]] == ["t"]
    assert body["bootstrap"] == {"agent": False, "client": False}


def test_create_revoke_and_delete_through_the_api(db, monkeypatch):
    monkeypatch.setenv("GATEWAY_AGENT_TOKEN", "the-env-secret")
    client = grid_app.app.test_client()
    r = client.post("/api/tokens", json={"name": "farm", "kind": "agent", "nodeName": "Node-1", "expiresInDays": 7},
                    headers=ADMIN)
    assert r.status_code == 201 and r.headers["Cache-Control"] == "no-store"
    made = r.get_json()
    assert made["token"].startswith("pwga_") and made["nodeName"] == "node-1" and made["status"] == "active"

    listing = client.get("/api/tokens", headers=ADMIN)
    assert made["token"] not in listing.get_data(as_text=True)
    assert "the-env-secret" not in listing.get_data(as_text=True)
    assert listing.get_json()["bootstrap"] == {"agent": True, "client": False}

    r = client.post("/api/tokens", json={"name": "c", "kind": "client", "nodeName": "x"}, headers=ADMIN)
    assert r.status_code == 400 and "agent" in r.get_json()["error"]
    assert client.post("/api/tokens", json=["not", "an", "object"], headers=ADMIN).status_code == 400

    path = "/api/tokens/{}/".format(made["id"])
    r = client.post(path + "delete", json={}, headers=ADMIN)
    assert r.status_code == 409 and "Revoke" in r.get_json()["error"]
    r = client.post(path + "revoke", json={}, headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["token"]["status"] == "revoked"
    assert client.post(path + "revoke", json={}, headers=ADMIN).status_code == 200
    assert client.post(path + "delete", json={}, headers=ADMIN).get_json() == {"deleted": True, "id": made["id"]}
    assert client.post(path + "delete", json={}, headers=ADMIN).status_code == 404
    assert client.post("/api/tokens/9999/revoke", json={}, headers=ADMIN).status_code == 404


def test_bad_create_requests_are_400s_not_500s(db):
    client = grid_app.app.test_client()
    for body in ('{"name": "x", "kind": ["agent"]}', '{"name": "x", "kind": {"a": 1}}',
                 '{"name": "\\ud800", "kind": "agent"}'):
        r = client.post("/api/tokens", data=body, content_type="application/json", headers=ADMIN)
        assert r.status_code == 400 and r.get_json()["error"], body
    assert run(db, "SELECT COUNT(*) FROM accessTokens") == [(0,)]


def test_an_id_past_the_int_range_is_unknown_on_sqlite_too(db):
    """MySQL answers 404 for any id that isn't there; sqlite can't even bind one past 64 bits."""
    client = grid_app.app.test_client()
    add_token(db, "t", expires=600)
    for big in (2 ** 31, 2 ** 63, 10 ** 20):
        for action in ("revoke", "delete"):
            r = client.post("/api/tokens/{}/{}".format(big, action), json={}, headers=ADMIN)
            assert r.status_code == 404, (big, action)
    assert client.post("/api/tokens/0/delete", json={}, headers=ADMIN).status_code == 404
    assert run(db, "SELECT COUNT(*) FROM accessTokens") == [(1,)]


def test_token_actions_only_from_the_grid_page(db, monkeypatch):
    """The right admin token doesn't make a cross-site or rebound request acceptable."""
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())
    token_id = add_token(db, "t")
    client = grid_app.app.test_client()
    refused = [
        dict(data={"name": "x", "kind": "agent"}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}),
        dict(json={"name": "x", "kind": "agent"}, headers=dict(ADMIN, Origin="https://evil.example")),
        dict(json={"name": "x", "kind": "agent"}, headers=dict(ADMIN, **{"Sec-Fetch-Site": "cross-site"})),
        dict(json={"name": "x", "kind": "agent"}, headers=dict(ADMIN, Origin="http://evil.example:4444"),
             base_url="http://evil.example:4444"),
    ]
    for kwargs in refused:
        for url in ("/api/tokens", "/api/tokens/{}/revoke".format(token_id)):
            r = client.post(url, **kwargs)
            assert r.status_code == 403, (url, kwargs)
            assert "token_required" not in r.get_json() and "admin_token_unset" not in r.get_json()
    assert run(db, "SELECT COUNT(*) FROM accessTokens WHERE revokedAt IS NULL") == [(1,)]

    r = client.post("/api/tokens", base_url="http://127.0.0.1:4444", json={"name": "x", "kind": "client"},
                    headers=dict(ADMIN, Origin="http://127.0.0.1:4444", **{"Sec-Fetch-Site": "same-origin"}))
    assert r.status_code == 201
