"""Read/write access to the orchestrator's slot tables for the Grid UI.

Each row in portStatus/portUsage is one Playwright server slot. Slots are
grouped by PortServer into nodes. A PortServer with a row in `nodes` is a
gateway node that registered itself with the hub (PortNumber is the slot
index); one without is a legacy static host (PortNumber is the run-server
port). A slot with inUse = 1 is a live session, started at LastRunDate.

Checks don't connect to a slot: every one connects to the hub's /connect, and the hub claims a free
slot that fits the browser it launches. portUsage.claimedBy says who holds a claim: 'hub:<hub
instance>:<n>' for a session the hub routed, NULL for any other claimer. Databases the hub hasn't
upgraded yet have no such column; their claims are reported as of unknown origin. A hub's claim lasts
as long as its session, however long that is, and goes with the hub's lease (its row in hubs): it is
stale only once that hub has stopped renewing it. Any other claim is stale after GRID_STALE_AFTER.

All DATETIME columns hold UTC. "Now" is read from the database so that
online/stale decisions match the checks' slot selection, whatever the UI
host's clock says.

The connection settings match the orchestrator's: the mysql section of
src/database.yaml (or ORCHESTRATOR_CONFIG), overridden by DB_HOST, DB_PORT,
DB_USER, DB_PASSWORD and DB_NAME.
"""
import contextlib
import datetime
import hashlib
import logging
import os
import re
import secrets
import sqlite3
import urllib.parse

import yaml

DB_ENGINE = os.environ.get("GRID_DB_ENGINE", "mysql").lower()
SQLITE_PATH = os.environ.get("GRID_DB_PATH", "src/runner.db")
CONFIG_PATH = os.environ.get("ORCHESTRATOR_CONFIG", "src/database.yaml")
SCHEMA_PATH = os.environ.get("GRID_SCHEMA_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "schema.sql")
GATEWAY_URL = (os.environ.get("GATEWAY_URL") or "ws://hub:4500").rstrip("/")


def _fail_settings(env):
    """(threshold, cooldown s), read like the checks' slot selection reads them.

    A slot that failed `threshold` times in a row isn't handed out until `cooldown` seconds
    after its last run; then it gets another chance."""
    threshold = env.get("SLOT_FAIL_THRESHOLD") or env.get("GRID_FAIL_THRESHOLD") or "3"
    return int(threshold), int(env.get("SLOT_FAIL_COOLDOWN_S") or "300")


FAIL_THRESHOLD, FAIL_COOLDOWN_S = _fail_settings(os.environ)
STALE_AFTER_S = int(os.environ.get("GRID_STALE_AFTER", "3600"))
# The hub's GATEWAY_HUB_LEASE_S: a hub that hasn't renewed its row in hubs for this long is gone.
HUB_LEASE_S = int(os.environ.get("GATEWAY_HUB_LEASE_S") or "90")
ONLINE_WINDOW_S = 60  # same window as the checks' slot selection in src/database.py

log = logging.getLogger(__name__)

_ENV_OVERRIDES = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "database": "DB_NAME",
}
_schema_attempted = False


class DatabaseUnavailable(Exception):
    """The database can't be reached or queried.

    str() is the full detail for the server log and signed-in admins; .public says what went wrong
    without the file path or server names, for anyone else."""

    def __init__(self, message, public=None):
        super().__init__(message)
        self.public = public or "The database can't be reached."


class DatabasePermissionDenied(DatabaseUnavailable):
    """The database is up, but the UI's user isn't allowed to do this (e.g. a read-only user)."""


class _MissingTables(Exception):
    pass


class InvalidToken(ValueError):
    """A create request that doesn't pass validation; the message says why."""


class TokensUnavailable(Exception):
    """The database has no accessTokens table (yet)."""


# ER_DBACCESS_DENIED_ERROR, ER_TABLEACCESS_DENIED_ERROR, ER_COLUMNACCESS_DENIED_ERROR
_MYSQL_PERMISSION_ERRORS = {1044, 1142, 1143}


def _is_permission_error(error):
    if isinstance(error, sqlite3.OperationalError):
        return "readonly database" in str(error)
    return bool(error.args) and error.args[0] in _MYSQL_PERMISSION_ERRORS


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


