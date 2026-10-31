import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config, database  # noqa: E402


def write_config(tmp_path, monkeypatch, text):
    path = tmp_path / "database.yaml"
    path.write_text(text)
    monkeypatch.setenv("ORCHESTRATOR_CONFIG", str(path))
    return config.config()


class FakeSlots:
    """ in-memory portUsage that understands the statements database.py sends.
    `ago` is how many seconds ago LastRunDate was (None for NULL). """

    def __init__(self, rows, nodes=()):
        self.rows = {r[1]: dict(port=r[0], server=r[2], gateway=r[3], inUse=0, failCount=0, ago=None)
                     for r in rows}
        self.nodes = set(nodes)
        self.executed = []

    def cursor(self):
        return FakeCursor(self)

    def ping(self, reconnect=False):
        pass


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def executemany(self, sql, rows):
        rows = list(rows)
        self.db.executed.append((sql, rows))
        if "DELETE FROM portStatus WHERE id = %s" in sql:
            for (id,) in rows:
                if not self.db.rows[id]["inUse"]:
                    del self.db.rows[id]

    def execute(self, sql, args=None):
        self.db.executed.append((sql, args))
        rows = self.db.rows
        if sql == database.RECLAIM_STALE_SQL:
            stale = [r for r in rows.values() if r["inUse"] and r["ago"] is not None and r["ago"] > args[0]
                     and not (r.get("claimedBy") or "").startswith("hub:")]
            for r in stale:
                r.update(inUse=0, claimedBy=None)
            return len(stale)
        if sql == "SELECT name FROM nodes":
            self.result = [(name,) for name in sorted(self.db.nodes)]
            return len(self.result)
        if "SELECT ps.id, ps.PortServer, ps.PortNumber" in sql:
            self.result = [(id, r["server"], r["port"]) for id, r in rows.items()
                           if r["server"] not in self.db.nodes and not r["inUse"] and r["server"]]
            return len(self.result)
        return 0

    def fetchall(self):
        return self.result


@pytest.fixture
def make_db(tmp_path, monkeypatch):
    def make(rows, yaml_text="hosts: []\n", nodes=()):
        db = database.database(write_config(tmp_path, monkeypatch, yaml_text))
        db.settings.update(host="mysql", user="u", password="p", database="d")
        db.conn = FakeSlots(rows, nodes)
        return db
    return make


@pytest.fixture
def no_gateway_env(monkeypatch):
    for name in ("GATEWAY_URL", "GATEWAY_CLIENT_TOKEN", "GATEWAY_NODE"):
        monkeypatch.delenv(name, raising=False)


def test_gateway_settings_from_env(tmp_path, monkeypatch, no_gateway_env):
    cfg = write_config(tmp_path, monkeypatch, "hosts: []\n")
    assert cfg.get_gateway_config() == {"url": "ws://hub:4500", "client_token": None, "node": None}
    monkeypatch.setenv("GATEWAY_URL", "ws://10.0.0.5:4500/")
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "tok")
    monkeypatch.setenv("GATEWAY_NODE", " Runner-A ")
    assert cfg.get_gateway_config() == {"url": "ws://10.0.0.5:4500", "client_token": "tok", "node": "Runner-A"}


def test_gateway_settings_from_the_config_file(tmp_path, monkeypatch, no_gateway_env):
    cfg = write_config(tmp_path, monkeypatch, "gateway:\n  url: wss://grid.lab/\n  client_token: t\n  node: win-01\n")
    assert cfg.get_gateway_config() == {"url": "wss://grid.lab", "client_token": "t", "node": "win-01"}
    monkeypatch.setenv("GATEWAY_NODE", "other")
    assert cfg.get_gateway_config()["node"] == "other"


@pytest.mark.parametrize("url, token, node, endpoint", [
    ("ws://hub:4500", None, None, "ws://hub:4500/connect"),
    ("ws://hub:4500/", "t", None, "ws://hub:4500/connect?gateway_token=t"),
    ("wss://gw.example.com", "s3cr&t=/?", None, "wss://gw.example.com/connect?gateway_token=s3cr%26t%3D%2F%3F"),
    ("ws://192.168.112.184:4500", "pwgc_x", "Runner-7",
     "ws://192.168.112.184:4500/connect?gateway_token=pwgc_x&node=Runner-7"),
    ("ws://hub:4500", None, "a.b_c-d", "ws://hub:4500/connect?node=a.b_c-d"),
    # a GATEWAY_URL copied from a client snippet
    ("ws://hub:4500/connect", "t", None, "ws://hub:4500/connect?gateway_token=t"),
    ("ws://hub:4500/connect/", None, None, "ws://hub:4500/connect"),
])
def test_client_endpoint_is_the_hubs_single_connect_url(url, token, node, endpoint):
    assert config.client_endpoint(url, token, node) == endpoint


