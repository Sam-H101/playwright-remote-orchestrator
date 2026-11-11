"""Managed access tokens: store.py and the API against sqlite.  Run from the repo root: python -m pytest ui/tests"""
import contextlib
import datetime
import hashlib
import html.parser
import json
import os
import re
import secrets
import sqlite3

import pytest
import werkzeug.exceptions
import werkzeug.routing

from ui import app as grid_app, demo, permissions, store
from ui.tests.test_accounts import add_role, make_user, oidc_on, sign_in_as

ADMIN = {"X-Grid-Token": "s3cret"}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.create_schema(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.delenv("GATEWAY_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return path


def run(path, sql, params=()):
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute(sql, params).fetchall()


def add_token(path, name, kind="agent", expires=None, revoked=None, node_name=None):
    """expires / revoked: seconds ago (negative: in the future) or None."""
    with contextlib.closing(sqlite3.connect(path)) as con, con:
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
    with contextlib.closing(sqlite3.connect(path)) as con, con:
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
    # An agent token made before there were owners: nobody's, and checks don't use it to act as anyone.
    assert (body["tokens"][0]["owner"], body["tokens"][0]["actsAs"]) == (None, None)
    assert body["kindCounts"] == {"agent": {"rows": 1, "active": 1, "revoked": 0},
                                  "client": {"rows": 0, "active": 0, "revoked": 0}}


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


# ---- scopes ------------------------------------------------------------------------
#
# A made-up token and what is kept for it: its sha256, and the scoped form, which is "scoped:" and the first 57
# characters of that.
T = "pwgc_" + "A" * 43
T_PLAIN = "1f0bc52f7fa82946861c65a388c72cfdd15561d997682cee0eba9fa2becc4bd0"
T_SCOPED = "scoped:1f0bc52f7fa82946861c65a388c72cfdd15561d997682cee0eba9fa2b"
NOT_A_LIST = "Send scope as a list of scope ids, or leave it out for no limit."
NOTHING_PICKED = "Pick at least one thing the token may do, or leave scope out for no limit."
ADD_NEEDS_USE = ("A scope with “Add saved sign-ins” needs “Use saved sign-ins” too: a check that adds a sign-in "
                 "then uses it.")
NO_SCOPE_SENT = ("Send scope: the list of scope ids the token is limited to. A token with no limit is made with "
                 "POST /api/tokens.")
SCOPED_ELSEWHERE = "A token with a scope is made with POST /api/tokens/scoped."


def scoped(client, body, headers=ADMIN):
    return client.post("/api/tokens/scoped", json=body, headers=headers)


def add_scoped_row(path, name, scope, plain=False):
    """A client token's row written straight to the table: in the scoped form unless plain, with that scope
    column (None: NULL). Returns its id."""
    value = "pwgc_" + name
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute(
            "INSERT INTO accessTokens (name, kind, tokenHash, prefix, createdAt, scope) VALUES (?, 'client', ?, ?, ?, ?)",
            (name, store.token_hash(value) if plain else store.scoped_token_hash(value), value[:12], ts(3600),
             scope)).lastrowid


def token_count(path):
    return run(path, "SELECT COUNT(*) FROM accessTokens")[0][0]


def audit_details(path):
    return [(action, name, None if details is None else json.loads(details)) for action, name, details in
            run(path, "SELECT action, targetName, details FROM auditLog ORDER BY id")]


def unknown_setting(key):
    return "There is no token setting {!r} here.".format(key)


# What a Grid UI from before scopes does with a bearer token and which token routes it has, kept as it was
# released (commit d05b719) and never updated: the tests below run it against what this version stores.
RELEASED_LOOKUP = "SELECT id FROM accessTokens WHERE tokenHash = ? AND kind = 'client'"
RELEASED_TOKEN_RULES = [("/api/tokens", ["GET", "POST"]), ("/api/tokens/<int:token_id>/revoke", ["POST"]),
                        ("/api/tokens/<int:token_id>/delete", ["POST"]), ("/api/tokens/<int:token_id>/owner", ["POST"])]


def released_lookup(path, bearer):
    return run(path, RELEASED_LOOKUP, (hashlib.sha256(bearer.encode()).hexdigest(),))


def test_a_grid_ui_from_before_scopes_finds_no_row_for_a_scoped_token(db):
    """Whatever it is sent: the token, or the token with scoped: put in front, once or twice. So a rolled-back
    Grid UI turns a scoped token away; it never takes it for one with no limit."""
    limited = store.create_token("limited", "client", scope=["grid.view", "mfa.use"])
    plain = store.create_token("plain", "client")
    add_scoped_row(db, "A" * 43, "grid.view")  # T, whose hashes are written out above
    for token in (limited["token"], T):
        for bearer in (token, "scoped:" + token, "scoped:scoped:" + token):
            assert released_lookup(db, bearer) == [], bearer[:-43]
    assert released_lookup(db, plain["token"]) == [(plain["id"],)]  # the query is the right one
    assert token_count(db) == 3


def test_a_scoped_tokens_stored_form_is_not_a_sha256(db):
    limited = store.create_token("limited", "client", scope=["sessions.open"])
    plain = store.create_token("plain", "client")
    ((stored,),) = run(db, "SELECT tokenHash FROM accessTokens WHERE id = ?", (limited["id"],))
    assert len(stored) == 64 and stored.startswith("scoped:")
    assert not re.fullmatch(r"[0-9a-f]{64}", stored)
    ((stored,),) = run(db, "SELECT tokenHash FROM accessTokens WHERE id = ?", (plain["id"],))
    assert re.fullmatch(r"[0-9a-f]{64}", stored)


def test_the_scoped_form_is_part_of_the_plain_hash():
    plain = hashlib.sha256(T.encode()).hexdigest()
    assert store.token_hash(T) == plain == T_PLAIN
    assert store.scoped_token_hash(T) == T_SCOPED
    assert store.scoped_token_hash(T) == "scoped:" + store.token_hash(T)[:57] == "scoped:" + plain[:57]
    assert len(T_SCOPED) == 64
    # What software from before scopes computes for the token with scoped: in front is something else again.
    old = hashlib.sha256(("scoped:" + T).encode()).hexdigest()
    assert old == "c9795951395e62fd4872f788f64603b879af3dcdb0954109b0d77aad5eeabd7a"
    assert T_SCOPED not in (old, hashlib.sha256(("scoped:scoped:" + T).encode()).hexdigest())


def test_a_grid_ui_from_before_scopes_has_no_route_that_makes_a_scoped_token():
    """It answers 404 and makes nothing: a scope sent to it can't come back as a token with no limit."""
    released = werkzeug.routing.Map([werkzeug.routing.Rule(rule, methods=methods, endpoint=rule)
                                     for rule, methods in RELEASED_TOKEN_RULES]).bind("localhost")
    assert released.match("/api/tokens", method="POST") == ("/api/tokens", {})
    assert released.match("/api/tokens/7/revoke", method="POST")[1] == {"token_id": 7}
    with pytest.raises(werkzeug.exceptions.NotFound):
        released.match("/api/tokens/scoped", method="POST")
    assert grid_app.app.url_map.bind("localhost").match("/api/tokens/scoped", method="POST") == (
        "create_scoped_token", {})


def test_a_token_made_without_a_scope_is_stored_and_listed_as_before(db):
    client = grid_app.app.test_client()
    r = client.post("/api/tokens", json={"name": "checks", "kind": "client", "expiresInDays": 7}, headers=ADMIN)
    made = r.get_json()
    assert r.status_code == 201 and made["scope"] is None and "scopeLost" not in made
    assert run(db, "SELECT tokenHash, scope FROM accessTokens") == [
        (hashlib.sha256(made["token"].encode()).hexdigest(), None)]
    listed = by_name(client.get("/api/tokens", headers=ADMIN).get_json())["checks"]
    assert listed["scope"] is None and "scopeLost" not in listed
    agent = store.create_token("farm", "agent")
    assert agent["scope"] is None and by_name(store.list_tokens())["farm"]["scope"] is None
    assert run(db, "SELECT tokenHash, scope FROM accessTokens WHERE id = ?", (agent["id"],)) == [
        (hashlib.sha256(agent["token"].encode()).hexdigest(), None)]


def test_a_scoped_token_is_stored_in_the_scoped_form_with_its_ids_sorted(db):
    client = grid_app.app.test_client()
    r = scoped(client, {"name": "limited", "scope": ["signins.use", "grid.view", "sessions.open", "grid.view"],
                        "expiresInDays": 30})
    made = r.get_json()
    assert r.status_code == 201 and r.headers["Cache-Control"] == "no-store"
    assert (made["kind"], made["scope"], made["status"]) == ("client", ["grid.view", "sessions.open", "signins.use"],
                                                             "active")
    assert re.fullmatch(r"pwgc_[A-Za-z0-9_-]{43}", made["token"]) and "scopeLost" not in made
    assert run(db, "SELECT tokenHash, scope, kind, prefix FROM accessTokens") == [
        (store.scoped_token_hash(made["token"]), "grid.view sessions.open signins.use", "client", made["token"][:12])]
    assert run(db, "SELECT COUNT(*) FROM accessTokens WHERE tokenHash = ?", (store.token_hash(made["token"]),)) == [(0,)]
    listed = by_name(client.get("/api/tokens", headers=ADMIN).get_json())["limited"]
    assert listed["scope"] == ["grid.view", "sessions.open", "signins.use"] and "scopeLost" not in listed
    every = store.create_token("every", "client", scope=tuple(permissions.TOKEN_SCOPES))
    assert every["scope"] == sorted(permissions.TOKEN_SCOPES)


def test_scope_requests_that_are_refused(db):
    client = grid_app.app.test_client()
    for scope, error in (("grid.view", NOT_A_LIST), (["grid.view", 7], NOT_A_LIST), ([], NOTHING_PICKED),
                         ("", NOT_A_LIST), (0, NOT_A_LIST), (False, NOT_A_LIST), ({"grid.view": True}, NOT_A_LIST),
                         (["grid.view", "grid.operate"], "There is no scope 'grid.operate'."),
                         (["sessions.openx"], "There is no scope 'sessions.openx'."),
                         (["signins.add"], ADD_NEEDS_USE), (["signins.add", "grid.view"], ADD_NEEDS_USE)):
        r = scoped(client, {"name": "limited", "scope": scope})
        assert (r.status_code, r.get_json()) == (400, {"error": error}), scope
    for scope in (["grid.view"], [], "grid.view"):
        with pytest.raises(store.InvalidToken, match="Only client tokens can have a scope."):
            store.create_token("farm", "agent", scope=scope)
    assert token_count(db) == 0 and audit_details(db) == []


def test_the_scoped_route_never_makes_a_token_without_a_scope(db):
    """A scope that is missing, null or misspelt is refused. Ignoring it would make a token with no limit."""
    client = grid_app.app.test_client()
    for body, error in (({"name": "x"}, NO_SCOPE_SENT), ({"name": "x", "scope": None}, NO_SCOPE_SENT),
                        ({"name": "x", "expiresInDays": 7}, NO_SCOPE_SENT),
                        ({"name": "x", "scopes": ["grid.view"]}, unknown_setting("scopes")),
                        ({"name": "x", "Scope": ["grid.view"]}, unknown_setting("Scope")),
                        ({"name": "x", "scope ": ["grid.view"]}, unknown_setting("scope ")),
                        ({"name": "x", "scope": ["grid.view"], "scopes": None}, unknown_setting("scopes"))):
        r = scoped(client, body)
        assert (r.status_code, r.get_json()) == (400, {"error": error}), body
    assert token_count(db) == 0 and audit_details(db) == []


def test_the_scoped_route_makes_client_tokens_only(db):
    client = grid_app.app.test_client()
    for key, value in (("kind", "agent"), ("kind", "client"), ("nodeName", "node-1"), ("nodeName", None)):
        r = scoped(client, {"name": "x", "scope": ["grid.view"], key: value})
        assert (r.status_code, r.get_json()) == (400, {"error": unknown_setting(key)}), (key, value)
    assert token_count(db) == 0
    made = scoped(client, {"name": "x", "scope": ["grid.view"]}).get_json()
    assert made["kind"] == "client" and made["token"].startswith("pwgc_") and made["nodeName"] is None
    assert run(db, "SELECT kind, nodeName FROM accessTokens") == [("client", None)]


def test_the_plain_route_refuses_a_scope_and_any_key_it_does_not_know(db):
    """A key it doesn't know is never ignored, a scope least of all."""
    client = grid_app.app.test_client()
    for kind in ("client", "agent"):
        for extra, error in (({"scope": ["grid.view"]}, SCOPED_ELSEWHERE), ({"scope": None}, SCOPED_ELSEWHERE),
                             ({"scope": []}, SCOPED_ELSEWHERE),
                             ({"scopes": ["grid.view"]}, unknown_setting("scopes")),
                             ({"Scope": ["grid.view"]}, unknown_setting("Scope")),
                             ({"expiresInDay": 7}, unknown_setting("expiresInDay"))):
            r = client.post("/api/tokens", json=dict({"name": "x", "kind": kind}, **extra), headers=ADMIN)
            assert (r.status_code, r.get_json()) == (400, {"error": error}), (kind, extra)
    assert token_count(db) == 0 and audit_details(db) == []
    r = client.post("/api/tokens", json={"name": "farm", "kind": "agent", "nodeName": "node-1", "expiresInDays": 7},
                    headers=ADMIN)
    assert r.status_code == 201 and r.get_json()["scope"] is None
    assert client.post("/api/tokens", json={"name": "checks", "kind": "client"}, headers=ADMIN).status_code == 201


def test_a_scope_may_name_what_its_maker_lacks(db, monkeypatch):
    """It is made as asked, and grants nothing: what the token gets still goes by its owner's roles."""
    oidc_on(monkeypatch)
    store.create_token("legacy", "client")  # not the grid's first client token, then
    maker = make_user(db, "Mo Maker",[add_role(db, "Makes tokens", "tokens.own")])
    client = grid_app.app.test_client()
    sign_in_as(client, maker)
    r = scoped(client, {"name": "mo-ci", "scope": ["mfa.use", "grid.view"]}, headers={})
    made = r.get_json()
    assert r.status_code == 201 and made["scope"] == ["grid.view", "mfa.use"] and made["owner"]["id"] == maker
    r = grid_app.app.test_client().post("/api/mfa/code", json={"name": "intranet"},
                                        headers={"Authorization": "Bearer " + made["token"]})
    assert r.status_code == 403 and r.get_json()["forbidden"] is True and "out_of_scope" not in r.get_json()


def test_a_scoped_token_is_refused_when_the_table_has_no_scope_column(db):
    """A database no hub that knows scopes has started on yet: the limit can't be kept, so no token is made."""
    run(db, "ALTER TABLE accessTokens DROP COLUMN scope")
    client = grid_app.app.test_client()
    r = scoped(client, {"name": "limited", "scope": ["grid.view"]})
    assert (r.status_code, r.get_json()) == (503, {"error": store._NO_TOKEN_SCOPE})
    with pytest.raises(store.TokensUnavailable):
        store.create_token("limited", "client", scope=["grid.view"])
    assert token_count(db) == 0 and audit_details(db) == []
    r = client.post("/api/tokens", json={"name": "checks", "kind": "client"}, headers=ADMIN)
    assert r.status_code == 201 and r.get_json()["scope"] is None
    assert audit_details(db) == [("token.created", "checks", {"kind": "client", "owner": None, "scope": None})]


def test_a_table_without_the_scope_column_lists_every_token_with_no_scope(db):
    run(db, "ALTER TABLE accessTokens DROP COLUMN scope")
    add_token(db, "farm")
    checks = add_token(db, "checks", kind="client")
    listing = grid_app.app.test_client().get("/api/tokens", headers=ADMIN).get_json()
    assert listing["ready"] is True and [(t["name"], t["scope"]) for t in listing["tokens"]] == [
        ("checks", None), ("farm", None)]
    assert all("scopeLost" not in t for t in listing["tokens"])
    revoked = store.revoke_token(checks)
    assert revoked["scope"] is None and "scopeLost" not in revoked


def test_an_empty_stored_scope_is_listed_as_an_empty_list_not_as_none(db):
    """None is no limit and an empty scope allows nothing: the two never read alike."""
    add_scoped_row(db, "nothing", "")
    add_scoped_row(db, "plain-nothing", "", plain=True)  # a plain row someone gave an empty scope
    add_token(db, "checks", kind="client")
    tokens = by_name(store.list_tokens())
    assert (tokens["nothing"]["scope"], tokens["plain-nothing"]["scope"], tokens["checks"]["scope"]) == ([], [], None)
    assert all("scopeLost" not in t for t in tokens.values())


def test_a_scoped_row_whose_scope_is_gone_is_listed_as_lost_never_as_no_limit(db):
    """Its scope set to NULL by hand, or the column dropped: the row allows nothing and the list says so, in
    the answers to revoking and giving it too."""
    client = grid_app.app.test_client()
    dana = make_user(db, "Dana Lee")
    lost, healthy, later = (store.create_token(name, "client", scope=["grid.view"])["id"]
                            for name in ("lost", "healthy", "later"))
    plain = store.create_token("plain", "client")["id"]
    run(db, "UPDATE accessTokens SET scope = NULL WHERE id = ?", (lost,))

    def listed():
        return {name: (t["scope"], t.get("scopeLost", "no key"))
                for name, t in by_name(client.get("/api/tokens", headers=ADMIN).get_json()).items()}

    assert listed() == {"lost": ([], True), "healthy": (["grid.view"], "no key"), "later": (["grid.view"], "no key"),
                        "plain": (None, "no key")}
    r = client.post("/api/tokens/{}/owner".format(lost), json={"userId": dana}, headers=ADMIN)
    assert r.status_code == 200 and (r.get_json()["token"]["scope"], r.get_json()["token"]["scopeLost"]) == ([], True)
    r = client.post("/api/tokens/{}/revoke".format(lost), json={}, headers=ADMIN)
    assert r.status_code == 200 and (r.get_json()["token"]["scope"], r.get_json()["token"]["scopeLost"]) == ([], True)
    for token_id in (healthy, plain):
        assert "scopeLost" not in store.revoke_token(token_id)

    run(db, "ALTER TABLE accessTokens DROP COLUMN scope")
    assert listed() == {"lost": ([], True), "healthy": ([], True), "later": ([], True), "plain": (None, "no key")}
    r = client.post("/api/tokens/{}/owner".format(later), json={"userId": dana}, headers=ADMIN)
    assert r.status_code == 200 and (r.get_json()["token"]["scope"], r.get_json()["token"]["scopeLost"]) == ([], True)
    r = client.post("/api/tokens/{}/revoke".format(later), json={}, headers=ADMIN)
    assert r.status_code == 200 and (r.get_json()["token"]["scope"], r.get_json()["token"]["scopeLost"]) == ([], True)


def test_ids_this_version_does_not_know_are_left_out_of_a_listed_scope(db):
    add_scoped_row(db, "newer", "grid.view made.up sessions.openx")
    add_scoped_row(db, "newest", "made.up grid.operate")  # a permission is not a scope id either
    tokens = by_name(store.list_tokens())
    assert tokens["newer"]["scope"] == ["grid.view"] and tokens["newest"]["scope"] == []
    assert "scopeLost" not in tokens["newer"] and "scopeLost" not in tokens["newest"]


def test_the_audit_log_records_the_scope_a_client_token_was_made_with(db):
    client = grid_app.app.test_client()
    made = [scoped(client, {"name": "limited", "scope": ["mfa.use", "grid.view"]}).get_json(),
            client.post("/api/tokens", json={"name": "checks", "kind": "client"}, headers=ADMIN).get_json(),
            client.post("/api/tokens", json={"name": "farm", "kind": "agent"}, headers=ADMIN).get_json()]
    assert audit_details(db) == [
        ("token.created", "limited", {"kind": "client", "owner": None, "scope": ["grid.view", "mfa.use"]}),
        ("token.created", "checks", {"kind": "client", "owner": None, "scope": None}),
        ("token.created", "farm", {"kind": "agent", "owner": None})]
    rows = run(db, "SELECT * FROM auditLog")
    for token in made:
        assert not any(token["token"] in str(value) for row in rows for value in row)


def test_the_plaintext_of_a_scoped_token_is_returned_once_and_stored_nowhere(db):
    client = grid_app.app.test_client()
    made = scoped(client, {"name": "limited", "scope": ["grid.view"]}).get_json()
    token = made["token"]
    assert re.fullmatch(r"pwgc_[A-Za-z0-9_-]{43}", token)
    for table in ("accessTokens", "auditLog", "itemOwners"):
        assert not any(token in str(value) for row in run(db, "SELECT * FROM " + table) for value in row), table
    # Nor is what is kept enough to find the row with by its sha256: it is 57 of the 64 characters.
    assert store.token_hash(token) not in str(run(db, "SELECT * FROM accessTokens"))
    listing = client.get("/api/tokens", headers=ADMIN)
    assert token not in listing.get_data(as_text=True) and "token" not in by_name(listing.get_json())["limited"]
    r = client.post("/api/tokens/{}/revoke".format(made["id"]), json={}, headers=ADMIN)
    assert token not in r.get_data(as_text=True) and "token" not in r.get_json()["token"]
    assert scoped(client, {"name": "limited", "scope": ["grid.view"]}).get_json()["token"] != token


def test_revoking_and_giving_a_scoped_token_answer_with_its_scope(db):
    client = grid_app.app.test_client()
    dana = make_user(db, "Dana Lee")
    made = scoped(client, {"name": "limited", "scope": ["signins.use", "mfa.use"]}).get_json()
    stored = run(db, "SELECT tokenHash, scope FROM accessTokens")
    r = client.post("/api/tokens/{}/owner".format(made["id"]), json={"userId": dana}, headers=ADMIN)
    given = r.get_json()["token"]
    assert r.status_code == 200 and given["scope"] == ["mfa.use", "signins.use"] and given["owner"]["id"] == dana
    r = client.post("/api/tokens/{}/revoke".format(made["id"]), json={}, headers=ADMIN)
    revoked = r.get_json()["token"]
    assert r.status_code == 200 and (revoked["scope"], revoked["status"]) == (["mfa.use", "signins.use"], "revoked")
    assert "scopeLost" not in given and "scopeLost" not in revoked
    assert run(db, "SELECT tokenHash, scope FROM accessTokens") == stored


def test_sessions_open_is_a_scope_and_not_a_permission():
    """The hub enforces it. No role holds it, and a scope's ids are never read as permissions."""
    assert list(permissions.TOKEN_SCOPES.items()) == [
        ("sessions.open", "Open browser sessions"), ("grid.view", "See the grid"), ("mfa.use", "Use MFA secrets"),
        ("signins.use", "Use saved sign-ins"), ("signins.add", "Add saved sign-ins")]
    assert permissions.SESSIONS_SCOPE == "sessions.open"
    assert "sessions.open" not in permissions.PERMISSIONS and "sessions.open" not in permissions.ALL
    assert "sessions.open" not in [p["id"] for p in permissions.catalogue()]
    assert permissions.expand({"sessions.open"}) == permissions.parse("sessions.open") == frozenset()
    assert set(permissions.TOKEN_SCOPES) - {"sessions.open"} <= permissions.ALL
    assert permissions.parse_scope("mfa.use  made.up sessions.open grid.operate") == {"mfa.use", "sessions.open"}
    assert permissions.parse_scope("") == permissions.parse_scope(None) == frozenset()


class _ScopeChoices(html.parser.HTMLParser):
    """The (value, label) of each scope checkbox inside the new-token dialog's #nt-scope-list."""

    def __init__(self):
        super().__init__()
        self.choices, self._depth, self._value, self._label = [], 0, None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._depth:
            self._depth += tag == "div"
            if tag == "input" and attrs.get("type") == "checkbox" and attrs.get("name") == "scope":
                self._value = attrs.get("value")
            elif tag == "strong" and self._value is not None:
                self._label = ""
        elif tag == "div" and attrs.get("id") == "nt-scope-list":
            self._depth = 1

    def handle_data(self, data):
        if self._label is not None:
            self._label += data

    def handle_endtag(self, tag):
        if self._depth and tag == "div":
            self._depth -= 1
        elif tag == "strong" and self._label is not None:
            self.choices.append((self._value, self._label.strip()))
            self._value = self._label = None


def test_the_dialog_offers_exactly_the_scopes_the_server_knows():
    """A scope the dialog lacks can't be picked, and one the server lacks would be refused."""
    parser = _ScopeChoices()
    with open(os.path.join(os.path.dirname(grid_app.__file__), "templates", "index.html"), encoding="utf-8") as f:
        parser.feed(f.read())
    assert parser.choices == list(permissions.TOKEN_SCOPES.items())


def test_the_demo_stores_its_scoped_token_the_way_the_store_does(tmp_path, monkeypatch):
    made, token_urlsafe = [], secrets.token_urlsafe

    def recorded(nbytes=None):
        made.append(token_urlsafe(nbytes))
        return made[-1]

    monkeypatch.setattr(secrets, "token_urlsafe", recorded)
    path = str(tmp_path / "demo.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    ((stored, scope),) = run(path, "SELECT tokenHash, scope FROM accessTokens WHERE name = 'Grid monitor'")
    (token,) = ["pwgc_" + value for value in made if store.scoped_token_hash("pwgc_" + value) == stored]
    assert scope == "grid.view"
    assert store.client_token(token)["scope"] == {"grid.view"}
    assert by_name(store.list_tokens())["Grid monitor"]["scope"] == ["grid.view"]
    # Every other demo token has no scope and is stored by its sha256.
    assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE scope IS NOT NULL OR tokenHash LIKE 'scoped:%'") == [(1,)]
