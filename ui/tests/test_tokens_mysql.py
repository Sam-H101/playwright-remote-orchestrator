"""Managed access tokens: store.py against a real MySQL. Opt in with ORCHESTRATOR_TEST_DB=<database to
wipe>, plus DB_HOST/DB_PORT/DB_USER/DB_PASSWORD. The orchestrator's tables in that database are dropped."""
import datetime
import hashlib
import os

import pytest

from ui import app as grid_app, store

TEST_DB = os.environ.get("ORCHESTRATOR_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORCHESTRATOR_TEST_DB to run against MySQL")

# The tables as they were before managed tokens: no accessTokens, no nodes.tokenId.
OLD_SCHEMA = [
    """CREATE TABLE portStatus (id INT AUTO_INCREMENT PRIMARY KEY, PortNumber INT NOT NULL,
       PortStatus INT NOT NULL DEFAULT 0, PortServer VARCHAR(255) NOT NULL, LastUpdateTime DATETIME NULL,
       UNIQUE KEY uq_server_port (PortServer, PortNumber))""",
    """CREATE TABLE portUsage (id INT PRIMARY KEY, portNumber INT NOT NULL, inUse TINYINT NOT NULL DEFAULT 0,
       LastRunDate DATETIME NULL, failCount INT NOT NULL DEFAULT 0,
       CONSTRAINT fk_port_usage_status FOREIGN KEY (id) REFERENCES portStatus (id) ON DELETE CASCADE)""",
    """CREATE TABLE nodes (name VARCHAR(255) NOT NULL PRIMARY KEY, online TINYINT NOT NULL DEFAULT 0,
       slots INT NOT NULL DEFAULT 0, playwrightVersion VARCHAR(32) NULL, agentVersion VARCHAR(32) NULL,
       platform VARCHAR(64) NULL, browsers VARCHAR(255) NULL, remoteAddress VARCHAR(255) NULL,
       ephemeral TINYINT NOT NULL DEFAULT 0, registeredAt DATETIME NULL, connectedAt DATETIME NULL,
       lastSeen DATETIME NULL)""",
]


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sql(statement, args=None):
    with store._session() as cur:
        cur.execute(statement, args)
        return cur.fetchall()


@pytest.fixture
def mysql(monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB or "")
    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "_schema_attempted", False)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    sql("DROP TABLE IF EXISTS portUsage, portStatus, nodes, accessTokens")


def add_node(name, token_id, online=1):
    sql("INSERT INTO nodes (name, online, slots, lastSeen, tokenId) VALUES (%s, %s, 1, UTC_TIMESTAMP(), %s)",
        (name, online, token_id))