def _is_missing_table(error):
    if isinstance(error, sqlite3.OperationalError):
        return "no such table" in str(error)
    return bool(error.args) and error.args[0] == 1146  # ER_NO_SUCH_TABLE


@contextlib.contextmanager
def _session():
    """A cursor on a fresh connection; DB errors come out as DatabaseUnavailable or _MissingTables."""
    if DB_ENGINE == "mysql":
        import pymysql
        db_errors = (pymysql.err.MySQLError,)
    else:
        db_errors = (sqlite3.Error,)
    try:
        if DB_ENGINE == "mysql":
            s = mysql_settings()
            con = pymysql.connect(
                host=s.get("host", "localhost"),
                port=s["port"],
                user=s.get("user"),
                password=s.get("password", ""),
                database=s.get("database", "orchestrator"),
                connect_timeout=5,
            )
        else:
            # mode=rw: don't silently create an empty file when the path is wrong.
            uri = "file:{}?mode=rw".format(urllib.parse.quote(os.path.abspath(SQLITE_PATH).replace("\\", "/"), safe="/:"))
            con = sqlite3.connect(uri, uri=True, timeout=5)
        with contextlib.closing(con), contextlib.closing(con.cursor()) as cur:
            try:
                yield cur
                con.commit()
            except BaseException:
                con.rollback()
                raise
    except db_errors as e:
        if _is_missing_table(e):
            raise _MissingTables(str(e)) from e
        if _is_permission_error(e):
            raise DatabasePermissionDenied(
                "The grid UI's database user lacks the privileges for this: {}".format(e),
                "The grid UI's database user lacks the privileges for this.") from e
        where = "" if DB_ENGINE == "mysql" else " ({})".format(SQLITE_PATH)
        raise DatabaseUnavailable("{}{}: {}".format(type(e).__name__, where, e), _public_reason(e)) from e


def _public_reason(error):
    """The kind of failure, without paths or host names."""
    code = error.args[0] if DB_ENGINE == "mysql" and error.args and isinstance(error.args[0], int) else None
    if code is not None:
        return "{}: MySQL error {}.".format(type(error).__name__, code)
    first = str(error).splitlines()[0] if str(error) else ""
    if first and "/" not in first and "\\" not in first:
        return "{}: {}.".format(type(error).__name__, first.rstrip("."))
    return "{}.".format(type(error).__name__)


def db_identity():
    """Which database this process reads, as a value that changes when the setting does."""
    if DB_ENGINE == "mysql":
        s = mysql_settings()
        return ("mysql", s.get("host"), s.get("port"), s.get("database"))
    return ("sqlite", os.path.abspath(SQLITE_PATH))


def _sql(sql):
    return sql.replace("?", "%s") if DB_ENGINE == "mysql" else sql


def _now_sql():
    return "UTC_TIMESTAMP()" if DB_ENGINE == "mysql" else "datetime('now')"


def _parse_time(value):
    """DB value -> naive UTC datetime (None when empty or unparseable)."""
    if value in (None, "", b""):
        return None
    if isinstance(value, bytes):
        value = value.decode()
    if not isinstance(value, datetime.datetime):
        try:
            value = datetime.datetime.fromisoformat(str(value).strip())
        except ValueError:
            return None
    if value.tzinfo is not None:
        value = value.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return value


def _iso(value):
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _age_s(now, then):
    return max(0, int((now - then).total_seconds())) if then else None


def _schema_statements(text):
    statement = []
    for line in text.splitlines():
        if line.strip() == ";":
            yield "\n".join(statement)
            statement = []
        else:
            statement.append(line)
    yield "\n".join(statement)


def ensure_schema():
    """Create the tables on MySQL if src/schema.sql is reachable. Tried once per process."""
    global _schema_attempted
    if _schema_attempted or DB_ENGINE != "mysql":
        return
    try:
        with open(SCHEMA_PATH) as stream:
            text = stream.read()
    except OSError:
        _schema_attempted = True
        log.info("schema not found at %s; waiting for the hub or a check to create the tables", SCHEMA_PATH)
        return
    with _session() as cur:  # DatabaseUnavailable propagates, so we try again next time
        _schema_attempted = True
        for statement in _schema_statements(text):
            code = "\n".join(l for l in statement.splitlines() if not l.strip().startswith("--")).strip()
            if code:
                try:
                    cur.execute(code)
                except Exception as e:  # e.g. no CREATE privilege: the UI still works read-only
                    log.warning("could not apply schema statement: %s", e)


