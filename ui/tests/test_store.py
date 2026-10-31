"""store.py and the API against sqlite.  Run from the repo root: python -m pytest ui/tests"""
import datetime
import os
import sqlite3

import pytest

from ui import app as grid_app, demo, store

SCHEMA_SQL = os.path.join(os.path.dirname(__file__), "..", "..", "src", "schema.sql")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    sqlite3.connect(path).close()  # an empty file: no tables yet
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(store, "GATEWAY_URL", "ws://hub:4500")
    return path


def run(path, sql, params=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, params).fetchall()


def add_node(path, name, online, seen_ago, slots):
    """slots: list of (healthy, in use since (s ago) or None, failCount)."""
    with sqlite3.connect(path) as con:
        demo.create_schema(con)
        con.execute("INSERT INTO nodes (name, online, slots, playwrightVersion, platform, browsers, lastSeen) "
                    "VALUES (?, ?, ?, '1.64.1', 'linux', 'chromium, firefox', ?)", (name, online, len(slots), ts(seen_ago)))
        for i, (healthy, since, fails) in enumerate(slots, start=1):
            cur = con.execute("INSERT INTO portStatus (PortNumber, PortStatus, PortServer) VALUES (?, ?, ?)", (i, healthy, name))
            con.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate, failCount) VALUES (?, ?, ?, ?, ?)",
                        (cur.lastrowid, i, int(since is not None), None if since is None else ts(since), fails))


def node(snap, name):
    return next(n for n in snap["nodes"] if n["host"] == name)


def test_missing_tables_is_an_empty_grid_not_an_error(db):
    snap = store.snapshot()
    assert snap["db"]["initialised"] is False
    assert snap["nodes"] == [] and snap["sessions"] == []
    assert snap["ready"] is False
    assert store.release_slot(1) == 0
    assert store.forget_node("HOST1") == ("not_found", 0)


def test_legacy_db_without_nodes_table_shows_static_hosts(db):
    with sqlite3.connect(db) as con:
        demo.create_schema(con)
        con.execute("DROP TABLE nodes")
        cur = con.execute("INSERT INTO portStatus (PortNumber, PortServer) VALUES (3000, 'static1')")
        con.execute("INSERT INTO portUsage (id, portNumber) VALUES (?, 3000)", (cur.lastrowid,))
    n = node(store.snapshot(), "static1")
    assert n["kind"] == "static" and n["status"] == "UP"
    assert "endpoint" not in n["slots"][0] and n["slots"][0]["via_hub"] is None


def test_missing_database_file_is_unavailable(db, monkeypatch):
    monkeypatch.setattr(store, "SQLITE_PATH", db + ".missing")
    with pytest.raises(store.DatabaseUnavailable):
        store.snapshot()
    assert not os.path.exists(db + ".missing")


def test_timestamps_are_utc_with_z_and_durations_use_utc(db):
    add_node(db, "HOST1", 1, 5, [(1, 90, 0)])
    snap = store.snapshot()
    session = snap["sessions"][0]
    assert snap["generated"].endswith("Z")
    assert session["started"].endswith("Z") and node(snap, "HOST1")["last_seen"].endswith("Z")
    assert 85 <= session["duration_s"] <= 100
    started = datetime.datetime.fromisoformat(session["started"].replace("Z", "+00:00"))
    assert abs((datetime.datetime.now(datetime.timezone.utc) - started).total_seconds() - 90) < 10


def test_parse_time_normalises_to_naive_utc():
    assert store._parse_time("2026-01-01T10:00:00+02:00") == datetime.datetime(2026, 1, 1, 8, 0)
    assert store._parse_time("2026-01-01 10:00:00") == datetime.datetime(2026, 1, 1, 10, 0)
    assert store._parse_time(b"2026-01-01 10:00:00.250000") == datetime.datetime(2026, 1, 1, 10, 0, 0, 250000)
    assert store._parse_time("") is None and store._parse_time("garbage") is None
    assert store._iso(datetime.datetime(2026, 1, 1, 8, 0)) == "2026-01-01T08:00:00Z"


