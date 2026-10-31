import datetime
import random
import time

import pymysql

from . import config

c = config.config()


class database():
    conn = None

    def __init__(self):
        self.settings = c.get_mysql_config()

    def create_connection(self, retries=10, delay=3):
        """ create (or reuse) a connection to the MySQL database """
        if self.conn is not None:
            self.conn.ping(reconnect=True)
            return self.conn

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
        """ create the tables and seed a row per host/port. Safe to run more than once. """
        con = self.create_connection()
        now = datetime.datetime.now()
        with con.cursor() as cur:
            cur.execute("""
               CREATE TABLE IF NOT EXISTS portStatus (
               id INT AUTO_INCREMENT PRIMARY KEY,
               PortNumber INT NOT NULL,
               PortStatus INT NOT NULL DEFAULT 0,
               PortServer VARCHAR(255) NOT NULL,
               LastUpdateTime DATETIME NULL,
               UNIQUE KEY uq_server_port (PortServer, PortNumber)
               )
            """)
            cur.execute("""
               CREATE TABLE IF NOT EXISTS portUsage (
               id INT PRIMARY KEY,
               portNumber INT NOT NULL,
               inUse TINYINT NOT NULL DEFAULT 0,
               LastRunDate DATETIME NULL,
               failCount INT NOT NULL DEFAULT 0,
               KEY idx_in_use (inUse),
               CONSTRAINT fk_port_usage_status FOREIGN KEY (id) REFERENCES portStatus (id) ON DELETE CASCADE
               )
            """)

            rows = []
            for server in c.get_hosts_config():
                host = server["server"]
                for port in range(server["port_start"], server["port_end"]):
                    rows.append((port, host, now))
            if rows:
                cur.executemany(
                    "INSERT IGNORE INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime) VALUES (%s, 0, %s, %s)",
                    rows)
            cur.execute("""
               INSERT IGNORE INTO portUsage (id, portNumber, inUse, LastRunDate, failCount)
               SELECT id, PortNumber, 0, NULL, 0 FROM portStatus
            """)

    def check_ports_not_in_use(self):
        con = self.create_connection()
        sql = """
        SELECT ps.PortNumber, ps.id, ps.PortServer, pu.inUse FROM portStatus ps JOIN portUsage pu
        ON ps.id = pu.id
        WHERE pu.inUse = 0
        """
        with con.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def set_in_use_port(self, portnum, id=None):
        """ mark a port in use. Returns True only if this call claimed a port that was free. """
        con = self.create_connection()
        with con.cursor() as cur:
            if id is not None:
                count = cur.execute(
                    "UPDATE portUsage SET inUse = 1, LastRunDate = %s WHERE id = %s AND inUse = 0",
                    (datetime.datetime.now(), id))
            else:
                count = cur.execute(
                    "UPDATE portUsage SET inUse = 1, LastRunDate = %s WHERE portNumber = %s AND inUse = 0",
                    (datetime.datetime.now(), portnum[0]))
        return count > 0

    def release_in_use_port(self, portnum, id=None):
        con = self.create_connection()
        with con.cursor() as cur:
            if id is not None:
                cur.execute(
                    "UPDATE portUsage SET inUse = 0, LastRunDate = %s WHERE id = %s",
                    (datetime.datetime.now(), id))
            else:
                cur.execute(
                    "UPDATE portUsage SET inUse = 0, LastRunDate = %s WHERE portNumber = %s",
                    (datetime.datetime.now(), portnum[0]))

    def increment_failure(self, port):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute("UPDATE portUsage SET failCount = failCount + 1 WHERE id = %s", (port[1],))

    def reset_failure(self, port):
        con = self.create_connection()
        with con.cursor() as cur:
            cur.execute("UPDATE portUsage SET failCount = 0 WHERE id = %s", (port[1],))

    def get_a_port(self, attempts=50):
        # The conditional UPDATE in set_in_use_port is atomic, so two orchestrators
        # can never claim the same port. If another one wins the race, pick again.
        for _ in range(attempts):
            ports = self.check_ports_not_in_use()
            if not ports:
                raise RuntimeError("no free ports available")
            port = random.choice(ports)
            if self.set_in_use_port(port, port[1]):
                return port
        raise RuntimeError("could not claim a free port after {} attempts".format(attempts))

    def release_port(self, port_num):
        self.release_in_use_port(port_num, port_num[1])
