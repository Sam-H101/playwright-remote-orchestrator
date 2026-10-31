""" database.py against a real MySQL. Opt in with ORCHESTRATOR_TEST_DB=<database to wipe>, plus
DB_HOST/DB_PORT/DB_USER/DB_PASSWORD. Every table in that database is dropped. """
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, database  # noqa: E402

TEST_DB = os.environ.get("ORCHESTRATOR_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORCHESTRATOR_TEST_DB to run against MySQL")


@pytest.fixture
def make_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB or "")
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    monkeypatch.setattr(database, "SLOT_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(database, "SLOT_FAIL_COOLDOWN", 300)
    monkeypatch.setattr(database, "STALE_CLAIM_AFTER", 2100)
    made = []

    def make(hosts=""):
        path = tmp_path / "database.yaml"
        path.write_text("hosts:\n" + hosts)
        monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(path))
        db = database.database(config.config())
        if not made:
            with db.create_connection().cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS portUsage, portStatus, nodes")
        made.append(db)
        return db
    yield make
    for db in made:
        db.conn.close()


def sql(db, statement, args=None):
    with db.create_connection().cursor() as cur:
        cur.execute(statement, args)
        return cur.fetchall()


RUNNER = "  - {server: runner01, port_start: 3000, port_end: 3002}\n"


def add_node(db, name, slots):
    sql(db, "INSERT INTO nodes (name, online, slots, lastSeen) VALUES (%s, 1, %s, UTC_TIMESTAMP())", (name, slots))
    for slot in range(1, slots + 1):
        sql(db, "INSERT INTO portStatus (PortNumber, PortStatus, PortServer) VALUES (%s, 1, %s)", (slot, name))
    sql(db, "INSERT IGNORE INTO portUsage (id, portNumber) SELECT id, PortNumber FROM portStatus")


def slots(db):
    return sorted((server, port) for server, port in sql(db, "SELECT PortServer, PortNumber FROM portStatus"))


def test_seed_skip_and_prune(make_db):
    db = make_db(RUNNER + "  - {server: old, port_start: 3000, port_end: 3001}\n")
    db.initial_db_start()
    add_node(db, "tl-node", 2)
    assert slots(db) == [("old", 3000), ("runner01", 3000), ("runner01", 3001), ("tl-node", 1), ("tl-node", 2)]

    # old is removed from hosts:, runner01 shrinks, and a hosts: entry collides with the node
    db = make_db("  - {server: runner01, port_start: 3000, port_end: 3001}\n"
                 "  - {server: tl-node, port_start: 3000, port_end: 3003}\n")
    sql(db, "UPDATE portUsage SET inUse = 1 WHERE id = (SELECT id FROM portStatus WHERE PortServer = 'old')")
    db.initial_db_start()
    assert slots(db) == [("old", 3000), ("runner01", 3000), ("tl-node", 1), ("tl-node", 2)]  # old is busy
    sql(db, "UPDATE portUsage SET inUse = 0")
    db.initial_db_start()
    db.initial_db_start()
    assert slots(db) == [("runner01", 3000), ("tl-node", 1), ("tl-node", 2)]


def test_failing_slot_gets_one_retry_per_cooldown(make_db):
    db = make_db(RUNNER)
    db.initial_db_start()
    sql(db, "UPDATE portUsage SET failCount = 3, LastRunDate = UTC_TIMESTAMP() - INTERVAL 10 SECOND"
            " WHERE portNumber = 3000")
    assert [s.port for s in db.check_ports_not_in_use()] == [3001]
    sql(db, "UPDATE portUsage SET failCount = 2 WHERE portNumber = 3000")
    assert sorted(s.port for s in db.check_ports_not_in_use()) == [3000, 3001]
    sql(db, "UPDATE portUsage SET failCount = 7, LastRunDate = UTC_TIMESTAMP() - INTERVAL 301 SECOND"
            " WHERE portNumber = 3000")
    assert sorted(s.port for s in db.check_ports_not_in_use()) == [3000, 3001]
    slot = [s for s in db.check_ports_not_in_use() if s.port == 3000][0]
    assert db.set_in_use_port(slot, slot.id)
    db.release_port(slot)  # still failing: the next retry waits for the cooldown again
    assert [s.port for s in db.check_ports_not_in_use()] == [3001]
    db.reset_failure(slot)
    assert sorted(s.port for s in db.check_ports_not_in_use()) == [3000, 3001]


def test_gateway_slot_failing_is_skipped(make_db):
    db = make_db()
    db.initial_db_start()
    add_node(db, "node1", 1)
    assert len(db.check_ports_not_in_use()) == 1
    sql(db, "UPDATE portUsage SET failCount = 3, LastRunDate = UTC_TIMESTAMP()")
    assert db.check_ports_not_in_use() == []


def test_stale_claims_are_reclaimed(make_db):
    db = make_db(RUNNER)
    db.initial_db_start()
    sql(db, "UPDATE portUsage SET inUse = 1, LastRunDate = UTC_TIMESTAMP() - INTERVAL 2101 SECOND"
            " WHERE portNumber = 3000")
    sql(db, "UPDATE portUsage SET inUse = 1, LastRunDate = UTC_TIMESTAMP() - INTERVAL 60 SECOND"
            " WHERE portNumber = 3001")
    slot = db.get_a_port()
    assert slot.port == 3000
    assert sql(db, "SELECT portNumber, inUse FROM portUsage ORDER BY portNumber") == ((3000, 1), (3001, 1))