def test_online_needs_flag_and_recent_heartbeat(db):
    add_node(db, "fresh", 1, 30, [(1, None, 0), (0, None, 0), (1, None, 5)])
    add_node(db, "stale", 1, 120, [(1, None, 0)])
    add_node(db, "down", 0, 2, [(1, None, 0)])
    snap = store.snapshot()

    fresh = node(snap, "fresh")
    assert fresh["online"] is True and fresh["status"] == "DEGRADED"
    assert [s["state"] for s in fresh["slots"]] == ["free", "unhealthy", "failing"]
    assert [s["selectable"] for s in fresh["slots"]] == [True, False, True]
    assert fresh["browsers"] == ["chromium", "firefox"]

    for name in ("stale", "down"):
        n = node(snap, name)
        assert n["online"] is False and n["status"] == "OFFLINE"
        assert n["slots"][0]["state"] == "offline" and not n["slots"][0]["selectable"]
    assert snap["totals"]["online"] == 1 and snap["totals"]["offline"] == 2
    assert snap["totals"]["available"] == 2 and snap["ready"] is True


def test_blank_hosts_are_hidden_and_names_are_quoted(db):
    add_node(db, "a b", 1, 1, [(1, None, 0)])
    with sqlite3.connect(db) as con:
        cur = con.execute("INSERT INTO portStatus (PortNumber, PortServer) VALUES (3000, '')")
        con.execute("INSERT INTO portUsage (id, portNumber) VALUES (?, 3000)", (cur.lastrowid,))
    snap = store.snapshot()
    assert [n["host"] for n in snap["nodes"]] == ["a b"]
    assert snap["nodes"][0]["host"] == "a b" and snap["connect_url"] == "ws://hub:4500/connect"


def test_release_writes_a_utc_timestamp(db):
    add_node(db, "HOST1", 1, 1, [(1, 600, 0)])
    slot_id = store.snapshot()["sessions"][0]["slot_id"]
    assert store.release_slot(slot_id) == 1
    (in_use, last_run), = run(db, "SELECT inUse, LastRunDate FROM portUsage WHERE id = ?", (slot_id,))
    assert in_use == 0
    assert abs((utcnow() - store._parse_time(last_run)).total_seconds()) < 10


def test_forget_node(db):
    add_node(db, "live", 1, 1, [(1, None, 0)])
    add_node(db, "busy", 0, 900, [(1, 60, 0), (1, None, 0)])
    add_node(db, "gone", 0, 900, [(1, None, 0), (1, None, 2)])
    assert store.forget_node("live") == ("online", 0)
    assert store.forget_node("busy") == ("in_use", 1)
    assert store.forget_node("nope") == ("not_found", 0)
    assert store.forget_node("gone") == ("forgotten", 2)
    assert run(db, "SELECT COUNT(*) FROM nodes WHERE name = 'gone'") == [(0,)]
    assert run(db, "SELECT COUNT(*) FROM portStatus WHERE PortServer = 'gone'") == [(0,)]
    assert run(db, "SELECT COUNT(*) FROM portUsage") == [(3,)]  # live 1 + busy 2; gone's rows are deleted too
    assert {n["host"] for n in store.snapshot()["nodes"]} == {"live", "busy"}


def test_schema_file_splits_into_statements():
    with open(SCHEMA_SQL) as f:
        statements = [s for s in store._schema_statements(f.read()) if "CREATE TABLE" in s]
    assert len(statements) == 5


