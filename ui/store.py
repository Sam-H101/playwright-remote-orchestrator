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
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import zlib

import yaml

from . import mfa

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


class MfaUnavailable(Exception):
    """The database has no MFA tables (and the UI couldn't create them)."""


class MfaBusy(Exception):
    """Every time step a code may come from has been handed out already: ask again in retry_after_s."""

    def __init__(self, retry_after_s):
        super().__init__("Codes for this secret are being handed out faster than they change. Try again in {:.0f} s.".format(retry_after_s))
        self.retry_after_s = retry_after_s


class TokensUnavailable(Exception):
    """The database has no accessTokens table (yet)."""


class InvalidSignin(ValueError):
    """A sign-in request that doesn't pass validation; the message says why and never quotes the state."""


class SigninTooLarge(InvalidSignin):
    """A state over SIGNIN_MAX_STATE_BYTES."""


class LeaseLost(Exception):
    """The lease token doesn't hold the lease: it is wrong, or the lease ran out (and may be someone else's now)."""


class SigninsFull(Exception):
    """A new name, while SIGNIN_MAX_COUNT sign-ins are kept already."""


class SigninsUnavailable(Exception):
    """The database has no savedSignins table (and the UI couldn't create it)."""


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


def client_token_name(token):
    """The name of the active managed client token `token` is, or None. Stamps the token's lastUsedAt,
    as the hub does when a check connects with it."""
    if not token:
        return None
    try:
        with _session() as cur:
            cur.execute(_sql("SELECT {}, id, name, expiresAt, revokedAt FROM accessTokens WHERE tokenHash = ? AND kind = 'client'"
                             .format(_now_sql())), (token_hash(token),))
            row = cur.fetchone()
            if row is None or _token_status(_parse_time(row[0]), _parse_time(row[3]), _parse_time(row[4])) != "active":
                return None
            cur.execute(_sql("UPDATE accessTokens SET lastUsedAt = {} WHERE id = ?".format(_now_sql())), (row[1],))
            return row[2]
    except _MissingTables:
        return None


# ---- MFA secrets ----------------------------------------------------------------
#
# Authenticator-app enrolments (TOTP seeds) for checks that sign in with two-step verification. The
# seed is encrypted before it reaches the database (see mfa.py) and never leaves this process: checks
# and admins get codes. Each time step is handed out once, like sites accept each code once, so checks
# that sign in to the same account at the same time never get the same code.

_MFA_NAME = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_MFA_COLUMNS = "id, name, issuer, account, algorithm, digits, period, createdAt, lastStep, lastIssuedAt, lastIssuedTo, issuedCount"
# A code is handed out at most this many steps ahead of the current one; past that, MfaBusy.
MFA_MAX_AHEAD_STEPS = 2
# A code handed out has at least this much of its window left, unless the caller asks otherwise.
MFA_MIN_VALID_S = 5
MFA_LOG_DAYS = 7
MFA_LOG_LIMIT = 100
_NO_MFA_TABLE = ("This database has no mfaSecrets table yet. The grid UI creates it on MySQL when its database user may "
                 "create tables, and so does the gateway hub when it starts.")


def _days_ago_sql(days):
    return ("UTC_TIMESTAMP() - INTERVAL {} DAY" if DB_ENGINE == "mysql" else "datetime('now', '-{} days')").format(int(days))