def _tables(cur):
    if DB_ENGINE == "mysql":
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
    else:
        cur.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {str(r[0]).lower() for r in cur.fetchall()}


def _columns(cur, table):
    if DB_ENGINE == "mysql":
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
        return {str(r[0]).lower() for r in cur.fetchall()}
    cur.execute("PRAGMA table_info({})".format(table))
    return {str(r[1]).lower() for r in cur.fetchall()}


def _read(cur):
    """(now, node rows or None, slot rows, whether claimedBy exists, {hub instance: lastSeen} or None)."""
    cur.execute("SELECT " + _now_sql())
    now = _parse_time(cur.fetchone()[0])
    tables = _tables(cur)
    if not {"portstatus", "portusage"} <= tables:
        return now, None, [], False, None
    node_rows = []
    if "nodes" in tables:
        # ephemeral came later; databases created before it don't have the column.
        ephemeral = "ephemeral" if "ephemeral" in _columns(cur, "nodes") else "0"
        cur.execute("""
            SELECT name, online, slots, playwrightVersion, agentVersion, platform, browsers,
                   remoteAddress, registeredAt, connectedAt, lastSeen, {}
            FROM nodes
        """.format(ephemeral))
        node_rows = cur.fetchall()
    # claimedBy came with hub routing; the hub adds it to portUsage tables made before that.
    claimed_by = "pu.claimedBy" if "claimedby" in _columns(cur, "portUsage") else "NULL"
    cur.execute("""
        SELECT ps.id, ps.PortServer, ps.PortNumber, ps.PortStatus, pu.inUse, pu.LastRunDate, pu.failCount, {}
        FROM portStatus ps JOIN portUsage pu ON ps.id = pu.id
        ORDER BY ps.PortServer, ps.PortNumber
    """.format(claimed_by))
    slot_rows = cur.fetchall()
    hubs = None
    if "hubs" in tables:
        cur.execute("SELECT instance, lastSeen FROM hubs")
        hubs = {str(instance): _parse_time(seen) for instance, seen in cur.fetchall()}
    return now, node_rows, slot_rows, claimed_by != "NULL", hubs


def _hub_alive(now, hubs, claimed_by):
    """Whether the hub that made a 'hub:<instance>:<n>' claim still renews its lease; None when the
    database can't say (no hubs table: a hub from before leases)."""
    if hubs is None or not claimed_by:
        return None
    parts = str(claimed_by).split(":")
    seen = hubs.get(parts[1]) if len(parts) >= 3 else None
    return seen is not None and (now - seen).total_seconds() <= HUB_LEASE_S


def _stale(duration_s, via_hub, hub_alive):
    """A hub's claim is stale when its hub is gone; any other when it is older than GRID_STALE_AFTER."""
    if via_hub and hub_alive is not None:
        return not hub_alive
    return duration_s is None or duration_s > STALE_AFTER_S


def _claim(busy, claims_known, claimed_by):
    """(via_hub, claimed_by) of a slot: via_hub is None when free or when the database can't say."""
    if not busy or not claims_known:
        return None, None
    claimed_by = claimed_by.decode() if isinstance(claimed_by, bytes) else claimed_by
    return bool(claimed_by and str(claimed_by).startswith("hub:")), claimed_by or None


def _new_node(host, kind):
    return {
        "host": host, "kind": kind, "online": None, "status": None, "slots": [],
        "total": 0, "busy": 0, "free": 0, "failing": 0, "unhealthy": 0, "available": 0,
        "last_activity": None, "last_seen": None, "last_seen_s": None, "connected_at": None,
        "registered_at": None, "declared_slots": None, "playwright_version": None,
        "agent_version": None, "platform": None, "browsers": [], "remote_address": None,
        "ephemeral": False,
    }