def test_api(db, monkeypatch):
    client = grid_app.app.test_client()
    assert client.get("/api/status").get_json()["db"]["initialised"] is False

    add_node(db, "live", 1, 1, [(1, None, 0)])
    add_node(db, "gone", 0, 900, [(1, None, 0)])
    status = client.get("/status").get_json()
    for key in ("generated", "fail_threshold", "stale_after_s", "totals", "nodes", "sessions", "ready"):
        assert key in status
    assert client.post("/api/nodes/forget", json={"host": "live"}).status_code == 409
    assert client.post("/api/nodes/forget", json={}).status_code == 400
    assert client.post("/api/nodes/forget", json={"host": "nope"}).status_code == 404

    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    assert client.post("/api/nodes/forget", json={"host": "gone"}).status_code == 403
    assert client.post("/api/nodes/forget", json={"host": "gone"}, headers={"X-Grid-Token": "wrongé"}).status_code == 403
    r = client.post("/api/nodes/forget", json={"host": "gone"}, headers={"X-Grid-Token": "s3cret"})
    assert r.status_code == 200 and r.get_json()["forgotten"] is True

    monkeypatch.setattr(store, "SQLITE_PATH", db + ".missing")
    r = client.get("/api/status")
    assert r.status_code == 503 and "error" in r.get_json()


def test_ensure_schema_runs_each_statement_once_on_mysql(monkeypatch):
    import contextlib

    executed = []

    class Cursor:
        def execute(self, sql):
            executed.append(sql)

    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "SCHEMA_PATH", SCHEMA_SQL)
    monkeypatch.setattr(store, "_schema_attempted", False)
    monkeypatch.setattr(store, "_session", contextlib.contextmanager(lambda: (yield Cursor())))
    store.ensure_schema()
    store.ensure_schema()
    assert [s.split("(")[0].split()[-1] for s in executed] == ["portStatus", "portUsage", "nodes", "accessTokens", "hubs"]
    assert not any(line.strip().startswith("--") for s in executed for line in s.splitlines())


def set_last_run(path, name, number, seconds_ago):
    run(path, "UPDATE portUsage SET LastRunDate = ? WHERE id = "
              "(SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?)", (ts(seconds_ago), name, number))


def test_fail_settings_match_the_checks():
    assert store._fail_settings({}) == (3, 300)
    assert store._fail_settings({"GRID_FAIL_THRESHOLD": "4"}) == (4, 300)
    assert store._fail_settings({"SLOT_FAIL_THRESHOLD": "5", "GRID_FAIL_THRESHOLD": "4", "SLOT_FAIL_COOLDOWN_S": "60"}) == (5, 60)


def test_failing_slots_are_skipped_until_their_cooldown_ends(db, monkeypatch):
    monkeypatch.setattr(store, "FAIL_THRESHOLD", 3)
    monkeypatch.setattr(store, "FAIL_COOLDOWN_S", 300)
    add_node(db, "gw", 1, 1, [(1, None, 3), (1, None, 3), (1, None, 3), (1, None, 2), (1, 30, 7)])
    set_last_run(db, "gw", 1, 60)   # failed a minute ago: cooling down
    set_last_run(db, "gw", 2, 600)  # cooldown over: gets another chance
    set_last_run(db, "gw", 4, 10)   # below the threshold: no cooldown
    snap = store.snapshot()
    slots = node(snap, "gw")["slots"]
    assert [s["state"] for s in slots] == ["failing", "failing", "failing", "free", "busy"]
    assert [s["selectable"] for s in slots] == [False, True, True, True, False]
    retry = store._parse_time(slots[0]["retry_at"])
    assert 230 <= (retry - utcnow()).total_seconds() <= 250
    assert [s["retry_at"] for s in slots[1:]] == [None] * 4
    assert snap["fail_cooldown_s"] == 300 and snap["totals"]["available"] == 3

    # Every free slot cooling down: nothing can take a check.
    run(db, "UPDATE portUsage SET failCount = 3, inUse = 0, LastRunDate = ?", (ts(5),))
    snap = store.snapshot()
    assert snap["totals"]["available"] == 0 and snap["ready"] is False