def test_tokens_round_trip_on_a_new_database(mysql):
    listing = store.list_tokens()  # runs the schema: the tables don't exist yet
    assert listing == {"ready": True, "generated": listing["generated"], "tokens": []}
    assert "tokenid" in {c.lower() for (c,) in sql(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'nodes'")}

    agent = store.create_token("Build farm", "agent", "Win-07", 30)
    client = store.create_token("Checks", "client")
    ((stored_hash, created, expires),) = sql("SELECT tokenHash, createdAt, expiresAt FROM accessTokens WHERE id = %s", (agent["id"],))
    assert stored_hash == hashlib.sha256(agent["token"].encode()).hexdigest()
    assert abs((created - utcnow()).total_seconds()) < 10  # UTC, although the server's zone isn't
    assert expires - created == datetime.timedelta(days=30)
    assert not sql("SELECT 1 FROM accessTokens WHERE tokenHash = %s OR prefix = %s OR name = %s",
                   (agent["token"], agent["token"], agent["token"]))

    add_node("win-07", agent["id"])
    add_node("old", agent["id"], online=0)
    tokens = {t["name"]: t for t in store.list_tokens()["tokens"]}
    assert tokens["Build farm"]["nodes"] == ["old", "win-07"] and tokens["Build farm"]["nodesOnline"] == ["win-07"]
    assert tokens["Build farm"]["nodeName"] == "win-07" and tokens["Checks"]["status"] == "active"
    assert tokens["Build farm"]["createdAt"].endswith("Z")

    assert store.delete_token(agent["id"]) == "active"
    assert store.revoke_token(agent["id"])["status"] == "revoked"
    first = sql("SELECT revokedAt FROM accessTokens WHERE id = %s", (agent["id"],))
    assert store.revoke_token(agent["id"])["status"] == "revoked"
    assert sql("SELECT revokedAt FROM accessTokens WHERE id = %s", (agent["id"],)) == first
    assert store.delete_token(agent["id"]) == "deleted"
    assert sql("SELECT name, tokenId FROM nodes ORDER BY name") == (("old", None), ("win-07", None))
    assert store.delete_token(agent["id"]) == "not_found" and store.revoke_token(agent["id"]) is None
    assert [t["name"] for t in store.list_tokens()["tokens"]] == ["Checks"]
    for big in (2 ** 31, 10 ** 20):  # the same answer as the sqlite demo gives
        assert store.revoke_token(big) is None and store.delete_token(big) == "not_found"


def test_expiry_is_compared_in_utc(mysql):
    store.ensure_schema()
    soon = store.create_token("soon", "agent", expires_in_days=1)
    gone = store.create_token("gone", "agent", expires_in_days=1)
    # A minute either side of now, in UTC: read as local (EDT) time, both would be wrong by hours.
    sql("UPDATE accessTokens SET expiresAt = UTC_TIMESTAMP() + INTERVAL 1 MINUTE WHERE id = %s", (soon["id"],))
    sql("UPDATE accessTokens SET expiresAt = UTC_TIMESTAMP() - INTERVAL 1 MINUTE WHERE id = %s", (gone["id"],))
    assert {t["name"]: t["status"] for t in store.list_tokens()["tokens"]} == {"soon": "active", "gone": "expired"}
    assert store.delete_token(soon["id"]) == "active"
    assert store.delete_token(gone["id"]) == "deleted"


def test_a_database_from_before_managed_tokens(mysql, monkeypatch):
    for statement in OLD_SCHEMA:
        sql(statement)
    sql("INSERT INTO nodes (name, online, slots, lastSeen) VALUES ('n1', 1, 1, UTC_TIMESTAMP())")

    # The schema can't be applied (say the UI's user has no CREATE privilege): an empty state.
    monkeypatch.setattr(store, "_schema_attempted", True)
    assert store.list_tokens()["ready"] is False
    with pytest.raises(store.TokensUnavailable):
        store.create_token("t", "agent")
    assert store.revoke_token(1) is None and store.delete_token(1) == "not_found"
    assert [n["host"] for n in store.snapshot()["nodes"]] == ["n1"]
    client = grid_app.app.test_client()
    assert client.get("/api/tokens", headers={"X-Grid-Token": "s3cret"}).get_json()["ready"] is False
    assert client.post("/api/tokens", json={"name": "t", "kind": "agent"}, headers={"X-Grid-Token": "s3cret"}).status_code == 503

    # It can: the schema adds accessTokens. nodes keeps its old columns until the hub adds tokenId.
    monkeypatch.setattr(store, "_schema_attempted", False)
    listed = store.list_tokens()  # one call: a second one can fall in the next second of "generated"
    assert listed == {"ready": True, "generated": listed["generated"], "tokens": []}
    made = store.create_token("t", "agent")
    assert store.list_tokens()["tokens"][0]["nodes"] == []
    store.revoke_token(made["id"])
    assert store.delete_token(made["id"]) == "deleted"

    sql("ALTER TABLE nodes ADD COLUMN tokenId INT NULL")  # what the hub does
    made = store.create_token("t2", "agent")
    sql("UPDATE nodes SET tokenId = %s", (made["id"],))
    assert store.list_tokens()["tokens"][0]["nodes"] == ["n1"]

    r = client.post("/api/tokens", json={"name": "api", "kind": "client", "expiresInDays": 3}, headers={"X-Grid-Token": "s3cret"})
    assert r.status_code == 201 and r.get_json()["token"].startswith("pwgc_")