def snapshot():
    """Current state of every node, slot and live session."""
    ensure_schema()
    try:
        with _session() as cur:
            now, node_rows, slot_rows, claims_known, hubs = _read(cur)
    except _MissingTables:  # dropped or half-created between queries
        now, node_rows, slot_rows, claims_known, hubs = \
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), None, [], False, None
    initialised = node_rows is not None

    # Match slots to nodes the way the database compares names: MySQL's default collation
    # ignores case, so the checks treat "win-build-01" slots as gateway node "WIN-BUILD-01"'s.
    key = (lambda name: name.casefold()) if DB_ENGINE == "mysql" else (lambda name: name)
    nodes = {}
    for (name, online, slots, pw, agent, platform, browsers, address,
         registered, connected, last_seen, ephemeral) in node_rows or []:
        if not (name or "").strip():
            continue
        node = nodes[key(name)] = _new_node(name, "gateway")
        seen = _parse_time(last_seen)
        # Same rule as the checks' slot selection: flagged online AND heard from recently.
        node["online"] = bool(online) and seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S
        node.update({
            "last_seen": _iso(seen), "last_seen_s": _age_s(now, seen),
            "connected_at": _iso(_parse_time(connected)), "registered_at": _iso(_parse_time(registered)),
            "declared_slots": slots, "playwright_version": pw, "agent_version": agent, "platform": platform,
            "browsers": [b.strip() for b in (browsers or "").split(",") if b.strip()],
            "remote_address": address, "ephemeral": bool(ephemeral),
        })

    sessions = []
    for slot_id, host, port, port_status, in_use, last_run, fails, claimed_by in slot_rows:
        busy = bool(in_use)
        fails = fails or 0
        started = _parse_time(last_run)
        via_hub, claimed_by = _claim(busy, claims_known, claimed_by)
        hub_alive = _hub_alive(now, hubs, claimed_by) if via_hub else None
        if not (host or "").strip():
            # Blank PortServer rows from old configs aren't real hosts and are never handed out.
            # One still marked in use can only be cleared by hand, so list it as a session.
            if busy:
                duration_s = _age_s(now, started)
                sessions.append({
                    "slot_id": slot_id, "host": "", "port": port, "kind": "legacy", "via_hub": via_hub, "claimed_by": claimed_by,
                    "hub_alive": hub_alive, "started": _iso(started), "duration_s": duration_s,
                    "stale": _stale(duration_s, via_hub, hub_alive),
                    "fails": fails, "node_online": None,
                })
            continue
        node = nodes.get(key(host)) or nodes.setdefault(key(host), _new_node(host, "static"))
        gateway = node["kind"] == "gateway"
        healthy = bool(port_status) if gateway else None
        failing = fails >= FAIL_THRESHOLD
        # A failing slot is skipped until FAIL_COOLDOWN_S after its last run (never run: no wait).
        retry_at = started + datetime.timedelta(seconds=FAIL_COOLDOWN_S) if failing and started else None
        cooling = failing and not busy and retry_at is not None and retry_at > now
        if gateway and not node["online"]:
            state = "offline"
        elif busy:
            state = "busy"
        elif gateway and not healthy:
            state = "unhealthy"
        elif failing:
            state = "failing"
        else:
            state = "free"
        selectable = not busy and not cooling and (not gateway or (node["online"] and healthy))

        node["total"] += 1
        node["busy"] += busy
        node["failing"] += failing
        node["unhealthy"] += gateway and node["online"] and not healthy
        node["available"] += selectable
        last = _parse_time(node["last_activity"]) if node["last_activity"] else None
        if started and (last is None or started > last):
            node["last_activity"] = _iso(started)
        node["slots"].append({
            "id": slot_id, "port": port, "busy": busy, "fails": fails, "healthy": healthy,
            "state": state, "selectable": selectable, "via_hub": via_hub,
            "last_run": _iso(started), "retry_at": _iso(retry_at) if cooling else None,
        })
        if busy:
            duration_s = _age_s(now, started)
            sessions.append({
                "slot_id": slot_id,
                "host": host,
                "port": port,
                "kind": node["kind"],
                "via_hub": via_hub,
                "claimed_by": claimed_by,
                "hub_alive": hub_alive,
                "started": _iso(started),
                "duration_s": duration_s,
                "stale": _stale(duration_s, via_hub, hub_alive),
                "fails": fails,
                "node_online": node["online"],
            })

    for node in nodes.values():
        node["free"] = node["total"] - node["busy"]
        if node["kind"] == "gateway":
            if not node["online"]:
                node["status"] = "OFFLINE"
            elif node["unhealthy"] or node["failing"]:
                node["status"] = "DEGRADED"
            else:
                node["status"] = "ONLINE"
        elif node["total"] and node["failing"] == node["total"]:
            node["status"] = "DOWN"
        elif node["failing"]:
            node["status"] = "DEGRADED"
        else:
            node["status"] = "UP"

    node_list = sorted(nodes.values(), key=lambda n: (n["kind"] != "gateway", n["host"].lower()))
    total = sum(n["total"] for n in node_list)
    busy = sum(n["busy"] for n in node_list)
    available = sum(n["available"] for n in node_list)
    return {
        "generated": _iso(now),
        "db": {"engine": DB_ENGINE, "initialised": initialised},
        "gateway_url": GATEWAY_URL,
        # The one address every check connects to; the hub picks the slot.
        "connect_url": GATEWAY_URL + "/connect",
        "fail_threshold": FAIL_THRESHOLD,
        "fail_cooldown_s": FAIL_COOLDOWN_S,
        "stale_after_s": STALE_AFTER_S,
        "hub_lease_s": HUB_LEASE_S,
        "online_window_s": ONLINE_WINDOW_S,
        "ready": available > 0,
        "totals": {
            "nodes": len(node_list),
            "gateway": sum(n["kind"] == "gateway" for n in node_list),
            "static": sum(n["kind"] == "static" for n in node_list),
            "online": sum(n["kind"] == "gateway" and n["online"] for n in node_list),
            "offline": sum(n["kind"] == "gateway" and not n["online"] for n in node_list),
            "slots": total,
            "busy": busy,
            "free": total - busy,
            "available": available,
            "failing": sum(n["failing"] for n in node_list),
            "unhealthy": sum(n["unhealthy"] for n in node_list),
            "stale": sum(s["stale"] for s in sessions),
        },
        "nodes": node_list,
        "sessions": sorted(sessions, key=lambda s: s["started"] or ""),
    }