def test_ephemeral_nodes_and_older_nodes_tables(db):
    add_node(db, "runner-1", 0, 900, [(1, None, 0)])
    add_node(db, "WIN-1", 1, 1, [(1, None, 0)])
    run(db, "UPDATE nodes SET ephemeral = 1 WHERE name = 'runner-1'")
    snap = store.snapshot()
    assert node(snap, "runner-1")["ephemeral"] is True and node(snap, "WIN-1")["ephemeral"] is False

    run(db, "ALTER TABLE nodes DROP COLUMN ephemeral")  # a database from before the column
    snap = store.snapshot()
    assert [n["ephemeral"] for n in snap["nodes"]] == [False, False]
    assert node(snap, "WIN-1")["online"] is True


def test_mysql_matches_slots_to_nodes_ignoring_case(monkeypatch):
    """MySQL compares names case-insensitively, so the UI must group slots the way the checks do."""
    import contextlib

    now = utcnow()
    node_rows = [("Case-Node", 1, 1, "1.64.1", "1.0.0", "linux", "chromium", None, now, now, now, 0)]
    slot_rows = [(1, "Case-Node", 1, 1, 0, None, 0, None), (2, "case-node", 3000, 0, 0, None, 0, None)]
    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "_schema_attempted", True)
    monkeypatch.setattr(store, "_session", contextlib.contextmanager(lambda: (yield None)))
    monkeypatch.setattr(store, "_read", lambda cur: (now, node_rows, slot_rows, True, None))
    snap = store.snapshot()
    assert [(n["host"], n["kind"], n["total"]) for n in snap["nodes"]] == [("Case-Node", "gateway", 2)]
    assert [(s["state"], s["selectable"]) for s in snap["nodes"][0]["slots"]] == [("free", True), ("unhealthy", False)]
    assert snap["totals"]["available"] == 1

    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")  # sqlite's = is case-sensitive
    assert [n["host"] for n in store.snapshot()["nodes"]] == ["Case-Node", "case-node"]


def test_blank_host_rows_still_in_use_can_be_released(db):
    add_node(db, "gw", 1, 1, [(1, None, 0)])
    with sqlite3.connect(db) as con:
        for number, in_use in ((3000, 1), (3001, 0)):
            cur = con.execute("INSERT INTO portStatus (PortNumber, PortServer) VALUES (?, '')", (number,))
            con.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate) VALUES (?, ?, ?, ?)",
                        (cur.lastrowid, number, in_use, ts(90000)))
    snap = store.snapshot()
    assert [n["host"] for n in snap["nodes"]] == ["gw"]
    (legacy,) = snap["sessions"]
    assert legacy["kind"] == "legacy" and legacy["host"] == "" and legacy["port"] == 3000
    assert legacy["via_hub"] is False and legacy["stale"] is True
    assert store.release_slot(legacy["slot_id"]) == 1
    assert store.snapshot()["sessions"] == []


def test_permission_errors_are_not_reported_as_an_outage(db):
    import pymysql

    assert store._is_permission_error(pymysql.err.OperationalError(1142, "UPDATE command denied to user"))
    assert store._is_permission_error(pymysql.err.OperationalError(1044, "Access denied for user to database"))
    assert not store._is_permission_error(pymysql.err.OperationalError(2003, "Can't connect"))
    assert issubclass(store.DatabasePermissionDenied, store.DatabaseUnavailable)

    add_node(db, "gw", 1, 1, [(1, 60, 0)])
    os.chmod(db, 0o444)  # a read-only database: reads work, writes don't
    try:
        assert store.snapshot()["sessions"]
        with pytest.raises(store.DatabasePermissionDenied):
            store.release_slot(1)
        r = grid_app.app.test_client().post("/api/slots/1/release", headers={"X-Grid-Token": ""})
        assert r.status_code == 403
        assert "privileges" in r.get_json()["error"] and "token_required" not in r.get_json()
    finally:
        os.chmod(db, 0o666)


