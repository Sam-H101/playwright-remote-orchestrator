import collections
import logging
import os
import random
import time
from urllib.parse import quote

import pymysql

from . import config

SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")

# main.py sends this logger to the console and logs/check_info.log.
log = logging.getLogger("checklog")


def env_int(*names, default):
    """ the first of the environment variables that is set, as an int """
    for name in names:
        if os.environ.get(name):
            return int(os.environ[name])
    return default


# failCount counts consecutive connect failures. A slot at the threshold is skipped, except for
# one retry per cooldown (counted from its last run), so it comes back by itself once it works.
SLOT_FAIL_THRESHOLD = env_int("SLOT_FAIL_THRESHOLD", "GRID_FAIL_THRESHOLD", default=3)
SLOT_FAIL_COOLDOWN = env_int("SLOT_FAIL_COOLDOWN_S", default=300)
# A claim older than this was left by an orchestrator that died mid-check, and is freed. Every
# orchestrator shares the table, so it must stay above the longest CHECK_TIMEOUT_S of any of them.
STALE_CLAIM_AFTER = env_int("SLOT_STALE_AFTER_S",
                            default=max(env_int("CHECK_TIMEOUT_S", default=1800), 1800) + 300)
# How long release_port keeps retrying while MySQL is unreachable.
RELEASE_RETRY_FOR = 120

# A gateway slot is only handed out while its node is connected to the hub, has sent a
# heartbeat in the last minute and reports the slot's run-server healthy. Slots whose
# PortServer has no nodes row are legacy static hosts and are always eligible.
# Either kind is skipped while failing. Parameters: fail threshold, fail cooldown in seconds.
FREE_SLOTS_SQL = """
SELECT ps.PortNumber, ps.id, ps.PortServer, n.name IS NOT NULL AS gateway
FROM portStatus ps
JOIN portUsage pu ON ps.id = pu.id
LEFT JOIN nodes n ON n.name = ps.PortServer
WHERE pu.inUse = 0
  AND ps.PortServer <> ''
  AND (n.name IS NULL
       OR (n.online = 1 AND n.lastSeen >= UTC_TIMESTAMP() - INTERVAL 60 SECOND AND ps.PortStatus = 1))
  AND (pu.failCount < %s OR pu.LastRunDate IS NULL
       OR pu.LastRunDate < UTC_TIMESTAMP() - INTERVAL %s SECOND)
"""

# Parameter: STALE_CLAIM_AFTER. LastRunDate is the claim time while inUse = 1.
RECLAIM_STALE_SQL = """
UPDATE portUsage SET inUse = 0
WHERE inUse = 1 AND LastRunDate < UTC_TIMESTAMP() - INTERVAL %s SECOND
"""

# Same index meaning as the old (PortNumber, id, PortServer) rows, plus where to connect.
Slot = collections.namedtuple("Slot", "port id server endpoint gateway")


class NoFreeSlot(RuntimeError):
    pass


def gateway_endpoint(node, slot, url=config.DEFAULT_GATEWAY_URL, token=None):
    endpoint = "{}/connect/{}/{}".format(url.rstrip("/"), quote(str(node), safe=""), slot)
    if token:
        endpoint += "?gateway_token=" + quote(token, safe="")
    return endpoint


def static_endpoint(host, port):
    return "ws://{}:{}".format(host, port)


def schema_statements(path=SCHEMA_PATH):
    """ the statements of schema.sql, which are separated by lines holding only ';' """
    statements, current = [], []
    with open(path, "r", encoding="utf-8") as stream:
        for line in stream:
            if line.strip() == ";":
                statements.append("\n".join(current))
                current = []
            elif not line.lstrip().startswith("--"):
                current.append(line.rstrip())
    statements.append("\n".join(current))
    return [s.strip() for s in statements if s.strip()]


