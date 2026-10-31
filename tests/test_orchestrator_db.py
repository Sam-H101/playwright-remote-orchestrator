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
        self.before_claim = None

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
        if sql == database.FREE_SLOTS_SQL:
            threshold, cooldown = args
            self.result = [(r["port"], id, r["server"], r["gateway"]) for id, r in rows.items()
                           if not r["inUse"] and (r["failCount"] < threshold or r["ago"] is None or r["ago"] > cooldown)]
            return len(self.result)
        if sql == database.RECLAIM_STALE_SQL:
            stale = [r for r in rows.values() if r["inUse"] and r["ago"] is not None and r["ago"] > args[0]]
            for r in stale:
                r["inUse"] = 0
            return len(stale)
        if sql == "SELECT name FROM nodes":
            self.result = [(name,) for name in sorted(self.db.nodes)]
            return len(self.result)
        if "SELECT ps.id, ps.PortServer, ps.PortNumber" in sql:
            self.result = [(id, r["server"], r["port"]) for id, r in rows.items()
                           if r["server"] not in self.db.nodes and not r["inUse"] and r["server"]]
            return len(self.result)
        if "SET inUse = 1" in sql:
            if self.db.before_claim:
                self.db.before_claim(args[0])
            if rows[args[0]]["inUse"]:
                return 0
            rows[args[0]].update(inUse=1, ago=0)
            return 1
        if "SET inUse = 0" in sql:
            rows[args[0]].update(inUse=0, ago=0)
            return 1
        if "failCount = failCount + 1" in sql:
            rows[args[0]]["failCount"] += 1
            return 1
        if "SET failCount = 0" in sql:
            rows[args[0]]["failCount"] = 0
            return 1
        return 0

    def fetchall(self):
        return self.result


@pytest.fixture
def make_db(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)

    def make(rows, yaml_text="hosts: []\n", nodes=()):
        db = database.database(write_config(tmp_path, monkeypatch, yaml_text))
        db.settings.update(host="mysql", user="u", password="p", database="d")
        db.conn = FakeSlots(rows, nodes)
        return db
    return make


def test_gateway_endpoint_default_and_quoting():
    assert database.gateway_endpoint("HOST1", 2) == "ws://hub:4500/connect/HOST1/2"
    assert database.gateway_endpoint("a b/c", 1, "ws://gw:9000/") == "ws://gw:9000/connect/a%20b%2Fc/1"


def test_gateway_endpoint_token_is_urlencoded():
    url = database.gateway_endpoint("n", 3, "wss://gw.example.com", "s3cr&t=/?")
    assert url == "wss://gw.example.com/connect/n/3?gateway_token=s3cr%26t%3D%2F%3F"


def test_static_endpoint():
    assert database.static_endpoint("runner01", 3000) == "ws://runner01:3000"


def test_gateway_settings_from_env(tmp_path, monkeypatch):
    cfg = write_config(tmp_path, monkeypatch, "hosts: []\n")
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    assert cfg.get_gateway_config() == {"url": "ws://hub:4500", "client_token": None}
    monkeypatch.setenv("GATEWAY_URL", "ws://10.0.0.5:4500/")
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "tok")
    assert cfg.get_gateway_config() == {"url": "ws://10.0.0.5:4500", "client_token": "tok"}


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


def test_selection_sql_has_contract_conditions():
    sql = " ".join(database.FREE_SLOTS_SQL.split())
    assert "LEFT JOIN nodes n ON n.name = ps.PortServer" in sql
    assert "pu.inUse = 0" in sql
    assert re.search(r"n\.name IS NULL OR \(n\.online = 1 AND "
                     r"n\.lastSeen >= UTC_TIMESTAMP\(\) - INTERVAL 60 SECOND AND ps\.PortStatus = 1\)", sql)
    assert ("AND (pu.failCount < %s OR pu.LastRunDate IS NULL"
            " OR pu.LastRunDate < UTC_TIMESTAMP() - INTERVAL %s SECOND)") in sql


def test_selection_passes_fail_threshold_and_cooldown(make_db, monkeypatch):
    monkeypatch.setattr(database, "SLOT_FAIL_THRESHOLD", 4)
    monkeypatch.setattr(database, "SLOT_FAIL_COOLDOWN", 120)
    db = make_db([(1, 1, "A", 1)])
    db.check_ports_not_in_use()
    assert (database.FREE_SLOTS_SQL, (4, 120)) in db.conn.executed


def test_fail_threshold_env(monkeypatch):
    monkeypatch.delenv("SLOT_FAIL_THRESHOLD", raising=False)
    monkeypatch.delenv("GRID_FAIL_THRESHOLD", raising=False)
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 3
    monkeypatch.setenv("GRID_FAIL_THRESHOLD", "5")
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 5
    monkeypatch.setenv("SLOT_FAIL_THRESHOLD", "2")
    assert database.env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3) == 2


def test_failing_slot_is_skipped_until_its_cooldown_passes(make_db, monkeypatch):
    monkeypatch.setattr(database, "SLOT_FAIL_THRESHOLD", 3)
    monkeypatch.setattr(database, "SLOT_FAIL_COOLDOWN", 300)
    db = make_db([(3000, 1, "dead", 0)])
    db.conn.rows[1].update(failCount=3, ago=10)
    with pytest.raises(database.NoFreeSlot):
        db.get_a_port()
    db.conn.rows[1]["ago"] = 301  # one retry per cooldown
    slot = db.get_a_port()
    db.increment_failure(slot)
    db.release_port(slot)
    with pytest.raises(database.NoFreeSlot):
        db.get_a_port()
    db.conn.rows[1]["ago"] = 301
    slot = db.get_a_port()
    db.reset_failure(slot)  # the retry got through
    db.release_port(slot)
    assert db.get_a_port().id == 1