def test_actions_only_from_the_grid_page(db, monkeypatch):
    """No token set (the default), yet other sites can't release slots: no CSRF, no DNS rebinding."""
    add_node(db, "gw", 1, 1, [(1, 60, 0)])
    client = grid_app.app.test_client()
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())

    def busy():
        return run(db, "SELECT inUse FROM portUsage WHERE id = 1") == [(1,)]

    refused = [
        # a hidden cross-site form: no preflight, no custom header
        dict(data={"x": "1"}, headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}),
        dict(data={"x": "1"}),  # same, from a browser that sends neither header
        dict(json={}, headers={"Origin": "https://evil.example"}),
        dict(json={}, headers={"Origin": "null"}),
        dict(json={}, headers={"Sec-Fetch-Site": "same-site"}),
        # DNS rebinding: evil.example now resolves to the UI, so the page is "same-origin"
        dict(json={}, headers={"Origin": "http://evil.example:4444", "Sec-Fetch-Site": "same-origin"},
             base_url="http://evil.example:4444"),
    ]
    for kwargs in refused:
        r = client.post("/api/slots/1/release", **kwargs)
        assert r.status_code == 403, kwargs
        assert "token_required" not in r.get_json()
    assert busy()
    assert client.get("/api/status", base_url="http://evil.example:4444").status_code == 200  # reads are unchanged

    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", {"grid.example"})
    r = client.post("/api/slots/1/reset-failures", base_url="http://grid.example", json={},
                    headers={"Origin": "https://grid.example"})  # TLS proxy in front, Host passed through
    assert r.status_code == 200

    # What grid.js sends: same-origin, X-Grid-Token header (empty without a token), JSON body.
    for base in ("http://localhost:4444", "http://127.0.0.1:4444", "http://[::1]:4444", "http://10.1.2.3:4444"):
        r = client.post("/api/slots/1/release", base_url=base, json={},
                        headers={"Origin": base, "Sec-Fetch-Site": "same-origin", "X-Grid-Token": ""})
        assert r.status_code == 200, base
    assert not busy()

    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    r = client.post("/api/slots/1/release", json={}, headers={"X-Grid-Token": "nope"})
    assert r.status_code == 403 and r.get_json()["token_required"] is True


def test_checks_connect_to_one_address_and_no_slot_has_its_own(db):
    """The hub picks the slot: the API offers /connect and no per-slot address to copy."""
    add_node(db, "gw", 1, 1, [(1, None, 0), (1, 30, 0)])
    with sqlite3.connect(db) as con:
        cur = con.execute("INSERT INTO portStatus (PortNumber, PortServer) VALUES (3000, 'static1')")
        con.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate) VALUES (?, 3000, 1, ?)", (cur.lastrowid, ts(10)))
    snap = store.snapshot()
    assert snap["connect_url"] == "ws://hub:4500/connect"
    text = repr(snap)
    assert "/connect/" not in text and "ws://static1" not in text
    for n in snap["nodes"]:
        assert all("endpoint" not in s and "address" not in s for s in n["slots"])
    assert all("endpoint" not in s for s in snap["sessions"])


def test_claims_say_whether_the_hub_made_them(db):
    add_node(db, "gw", 1, 1, [(1, 30, 0), (1, 20, 0), (1, None, 0)])
    run(db, "UPDATE portUsage SET claimedBy = 'hub:5c1e09a2:7' WHERE id = 1")
    run(db, "UPDATE portUsage SET claimedBy = 'hub:5c1e09a2:9' WHERE id = 3")  # a leftover on a free slot: not a claim
    snap = store.snapshot()
    by_slot = {s["slot_id"]: s for s in snap["sessions"]}
    assert (by_slot[1]["via_hub"], by_slot[1]["claimed_by"]) == (True, "hub:5c1e09a2:7")
    assert (by_slot[2]["via_hub"], by_slot[2]["claimed_by"]) == (False, None)  # claimedBy NULL: some other claimer
    slots = {s["id"]: s for s in node(snap, "gw")["slots"]}
    assert [slots[i]["via_hub"] for i in (1, 2, 3)] == [True, False, None]