class database():
    conn = None

    def __init__(self, cfg=None):
        self.config = cfg or config.config()
        self.settings = self.config.get_mysql_config()
        self.gateway = self.config.get_gateway_config()

    def create_connection(self, retries=10, delay=3):
        """ create (or reuse) a connection to the MySQL database """
        if self.conn is not None:
            try:
                self.conn.ping(reconnect=False)
                return self.conn
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
                print("lost the mysql connection: {}".format(e))
                self.conn = None

        last_error = None
        for _ in range(retries):
            try:
                self.conn = pymysql.connect(
                    host=self.settings["host"],
                    port=self.settings["port"],
                    user=self.settings["user"],
                    password=self.settings["password"],
                    database=self.settings["database"],
                    autocommit=True,
                )
                return self.conn
            except pymysql.err.OperationalError as e:  # MySQL may still be starting
                last_error = e
                print("waiting for mysql: {}".format(e))
                time.sleep(delay)
        raise last_error

    def initial_db_start(self):
        """ create the tables and seed a row per static host/port. Safe to run more than once. """
        con = self.create_connection()
        with con.cursor() as cur:
            for statement in schema_statements():
                cur.execute(statement)

            cur.execute("SELECT name FROM nodes")
            # Node names compare case-insensitively in MySQL (and the hub stores them lower-case).
            nodes = {name.lower() for (name,) in cur.fetchall()}
            rows = []
            for host, port_start, port_end in self.config.get_hosts_config():
                if host.lower() in nodes:
                    log.warning("not seeding hosts: entry %s, the gateway node of that name owns its slots;"
                                " remove it from hosts: in database.yaml", host)
                    continue
                rows.extend((port, host) for port in range(port_start, port_end))
            if rows:
                cur.executemany(
                    "INSERT IGNORE INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime)"
                    " VALUES (%s, 0, %s, UTC_TIMESTAMP())",
                    rows)
            cur.execute("""
               INSERT IGNORE INTO portUsage (id, portNumber, inUse, LastRunDate, failCount)
               SELECT id, PortNumber, 0, NULL, 0 FROM portStatus
            """)
            # Older configs shipped `server: ""` hosts, which seeded slots with no host to
            # connect to. The derived table is materialised, so MySQL allows the subquery.
            cur.execute("""
               DELETE FROM portStatus WHERE PortServer = ''
               AND id NOT IN (SELECT id FROM (SELECT id FROM portUsage WHERE inUse = 1) busy)
            """)
            # hosts: is the only source of static slots, so drop idle ones it no longer lists.
            # Slots whose PortServer has a nodes row belong to the hub and are left alone.
            cur.execute("""
               SELECT ps.id, ps.PortServer, ps.PortNumber FROM portStatus ps
               JOIN portUsage pu ON pu.id = ps.id
               LEFT JOIN nodes n ON n.name = ps.PortServer
               WHERE n.name IS NULL AND pu.inUse = 0 AND ps.PortServer <> ''
            """)
            wanted = set(rows)
            gone = [(id, server, port) for id, server, port in cur.fetchall() if (port, server) not in wanted]
            if gone:
                log.warning("removing static slots that are no longer in hosts: %s",
                            ", ".join("{}:{}".format(server, port) for _, server, port in gone))
                cur.executemany("""
                   DELETE FROM portStatus WHERE id = %s
                   AND id IN (SELECT id FROM (SELECT id FROM portUsage WHERE inUse = 0) idle)
                """, [(id,) for id, _, _ in gone])

    def endpoint_for(self, server, port, gateway):
        if gateway:
            return gateway_endpoint(server, port, self.gateway["url"], self.gateway["client_token"])
        return static_endpoint(server, port)

    def check_ports_not_in_use(self):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute(FREE_SLOTS_SQL, (SLOT_FAIL_THRESHOLD, SLOT_FAIL_COOLDOWN))
            rows = cur.fetchall()
        return [Slot(port, id, server, self.endpoint_for(server, port, gateway), bool(gateway))
                for port, id, server, gateway in rows]

    def set_in_use_port(self, portnum, id=None):
        """ mark a slot in use. Returns True only if this call claimed a slot that was free. """
        con = self.create_connection()
        with con.cursor() as cur:
            count = cur.execute(
                "UPDATE portUsage SET inUse = 1, LastRunDate = UTC_TIMESTAMP() WHERE id = %s AND inUse = 0",
                (portnum[1] if id is None else id,))
        return count > 0

    def release_in_use_port(self, portnum, id=None):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute(
                "UPDATE portUsage SET inUse = 0, LastRunDate = UTC_TIMESTAMP() WHERE id = %s",
                (portnum[1] if id is None else id,))

    def increment_failure(self, port):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute("UPDATE portUsage SET failCount = failCount + 1 WHERE id = %s", (port[1],))

    def reset_failure(self, port):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute("UPDATE portUsage SET failCount = 0 WHERE id = %s AND failCount <> 0", (port[1],))

    def reclaim_stale_claims(self):
        """ free slots claimed more than STALE_CLAIM_AFTER seconds ago. Returns how many. """
        con = self.create_connection()
        with con.cursor() as cur:
            count = cur.execute(RECLAIM_STALE_SQL, (STALE_CLAIM_AFTER,))
        if count:
            log.warning("freed %s slot(s) claimed more than %s seconds ago by a check that never released them",
                        count, STALE_CLAIM_AFTER)
        return count

    def get_a_port(self, attempts=50):
        self.reclaim_stale_claims()
        # The conditional UPDATE in set_in_use_port is atomic, so two orchestrators
        # can never claim the same slot. If another one wins the race, pick again.
        for _ in range(attempts):
            ports = self.check_ports_not_in_use()
            if not ports:
                raise NoFreeSlot("no free slots available")
            port = random.choice(ports)
            if self.set_in_use_port(port, port.id):
                return port
        raise NoFreeSlot("could not claim a free slot after {} attempts".format(attempts))

    def release_port(self, port_num, retry_for=None, sleep=time.sleep):
        """ free a slot, retrying for up to RELEASE_RETRY_FOR seconds while MySQL is unreachable """
        retry_for = RELEASE_RETRY_FOR if retry_for is None else retry_for
        delay, waited = 1, 0
        while True:
            try:
                return self.release_in_use_port(port_num, port_num[1])
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
                self.conn = None  # connect from scratch on the next try
                if waited >= retry_for:
                    raise
                log.warning("could not release slot %s (%s), retrying in %s seconds", port_num[1], e, delay)
                sleep(delay)
                waited += delay
                delay = min(delay * 2, 15)