def _optional_text(value, what, limit):
    """Free text for display: None or "" -> None; no control characters (nor lone surrogates)."""
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value.strip()) > limit or any(
            ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise mfa.InvalidSecret("{} is text of at most {} characters.".format(what, limit))
    return value.strip() or None


def _mfa(row):
    secret_id, name, issuer, account, algorithm, digits, period = row[:7]
    created, last_step, last_issued, last_to, count = row[7:]
    return {"id": secret_id, "name": name, "issuer": issuer, "account": account, "algorithm": algorithm,
            "digits": digits, "period": period, "createdAt": _iso(_parse_time(created)),
            "lastStep": last_step, "lastIssuedAt": _iso(_parse_time(last_issued)), "lastIssuedTo": last_to,
            "issuedCount": count}


def _is_duplicate_key(error):
    if isinstance(error, sqlite3.IntegrityError):
        return "UNIQUE constraint failed" in str(error)
    return type(error).__name__ == "IntegrityError" and bool(error.args) and error.args[0] == 1062  # ER_DUP_ENTRY


def _lock(cur):
    """The row lock for a read-then-write transaction: FOR UPDATE on MySQL, the write lock on sqlite."""
    if DB_ENGINE == "mysql":
        return " FOR UPDATE"
    cur.execute("BEGIN IMMEDIATE")
    return ""


def list_mfa():
    """{"ready", "generated", "secrets": [...], "issues": [the newest MFA_LOG_LIMIT hand-outs]}, without seeds.

    ready is False while the database has no MFA tables."""
    ensure_schema()
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            if not {"mfasecrets", "mfaissues"} <= _tables(cur):
                return {"ready": False, "generated": _iso(now), "secrets": [], "issues": []}
            cur.execute("SELECT " + _MFA_COLUMNS + " FROM mfaSecrets ORDER BY name")
            secrets_ = [_mfa(row) for row in cur.fetchall()]
            # The page says "last 7 days": rows not purged yet don't count.
            cur.execute("""
                SELECT i.id, i.secretId, s.name, i.step, i.issuedAt, i.issuedTo, i.label
                FROM mfaIssues i JOIN mfaSecrets s ON s.id = i.secretId
                WHERE i.issuedAt >= {}
                ORDER BY i.id DESC LIMIT {}
            """.format(_days_ago_sql(MFA_LOG_DAYS), MFA_LOG_LIMIT))
            issues = [{"id": i, "secretId": sid, "name": name, "step": step, "issuedAt": _iso(_parse_time(at)),
                       "issuedTo": to, "label": label} for i, sid, name, step, at, to, label in cur.fetchall()]
    except _MissingTables:
        return {"ready": False, "generated": None, "secrets": [], "issues": []}
    return {"ready": True, "generated": _iso(now), "secrets": secrets_, "issues": issues}


def create_mfa(name, secret, issuer=None, account=None, algorithm=None, digits=None, period=None):
    """Store a secret. `secret` is a base32 seed or an otpauth://totp/ URI (whose issuer, account and
    settings are used unless given here). Returns the secret as list_mfa shows it, never the seed.

    Raises mfa.InvalidSecret, mfa.SecretsKeyMissing or MfaUnavailable."""
    if not isinstance(name, str) or not _MFA_NAME.match(name.strip()):
        raise mfa.InvalidSecret("A secret's name is 1 to 64 characters: letters, digits, '.', '_' and '-'. "
                                "Checks ask for codes by this name.")
    name = name.strip()
    if isinstance(secret, str) and secret.strip().lower().startswith("otpauth:"):
        parsed = mfa.parse_otpauth(secret)
        seed = parsed["seed"]
        issuer = parsed["issuer"] if issuer in (None, "") else issuer
        account = parsed["account"] if account in (None, "") else account
        algorithm = parsed["algorithm"] if algorithm is None else algorithm
        digits = parsed["digits"] if digits is None else digits
        period = parsed["period"] if period is None else period
    else:
        seed = mfa.normalize_seed(secret)
    algorithm, digits, period = mfa.validate_settings(algorithm, digits, period)
    issuer = _optional_text(issuer, "The issuer", 128)
    account = _optional_text(account, "The account", 255)
    cipher = mfa.encrypt(seed, name)  # SecretsKeyMissing before anything is written
    taken = mfa.InvalidSecret("There is already a secret called {!r}. Delete it first to replace it.".format(name))
    ensure_schema()
    try:
        with _session() as cur:
            # No locking read: on MySQL, FOR UPDATE on a name nobody has locks a gap, and two creates
            # holding it deadlock on their INSERTs. uq_mfa_name decides a race; this is the common case.
            cur.execute(_sql("SELECT id FROM mfaSecrets WHERE name = ?"), (name,))
            if cur.fetchone() is not None:
                raise taken
            try:
                cur.execute(_sql("""
                    INSERT INTO mfaSecrets (name, issuer, account, algorithm, digits, period, seedCipher, createdAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, {})
                """.format(_now_sql())), (name, issuer, account, algorithm, digits, period, cipher))
            except Exception as e:
                if not _is_duplicate_key(e):
                    raise
                raise taken from None
            cur.execute(_sql("SELECT " + _MFA_COLUMNS + " FROM mfaSecrets WHERE id = ?"), (cur.lastrowid,))
            return _mfa(cur.fetchone())
    except _MissingTables as e:
        raise MfaUnavailable(_NO_MFA_TABLE) from e


def delete_mfa(secret_id):
    """Delete a secret and its hand-out log. Returns False if there is no such secret."""
    if not 1 <= secret_id <= _MAX_TOKEN_ID:
        return False
    try:
        with _session() as cur:
            cur.execute(_sql("DELETE FROM mfaSecrets WHERE id = ?"), (secret_id,))
            deleted = cur.rowcount
            cur.execute(_sql("DELETE FROM mfaIssues WHERE secretId = ?"), (secret_id,))
            return bool(deleted)
    except _MissingTables:
        return False


def _window(t, step, period):
    """(seconds until the step's window opens (0 if it has), seconds from now until it closes)."""
    return round(max(0.0, step * period - t), 3), round((step + 1) * period - t, 3)


def issue_mfa_code(issued_to, name=None, secret_id=None, label=None, min_valid_s=None, now=None):
    """Hand out the code of a time step nobody has had yet, for the secret called `name` (or with id
    `secret_id`): the current step if it has at least min_valid_s left and is still free, else the next
    free one. A code from a later step is handed out before its window opens: waitS says how long to
    wait before typing it, validForS how long until it stops working (both from now, by this server's
    clock, which must be in sync with the site's).

    Returns {"id", "name", "code", "step", "waitS", "validForS", "period", "digits"}, or None for an
    unknown secret. Raises MfaBusy when MFA_MAX_AHEAD_STEPS steps ahead are taken, mfa.CannotDecrypt
    (nothing is used up then) and MfaUnavailable."""
    label = _optional_text(label, "The label", 128)
    if secret_id is not None and not 1 <= secret_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            lock = _lock(cur)
            where, arg = ("name = ?", name) if secret_id is None else ("id = ?", secret_id)
            cur.execute(_sql("SELECT id, name, algorithm, digits, period, seedCipher, lastStep FROM mfaSecrets WHERE "
                             + where + lock), (arg,))
            row = cur.fetchone()
            if row is None:
                return None
            sid, name, algorithm, digits, period, cipher, last_step = row
            seed = mfa.decrypt(cipher, name)
            t = time.time() if now is None else now
            min_valid = MFA_MIN_VALID_S if min_valid_s is None else min_valid_s
            # Compared before float(): JSON can send an integer too large for a float.
            min_valid = max(0.0, float(min(min_valid, period - 1.0)))
            current = mfa.step_at(t, period)
            step = current if period - t % period >= min_valid else current + 1
            if last_step is not None and step <= last_step:
                step = last_step + 1
            if step - current > MFA_MAX_AHEAD_STEPS:
                raise MfaBusy(max(1.0, (step - MFA_MAX_AHEAD_STEPS) * period - t))
            cur.execute(_sql("UPDATE mfaSecrets SET lastStep = ?, lastIssuedAt = {now}, lastIssuedTo = ?, "
                             "issuedCount = issuedCount + 1 WHERE id = ?".format(now=_now_sql())), (step, issued_to, sid))
            cur.execute(_sql("INSERT INTO mfaIssues (secretId, step, issuedAt, issuedTo, label) VALUES (?, ?, {}, ?, ?)"
                             .format(_now_sql())), (sid, step, issued_to, label))
            # Every secret's old hand-outs, not only this one's: a secret nobody asks for again keeps none.
            # By id: on MySQL, a DELETE by issuedAt would lock every row it scans, other secrets' new ones too.
            cur.execute("SELECT id FROM mfaIssues WHERE issuedAt < " + _days_ago_sql(MFA_LOG_DAYS))
            old = [str(int(i)) for (i,) in cur.fetchall()]
            if old:
                cur.execute("DELETE FROM mfaIssues WHERE id IN ({})".format(", ".join(old)))
    except _MissingTables as e:
        raise MfaUnavailable(_NO_MFA_TABLE) from e
    wait_s, valid_for_s = _window(t, step, period)
    return {"id": sid, "name": name, "code": mfa.code(seed, step, digits, algorithm), "step": step,
            "waitS": wait_s, "validForS": valid_for_s, "period": period, "digits": digits}


def peek_mfa_code(secret_id, now=None):
    """The current step's code for an admin to look at, using nothing up. "issued" says whether that
    step was already handed out (a site would refuse it from a second sign-in).

    Returns {"id", "name", "code", "step", "secondsLeft", "period", "digits", "issued"} or None."""
    if not 1 <= secret_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            cur.execute(_sql("SELECT id, name, algorithm, digits, period, seedCipher, lastStep FROM mfaSecrets WHERE id = ?"),
                        (secret_id,))
            row = cur.fetchone()
    except _MissingTables:
        return None
    if row is None:
        return None
    sid, name, algorithm, digits, period, cipher, last_step = row
    seed = mfa.decrypt(cipher, name)
    t = time.time() if now is None else now
    step = mfa.step_at(t, period)
    return {"id": sid, "name": name, "code": mfa.code(seed, step, digits, algorithm), "step": step,
            "secondsLeft": round(period - t % period, 3), "period": period, "digits": digits,
            "issued": last_step is not None and step <= last_step}


# ---- saved sign-ins -------------------------------------------------------------
#
# Signed-in browser states (Playwright's storage_state: cookies and local storage) that checks reuse from
# run to run, so most runs skip the sign-in and its MFA prompt, and the site sees few sign-ins. A state is
# encrypted before it reaches the database (see mfa.seal) and only checks get it back: admins see what is
# saved and who used it, never a cookie. When nothing usable is saved for a name, one check at a time signs
# in (it holds the lease) and the others wait for the state it saves.

_SIGNIN_COLUMNS = ("id, name, version, stateCipher IS NOT NULL, domains, cookieCount, originCount, savedAt, savedBy, "
                   "expiresAt, signInCount, refreshCount, useCount, lastUsedAt, lastUsedBy, rejectedCount, lastRejectedAt, "
                   "leaseHash IS NOT NULL, leaseUntil, leaseHolder")
# Checks add names themselves (a name's first checkout adds it), so their number is capped. Checks adding
# names at the very same moment can get a few past it.
SIGNIN_MAX_COUNT = 200
SIGNIN_MAX_STATE_BYTES = 2 * 1024 * 1024  # the state as compact JSON
# A request may carry the state with spacing and escapes the stored form doesn't have.
SIGNIN_MAX_BODY_BYTES = 4 * SIGNIN_MAX_STATE_BYTES
SIGNIN_LEASE_S = 180
SIGNIN_LEASE_RANGE_S = (30, 600)
SIGNIN_MAX_AGE_S = 30 * 86400
SIGNIN_MAX_AGE_RANGE_S = (60, 90 * 86400)
# How long a check waits for another check's sign-in before it asks again.
SIGNIN_RETRY_RANGE_S = (1, 5)
_LEASE_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")  # what secrets.token_urlsafe makes
_COOKIE_DOMAIN = re.compile(r"[a-z0-9_.:\[\]-]{1,253}\Z")
_MAX_DOMAINS_TEXT = 1024
_NO_SIGNIN_TABLE = ("This database has no savedSignins table yet. The grid UI creates it on MySQL when its database user may "
                    "create tables, and so does the gateway hub when it starts.")


def _signin_name(name):
    # The rule of MFA secrets' names.
    if not isinstance(name, str) or not _MFA_NAME.match(name.strip()):
        raise InvalidSignin("A sign-in's name is 1 to 64 characters: letters, digits, '.', '_' and '-'.")
    return name.strip()


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _version(value, what):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise InvalidSignin("{} is a version number: a whole number, 0 or more.".format(what))
    return value


def _signin_id(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 1):
        raise InvalidSignin("id is the sign-in's id, as the checkout answered it.")
    return value


def _clamp(value, low, high):
    """A number from JSON (any size, NaN even) as whole seconds from low to high."""
    if value >= high:
        return high
    if not value >= low:
        return low
    return int(value)


def _cookie_domains(cookies):
    """The cookies' domains for display: lower case, without the leading dot, comma separated, at most
    _MAX_DOMAINS_TEXT characters (or None)."""
    domains = sorted({c["domain"].strip().lower().lstrip(".") for c in cookies if isinstance(c.get("domain"), str)})
    kept, length = [], -1
    for domain in domains:
        if _COOKIE_DOMAIN.match(domain) and length + 1 + len(domain) <= _MAX_DOMAINS_TEXT:
            kept.append(domain)
            length += 1 + len(domain)
    return ",".join(kept) or None


def _pack_state(state):
    """(the state as compressed JSON, ready to seal; its cookies' domains; cookie count; origin count)."""
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
        raise InvalidSignin('Send the state as Playwright saves it: an object with a "cookies" list and an "origins" list.')
    if not all(isinstance(item, dict) for item in state["cookies"] + state["origins"]):
        raise InvalidSignin("Every cookie and every origin of the state is an object.")
    try:
        serialized = json.dumps(state, separators=(",", ":"), allow_nan=False).encode()
    except ValueError:
        raise InvalidSignin("The state holds a number JSON can't carry (NaN or Infinity).") from None
    if len(serialized) > SIGNIN_MAX_STATE_BYTES:
        raise SigninTooLarge("The state is {} bytes as JSON; a saved sign-in holds at most {}.".format(
            len(serialized), SIGNIN_MAX_STATE_BYTES))
    return zlib.compress(serialized), _cookie_domains(state["cookies"]), len(state["cookies"]), len(state["origins"])


def _open_state(cipher, name):
    """The state saved under that name. Raises mfa.CannotDecrypt and mfa.SecretsKeyMissing."""
    try:
        return json.loads(zlib.decompress(mfa.unseal(cipher, name, "signin")))
    except (zlib.error, ValueError):
        raise mfa.CannotDecrypt("The saved sign-in {!r} decrypts, but isn't a state.".format(name)) from None


def _need_signin_key():
    problem = mfa.key_problem("signin")
    if problem:
        raise mfa.SecretsKeyMissing(problem)


def _lease_live(lease_hash, lease_until, now):
    return lease_hash is not None and lease_until is not None and lease_until > now


def _holds(lease_hash, lease_token):
    """Whether lease_token is the token of the lease whose hash is stored (compared in constant time)."""
    return lease_hash is not None and bool(_LEASE_TOKEN.match(lease_token)) and \
        hmac.compare_digest(str(lease_hash), token_hash(lease_token))


def _signin(row, now):
    (signin_id, name, version, saved, domains, cookies, origins, saved_at, saved_by, expires, sign_ins, refreshes, uses,
     last_used, last_used_by, rejected, last_rejected, leased, lease_until, holder) = row
    expires, lease_until = _parse_time(expires), _parse_time(lease_until)
    live = bool(leased) and lease_until is not None and lease_until > now
    return {"id": signin_id, "name": name, "version": version, "saved": bool(saved),
            "domains": [d for d in (domains or "").split(",") if d], "cookieCount": cookies, "originCount": origins,
            "savedAt": _iso(_parse_time(saved_at)), "savedBy": saved_by, "expiresAt": _iso(expires),
            "expired": bool(saved) and expires is not None and expires <= now,
            "signInCount": sign_ins, "refreshCount": refreshes, "useCount": uses,
            "lastUsedAt": _iso(_parse_time(last_used)), "lastUsedBy": last_used_by,
            "rejectedCount": rejected, "lastRejectedAt": _iso(_parse_time(last_rejected)),
            "leaseHolder": holder if live else None,
            "leaseForS": max(1, int((lease_until - now).total_seconds())) if live else None}


def list_signins():
    """{"ready", "generated", "signins": [...]}: what is saved for each name and who used it, never a state.

    ready is False while the database has no savedSignins table."""
    ensure_schema()
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            if "savedsignins" not in _tables(cur):
                return {"ready": False, "generated": _iso(now), "signins": []}
            cur.execute("SELECT " + _SIGNIN_COLUMNS + " FROM savedSignins ORDER BY name")
            signins = [_signin(row, now) for row in cur.fetchall()]
    except _MissingTables:
        return {"ready": False, "generated": None, "signins": []}
    return {"ready": True, "generated": _iso(now), "signins": signins}


def _add_signin(name):
    """Drop every state past its expiresAt, and add the row of a name no check used yet (SigninsFull past
    SIGNIN_MAX_COUNT names)."""
    with _session() as cur:
        # Every name's, not only this one's: a name nobody checks out again keeps nothing. By id: on MySQL, an
        # UPDATE by expiresAt would lock every row it scans, the rows other checkouts hold too.
        cur.execute("SELECT id FROM savedSignins WHERE stateCipher IS NOT NULL AND expiresAt <= " + _now_sql())
        expired = [str(int(i)) for (i,) in cur.fetchall()]
        if expired:
            cur.execute("UPDATE savedSignins SET stateCipher = NULL WHERE id IN ({}) AND expiresAt <= {}".format(
                ", ".join(expired), _now_sql()))
        # No locking read: on MySQL, FOR UPDATE on a name nobody has locks a gap, and two checkouts holding it
        # deadlock on their INSERTs. uq_signin_name decides a race.
        cur.execute(_sql("SELECT id FROM savedSignins WHERE name = ?"), (name,))
        if cur.fetchone() is not None:
            return
        cur.execute("SELECT COUNT(*) FROM savedSignins")
        if cur.fetchone()[0] >= SIGNIN_MAX_COUNT:
            raise SigninsFull("There are {} saved sign-ins already, the most the grid keeps. Delete some on the Grid UI "
                              "to use new names.".format(SIGNIN_MAX_COUNT))
        try:
            cur.execute(_sql("INSERT INTO savedSignins (name, createdAt) VALUES (?, {})".format(_now_sql())), (name,))
        except Exception as e:
            if not _is_duplicate_key(e):
                raise


def checkout_signin(used_by, name, rejected_version=None, lease_s=None, signin_id=None):
    """What a check should do to be signed in as `name`:

    {"status": "saved", "id", "name", "version", "state", "savedAt", "expiresAt"}: open a context with that
        state, and send id with its version when saving it again.
    {"status": "sign-in", "id", "name", "version", "leaseToken", "leaseForS"}: nothing usable is saved, so sign
        in and save the state with the lease token within leaseForS seconds (lease_s, by default
        SIGNIN_LEASE_S), or release it. Nobody else is told to sign in for the name meanwhile.
    {"status": "wait", "name", "retryAfterS"}: another check is signing in; ask again after retryAfterS seconds.

    rejected_version is the version of a state the site refused (it asked to sign in again), and signin_id
    the id it was checked out with: while that is still the saved one, it is dropped, else the newer one is
    answered. A name no check used yet is added.
    Raises InvalidSignin, SigninsFull, mfa.SecretsKeyMissing (before anything is written) and SigninsUnavailable."""
    name = _signin_name(name)
    rejected_version = _version(rejected_version, "rejectedVersion")
    signin_id = _signin_id(signin_id)
    if lease_s is not None and not _is_number(lease_s):
        raise InvalidSignin("leaseS is a number of seconds.")
    lease_s = SIGNIN_LEASE_S if lease_s is None else _clamp(lease_s, *SIGNIN_LEASE_RANGE_S)
    _need_signin_key()  # a check told to sign in couldn't save what it signed in with
    ensure_schema()
    try:
        while True:
            _add_signin(name)
            with _session() as cur:
                lock = _lock(cur)
                cur.execute("SELECT " + _now_sql())
                now = _parse_time(cur.fetchone()[0])
                cur.execute(_sql("SELECT id, name, version, stateCipher, savedAt, expiresAt, leaseHash, leaseUntil "
                                 "FROM savedSignins WHERE name = ?" + lock), (name,))
                row = cur.fetchone()
                if row is not None:
                    return _checkout(cur, now, row, used_by, rejected_version, signin_id, lease_s)
            # An admin deleted it in between: add it again.
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e


def _checkout(cur, now, row, used_by, rejected_version, rejected_id, lease_s):
    signin_id, name, version, cipher, saved_at, expires, lease_hash, lease_until = row
    expires, lease_until = _parse_time(expires), _parse_time(lease_until)
    # The refused state by its version and the id it came with (a name deleted and added again starts over at
    # version 0 under a new id), or by its version alone when no id is sent.
    if cipher is not None and rejected_version == version and rejected_id in (None, signin_id):
        cur.execute(_sql("UPDATE savedSignins SET stateCipher = NULL, rejectedCount = rejectedCount + 1, lastRejectedAt = ? "
                         "WHERE id = ?"), (_db_time(now), signin_id))
        cipher = None
        log.info("saved sign-in %r: the site asked %s to sign in again with version %d", name, used_by, version)
    state = None
    if cipher is not None and version > 0 and (expires is None or expires > now):
        try:
            state = _open_state(cipher, name)
        except mfa.CannotDecrypt as e:
            log.warning("saved sign-in %r: %s A check signs in again.", name, e)
    if state is not None:
        cur.execute(_sql("UPDATE savedSignins SET useCount = useCount + 1, lastUsedAt = ?, lastUsedBy = ? WHERE id = ?"),
                    (_db_time(now), used_by, signin_id))
        return {"status": "saved", "id": signin_id, "name": name, "version": version, "state": state,
                "savedAt": _iso(_parse_time(saved_at)), "expiresAt": _iso(expires)}
    if _lease_live(lease_hash, lease_until, now):
        low, high = SIGNIN_RETRY_RANGE_S
        return {"status": "wait", "name": name,
                "retryAfterS": min(max(int((lease_until - now).total_seconds()), low), high)}
    token = secrets.token_urlsafe(32)
    cur.execute(_sql("UPDATE savedSignins SET leaseHash = ?, leaseUntil = ?, leaseHolder = ? WHERE id = ?"),
                (token_hash(token), _db_time(now + datetime.timedelta(seconds=lease_s)), used_by, signin_id))
    return {"status": "sign-in", "id": signin_id, "name": name, "version": version, "leaseToken": token,
            "leaseForS": lease_s}


def save_signin(saved_by, name, state, lease_token=None, base_version=None, max_age_s=None, signin_id=None):
    """Keep the state a check is signed in with, for max_age_s seconds (SIGNIN_MAX_AGE_S by default):

    with lease_token, the state of the sign-in that lease was for, which ends the lease;
    with base_version and signin_id instead (the version and id the context was opened with), the newer
    state of that context (cookies roll over), kept only while that version of that id is still the saved
    one and nobody is signing in. The id tells a state from one of the same version an admin deleted:
    a name added again starts over at version 0.

    Returns {"saved": True, "version"}, {"saved": False, "reason": "newer" or "signing-in", "version"}, or
    None for an unknown name. Raises InvalidSignin (SigninTooLarge), LeaseLost, mfa.SecretsKeyMissing and
    SigninsUnavailable."""
    name = _signin_name(name)
    if lease_token is not None and (not isinstance(lease_token, str) or not lease_token):
        raise InvalidSignin("leaseToken is the token the checkout answered.")
    base_version = _version(base_version, "baseVersion")
    signin_id = _signin_id(signin_id)
    if lease_token is None and base_version is None:
        raise InvalidSignin("Send the leaseToken of your sign-in, or the baseVersion of the state you reused.")
    if lease_token is None and signin_id is None:
        raise InvalidSignin("Send the id the checkout answered with baseVersion.")
    low, high = SIGNIN_MAX_AGE_RANGE_S
    if max_age_s is None:
        max_age_s = SIGNIN_MAX_AGE_S
    elif not _is_number(max_age_s) or not low <= max_age_s <= high:
        raise InvalidSignin("maxAgeS is a number of seconds from {} to {} ({} days).".format(low, high, high // 86400))
    packed, domains, cookies, origins = _pack_state(state)
    _need_signin_key()
    try:
        with _session() as cur:
            lock = _lock(cur)
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            cur.execute(_sql("SELECT id, name, version, leaseHash, leaseUntil FROM savedSignins WHERE name = ?" + lock),
                        (name,))
            row = cur.fetchone()
            if row is None:
                return None
            row_id, name, version, lease_hash, lease_until = row
            leased = _lease_live(lease_hash, _parse_time(lease_until), now)
            if lease_token is not None:
                if not leased or not _holds(lease_hash, lease_token):
                    raise LeaseLost("The lease for {!r} isn't yours any more: it ran out, or an admin forgot the sign-in. "
                                    "Nothing was saved.".format(name))
                counter = "signInCount"
            elif base_version != version or signin_id != row_id:
                return {"saved": False, "reason": "newer", "version": version}
            elif leased:
                return {"saved": False, "reason": "signing-in", "version": version}
            else:
                counter = "refreshCount"
            expires = now + datetime.timedelta(seconds=int(max_age_s))
            cur.execute(_sql("""
                UPDATE savedSignins SET stateCipher = ?, version = version + 1, domains = ?, cookieCount = ?,
                       originCount = ?, savedAt = ?, savedBy = ?, expiresAt = ?, {counter} = {counter} + 1,
                       leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL
                WHERE id = ?
            """.format(counter=counter)), (mfa.seal(packed, name, "signin"), domains, cookies, origins, _db_time(now),
                                           saved_by, _db_time(expires), row_id))
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e
    if lease_token is not None:
        log.info("saved sign-in %r: signed in by %s (version %d)", name, saved_by, version + 1)
    return {"saved": True, "version": version + 1}


def release_signin(name, lease_token):
    """End a lease without saving (the sign-in failed), so the next check signs in at once. Returns whether the
    lease was the token's, or None for an unknown name."""
    name = _signin_name(name)
    if not isinstance(lease_token, str) or not lease_token:
        raise InvalidSignin("leaseToken is the token the checkout answered.")
    try:
        with _session() as cur:
            lock = _lock(cur)
            cur.execute(_sql("SELECT id, leaseHash FROM savedSignins WHERE name = ?" + lock), (name,))
            row = cur.fetchone()
            if row is None:
                return None
            if not _holds(row[1], lease_token):
                return False
            cur.execute(_sql("UPDATE savedSignins SET leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL WHERE id = ?"),
                        (row[0],))
            return True
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e


def forget_signin(signin_id):
    """Drop the saved state and any lease, so the next check signs in again. The version goes up, so a check
    still using the old state can't save it back. Returns the sign-in as list_signins shows it, or None."""
    if not 1 <= signin_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            # expiresAt goes too: nothing is left to expire, and the page tells a forgotten state from an expired
            # one by it.
            cur.execute(_sql("UPDATE savedSignins SET stateCipher = NULL, version = version + 1, expiresAt = NULL, "
                             "leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL WHERE id = ?"), (signin_id,))
            cur.execute(_sql("SELECT {}, {} FROM savedSignins WHERE id = ?".format(_now_sql(), _SIGNIN_COLUMNS)),
                        (signin_id,))
            row = cur.fetchone()
    except _MissingTables:
        return None
    return _signin(row[1:], _parse_time(row[0])) if row else None


def delete_signin(signin_id):
    """Delete a sign-in and its state. Returns False if there is no such sign-in. A check that checks out the
    name again adds a new row, starting over at version 0 under a new id (ids aren't reused), so a check
    still holding the old state can't save it back over the new one's version: save_signin compares both."""
    if not 1 <= signin_id <= _MAX_TOKEN_ID:
        return False
    try:
        with _session() as cur:
            cur.execute(_sql("DELETE FROM savedSignins WHERE id = ?"), (signin_id,))
            return bool(cur.rowcount)
    except _MissingTables:
        return False
