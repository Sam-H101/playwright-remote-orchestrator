"""Read/write access to the orchestrator's slot tables for the Grid UI.

Each row in portStatus/PortUsage is one Playwright server port ("slot").
Slots are grouped by PortServer into nodes. A slot with InUse = 1 is a live
session, started at LastRunDate.
"""
import datetime
import os
import sqlite3

DB_ENGINE = os.environ.get("GRID_DB_ENGINE", "sqlite").lower()
SQLITE_PATH = os.environ.get("GRID_DB_PATH", "src/runner.db")
QUEUE_PATH = os.environ.get("GRID_QUEUE_PATH", "queue.db")
FAIL_THRESHOLD = int(os.environ.get("GRID_FAIL_THRESHOLD", "3"))
STALE_AFTER_S = int(os.environ.get("GRID_STALE_AFTER", "3600"))


def _connect():
    if DB_ENGINE == "mysql":
        import pymysql

        return pymysql.connect(
            host=os.environ.get("MYSQL_HOST", "localhost"),
            port=int(os.environ.get("MYSQL_PORT", "3306")),
            user=os.environ.get("MYSQL_USER", "root"),
            password=os.environ.get("MYSQL_PASSWORD", ""),
            database=os.environ.get("MYSQL_DATABASE", "orchestrator"),
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


def queue_size():
    """Pending items in the persist-queue directory, or None if unreadable."""
    path = os.path.join(QUEUE_PATH, "data.db") if os.path.isdir(QUEUE_PATH) else QUEUE_PATH
    if not os.path.exists(path):
        return None
    try:
        con = sqlite3.connect("file:{}?mode=ro".format(path), uri=True, timeout=2)
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'queue%'")]
            return sum(con.execute("SELECT COUNT(*) FROM {}".format(t)).fetchone()[0] for t in tables)
        finally:
            con.close()
    except sqlite3.Error:
        return None


def snapshot():
    """Current state of every node, slot and live session."""
    con = _connect()
    try:
        cur = con.cursor()
        cur.execute("""
            SELECT ps.id, ps.PortServer, ps.PortNumber, pu.InUse, pu.LastRunDate, pu.failCount
            FROM portStatus ps JOIN PortUsage pu ON ps.id = pu.id
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
        "queue_size": queue_size(),
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
        "UPDATE PortUsage SET InUse = 0, LastRunDate = ? WHERE id = ?",
        (str(datetime.datetime.now()), slot_id))


def reset_slot_failures(slot_id):
    return _execute("UPDATE PortUsage SET failCount = 0 WHERE id = ?", (slot_id,))


def reset_node_failures(host):
    return _execute(
        "UPDATE PortUsage SET failCount = 0 WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?)",
        (host,))