@pytest.mark.parametrize("node", ["runner a", "a/3", "x" * 129, "n\u00f6de"])
def test_client_endpoint_refuses_a_bad_node_name(node):
    with pytest.raises(ValueError, match="GATEWAY_NODE must be a node name"):
        config.client_endpoint("ws://hub:4500", "t", node)


@pytest.mark.parametrize("url", ["ws://hub:4500/connect/runner-7f3a9c/3",
                                 "ws://hub:4500/connect/runner-7f3a9c/3?gateway_token=s3cret"])
def test_client_endpoint_refuses_a_slot_endpoint(url):
    with pytest.raises(ValueError, match="not a /connect/<node>/<slot> endpoint") as error:
        config.client_endpoint(url, "t")
    assert "s3cret" not in str(error.value)


def test_hosts_blank_servers_skipped(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, monkeypatch, """
hosts:
  - server: ""
    port_start: 3000
    port_end: 4000
  - server: "  "
    port_start: 3000
    port_end: 4000
  - port_start: 3000
    port_end: 4000
  - server: runner01
    port_start: 3000
    port_end: 3002
""")
    assert cfg.get_hosts_config() == [("runner01", 3000, 3002)]


@pytest.mark.parametrize("text", ["mysql: {}\n", "hosts:\n", "hosts: []\n", ""])
def test_hosts_missing_or_empty(tmp_path, monkeypatch, text):
    assert write_config(tmp_path, monkeypatch, text).get_hosts_config() == []


def test_repo_config_has_no_static_hosts(monkeypatch):
    monkeypatch.delenv("ORCHESTRATOR_CONFIG", raising=False)
    assert config.config().get_hosts_config() == []


def test_env_int_takes_the_first_name_set(monkeypatch):
    monkeypatch.delenv("SLOT_FAIL_THRESHOLD", raising=False)
    monkeypatch.delenv("GRID_FAIL_THRESHOLD", raising=False)
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 3
    monkeypatch.setenv("GRID_FAIL_THRESHOLD", "5")
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 5
    monkeypatch.setenv("SLOT_FAIL_THRESHOLD", "2")
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 2


def test_reclaim_stale_claims_frees_only_old_claims(make_db, monkeypatch, caplog):
    monkeypatch.setattr(database, "STALE_CLAIM_AFTER", 2100)
    db = make_db([(1, 1, "A", 1), (2, 2, "A", 1)])
    db.conn.rows[1].update(inUse=1, ago=5000)  # its claimer died mid-session
    db.conn.rows[2].update(inUse=1, ago=60)    # a session that is still running
    with caplog.at_level("WARNING", logger="checklog"):
        assert db.reclaim_stale_claims() == 1
    assert db.conn.executed == [(database.RECLAIM_STALE_SQL, (2100,))]
    assert (db.conn.rows[1]["inUse"], db.conn.rows[2]["inUse"]) == (0, 1)
    assert "freed 1 slot(s) claimed more than 2100 seconds ago" in caplog.text


def test_reclaim_stale_claims_leaves_hub_sessions_alone_however_long_they_run(make_db, monkeypatch):
    # A /connect session through the hub may run for hours; its claim goes with its hub's lease, not its age.
    # Sweep after sweep it stays claimed, so it is never freed and claimed again in a loop.
    monkeypatch.setattr(database, "STALE_CLAIM_AFTER", 2100)
    db = make_db([(1, 1, "A", 1), (2, 2, "A", 1)])
    db.conn.rows[1].update(inUse=1, ago=9000, claimedBy="hub:5c1e09a2:7")
    db.conn.rows[2].update(inUse=1, ago=9000, claimedBy=None)
    assert [db.reclaim_stale_claims() for _ in range(3)] == [1, 0, 0]
    assert (db.conn.rows[1]["inUse"], db.conn.rows[1]["claimedBy"]) == (1, "hub:5c1e09a2:7")
    assert db.conn.rows[2]["inUse"] == 0


def test_stale_claim_default_covers_the_check_timeout(monkeypatch):
    sql = " ".join(database.RECLAIM_STALE_SQL.split())
    # The hub's claims live as long as its lease (see src/schema.sql); %% is pymysql's literal %.
    assert sql == ("UPDATE portUsage SET inUse = 0, claimedBy = NULL WHERE inUse = 1 AND LastRunDate < UTC_TIMESTAMP() "
                   "- INTERVAL %s SECOND AND (claimedBy IS NULL OR claimedBy NOT LIKE 'hub:%%')")
    assert database.STALE_CLAIM_AFTER > int(os.environ.get("CHECK_TIMEOUT_S", "1800"))