def test_below_threshold_stays_eligible(make_db, monkeypatch):
    monkeypatch.setattr(database, "SLOT_FAIL_THRESHOLD", 3)
    db = make_db([(1, 1, "A", 1)])
    db.conn.rows[1].update(failCount=2, ago=0)
    assert db.get_a_port().id == 1


def test_reset_failure_only_writes_when_needed(make_db):
    db = make_db([(1, 5, "A", 1)])
    db.conn.rows[5]["failCount"] = 4
    db.reset_failure(db.get_a_port())
    assert db.conn.rows[5]["failCount"] == 0
    assert any("WHERE id = %s AND failCount <> 0" in sql for sql, _ in db.conn.executed)


def test_get_a_port_reclaims_stale_claims_first(make_db, monkeypatch):
    monkeypatch.setattr(database, "STALE_CLAIM_AFTER", 2100)
    db = make_db([(1, 1, "A", 1), (2, 2, "A", 1)])
    db.conn.rows[1].update(inUse=1, ago=5000)  # orchestrator killed mid-check
    db.conn.rows[2].update(inUse=1, ago=60)    # a check that is still running
    assert db.get_a_port().id == 1
    assert db.conn.executed[0] == (database.RECLAIM_STALE_SQL, (2100,))
    assert db.conn.rows[2]["inUse"] == 1


def test_stale_claim_default_covers_the_check_timeout(monkeypatch):
    sql = " ".join(database.RECLAIM_STALE_SQL.split())
    assert sql == "UPDATE portUsage SET inUse = 0 WHERE inUse = 1 AND LastRunDate < UTC_TIMESTAMP() - INTERVAL %s SECOND"
    assert database.STALE_CLAIM_AFTER > int(os.environ.get("CHECK_TIMEOUT_S", "1800"))


class Unreachable:
    def __init__(self, failures):
        self.failures = failures

    def ping(self, reconnect=False):
        if self.failures:
            self.failures -= 1
            raise database.pymysql.err.OperationalError(2003, "Can't connect to MySQL server")


def test_release_retries_until_mysql_is_back(make_db, monkeypatch):
    db = make_db([(1, 7, "A", 1)])
    slot = db.get_a_port()
    fake = db.conn
    db.conn = Unreachable(1)
    attempts = []

    def connect(**kwargs):
        attempts.append(1)
        if len(attempts) <= 12:  # longer than create_connection's own 10 tries
            raise database.pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
        return fake
    monkeypatch.setattr(database.pymysql, "connect", connect)
    monkeypatch.setattr(database.time, "sleep", lambda s: None)  # create_connection's own retry delay
    sleeps = []
    db.release_port(slot, sleep=sleeps.append)
    assert sleeps == [1]
    assert fake.rows[7]["inUse"] == 0
    assert db.conn is fake


def test_release_gives_up_after_retry_window(make_db, monkeypatch):
    db = make_db([(1, 7, "A", 1)])
    slot = db.get_a_port()

    def connect(**kwargs):
        raise database.pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
    monkeypatch.setattr(database.pymysql, "connect", connect)
    monkeypatch.setattr(database.time, "sleep", lambda s: None)
    db.conn = Unreachable(1)
    sleeps = []
    with pytest.raises(database.pymysql.err.OperationalError):
        db.release_port(slot, retry_for=120, sleep=sleeps.append)
    assert sleeps == [1, 2, 4, 8, 15, 15, 15, 15, 15, 15, 15]
    assert db.conn is None


def test_lost_connection_reconnects(make_db, monkeypatch):
    db = make_db([(1, 7, "A", 1)])
    fake = db.conn
    db.conn = Unreachable(1)
    monkeypatch.setattr(database.pymysql, "connect", lambda **kwargs: fake)
    assert db.create_connection() is fake


def test_schema_statements_split():
    statements = database.schema_statements()
    assert [re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s).group(1) for s in statements] == \
        ["portStatus", "portUsage", "nodes", "accessTokens"]
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


def test_claim_and_release(make_db, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "t")
    db = make_db([(2, 7, "HOST 1", 1)])
    slot = db.get_a_port()
    assert (slot[0], slot[1], slot[2]) == (2, 7, "HOST 1")
    assert slot.endpoint == "ws://hub:4500/connect/HOST%201/2?gateway_token=t"
    assert slot.gateway is True
    assert db.conn.rows[7]["inUse"] == 1
    with pytest.raises(database.NoFreeSlot):
        db.get_a_port()
    db.release_port(slot)
    assert db.conn.rows[7]["inUse"] == 0


def test_static_slot_endpoint(make_db):
    slot = make_db([(3001, 1, "runner01", 0)]).get_a_port()
    assert slot.endpoint == "ws://runner01:3001"
    assert slot.gateway is False


def test_lost_claim_race_picks_again(make_db):
    db = make_db([(1, 1, "A", 1), (1, 2, "B", 1)])
    stolen = []

    def steal(id):
        # another orchestrator claims the slot between our SELECT and UPDATE
        if not stolen:
            stolen.append(id)
            db.conn.rows[id]["inUse"] = 1
    db.conn.before_claim = steal
    slot = db.get_a_port()
    assert slot.id != stolen[0]
    assert all(r["inUse"] for r in db.conn.rows.values())


def test_increment_failure(make_db):
    db = make_db([(1, 5, "A", 1)])
    slot = db.get_a_port()
    db.increment_failure(slot)
    assert db.conn.rows[5]["failCount"] == 1