def test_a_database_the_hub_has_not_upgraded_reports_claims_as_unknown(db):
    with sqlite3.connect(db) as con:
        demo.create_schema(con)
        con.execute("ALTER TABLE portUsage DROP COLUMN claimedBy")
    add_node(db, "gw", 1, 1, [(1, 30, 0)])
    (session,) = store.snapshot()["sessions"]
    assert session["via_hub"] is None and session["claimed_by"] is None
    assert store.release_slot(session["slot_id"]) == 1  # releasing doesn't need the column either
    assert run(db, "SELECT inUse FROM portUsage") == [(0,)]


def test_a_force_release_clears_the_claim_with_the_slot(db):
    """Releasing frees the slot and clears claimedBy, as every release does. (A hub whose session still
    runs there claims it again; one that is gone left a claim nobody holds.)"""
    add_node(db, "gw", 1, 1, [(1, 30, 0), (1, 20, 0)])
    run(db, "UPDATE portUsage SET claimedBy = 'hub:5c1e09a2:' || id")
    assert store.release_slot(1) == 1
    started = next(s for s in store.snapshot()["sessions"] if s["slot_id"] == 2)["started"]
    assert store.release_claim(2, started) == ("released", 1)
    assert run(db, "SELECT inUse, claimedBy FROM portUsage ORDER BY id") == [(0, None), (0, None)]


def test_a_hub_session_is_stale_only_once_its_hub_is_gone(db, monkeypatch):
    """A /connect session may run for hours: while its hub renews its lease it is a running session (not a
    problem to release); once the hub has stopped renewing it, nothing holds the claim. Other claims keep
    the GRID_STALE_AFTER rule."""
    monkeypatch.setattr(store, "STALE_AFTER_S", 3600)
    monkeypatch.setattr(store, "HUB_LEASE_S", 90)
    add_node(db, "gw", 1, 1, [(1, 5400, 0), (1, 5400, 0), (1, 5400, 0), (1, 60, 0)])
    run(db, "UPDATE portUsage SET claimedBy = 'hub:aaaa0001:' || id WHERE id IN (1, 4)")
    run(db, "UPDATE portUsage SET claimedBy = 'hub:dead0002:2' WHERE id = 2")  # slot 3: claimedBy NULL, another claimer
    run(db, "INSERT INTO hubs (instance, host, port, lastSeen) VALUES ('aaaa0001', 'hub', 4500, ?), ('dead0002', 'old', 4500, ?)",
        (ts(10), ts(400)))
    snap = store.snapshot()
    got = {s["slot_id"]: (s["via_hub"], s["hub_alive"], s["stale"]) for s in snap["sessions"]}
    assert got == {1: (True, True, False), 2: (True, False, True), 3: (False, None, True), 4: (True, True, False)}
    assert snap["totals"]["stale"] == 2 and snap["hub_lease_s"] == 90
    # A hub with no row at all (it stopped and removed it, or crashed before leases existed) is gone too.
    run(db, "DELETE FROM hubs WHERE instance = 'aaaa0001'")
    got = {s["slot_id"]: (s["hub_alive"], s["stale"]) for s in store.snapshot()["sessions"]}
    assert got[1] == (False, True) and got[4] == (False, True)
    # A database without the hubs table (a hub from before leases): the age rule, as before.
    run(db, "DROP TABLE hubs")
    got = {s["slot_id"]: (s["hub_alive"], s["stale"]) for s in store.snapshot()["sessions"]}
    assert got == {1: (None, True), 2: (None, True), 3: (None, True), 4: (None, False)}


def test_the_demo_hub_renews_its_lease_so_its_sessions_are_running(tmp_path, monkeypatch):
    path = str(tmp_path / "demo.db")
    with sqlite3.connect(path) as con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    sessions = store.snapshot()["sessions"]
    assert all(s["hub_alive"] and not s["stale"] for s in sessions if s["via_hub"])
    assert [s["host"] for s in sessions if s["stale"]] == ["pw-static.lab.local"]  # the claim from outside the hub
