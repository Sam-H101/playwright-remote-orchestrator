import sqlite3
from sqlite3 import Error
import datetime
import random
from . import queues, config

c = config.config()


class database():
    dbfile = ""
    mqueue = None

    def __init__(self):

        self.dbfile = 'src/{}'.format(c.get_db_file_file())
        self.mqueue = queues.queues()

    def create_connection(self):
        """ create a database connection to a SQLite database """
        conn = None

        try:
            conn = sqlite3.connect(self.dbfile)
            print(sqlite3.version)
        except Error as e:
            print(e)
        finally:
            if conn:
                conn.close()

    def initial_db_start(self):
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        now = datetime.datetime.now()
        id_ctr = 0
        for server in c.get_hosts_config():
            host = server["server"]
            start_port = server["port_start"]
            end_port = server["port_end"]
            i = start_port
            while i < end_port:
                try:  # Dirty method to create a database
                    cur.execute(
                        "INSERT INTO PortUsage(portNumber, InUse, LastRunDate, id, failCount)VALUES({}, 0, '', {}, 0)".format(
                            i, id_ctr))
                    cur.execute(
                        "INSERT INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime, id) VALUES({}, 0, '{}', '{}', {})".format(
                            i, host, now, id_ctr))
                except:
                    pass
                i = i + 1
                id_ctr = id_ctr + 1

        con.commit()
        con.close()

    def check_ports_not_in_use(self):
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        sql = """
        SELECT ps.PortNumber, ps.id, ps.PortServer, pu.InUse  FROM portStatus ps Join PortUsage pu 
        ON ps.id = pu.id 
        WHERE pu.InUse = 0
        """
        res = cur.execute(sql)
        res = res.fetchall()

        con.close()
        return res

    def set_in_use_port(self, portnum, id=None):
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        if id is not None:
            cur.execute(
                "UPDATE PortUsage SET InUse = 1, LastRunDate = '{}' WHERE id = {}".format(datetime.datetime.now(), id))
        else:
            cur.execute("UPDATE PortUsage SET InUse = 1, LastRunDate = '{}' WHERE portNumber = {}".format(
                datetime.datetime.now(), portnum[0]))

        con.commit()
        cur.close()
        con.close()

    def release_in_use_port(self, portnum, id=None):
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        if id is not None:
            cur.execute(
                "UPDATE PortUsage SET InUse = 0, LastRunDate = '{}' WHERE id = {}".format(datetime.datetime.now(), id))
        else:
            cur.execute("UPDATE PortUsage SET InUse = 0, LastRunDate = '{0}' WHERE portNumber = {1}".format(
                datetime.datetime.now(), portnum[0]))
        con.commit()
        cur.close()
        con.close()

    def increment_failure(self, port):
        id = port[1]
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        fail_count = cur.execute("SELECT failCount from PortUsage WHERE id = {}".format(id)).fetchone()
        fail_count = fail_count[0]
        fail_count = fail_count + 1
        print(fail_count)
        cur.execute("UPDATE PortUsage SET failCount = {} WHERE id = {}".format(fail_count, id))
        con.commit()
        cur.close()
        con.close()

    def reset_failure(self, port):
        con = sqlite3.connect(self.dbfile)
        cur = con.cursor()
        id = port[1]
        cur.execute("UPDATE PortUsage SET failCount = 0 WHERE id = {}".format(id))
        con.commit()
        cur.close()
        con.close()

    def get_a_port(self):
        self.create_connection()
        ports = self.check_ports_not_in_use()
        portnumselector = random.randint(0, ports.__len__() - 1)
        port = ports[portnumselector]
        self.set_in_use_port(port, port[1])
        self.mqueue.put_item(port)
        return self.mqueue.get_item()

    def release_port(self, port_num):
        self.release_in_use_port(port_num, port_num[1])