def _execute(sql, params):
    try:
        with _session() as cur:
            cur.execute(_sql(sql), params)
            return cur.rowcount
    except _MissingTables:
        return 0


def _release_sql(cur):
    """The SET clause that frees a slot. It also clears claimedBy, as every release does. A hub whose
    session still runs on the slot claims it again (it never hands a slot it pipes to a second
    client); a claim left by a hub that is gone stays released."""
    clear = ", claimedBy = NULL" if "claimedby" in _columns(cur, "portUsage") else ""
    return "UPDATE portUsage SET inUse = 0, LastRunDate = {}{} WHERE id = ?".format(_now_sql(), clear)


def release_slot(slot_id):
    """Force a stuck slot back to free."""
    try:
        with _session() as cur:
            cur.execute(_sql(_release_sql(cur)), (slot_id,))
            return cur.rowcount
    except _MissingTables:
        return 0


def release_claim(slot_id, claimed_at):
    """Release a slot only if it still holds the claim the caller looked at (claimed_at: the
    session's "started" as /api/status reports it). A check that finished and a new one that claimed
    the slot meanwhile must not be cut off by a confirm that described the old one.

    Returns ("released", rows), ("reclaimed", 0), ("free", 0) or ("not_found", 0)."""
    try:
        with _session() as cur:
            if DB_ENGINE == "mysql":
                lock = " FOR UPDATE"
            else:
                cur.execute("BEGIN IMMEDIATE")
                lock = ""
            cur.execute(_sql("SELECT inUse, LastRunDate FROM portUsage WHERE id = ?" + lock), (slot_id,))
            row = cur.fetchone()
            if row is None:
                return "not_found", 0
            if not row[0]:
                return "free", 0
            if _iso(_parse_time(row[1])) != claimed_at:
                return "reclaimed", 0
            cur.execute(_sql(_release_sql(cur)), (slot_id,))
            return "released", cur.rowcount
    except _MissingTables:
        return "not_found", 0


def reset_slot_failures(slot_id):
    return _execute("UPDATE portUsage SET failCount = 0 WHERE id = ?", (slot_id,))


def reset_node_failures(host):
    return _execute(
        "UPDATE portUsage SET failCount = 0 WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?)",
        (host,))


