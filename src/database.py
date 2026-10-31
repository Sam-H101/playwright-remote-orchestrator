import logging
import os
import time

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


# A claim older than this was left by a claimer that died mid-session, and is freed. Keep it above
# the longest session of such a claimer: a check is killed after CHECK_TIMEOUT_S.
STALE_CLAIM_AFTER = env_int("SLOT_STALE_AFTER_S",
                            default=max(env_int("CHECK_TIMEOUT_S", default=1800), 1800) + 300)

# Parameter: STALE_CLAIM_AFTER. LastRunDate is the claim time while inUse = 1. The hub's claims
# ('hub:...') are left alone: a /connect session may run for hours, and the hubs free the claims of a
# hub that stopped by its lease (the hubs table), so their age says nothing.
RECLAIM_STALE_SQL = """
UPDATE portUsage SET inUse = 0, claimedBy = NULL
WHERE inUse = 1 AND LastRunDate < UTC_TIMESTAMP() - INTERVAL %s SECOND
AND (claimedBy IS NULL OR claimedBy NOT LIKE 'hub:%%')
"""


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

    def create_connection(self, retries=10, delay=3, connect_timeout=10):
        """ create (or reuse) a connection to the MySQL database """
        if self.conn is not None:
            try:
                self.conn.ping(reconnect=False)
                return self.conn
            except (pymysql.err.OperationalError, pymysql.err.InterfaceError) as e:
                print("lost the mysql connection: {}".format(e))
                self.conn = None

        last_error = None
        for attempt in range(retries):
            try:
                self.conn = pymysql.connect(
                    host=self.settings["host"],
                    port=self.settings["port"],
                    user=self.settings["user"],
                    password=self.settings["password"],
                    database=self.settings["database"],
                    autocommit=True,
                    connect_timeout=connect_timeout,
                )
                return self.conn
            except pymysql.err.OperationalError as e:  # MySQL may still be starting
                last_error = e
                if attempt + 1 < retries:
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

    def reclaim_stale_claims(self):
        """ free slots claimed more than STALE_CLAIM_AFTER seconds ago by a claimer other than the hub.
        Returns how many. """
        con = self.create_connection()
        with con.cursor() as cur:
            count = cur.execute(RECLAIM_STALE_SQL, (STALE_CLAIM_AFTER,))
        if count:
            log.warning("freed %s slot(s) claimed more than %s seconds ago by a claimer that never released them",
                        count, STALE_CLAIM_AFTER)
        return count
