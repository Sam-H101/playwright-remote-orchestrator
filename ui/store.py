"""Read/write access to the orchestrator's slot tables for the Grid UI.

Each row in portStatus/portUsage is one Playwright server port ("slot").
Slots are grouped by PortServer into nodes. A slot with inUse = 1 is a live
session, started at LastRunDate.

The connection settings match the orchestrator's: the mysql section of
src/database.yaml (or ORCHESTRATOR_CONFIG), overridden by DB_HOST, DB_PORT,
DB_USER, DB_PASSWORD and DB_NAME.
"""
import datetime
import os
import sqlite3

import yaml

DB_ENGINE = os.environ.get("GRID_DB_ENGINE", "mysql").lower()
SQLITE_PATH = os.environ.get("GRID_DB_PATH", "src/runner.db")
CONFIG_PATH = os.environ.get("ORCHESTRATOR_CONFIG", "src/database.yaml")
FAIL_THRESHOLD = int(os.environ.get("GRID_FAIL_THRESHOLD", "3"))
STALE_AFTER_S = int(os.environ.get("GRID_STALE_AFTER", "3600"))

_ENV_OVERRIDES = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "database": "DB_NAME",
}


def mysql_settings():
    settings = {}
    try:
        with open(CONFIG_PATH) as stream:
            settings = dict((yaml.safe_load(stream) or {}).get("mysql") or {})
    except OSError:
        pass
    for key, env in _ENV_OVERRIDES.items():
        if os.environ.get(env):
            settings[key] = os.environ[env]
    settings["port"] = int(settings.get("port", 3306))
    return settings


def _connect():
    if DB_ENGINE == "mysql":
        import pymysql

        s = mysql_settings()
        return pymysql.connect(
            host=s.get("host", "localhost"),
            port=s["port"],
            user=s.get("user"),
            password=s.get("password", ""),
            database=s.get("database", "orchestrator"),
            connect_timeout=5,
        )
    return sqlite3.connect(SQLITE_PATH, timeout=5)


def _param():
    return "%s" if DB_ENGINE == "mysql" else "?"


def _parse_time(value):
    if value in (None, "", b""):
        return None
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return datetime.datetime.fromisoformat(str(value))
    except ValueError:
        return None


def snapshot():
    """Current state of every node, slot and live session."""
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT ps.id, ps.PortServer, ps.PortNumber, pu.inUse, pu.LastRunDate, pu.failCount
            FROM portStatus ps JOIN portUsage pu ON ps.id = pu.id
            ORDER BY ps.PortServer, ps.PortNumber
        """)
        rows = cur.fetchall()
    finally:
        con.close()

    now = datetime.datetime.now()
    nodes = {}
    sessions = []
    for slot_id, host, port, in_use, last_run, fails in rows:
        host = host or ""
        node = nodes.setdefault(host, {
            "host": host, "slots": [], "total": 0, "busy": 0, "failing": 0, "last_activity": None,
        })
        busy = bool(in_use)
        fails = fails or 0
        started = _parse_time(last_run)
        node["total"] += 1
        node["busy"] += busy
        node["failing"] += fails >= FAIL_THRESHOLD
        if started and (node["last_activity"] is None or started > node["last_activity"]):
            node["last_activity"] = started
        node["slots"].append({"id": slot_id, "port": port, "busy": busy, "fails": fails})
        if busy:
            duration_s = int((now - started).total_seconds()) if started else None
            sessions.append({
                "slot_id": slot_id,
                "host": host,
                "port": port,
                "endpoint": "ws://{}:{}".format(host, port),
                "started": started.isoformat(timespec="seconds") if started else None,
                "duration_s": duration_s,
                "stale": duration_s is None or duration_s > STALE_AFTER_S,
                "fails": fails,
            })

    for node in nodes.values():
        usable = node["total"] - node["failing"]
        if node["total"] and usable == 0:
            node["status"] = "DOWN"
        elif node["failing"]:
            node["status"] = "DEGRADED"
        else:
            node["status"] = "UP"
        node["free"] = node["total"] - node["busy"]
        last = node["last_activity"]
        node["last_activity"] = last.isoformat(timespec="seconds") if last else None

    node_list = sorted(nodes.values(), key=lambda n: n["host"])
    total = sum(n["total"] for n in node_list)
    busy = sum(n["busy"] for n in node_list)
    return {
        "generated": now.isoformat(timespec="seconds"),
        "fail_threshold": FAIL_THRESHOLD,
        "stale_after_s": STALE_AFTER_S,
        "totals": {
            "nodes": len(node_list),
            "slots": total,
            "busy": busy,
            "free": total - busy,
            "failing": sum(n["failing"] for n in node_list),
            "stale": sum(s["stale"] for s in sessions),
        },
        "nodes": node_list,
        "sessions": sorted(sessions, key=lambda s: s["started"] or ""),
    }


def _execute(sql, params):
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute(sql.replace("?", _param()), params)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def release_slot(slot_id):
    """Force a stuck slot back to free."""
    return _execute(
        "UPDATE portUsage SET inUse = 0, LastRunDate = ? WHERE id = ?",
        (str(datetime.datetime.now()), slot_id))


def reset_slot_failures(slot_id):
    return _execute("UPDATE portUsage SET failCount = 0 WHERE id = ?", (slot_id,))


def reset_node_failures(host):
    return _execute(
        "UPDATE portUsage SET failCount = 0 WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?)",
        (host,))