def forget_node(name):
    """Delete an offline gateway node and its slots.

    Returns ("forgotten", slots_deleted), ("not_found", 0), ("online", 0) or ("in_use", busy_count).
    """
    try:
        with _session() as cur:
            if DB_ENGINE == "mysql":
                lock = " FOR UPDATE"
            else:
                cur.execute("BEGIN IMMEDIATE")  # hold the write lock across check and delete
                lock = ""
            cur.execute(_sql("SELECT " + _now_sql() + ", online, lastSeen FROM nodes WHERE name = ?" + lock), (name,))
            row = cur.fetchone()
            if row is None:
                return "not_found", 0
            now, online, seen = _parse_time(row[0]), row[1], _parse_time(row[2])
            if online and seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S:
                return "online", 0
            cur.execute(_sql("""
                SELECT COUNT(*) FROM portStatus ps JOIN portUsage pu ON ps.id = pu.id
                WHERE ps.PortServer = ? AND pu.inUse = 1
            """ + lock), (name,))
            busy = cur.fetchone()[0]
            if busy:
                return "in_use", busy
            # portUsage cascades on MySQL, but sqlite only enforces foreign keys when asked to.
            cur.execute(_sql("DELETE FROM portUsage WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?)"), (name,))
            cur.execute(_sql("DELETE FROM portStatus WHERE PortServer = ?"), (name,))
            deleted = cur.rowcount
            cur.execute(_sql("DELETE FROM nodes WHERE name = ?"), (name,))
            return "forgotten", deleted
    except _MissingTables:
        return "not_found", 0


# ---- managed access tokens ----------------------------------------------------
#
# The hub accepts these besides its env bootstrap tokens (GATEWAY_AGENT_TOKEN and
# GATEWAY_CLIENT_TOKEN). Only the UI makes them and only their sha256 is stored: the plaintext
# is returned once, by create_token, and never logged.

TOKEN_PREFIXES = {"agent": "pwga_", "client": "pwgc_"}
_MAX_TOKEN_ID = 2 ** 31 - 1  # accessTokens.id is an INT; sqlite can't even bind a much larger number
_NODE_NAME = re.compile(r"[a-z0-9._-]{1,128}\Z")
_TOKEN_COLUMNS = "id, name, kind, prefix, nodeName, createdAt, expiresAt, lastUsedAt, revokedAt"
_NO_TOKEN_TABLE = ("This database has no accessTokens table yet. The gateway hub creates it when it starts, "
                   "and so does the grid UI on MySQL when its database user may create tables.")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _db_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _token_status(now, expires, revoked):
    if revoked is not None:
        return "revoked"
    if expires is not None and expires <= now:
        return "expired"
    return "active"


def _token(row, now):
    token_id, name, kind, prefix, node_name = row[:5]
    created, expires, last_used, revoked = (_parse_time(v) for v in row[5:])
    return {
        "id": token_id, "name": name, "kind": kind, "prefix": prefix, "nodeName": node_name,
        "createdAt": _iso(created), "expiresAt": _iso(expires), "lastUsedAt": _iso(last_used),
        "revokedAt": _iso(revoked), "status": _token_status(now, expires, revoked),
        "nodes": [], "nodesOnline": [],
    }


def list_tokens():
    """{"ready": bool, "generated": ISO, "tokens": [...]}, newest first.

    ready is False while the database has no accessTokens table (an older demo database, or a
    MySQL user that can't create it). Each token lists the nodes that last registered with it."""
    ensure_schema()
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            tables = _tables(cur)
            if "accesstokens" not in tables:
                return {"ready": False, "generated": _iso(now), "tokens": []}
            cur.execute("SELECT " + _TOKEN_COLUMNS + " FROM accessTokens ORDER BY createdAt DESC, id DESC")
            tokens = [_token(row, now) for row in cur.fetchall()]
            users = []
            # tokenId came with managed tokens; the hub adds it to nodes tables made before that.
            if "nodes" in tables and "tokenid" in _columns(cur, "nodes"):
                cur.execute("SELECT tokenId, name, online, lastSeen FROM nodes WHERE tokenId IS NOT NULL")
                users = cur.fetchall()
    except _MissingTables:  # dropped between queries
        return {"ready": False, "generated": _iso(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)), "tokens": []}
    by_id = {t["id"]: t for t in tokens}
    for token_id, name, online, last_seen in sorted(users, key=lambda u: str(u[1]).casefold()):
        token = by_id.get(token_id)
        if token is None:
            continue
        token["nodes"].append(name)
        seen = _parse_time(last_seen)
        if online and seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S:
            token["nodesOnline"].append(name)
    return {"ready": True, "generated": _iso(now), "tokens": tokens}