class Unreachable:
    def __init__(self, failures):
        self.failures = failures

    def ping(self, reconnect=False):
        if self.failures:
            self.failures -= 1
            raise database.pymysql.err.OperationalError(2003, "Can't connect to MySQL server")


def test_lost_connection_reconnects(make_db, monkeypatch):
    db = make_db([(1, 7, "A", 1)])
    fake = db.conn
    db.conn = Unreachable(1)
    monkeypatch.setattr(database.pymysql, "connect", lambda **kwargs: fake)
    assert db.create_connection() is fake


def test_schema_statements_split():
    statements = database.schema_statements()
    assert [re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1) for s in statements] == \
        ["portStatus", "portUsage", "nodes", "accessTokens", "hubs"]
    assert not any(line.lstrip().startswith("--") for s in statements for line in s.splitlines())


def test_initial_db_start_runs_schema_and_seeds_static_hosts(make_db):
    db = make_db([], """
hosts:
  - server: ""
    port_start: 3000
    port_end: 4000
  - server: runner01
    port_start: 3000
    port_end: 3002
""")
    db.initial_db_start()
    executed = db.conn.executed
    schema = database.schema_statements()
    assert [sql for sql, _ in executed[:len(schema)]] == schema
    seeded = [rows for sql, rows in executed if "INSERT IGNORE INTO portStatus" in sql]
    assert seeded == [[(3000, "runner01"), (3001, "runner01")]]
    assert any("INSERT IGNORE INTO portUsage" in sql for sql, _ in executed)


def test_initial_db_start_without_hosts_seeds_nothing(make_db):
    db = make_db([])
    db.initial_db_start()
    assert not any("INSERT IGNORE INTO portStatus" in sql for sql, _ in db.conn.executed)


def test_initial_db_start_skips_hosts_owned_by_a_gateway_node(make_db, caplog):
    db = make_db([(1, 1, "tl-node", 1), (2, 2, "tl-node", 1)], """
hosts:
  - server: tl-node
    port_start: 3000
    port_end: 3003
  - server: runner01
    port_start: 3000
    port_end: 3001
""", nodes=["tl-node"])
    with caplog.at_level("WARNING", logger="checklog"):
        db.initial_db_start()
    seeded = [rows for sql, rows in db.conn.executed if "INSERT IGNORE INTO portStatus" in sql]
    assert seeded == [[(3000, "runner01")]]
    assert "tl-node" in caplog.text and "remove it from hosts:" in caplog.text
    assert set(db.conn.rows) == {1, 2}  # the node's own slots are not pruned


def test_initial_db_start_removes_idle_static_slots_no_longer_listed(make_db, caplog):
    db = make_db([(3000, 1, "runner01", 0), (3001, 2, "runner01", 0), (3299, 3, "old", 0), (3300, 4, "old", 0),
                  (1, 5, "node1", 1)], """
hosts:
  - server: runner01
    port_start: 3000
    port_end: 3001
""", nodes=["node1"])
    db.conn.rows[4]["inUse"] = 1  # a check is still using it, so it stays for now
    with caplog.at_level("WARNING", logger="checklog"):
        db.initial_db_start()
    assert set(db.conn.rows) == {1, 4, 5}
    assert "runner01:3001" in caplog.text and "old:3299" in caplog.text
    deletes = [sql for sql, _ in db.conn.executed if "DELETE FROM portStatus WHERE id = %s" in sql]
    assert deletes and "inUse = 0" in deletes[0]


def test_initial_db_start_keeps_listed_static_slots(make_db):
    db = make_db([(3000, 1, "runner01", 0)], """
hosts:
  - server: runner01
    port_start: 3000
    port_end: 3001
""")
    db.initial_db_start()
    assert set(db.conn.rows) == {1}
    assert not any("DELETE FROM portStatus WHERE id" in sql for sql, _ in db.conn.executed)


def test_slot_claiming_is_left_to_the_hub():
    # The hub claims, releases and counts failures; database.py only seeds and sweeps stale claims.
    for name in ("get_a_port", "release_port", "set_in_use_port", "release_in_use_port", "increment_failure",
                 "reset_failure", "check_ports_not_in_use"):
        assert not hasattr(database.database, name)
    for name in ("FREE_SLOTS_SQL", "Slot", "NoFreeSlot", "gateway_endpoint", "static_endpoint"):
        assert not hasattr(database, name)
