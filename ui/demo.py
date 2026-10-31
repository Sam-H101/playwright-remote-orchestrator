"""Build a sqlite database with sample nodes and sessions, for demos and screenshots.

    python -m ui.demo demo.db               # sample data (UTC timestamps relative to now)
    python -m ui.demo demo.db --empty       # a database with no tables yet
    python -m ui.demo demo.db --heartbeat   # sample data, then keep the online nodes' lastSeen fresh

Then: GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app
"""
import argparse
import datetime
import hashlib
import os
import secrets
import sqlite3
import time

# sqlite translation of src/schema.sql
SCHEMA = """
CREATE TABLE IF NOT EXISTS portStatus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    PortNumber INTEGER NOT NULL,
    PortStatus INTEGER NOT NULL DEFAULT 0,
    PortServer TEXT NOT NULL,
    LastUpdateTime TEXT NULL,
    UNIQUE (PortServer, PortNumber)
);
CREATE TABLE IF NOT EXISTS portUsage (
    id INTEGER PRIMARY KEY REFERENCES portStatus (id) ON DELETE CASCADE,
    portNumber INTEGER NOT NULL,
    inUse INTEGER NOT NULL DEFAULT 0,
    LastRunDate TEXT NULL,
    failCount INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nodes (
    name TEXT NOT NULL PRIMARY KEY,
    online INTEGER NOT NULL DEFAULT 0,
    slots INTEGER NOT NULL DEFAULT 0,
    playwrightVersion TEXT NULL,
    agentVersion TEXT NULL,
    platform TEXT NULL,
    browsers TEXT NULL,
    remoteAddress TEXT NULL,
    registeredAt TEXT NULL,
    connectedAt TEXT NULL,
    lastSeen TEXT NULL,
    ephemeral INTEGER NOT NULL DEFAULT 0,
    tokenId INTEGER NULL
);
CREATE TABLE IF NOT EXISTS accessTokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    tokenHash TEXT NOT NULL UNIQUE,
    prefix TEXT NOT NULL,
    nodeName TEXT NULL,
    createdAt TEXT NOT NULL,
    expiresAt TEXT NULL,
    lastUsedAt TEXT NULL,
    revokedAt TEXT NULL
);
"""

# name, online, platform, browsers, address, last seen (s ago), connected (s ago),
# slots as (healthy, in use since (s ago) or None, failCount)
NODES = [
    ("WIN-BUILD-01", 1, "win32", "chromium,firefox", "10.20.4.17", 3, 86400 * 2, [
        (1, 754, 0), (1, None, 0), (1, 41, 0), (1, None, 1)]),
    ("runner-7f3a9c", 1, "linux", "chromium,firefox,webkit", "172.50.221.9", 6, 5400, [
        (1, 12, 0), (1, 199, 0), (1, None, 0), (1, None, 0), (1, 1530, 0), (1, None, 0)]),
    ("runner-b81e04", 1, "linux", "chromium", "172.50.221.11", 8, 5400, [
        (1, 88, 0), (0, None, 0), (1, None, 4), (1, None, 0)]),
    ("MAC-QA-02", 0, "darwin", "chromium,webkit", "10.20.6.40", 2820, 86400 * 9, [
        (0, None, 0), (0, None, 0), (0, None, 0)]),
]
# docker runners, which the hub removes once they go away
EPHEMERAL = {"runner-7f3a9c", "runner-b81e04"}
# (server, PortNumber): failing slots that last ran this many seconds ago, so they're cooling down
LAST_FAILED = {("runner-b81e04", 3): 90, ("pw-static.lab.local", 3001): 40}
# host, first port, slots as (in use since (s ago) or None, failCount)
STATIC_HOSTS = [
    ("pw-static.lab.local", 3000, [(7420, 0), (None, 3), (None, 0), (305, 1)]),
]