def _validate_token_request(name, kind, node_name, expires_in_days):
    # No control characters, and no lone surrogates: JSON can carry one, but no database can store it.
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 128 or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in name):
        raise InvalidToken("Give the token a name of 1 to 128 characters.")
    if not isinstance(kind, str) or kind not in TOKEN_PREFIXES:
        raise InvalidToken("kind must be 'agent' or 'client'.")
    if node_name is not None and node_name != "":
        if kind != "agent":
            raise InvalidToken("Only agent tokens can be bound to a node.")
        if not isinstance(node_name, str) or not _NODE_NAME.match(node_name.strip().lower()):
            raise InvalidToken("A node name is 1 to 128 characters: letters, digits, '.', '_' and '-'.")
        node_name = node_name.strip().lower()
    else:
        node_name = None
    if expires_in_days is not None and (isinstance(expires_in_days, bool) or not isinstance(expires_in_days, int)
                                        or not 1 <= expires_in_days <= 3650):
        raise InvalidToken("expiresInDays must be a whole number of days from 1 to 3650, or null for no expiry.")
    return name.strip(), kind, node_name, expires_in_days


def create_token(name, kind, node_name=None, expires_in_days=None):
    """Make a managed token. The result's "token" is the plaintext: the only time it's available.

    Raises InvalidToken for a bad request and TokensUnavailable without an accessTokens table."""
    name, kind, node_name, expires_in_days = _validate_token_request(name, kind, node_name, expires_in_days)
    ensure_schema()
    token = TOKEN_PREFIXES[kind] + secrets.token_urlsafe(32)
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0]).replace(microsecond=0)
            expires = now + datetime.timedelta(days=expires_in_days) if expires_in_days else None
            cur.execute(_sql("""
                INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """), (name, kind, token_hash(token), token[:12], node_name, _db_time(now), _db_time(expires)))
            token_id = cur.lastrowid
    except _MissingTables as e:
        raise TokensUnavailable(_NO_TOKEN_TABLE) from e
    result = _token((token_id, name, kind, token[:12], node_name, now, expires, None, None), now)
    result["token"] = token
    return result


def revoke_token(token_id):
    """Revoke a token (revoking it again keeps the first time). Returns the token, or None if unknown."""
    if not 1 <= token_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            cur.execute(_sql("UPDATE accessTokens SET revokedAt = {} WHERE id = ? AND revokedAt IS NULL".format(_now_sql())),
                        (token_id,))
            cur.execute(_sql("SELECT {}, {} FROM accessTokens WHERE id = ?".format(_now_sql(), _TOKEN_COLUMNS)), (token_id,))
            row = cur.fetchone()
    except _MissingTables:
        return None
    return _token(row[1:], _parse_time(row[0])) if row else None


def delete_token(token_id):
    """Delete a revoked or expired token and forget which nodes used it.

    Returns "deleted", "not_found" or "active" (revoke it first)."""
    if not 1 <= token_id <= _MAX_TOKEN_ID:
        return "not_found"
    try:
        with _session() as cur:
            if DB_ENGINE == "mysql":
                lock = " FOR UPDATE"
            else:
                cur.execute("BEGIN IMMEDIATE")  # hold the write lock across check and delete
                lock = ""
            cur.execute(_sql("SELECT " + _now_sql() + ", expiresAt, revokedAt FROM accessTokens WHERE id = ?" + lock),
                        (token_id,))
            row = cur.fetchone()
            if row is None:
                return "not_found"
            if _token_status(*(_parse_time(v) for v in row)) == "active":
                return "active"
            if "tokenid" in _columns(cur, "nodes"):
                cur.execute(_sql("UPDATE nodes SET tokenId = NULL WHERE tokenId = ?"), (token_id,))
            cur.execute(_sql("DELETE FROM accessTokens WHERE id = ?"), (token_id,))
            return "deleted"
    except _MissingTables:
        return "not_found"
