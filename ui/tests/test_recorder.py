"""The server-side flight recorder: diff(), the ring buffer, recording with no page open, admin
attribution, the guarded force release, the guessing limit and what a database error discloses."""
import datetime
import sqlite3

import pytest

from ui import app as grid_app, auth, demo, recorder, store


def utc(seconds=0):
    t = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def run(path, sql, params=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, params).fetchall()


FREE = [("WIN-BUILD-01", 2), ("WIN-BUILD-01", 4), ("runner-7f3a9c", 3), ("runner-7f3a9c", 4), ("runner-7f3a9c", 6),
        ("runner-b81e04", 4), ("pw-static.lab.local", 3002)]


def free_ids(path):
    return [run(path, "SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?", s)[0][0] for s in FREE]


@pytest.fixture
def grid(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with sqlite3.connect(path) as con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(store, "FAIL_THRESHOLD", 3)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    # A recorder of its own, without the background thread: the tests drive it.
    monkeypatch.setattr(grid_app, "RECORDER", recorder.Recorder(store.snapshot, store.db_identity, interval_s=0))
    return grid_app.app.test_client(), path


def slot(sid, **kw):
    s = {"id": sid, "port": sid, "busy": False, "fails": 0, "healthy": True, "state": "free", "last_run": None, "retry_at": None}
    s.update(kw)
    return s


def node(host, slots, **kw):
    n = {"host": host, "kind": "gateway", "online": True, "total": len(slots), "slots": slots, "last_seen": "2026-10-31T09:59:59Z"}
    n.update(kw)
    return n


def snap(gen, nodes):
    return {"generated": gen, "nodes": nodes}


T, T4 = "2026-10-31T10:00:00Z", "2026-10-31T10:00:04Z"
G0, G1 = "2026-10-31T10:00:01Z", "2026-10-31T10:00:06Z"


def test_diff_names_every_change_with_an_id_made_of_database_values():
    claim = recorder.diff(snap(G0, [node("n", [slot(1)])]), snap(G1, [node("n", [slot(1, busy=True, state="busy", last_run=T)])]))
    assert [(e["type"], e["id"], e["subject"]) for e in claim] == [("claimed", "c:1:" + str(recorder.ms(T)), "n / 1")]

    fin = recorder.diff(snap(G0, [node("n", [slot(1, busy=True, state="busy", last_run=T)])]), snap(G1, [node("n", [slot(1, last_run=T4)])]))
    assert [(e["type"], e["dur"], e.get("approx"), e["id"]) for e in fin] == [("finished", 4000, None, "f:1:" + str(recorder.ms(T4)))]

    between = recorder.diff(snap(G0, [node("n", [slot(1, last_run=T)])]), snap(G1, [node("n", [slot(1, last_run=T4)])]))
    # The same run seen exactly by one process and only approximately by another has the same id.
    assert [(e["type"], e["approx"], e["start"], e["id"]) for e in between] == [("finished", True, None, fin[0]["id"])]

    # Released and claimed again between snapshots (the hub hands a freed slot to a waiting client at
    # once): the run's start is known and it ended about when the next began, so it has a length.
    again = recorder.diff(snap(G0, [node("n", [slot(1, busy=True, state="busy", last_run=T)])]),
                          snap(G1, [node("n", [slot(1, busy=True, state="busy", last_run=T4)])]))
    assert [(e["type"], e.get("approx"), e.get("dur"), e["id"]) for e in again] == \
        [("finished", True, 4000, fin[0]["id"]), ("claimed", None, None, "c:1:" + str(recorder.ms(T4)))]

    failed = recorder.diff(snap(G0, [node("n", [slot(1, busy=True, state="busy", last_run=T, fails=2)])]),
                           snap(G1, [node("n", [slot(1, last_run=T4, fails=3, state="failing")])]))
    assert [(e["type"], e.get("failed"), e.get("nowFailing")) for e in failed] == [("finished", True, None), ("failure", None, True)]

    reset = recorder.diff(snap(G0, [node("n", [slot(1, fails=4, state="failing")])]), snap(G1, [node("n", [slot(1)])]))
    assert [(e["type"], e["was"]) for e in reset] == [("reset", 4)]

    off = recorder.diff(snap(G0, [node("n", [slot(1), slot(2)])]),
                        snap(G1, [node("n", [slot(1, state="offline"), slot(2, state="offline")], online=False, last_seen_s=70)]))
    assert [e["type"] for e in off] == ["node-offline"]  # the node says it; no line per slot

    nodes = recorder.diff(snap(G0, [node("a", [slot(1)]), node("gone", [slot(2)], online=False)]),
                          snap(G1, [node("a", [slot(1)]), node("new", [slot(3)], registered_at="2026-10-31T10:00:05Z")]))
    assert [(e["type"], e["id"]) for e in nodes] == [("node-joined", "nj:new:2026-10-31T10:00:05Z"), ("node-left", "nl:gone:2026-10-31T09:59:59Z")]

    beat = recorder.diff(snap(G0, [node("n", [slot(1)], last_seen_s=1)]), snap(G1, [node("n", [slot(1)], last_seen_s=6)]))
    assert beat == [] and recorder.diff(None, snap(G1, [])) == []


def test_claims_record_whether_the_hub_made_them():
    """portUsage.claimedBy: the event says hub or not, and keeps the id the page computes."""
    for via_hub, want in ((True, {"hub": True}), (False, {"hub": False}), (None, {})):
        (claim,) = recorder.diff(snap(G0, [node("n", [slot(1)])]),
                                 snap(G1, [node("n", [slot(1, busy=True, state="busy", last_run=T, via_hub=via_hub)])]))
        assert claim["id"] == "c:1:" + str(recorder.ms(T))
        assert {k: v for k, v in claim.items() if k == "hub"} == want
    again = recorder.diff(snap(G0, [node("n", [slot(1, busy=True, state="busy", last_run=T, via_hub=True)])]),
                          snap(G1, [node("n", [slot(1, busy=True, state="busy", last_run=T4, via_hub=False)])]))
    assert [(e["type"], e.get("hub")) for e in again] == [("finished", None), ("claimed", False)]


def test_the_ring_merges_by_id_keeps_15_minutes_and_at_most_500_events():
    snaps = []
    rec = recorder.Recorder(lambda: snaps[-1], lambda: "db", interval_s=0)
    base = recorder.ms("2026-10-31T10:00:00Z")

    def iso(ms_value):
        return datetime.datetime.fromtimestamp(ms_value / 1000, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    snaps.append(snap(iso(base), [node("n", [slot(i) for i in range(1, 601)])]))
    rec.observe()
    snaps.append(snap(iso(base + 5000), [node("n", [slot(i, last_run=iso(base + 4000)) for i in range(1, 601)])]))
    rec.observe()
    out = rec.export(0)
    assert len(out["events"]) == 500 and out["recorder"]["dropped"] == 100
    assert rec.export(base + 6000)["events"] == []  # nothing observed since that cursor

    rec.observe()  # the same snapshot again: no new events, no duplicates
    assert len(rec.export(0)["events"]) == 500
    snaps.append(snap(iso(base + 20 * 60 * 1000), [node("n", [slot(i, last_run=iso(base + 4000)) for i in range(1, 601)])]))
    rec.observe()
    assert rec.export(0)["events"] == []  # older than 15 minutes

    snaps.append(snap(iso(base), []))
    rec._identity = lambda: "another db"
    rec.observe()
    assert rec.export(0)["recorder"]["since"] == base  # a different database: a new recording


def test_short_runs_are_recorded_with_no_page_open_and_a_late_tab_sees_them(grid):
    client, path = grid
    rec = grid_app.RECORDER
    rec.interval_s = 0.01
    assert rec.tick()  # the background step: a first snapshot is the baseline
    ids = free_ids(path)
    marks = ",".join("?" * len(ids))
    run(path, "UPDATE portUsage SET inUse = 1, LastRunDate = ? WHERE id IN ({})".format(marks), [utc()] + ids)
    run(path, "UPDATE portUsage SET inUse = 0, LastRunDate = ? WHERE id IN ({})".format(marks), [utc(4)] + ids)
    import time
    time.sleep(0.02)
    assert rec.tick()
    # A tab opened now asks for everything: the seven runs are there.
    body = client.get("/api/status?soft=1&events=0").get_json()
    finished = [e for e in body["events"] if e["type"] == "finished"]
    assert len(finished) == 7 and all(e["approx"] for e in finished)
    assert body["recorder"]["since"] and body["recorder"]["samples"] and body["poll_ms"] >= 1000
    # The plain route is unchanged: no events unless asked for.
    assert "events" not in client.get("/api/status").get_json()
    r = client.get("/api/events?since=0")
    assert r.headers["Cache-Control"] == "no-store" and len([e for e in r.get_json()["events"] if e["type"] == "finished"]) == 7


def test_a_force_release_only_ends_the_claim_the_confirm_described(grid, monkeypatch):
    client, path = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    admin = {"X-Grid-Token": "s3cret"}
    status = client.get("/api/status").get_json()
    stale = next(s for s in status["sessions"] if s["stale"])
    sid = stale["slot_id"]

    # The check finished and another claimed the slot while the confirm was open. Its claim is stamped a
    # few seconds back, so the old run's end and the admin's release below never share a second (and an
    # event id) however the test is timed.
    run(path, "UPDATE portUsage SET LastRunDate = ? WHERE id = ?", (utc(-5), sid))
    r = client.post("/api/slots/{}/release".format(sid), json={"claimed_at": stale["started"]}, headers=admin)
    assert r.status_code == 409 and r.get_json()["reclaimed"] is True
    assert run(path, "SELECT inUse FROM portUsage WHERE id = ?", (sid,)) == [(1,)]

    current = next(s for s in client.get("/api/status").get_json()["sessions"] if s["slot_id"] == sid)
    r = client.post("/api/slots/{}/release".format(sid), json={"claimed_at": current["started"]}, headers=admin)
    assert r.status_code == 200 and r.get_json()["updated"] == 1
    assert run(path, "SELECT inUse FROM portUsage WHERE id = ?", (sid,)) == [(0,)]
    # One event for the action, attributed to who signed in, with the id the page will see.
    events = client.get("/api/status?events=0").get_json()["events"]
    mine = [e for e in events if e["id"] in r.get_json()["events"]]
    assert len(mine) == 1 and mine[0]["type"] == "finished" and mine[0]["by"] == "Admin" and mine[0]["kind"] == "admin"
    # Two runs ended: the stale one, when the slot was claimed again (seen between snapshots, so approx.,
    # with the length it had), and the one the admin released.
    finished = sorted((e for e in events if e.get("slot") == sid and e["type"] == "finished"), key=lambda e: e["end"])
    assert [(e["kind"], bool(e.get("approx"))) for e in finished] == [("finish", True), ("admin", False)]
    assert finished[0]["dur"] == finished[0]["end"] - finished[0]["start"] > 0
    assert finished[1]["start"] == finished[0]["end"]

    r = client.post("/api/slots/{}/release".format(sid), json={"claimed_at": current["started"]}, headers=admin)
    assert r.status_code == 409 and "already finished" in r.get_json()["error"]
    assert client.post("/api/slots/999999/release", json={"claimed_at": "x"}, headers=admin).status_code == 404
    # Without claimed_at the route answers as it always has.
    assert client.post("/api/slots/{}/release".format(sid), json={}, headers=admin).get_json()["updated"] == 1


def test_reset_and_forget_are_attributed_once(grid):
    client, path = grid
    status = client.get("/api/status").get_json()
    failing = next(s for n in status["nodes"] for s in n["slots"] if s["fails"] >= 3)
    r = client.post("/api/slots/{}/reset-failures".format(failing["id"]), json={})
    assert r.status_code == 200 and len(r.get_json()["events"]) == 1
    offline = next(n for n in status["nodes"] if n["kind"] == "gateway" and not n["online"])
    r = client.post("/api/nodes/forget", json={"host": offline["host"]})
    assert r.get_json()["forgotten"] is True and len(r.get_json()["events"]) == 1
    events = client.get("/api/status?events=0").get_json()["events"]
    left = [e for e in events if e["type"] == "node-left"]
    assert len(left) == 1 and left[0]["by"] == "" and left[0]["kind"] == "admin"  # open access: nobody signed in


def test_wrong_admin_tokens_are_limited_per_address(grid, monkeypatch):
    client, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    for _ in range(auth.FAILURE_LIMIT):
        r = client.post("/api/slots/1/reset-failures", json={}, headers={"X-Grid-Token": "guess"})
        assert r.status_code == 403 and r.get_json()["token_required"] is True
    # An empty header (a signed-out page proving it's same-origin) is not a guess.
    assert client.post("/api/slots/1/reset-failures", json={}, headers={"X-Grid-Token": ""}).status_code == 403
    for headers in ({"X-Grid-Token": "guess"}, {"X-Grid-Token": "s3cret"}):
        r = client.post("/api/slots/1/reset-failures", json={}, headers=headers)
        assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0 and "token_required" not in r.get_json()
    assert client.get("/api/session", headers={"X-Grid-Token": "s3cret"}).status_code == 429
    auth.reset_failures()
    assert client.post("/api/slots/1/reset-failures", json={}, headers={"X-Grid-Token": "s3cret"}).status_code == 200


def test_a_database_error_names_paths_only_to_a_signed_in_admin(grid, monkeypatch, tmp_path):
    client, _ = grid
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))  # a directory: every query fails
    for url in ("/api/status?soft=1", "/api/status"):
        text = client.get(url).get_json()["error"]
        assert text.startswith("OperationalError") and str(tmp_path) not in text, text
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    text = client.get("/api/status?soft=1", headers={"X-Grid-Token": "s3cret"}).get_json()["error"]
    assert str(tmp_path) in text