DAY = 86400
# Managed access tokens: name, kind, bound node, created / expires / last used / revoked as
# seconds ago (negative: in the future; None: never), nodes that last registered with it.
TOKENS = [
    ("WIN-BUILD-01 agent", "agent", "win-build-01", 40 * DAY, -325 * DAY, 180, None, ["WIN-BUILD-01"]),
    ("Docker runners", "agent", None, 12 * DAY, None, 20, None, ["runner-7f3a9c", "runner-b81e04"]),
    ("Example checks", "client", None, 12 * DAY, -5 * DAY, 41, None, []),
    ("Spare Linux hosts", "agent", None, 2 * DAY, -28 * DAY, None, None, []),
    ("Old build farm", "agent", None, 200 * DAY, None, 2 * DAY + 3600, 2 * DAY, ["MAC-QA-02"]),
    ("Staging checks 2025", "client", None, 375 * DAY, 10 * DAY, 11 * DAY, None, []),
]


def _ts(now, seconds_ago):
    return None if seconds_ago is None else (now - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def create_schema(con):
    con.executescript(SCHEMA)


def _add_slot(cur, server, number, status, in_use_since, fails, now):
    cur.execute("INSERT INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime) VALUES (?, ?, ?, ?)",
                (number, status, server, _ts(now, 0)))
    cur.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate, failCount) VALUES (?, ?, ?, ?, ?)",
                (cur.lastrowid, number, int(in_use_since is not None), _ts(now, in_use_since), fails))


def seed(con, now=None):
    """Sample data with timestamps relative to `now` (naive UTC)."""
    now = now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    create_schema(con)
    cur = con.cursor()
    for name, online, platform, browsers, address, seen, connected, slots in NODES:
        cur.execute("""
            INSERT INTO nodes (name, online, slots, playwrightVersion, agentVersion, platform, browsers,
                               remoteAddress, registeredAt, connectedAt, lastSeen)
            VALUES (?, ?, ?, '1.64.1', '1.0.0', ?, ?, ?, ?, ?, ?)
        """, (name, online, len(slots), platform, browsers, address,
              _ts(now, connected + 3600), _ts(now, connected), _ts(now, seen)))
        for i, (healthy, since, fails) in enumerate(slots, start=1):
            _add_slot(cur, name, i, healthy, since, fails, now)
    for host, first, slots in STATIC_HOSTS:
        for i, (since, fails) in enumerate(slots):
            _add_slot(cur, host, first + i, 0, since, fails, now)
    cur.executemany("UPDATE nodes SET ephemeral = 1 WHERE name = ?", [(name,) for name in EPHEMERAL])
    for (server, number), seconds_ago in LAST_FAILED.items():
        cur.execute("UPDATE portUsage SET LastRunDate = ? WHERE id = "
                    "(SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?)",
                    (_ts(now, seconds_ago), server, number))
    _add_slot(cur, "", 3000, 0, None, 0, now)  # a blank host from an old config; the UI must hide it
    for name, kind, node_name, created, expires, used, revoked, users in TOKENS:
        token = ("pwga_" if kind == "agent" else "pwgc_") + secrets.token_urlsafe(32)  # thrown away: a demo
        cur.execute("""
            INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, lastUsedAt, revokedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, kind, hashlib.sha256(token.encode()).hexdigest(), token[:12], node_name,
              _ts(now, created), _ts(now, expires), _ts(now, used), _ts(now, revoked)))
        cur.executemany("UPDATE nodes SET tokenId = ? WHERE name = ?", [(cur.lastrowid, user) for user in users])
    con.commit()


def heartbeat(path, interval=5):
    while True:
        with sqlite3.connect(path) as con:
            con.execute("UPDATE nodes SET lastSeen = datetime('now') WHERE online = 1")
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path")
    parser.add_argument("--empty", action="store_true", help="create the file but no tables")
    parser.add_argument("--heartbeat", action="store_true", help="keep online nodes online until interrupted")
    args = parser.parse_args()
    if os.path.exists(args.path):
        os.remove(args.path)
    con = sqlite3.connect(args.path)
    if not args.empty:
        seed(con)
    con.close()
    print("wrote", args.path)
    if args.heartbeat:
        heartbeat(args.path)


if __name__ == "__main__":
    main()
