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
import collections
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
import threading
import time
import urllib.parse
import zlib

import yaml

from . import mfa, permissions

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
_warned = set()
_warned_lock = threading.Lock()


def warn_once(text, logger=log):
    """Log a warning once per process, for what would otherwise be said on every request: a setting that is
    wrong, a table that is missing. logger is the module it is about (this one's by default)."""
    with _warned_lock:
        if text in _warned:
            return
        _warned.add(text)
    logger.warning("%s", text)


def reset_warnings():
    """Forget which warnings were logged: one test's must not keep another's from being logged."""
    with _warned_lock:
        _warned.clear()


_ENV_OVERRIDES = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "database": "DB_NAME",
}
_schema_attempted = False
_schema_lock = threading.Lock()
_schema_failure = None  # the last attempt that couldn't reach the database: its error's (type, message, public)
_SCHEMA_WAIT_S = 10  # two connect timeouts


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


class NotShared(Exception):
    """A check asked by name for an MFA secret or a saved sign-in that belongs to someone else and isn't shared
    with its token's owner; the message says so. Names are shared by everyone on the grid, so this tells nothing
    that "the name is taken" doesn't."""


class NotPermitted(Exception):
    """Someone may see this, but not do it to it; the message says why. rule is set when one of the rules that
    stop people giving themselves or others more than they have refused it (accounts.py), and permission names
    what they lacked, for the audit log."""

    def __init__(self, message, rule=None, permission=None):
        super().__init__(message)
        self.rule = rule
        self.permission = permission


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
    """Create the tables on MySQL if src/schema.sql is reachable. Tried once per process.

    The background recorder and a request can both be first on a new database: whoever comes second
    waits until the tables are made, rather than finding them missing. If the database can't be reached,
    those that waited get that error, rather than each waiting out a connect timeout of its own."""
    global _schema_attempted, _schema_failure
    if _schema_attempted or DB_ENGINE != "mysql":
        return
    failure = _schema_failure
    # Bounded: PyMySQL has no read timeout, so a server that takes the connection and never answers would
    # otherwise hold up every caller behind it. One that gives up carries on, as before there was a lock.
    if not _schema_lock.acquire(timeout=_SCHEMA_WAIT_S):
        return
    try:
        if _schema_attempted:
            return
        if _schema_failure is not failure:  # an attempt failed while we waited
            kind, message, public = _schema_failure
            raise kind(message, public)
        try:
            with open(SCHEMA_PATH) as stream:
                text = stream.read()
        except OSError:
            _schema_attempted = True
            log.info("schema not found at %s; waiting for the hub or a check to create the tables", SCHEMA_PATH)
            return
        try:
            with _session() as cur:  # DatabaseUnavailable propagates, so we try again next time
                for statement in _schema_statements(text):
                    code = "\n".join(l for l in statement.splitlines() if not l.strip().startswith("--")).strip()
                    if code:
                        try:
                            cur.execute(code)
                        except Exception as e:  # e.g. no CREATE privilege: the UI still works read-only
                            log.warning("could not apply schema statement: %s", e)
                _schema_attempted = True
        except DatabaseUnavailable as e:
            _schema_failure = (type(e), str(e), e.public)
            raise
    finally:
        _schema_lock.release()


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


def _release_sql(cur):
    """The SET clause that frees a slot. It also clears claimedBy, as every release does. A hub whose
    session still runs on the slot claims it again (it never hands a slot it pipes to a second
    client); a claim left by a hub that is gone stays released."""
    clear = ", claimedBy = NULL" if "claimedby" in _columns(cur, "portUsage") else ""
    return "UPDATE portUsage SET inUse = 0, LastRunDate = {}{} WHERE id = ?".format(_now_sql(), clear)


def _audit_slot(cur, actor, action, slot_id):
    # Named by its host and port, which say more in the audit log than an id, and outlive the slot.
    if actor is None:
        return
    cur.execute(_sql("SELECT PortServer, PortNumber FROM portStatus WHERE id = ?"), (slot_id,))
    row = cur.fetchone()
    _audit(cur, actor, action, "slot", slot_id, "{}:{}".format(*row) if row else None)


def release_slot(slot_id, actor=None):
    """Force a stuck slot back to free. actor is who did it, for the audit log (the grid actions record only
    what changed)."""
    try:
        with _session() as cur:
            cur.execute(_sql(_release_sql(cur)), (slot_id,))
            released = cur.rowcount
            if released:
                _audit_slot(cur, actor, "grid.slot_released", slot_id)
            return released
    except _MissingTables:
        return 0


def release_claim(slot_id, claimed_at, actor=None):
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
            released = cur.rowcount
            if released:
                _audit_slot(cur, actor, "grid.slot_released", slot_id)
            return "released", released
    except _MissingTables:
        return "not_found", 0


def reset_slot_failures(slot_id, actor=None):
    # Only counts that aren't 0 already (here and for a node): MySQL's rowcount is the rows an UPDATE changed,
    # sqlite's the rows it matched, and the answer and the audit log should be the same on both.
    try:
        with _session() as cur:
            cur.execute(_sql("UPDATE portUsage SET failCount = 0 WHERE id = ? AND failCount <> 0"), (slot_id,))
            updated = cur.rowcount
            if updated:
                _audit_slot(cur, actor, "grid.slot_reset", slot_id)
            return updated
    except _MissingTables:
        return 0


def reset_node_failures(host, actor=None):
    try:
        with _session() as cur:
            cur.execute(_sql("UPDATE portUsage SET failCount = 0 WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?) "
                             "AND failCount <> 0"), (host,))
            updated = cur.rowcount
            if updated:
                _audit(cur, actor, "grid.node_reset", "node", None, host)
            return updated
    except _MissingTables:
        return 0


def forget_node(name, actor=None):
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
            _audit(cur, actor, "grid.node_forgotten", "node", None, name, {"slots": deleted})
            return "forgotten", deleted
    except _MissingTables:
        return "not_found", 0


# ---- owners and sharing ---------------------------------------------------------
#
# Who owns an MFA secret, a saved sign-in or an access token is its row in itemOwners (itemKind 'mfa', 'signin'
# or 'token'); without one it is the admins'. Who else may use a secret or a sign-in is in itemShares: a user, or
# everyone with a role. Both tables came with single sign-on, so a database may not have them yet: everything is
# the admins' then, and only admins see it. An item is checked in Python on its own row, already selected (and
# locked when it will be written), with plain reads of its owner and share rows, which are never locked.

# Who is asking, for what MFA secrets, saved sign-ins and tokens it may see: user_id (None for admins without a
# user: the admin token, ownerless client tokens), their effective role ids (an admin's include the Admin role's)
# and permissions.
Access = collections.namedtuple("Access", "user_id role_ids permissions")
# Who made a change, for the audit log: users.id or None, the name the page shows, the provider id, the address
# and their permissions (for the subset rules, which work them out again inside the lock).
Actor = collections.namedtuple("Actor", "user_id name method address permissions")
# itemKind -> the prefix of the permissions that govern it (mfa.use, mfa.add, mfa.all; signins.use, ...).
_PERMISSION_GROUPS = {"mfa": "mfa", "signin": "signins"}
# itemKind -> the table its items are rows of.
_ITEM_TABLES = {"mfa": "mfaSecrets", "signin": "savedSignins"}
# Why someone with a user can't add a token, an MFA secret or a saved sign-in to a database without itemOwners: the
# row would have no owner, so it would be the admins' (and a client token without an owner acts as an admin).
_NO_OWNER_TABLE = ("This database has no itemOwners table yet, so only admins can add this. The grid UI creates it on "
                   "MySQL when its database user may create tables, and so does the gateway hub when it starts.")


def _owner_tables(cur, tables=None):
    """(whether the database has itemOwners, whether it has itemShares)."""
    tables = _tables(cur) if tables is None else tables
    return "itemowners" in tables, "itemshares" in tables


def _owner(cur, kind, item_id, tables=None):
    """The users.id that owns the item, or None: it is the admins' (always, without an itemOwners table)."""
    if not _owner_tables(cur, tables)[0]:
        return None
    cur.execute(_sql("SELECT userId FROM itemOwners WHERE itemKind = ? AND itemId = ?"), (kind, item_id))
    row = cur.fetchone()
    return row[0] if row else None


def _grantees(access):
    """(condition, params): what picks the itemShares rows of the viewer's user and of its roles ("" when it has
    neither). Role ids go in as numbers, not parameters, so the list can be any length."""
    branches, params = [], []
    if access.user_id is not None:
        branches.append("(granteeKind = 'user' AND granteeId = ?)")
        params.append(access.user_id)
    roles = sorted({int(r) for r in access.role_ids})
    if roles:
        branches.append("(granteeKind = 'role' AND granteeId IN ({}))".format(", ".join(str(r) for r in roles)))
    return " OR ".join(branches), params


def _vouched_for(text):
    """An itemShares.vouchedFor value as a set of permissions, or None for a share nobody vouched for."""
    return None if text is None else permissions.parse(text)


def _shared_with(cur, kind, item_id, access, tables=None):
    """(shared, vouches): whether the item is shared with the viewer or with one of its roles, and for each of those
    shares that someone who could see every item of its kind made, what its owner could do then
    (itemShares.vouchedFor, as a set of permissions)."""
    grantees, params = _grantees(access)
    if not grantees or not _owner_tables(cur, tables)[1]:
        return False, []
    cur.execute(_sql("SELECT vouchedFor FROM itemShares WHERE itemKind = ? AND itemId = ? AND (" + grantees + ")"),
                [kind, item_id] + params)
    rows = cur.fetchall()
    return bool(rows), [_vouched_for(text) for (text,) in rows if text is not None]


def _owner_permissions(cur, owner_id, owners=None):
    """What the item's owner may do, counted as if enabled (disabling someone keeps their shares), or None when
    they are no user. owners caches it by owner for a listing, which fills it for all of them at once
    (accounts.user_accesses). accounts works out what a user may do, and it imports this module, so it is
    imported here."""
    owners = {} if owners is None else owners
    if owner_id not in owners:
        from . import accounts
        found = accounts.user_access(owner_id, cur, as_enabled=True)
        owners[owner_id] = None if found is None else found["permissions"]
    return owners[owner_id]


def _vouch_holds(vouched_for, owner_perms):
    """Whether a share someone who could see every sign-in made (vouched_for: what its owner could do then) still
    stands for its owner, who may now do owner_perms (None: they are no user). It stands while they may still do
    all of that: a vouch for an admin's account says nothing of the account they save once they are a Tester."""
    return vouched_for is not None and owner_perms is not None and vouched_for <= owner_perms


def _reaches(cur, kind, owner_id, shared, vouches, access, owners=None):
    """Whether the item's shares with the viewer let its checks use it. Any share of an MFA secret does: a code for
    the wrong seed signs nobody in, and so does any share of what nobody owns, the admins'. A saved sign-in's checks
    run as whatever account its owner's checks saved, so its shares reach a viewer who may do something its owner
    may not only through one that someone who could see every sign-in made (vouches, from _shared_with), and only
    while its owner may still do all they could then: otherwise its owner could put an account of their own on the
    checks of someone who has more, through a role they also have (the one everyone gets, say), by sharing before
    they were given more, or by being given less after an admin shared it. owners caches what owners may do, for
    a listing (_owner_permissions)."""
    if not shared or kind != "signin" or owner_id is None:
        return shared
    held = _owner_permissions(cur, owner_id, owners)
    return held is not None and (access.permissions <= held or any(_vouch_holds(v, held) for v in vouches))


def _rights(kind, owner_id, shared, reaches, access):
    """(visible, manageable, steward, usable): what the viewer may do with an item that owner has.

    visible: listed, its codes shown and used: everyone's with k.all, else with k.use its own and those shared
    with it. manageable: shared, forgotten and deleted: everyone's with k.all, else with k.add its own. The last
    two matter for sign-ins. steward: its checks sign in fresh and save: the owner's, and for one nobody owns,
    those of whoever has signins.all. usable: its checks may use it at all, as a steward or through a share that
    reaches them (_reaches), with signins.use. signins.all shows everyone's on the page, but never makes a check
    use one someone else controls."""
    group = _PERMISSION_GROUPS[kind]
    perms = access.permissions
    mine = owner_id is not None and owner_id == access.user_id
    every = group + ".all" in perms
    visible = every or (group + ".use" in perms and (mine or shared))
    manageable = every or (group + ".add" in perms and mine)
    steward = mine or (owner_id is None and every)
    usable = group + ".use" in perms and (steward or reaches)
    return visible, manageable, steward, usable


def _item_access(cur, kind, item_id, access):
    """(owner_id, visible, manageable, steward, usable) of one item for the viewer (see _rights)."""
    tables = _tables(cur)
    owner_id = _owner(cur, kind, item_id, tables)
    shared, vouches = _shared_with(cur, kind, item_id, access, tables)
    reaches = _reaches(cur, kind, owner_id, shared, vouches, access)
    return (owner_id,) + _rights(kind, owner_id, shared, reaches, access)


def _visible_clause(kind, alias, access, tables):
    """(" AND (...)", params): what a listing adds to its WHERE so that only the items the viewer may see come
    back; alias is the item table's in the query. Nothing to add with k.all. Without k.use, or without both
    itemOwners and itemShares, nothing comes back."""
    group = _PERMISSION_GROUPS[kind]
    if group + ".all" in access.permissions:
        return "", []
    has_owners, has_shares = "itemowners" in tables, "itemshares" in tables
    branches, params = [], []
    if group + ".use" in access.permissions:
        if has_owners and access.user_id is not None:
            branches.append("{}.id IN (SELECT itemId FROM itemOwners WHERE itemKind = ? AND userId = ?)".format(alias))
            params += [kind, access.user_id]
        grantees, grantee_params = _grantees(access)
        if has_shares and grantees:
            branches.append("{}.id IN (SELECT itemId FROM itemShares WHERE itemKind = ? AND ({}))".format(alias, grantees))
            params += [kind] + grantee_params
    if not branches:
        return " AND 1 = 0", []
    return " AND (" + " OR ".join(branches) + ")", params


def _managed(cur, kind, item_id, access, verb):
    """Whether the viewer may see the item it wants to `verb` (False: answer as if it weren't there). Raises
    NotPermitted when it may see it, but not manage it. Without access (the store's own callers), it may."""
    if access is None:
        return True
    _, visible, manageable, _, _ = _item_access(cur, kind, item_id, access)
    if visible and not manageable:
        raise NotPermitted("Only its owner or an admin can {} it.".format(verb))
    return visible


def _add_owner(cur, kind, item_id, owner_id):
    """Record who made the item, in the transaction that adds it (_NO_OWNER_TABLE was checked before)."""
    cur.execute(_sql("INSERT INTO itemOwners (itemKind, itemId, userId, createdAt) VALUES (?, ?, ?, {})"
                     .format(_now_sql())), (kind, item_id, owner_id))


def _drop_item_rows(cur, kind, item_id, tables=None):
    """Delete the item's owner and share rows, in the transaction that deletes the item: a namesake added later
    starts unshared, and owned by whoever adds it."""
    has_owners, has_shares = _owner_tables(cur, tables)
    if has_owners:
        cur.execute(_sql("DELETE FROM itemOwners WHERE itemKind = ? AND itemId = ?"), (kind, item_id))
    if has_shares:
        cur.execute(_sql("DELETE FROM itemShares WHERE itemKind = ? AND itemId = ?"), (kind, item_id))


def _ownership(cur, kind, rows, access):
    """Add to each listed item (a dict with its "id") who has it, as far as the viewer may know: ownerId (a
    users.id, or None: the admins'), access ("owner", "shared" or "all": why the viewer sees it), canManage,
    usable (sign-ins only: whether checks acting as the viewer may use it) and shares ([{"kind": "user" or
    "role", "id", "vouchedFor"}], vouchedFor being a set of permissions or None as _shared_with reads it, only for
    those who may manage it, else []). accounts.annotate puts names to the ids. Returns rows."""
    has_owners, has_shares = _owner_tables(cur)
    ids = ", ".join(str(int(row["id"])) for row in rows)
    owners, shares = {}, collections.defaultdict(list)
    if ids and has_owners:
        cur.execute(_sql("SELECT itemId, userId FROM itemOwners WHERE itemKind = ? AND itemId IN ({})".format(ids)),
                    (kind,))
        owners = dict(cur.fetchall())
    if ids and has_shares:
        cur.execute(_sql("SELECT itemId, granteeKind, granteeId, vouchedFor FROM itemShares WHERE itemKind = ? "
                         "AND itemId IN ({}) ORDER BY id".format(ids)), (kind,))
        for item_id, grantee_kind, grantee_id, vouched_for in cur.fetchall():
            shares[item_id].append({"kind": grantee_kind, "id": grantee_id, "vouchedFor": _vouched_for(vouched_for)})
    roles = set(access.role_ids)
    mine = {row["id"]: [s for s in shares.get(row["id"], ())
                        if (s["id"] == access.user_id if s["kind"] == "user" else s["id"] in roles)] for row in rows}
    # What the owners of the sign-ins shared with the viewer may do, all in the same few queries: the page asks
    # for the listing every few seconds.
    judged = {owners.get(row["id"]) for row in rows if kind == "signin" and mine[row["id"]]} - {None}
    known = {}
    if judged:
        from . import accounts
        found = accounts.user_accesses(cur, judged, as_enabled=True)
        known = {owner_id: found[owner_id]["permissions"] if owner_id in found else None for owner_id in judged}
    for row in rows:
        owner_id, item_shares = owners.get(row["id"]), shares.get(row["id"], [])
        shared = bool(mine[row["id"]])
        vouches = [s["vouchedFor"] for s in mine[row["id"]] if s["vouchedFor"] is not None]
        reaches = _reaches(cur, kind, owner_id, shared, vouches, access, known)
        _, manageable, _, usable = _rights(kind, owner_id, shared, reaches, access)
        row["ownerId"] = owner_id
        row["access"] = "owner" if owner_id is not None and owner_id == access.user_id else "shared" if shared else "all"
        row["canManage"] = manageable
        if kind == "signin":
            row["usable"] = usable
        row["shares"] = item_shares if manageable else []
    return rows


def ownership(kind, rows, access):
    """_ownership in a transaction of its own, for rows a change answered with (a new MFA secret)."""
    ensure_schema()
    with _session() as cur:
        return _ownership(cur, kind, rows, access)


def _listed_item(cur, kind, item_id, access):
    """One MFA secret ('mfa') or saved sign-in ('signin') as its listing shows it to the viewer, with who has it
    (_ownership), or None when there is no such item: for a change to who has it to answer with."""
    if kind == "mfa":
        cur.execute(_sql("SELECT " + _MFA_COLUMNS + " FROM mfaSecrets WHERE id = ?"), (item_id,))
        row = cur.fetchone()
        item = None if row is None else _mfa(row)
    else:
        cur.execute(_sql("SELECT {}, {} FROM savedSignins WHERE id = ?".format(_now_sql(), _SIGNIN_COLUMNS)), (item_id,))
        row = cur.fetchone()
        item = None if row is None else _signin(row[1:], _parse_time(row[0]))
    return None if item is None else _ownership(cur, kind, [item], access)[0]


# ---- audit log ------------------------------------------------------------------
#
# What people changed on the Grid UI. A change writes its row in the same transaction, so the two commit or roll
# back together (audit_alone is for rows that must outlive a change that rolled back). No row holds a seed, code,
# state, token or cookie. Rows older than GRID_AUDIT_DAYS are deleted as new ones are added.

AUDIT_DAYS = 365
AUDIT_DAYS_RANGE = (30, 3650)
AUDIT_DETAILS_LIMIT = 4000
_AUDIT_PURGE_LIMIT = 500


def audit_days():
    """How many days the audit log keeps: GRID_AUDIT_DAYS, read now. Anything but a whole number from 30 to
    3650 means 365 (logged once)."""
    text = os.environ.get("GRID_AUDIT_DAYS", "").strip()
    low, high = AUDIT_DAYS_RANGE
    if text:
        try:
            if low <= int(text) <= high:
                return int(text)
        except ValueError:
            pass
        warn_once("GRID_AUDIT_DAYS is a number of days from {} to {}; the audit log keeps {} days meanwhile."
                  .format(low, high, AUDIT_DAYS))
    return AUDIT_DAYS


def _audit(cur, actor, action, target_kind=None, target_id=None, target_name=None, details=None):
    """Record a change, in the transaction that makes it. Nothing is recorded without an actor (a grid with no
    sign-in configured), or while the database has no auditLog table (logged once). details is a small object,
    kept as compact JSON; past AUDIT_DETAILS_LIMIT characters it is cut, so callers keep lists short."""
    if actor is None:
        return
    if "auditlog" not in _tables(cur):
        warn_once("the database has no auditLog table yet; changes aren't being recorded")
        return
    text = None if details is None else json.dumps(details, separators=(",", ":"))[:AUDIT_DETAILS_LIMIT]
    cur.execute(_sql("""
        INSERT INTO auditLog (occurredAt, actorId, actorName, actorMethod, address, action, targetKind, targetId,
                              targetName, details)
        VALUES ({}, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """.format(_now_sql())), (actor.user_id, str(actor.name or "?")[:255], str(actor.method or "?")[:32],
                              actor.address[:64] if actor.address else None, action, target_kind, target_id,
                              target_name[:255] if target_name else None, text))
    # By id, like the MFA hand-outs: on MySQL, a DELETE by occurredAt would lock every row it scans.
    cur.execute("SELECT id FROM auditLog WHERE occurredAt < {} LIMIT {}".format(
        _days_ago_sql(audit_days()), _AUDIT_PURGE_LIMIT))
    old = [str(int(i)) for (i,) in cur.fetchall()]
    if old:
        cur.execute("DELETE FROM auditLog WHERE id IN ({})".format(", ".join(old)))


def audit_alone(actor, action, target_kind=None, target_id=None, target_name=None, details=None):
    """_audit in a transaction of its own, for a row that must outlive a change that rolled back (a refusal), or
    must never fail what it records (a read). A failure is logged, never raised."""
    if actor is None:
        return
    try:
        ensure_schema()
        with _session() as cur:
            _audit(cur, actor, action, target_kind, target_id, target_name, details)
    except Exception as e:
        log.warning("couldn't record %s in the audit log: %s", action, e)


# ---- managed access tokens ----------------------------------------------------
#
# The hub accepts these besides its env bootstrap tokens (GATEWAY_AGENT_TOKEN and
# GATEWAY_CLIENT_TOKEN). Only the UI makes them and only their sha256 is stored: the plaintext
# is returned once, by create_token, and never logged. Whoever made one owns it (itemOwners, which
# the hub ignores): checks using a client token act as its owner, or as an admin when it has none.

TOKEN_PREFIXES = {"agent": "pwga_", "client": "pwgc_"}
_MAX_TOKEN_ID = 2 ** 31 - 1  # accessTokens.id is an INT; sqlite can't even bind a much larger number
_NODE_NAME = re.compile(r"[a-z0-9._-]{1,128}\Z")
_TOKEN_COLUMNS = "id, name, kind, prefix, nodeName, createdAt, expiresAt, lastUsedAt, revokedAt"
_NO_TOKEN_TABLE = ("This database has no accessTokens table yet. The gateway hub creates it when it starts, "
                   "and so does the grid UI on MySQL when its database user may create tables.")
_NO_TOKEN_OWNERS = ("This database has users but no itemOwners table, so the Grid UI can't tell whose client token this "
                    "is, and won't take it for an admin's. The grid UI creates the table on MySQL when its database user "
                    "may create tables, and so does the gateway hub when it starts.")
_NO_TOKEN_SCOPE = ("This database's accessTokens table has no scope column yet, so a token can't be given a scope. "
                   "The gateway hub adds the column when it starts: update the hub, start it, and make the token again.")
_TOKEN_FORM = ("Client token #{} is turned away: how it is stored and its scope don't agree, so its scope was "
               "changed in the database or the accessTokens table lost its scope column. Revoke it and make another.")
_TWO_TOKEN_ROWS = ("Client tokens #{} and #{} are one token stored twice, with and without a scope: it is turned "
                   "away. Delete both and make another.")
SCOPED_HASH_PREFIX = "scoped:"
# The hub turns away checks without a client token as soon as any client token row exists, revoked or expired
# ones too, so the first row and the last are switches for the whole grid.
FIRST_CLIENT_TOKEN = ("This would be the grid's first client token: from then on the hub turns away checks that don't "
                      "send one. Ask someone whose roles include “Every access token” to make the first one.")
LAST_CLIENT_TOKEN = ("It's the last client token on record: deleting it lets checks without a token back in after the "
                     "hub's next restart. Ask someone whose roles include “Every access token” to delete it.")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def scoped_token_hash(token):
    """What tokenHash holds for a token with a scope: 'scoped:' and the first 57 hex characters of token_hash,
    64 characters in all. It is not the sha256 of anything, so software from before scopes, which compares
    tokenHash with the sha256 of what it was sent, finds no row whatever it is sent."""
    return SCOPED_HASH_PREFIX + token_hash(token)[:64 - len(SCOPED_HASH_PREFIX)]


def _db_time(value):
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def _token_status(now, expires, revoked):
    if revoked is not None:
        return "revoked"
    if expires is not None and expires <= now:
        return "expired"
    return "active"


def _token_columns(cur):
    """_TOKEN_COLUMNS, whether the row is stored in the scoped form, and the scope, which is NULL for every
    row of a table from before scopes."""
    return (_TOKEN_COLUMNS + ", SUBSTR(tokenHash, 1, 7) = 'scoped:', "
            + ("scope" if "scope" in _columns(cur, "accessTokens") else "NULL"))


def _token(row, now):
    token_id, name, kind, prefix, node_name = row[:5]
    created, expires, last_used, revoked = (_parse_time(v) for v in row[5:9])
    scoped, scope = bool(row[9]), row[10]
    token = {
        "id": token_id, "name": name, "kind": kind, "prefix": prefix, "nodeName": node_name,
        "createdAt": _iso(created), "expiresAt": _iso(expires), "lastUsedAt": _iso(last_used),
        "revokedAt": _iso(revoked), "status": _token_status(now, expires, revoked),
        "nodes": [], "nodesOnline": [],
        # A row stored in the scoped form is never listed as a token with no limit: with its scope gone
        # (set to NULL by hand, or the column was lost) it allows nothing, and the list says so.
        "scope": sorted(permissions.parse_scope(scope)) if scoped or scope is not None else None,
    }
    if scoped and scope is None:
        token["scopeLost"] = True
    return token


def _token_rights(kind, owner_id, access):
    """Whether the viewer may see, revoke and delete a token that owner made (None: an admin did): everyone's with
    tokens.all; else its own with tokens.own, and every agent token with tokens.agents."""
    perms = access.permissions
    if "tokens.all" in perms or (kind == "agent" and "tokens.agents" in perms):
        return True
    return "tokens.own" in perms and owner_id is not None and owner_id == access.user_id


def _may_switch_client_tokens(access):
    """Whether the viewer may make the grid's first client token or delete its last row: with tokens.all, or
    anyone while the UI is given GATEWAY_CLIENT_TOKEN (the hub has it too, so it requires a token anyway).
    Without access (the store's own callers), it may."""
    return access is None or "tokens.all" in access.permissions or bool(os.environ.get("GATEWAY_CLIENT_TOKEN"))


def _kind_counts(tokens):
    """{kind: {"rows", "active", "revoked"}} over every token, whatever a viewer may see of them: the page warns
    about the first and the last of a kind by these."""
    counts = {kind: {"rows": 0, "active": 0, "revoked": 0} for kind in TOKEN_PREFIXES}
    for token in tokens:
        kind = counts.setdefault(token["kind"], {"rows": 0, "active": 0, "revoked": 0})
        kind["rows"] += 1
        if token["status"] in ("active", "revoked"):
            kind[token["status"]] += 1
    return counts


def list_tokens(access=None):
    """{"ready": bool, "generated": ISO, "tokens": [...]}, newest first.

    ready is False while the database has no accessTokens table (an older demo database, or a
    MySQL user that can't create it). Each token lists the nodes that last registered with it.

    With access (who asks), only the tokens it may see (_token_rights) are listed, each with its "ownerId", and
    "kindCounts" counts every token of each kind."""
    ensure_schema()
    owners = {}
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            tables = _tables(cur)
            if "accesstokens" not in tables:
                return _no_tokens(_iso(now), access)
            cur.execute("SELECT " + _token_columns(cur) + " FROM accessTokens ORDER BY createdAt DESC, id DESC")
            tokens = [_token(row, now) for row in cur.fetchall()]
            users = []
            # tokenId came with managed tokens; the hub adds it to nodes tables made before that.
            if "nodes" in tables and "tokenid" in _columns(cur, "nodes"):
                cur.execute("SELECT tokenId, name, online, lastSeen FROM nodes WHERE tokenId IS NOT NULL")
                users = cur.fetchall()
            if access is not None and "itemowners" in tables:
                cur.execute("SELECT itemId, userId FROM itemOwners WHERE itemKind = 'token'")
                owners = dict(cur.fetchall())
    except _MissingTables:  # dropped between queries
        return _no_tokens(_iso(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)), access)
    by_id = {t["id"]: t for t in tokens}
    for token_id, name, online, last_seen in sorted(users, key=lambda u: str(u[1]).casefold()):
        token = by_id.get(token_id)
        if token is None:
            continue
        token["nodes"].append(name)
        seen = _parse_time(last_seen)
        if online and seen is not None and (now - seen).total_seconds() <= ONLINE_WINDOW_S:
            token["nodesOnline"].append(name)
    if access is None:
        return {"ready": True, "generated": _iso(now), "tokens": tokens}
    counts = _kind_counts(tokens)
    tokens = [t for t in tokens if _token_rights(t["kind"], owners.get(t["id"]), access)]
    for token in tokens:
        token["ownerId"] = owners.get(token["id"])
    return {"ready": True, "generated": _iso(now), "tokens": tokens, "kindCounts": counts}


def _no_tokens(generated, access):
    listing = {"ready": False, "generated": generated, "tokens": []}
    if access is not None:
        listing["kindCounts"] = _kind_counts([])
    return listing


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


def _validate_scope(kind, scope):
    """None for no scope, or a client token's scope ids, sorted. An empty scope is refused: None alone means no limit."""
    if scope is None:
        return None
    if kind != "client":
        raise InvalidToken("Only client tokens can have a scope.")
    if not isinstance(scope, (list, tuple)) or not all(isinstance(s, str) for s in scope):
        raise InvalidToken("Send scope as a list of scope ids, or leave it out for no limit.")
    if not scope:
        raise InvalidToken("Pick at least one thing the token may do, or leave scope out for no limit.")
    for s in scope:
        if s not in permissions.TOKEN_SCOPES:
            raise InvalidToken("There is no scope {!r}.".format(s[:64]))
    if "signins.add" in scope and "signins.use" not in scope:
        raise InvalidToken("A scope with “Add saved sign-ins” needs “Use saved sign-ins” too: "
                           "a check that adds a sign-in then uses it.")
    return tuple(sorted(set(scope)))


def create_token(name, kind, node_name=None, expires_in_days=None, owner_id=None, access=None, actor=None,
                 scope=None):
    """Make a managed token, owned by owner_id (a users.id; None: the admins'). The result's "token" is the
    plaintext: the only time it's available. With access (who asks), the result has its "ownerId", and a client
    token that would be the grid's first needs _may_switch_client_tokens (NotPermitted). actor is who made it,
    for the audit log. scope (client tokens only) is the list of scope ids that limits what the token may do;
    None is no limit.

    Raises InvalidToken for a bad request and TokensUnavailable without an accessTokens table (or, for a token
    someone owns, without an itemOwners table; or, for a token with a scope, without a scope column)."""
    name, kind, node_name, expires_in_days = _validate_token_request(name, kind, node_name, expires_in_days)
    scope = _validate_scope(kind, scope)
    ensure_schema()
    token = TOKEN_PREFIXES[kind] + secrets.token_urlsafe(32)
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0]).replace(microsecond=0)
            expires = now + datetime.timedelta(days=expires_in_days) if expires_in_days else None
            tables = _tables(cur)
            if owner_id is not None and "accesstokens" in tables and "itemowners" not in tables:
                raise TokensUnavailable(_NO_OWNER_TABLE)
            if scope is not None and "accesstokens" in tables and "scope" not in _columns(cur, "accessTokens"):
                raise TokensUnavailable(_NO_TOKEN_SCOPE)
            if kind == "client" and not _may_switch_client_tokens(access):
                cur.execute("SELECT COUNT(*) FROM accessTokens WHERE kind = 'client'")
                if not cur.fetchone()[0]:
                    raise NotPermitted(FIRST_CLIENT_TOKEN, permission="tokens.all")
            if scope is None:
                cur.execute(_sql("""
                    INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """), (name, kind, token_hash(token), token[:12], node_name, _db_time(now), _db_time(expires)))
            else:
                cur.execute(_sql("""
                    INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, scope)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """), (name, kind, scoped_token_hash(token), token[:12], node_name, _db_time(now),
                       _db_time(expires), permissions.unparse(scope)))
            token_id = cur.lastrowid
            if owner_id is not None:
                _add_owner(cur, "token", token_id, owner_id)
            details = {"kind": kind, "owner": owner_id}
            if kind == "client":
                details["scope"] = None if scope is None else list(scope)
            _audit(cur, actor, "token.created", "token", token_id, name, details)
    except _MissingTables as e:
        raise TokensUnavailable(_NO_TOKEN_TABLE) from e
    result = _token((token_id, name, kind, token[:12], node_name, now, expires, None, None,
                     scope is not None, None if scope is None else permissions.unparse(scope)), now)
    if access is not None:
        result["ownerId"] = owner_id
    result["token"] = token
    return result


def _visible_token(cur, token_id, access, lock=""):
    """(kind, name, owner id) of a token the viewer may see (all of them without access), or None."""
    cur.execute(_sql("SELECT kind, name FROM accessTokens WHERE id = ?" + lock), (token_id,))
    row = cur.fetchone()
    if row is None:
        return None
    owner_id = _owner(cur, "token", token_id) if access is not None else None
    if access is not None and not _token_rights(row[0], owner_id, access):
        return None
    return row[0], row[1], owner_id


def revoke_token(token_id, access=None, actor=None):
    """Revoke a token (revoking it again keeps the first time). Returns the token, or None if unknown (or, with
    access, one the viewer may not see). Revoking is never refused otherwise: someone must always be able to stop
    their own leaked token, and a revoked row keeps the hub requiring a token like an active one."""
    if not 1 <= token_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            found = _visible_token(cur, token_id, access)
            if found is None:
                return None
            cur.execute(_sql("UPDATE accessTokens SET revokedAt = {} WHERE id = ? AND revokedAt IS NULL".format(_now_sql())),
                        (token_id,))
            if cur.rowcount:
                _audit(cur, actor, "token.revoked", "token", token_id, found[1])
            cur.execute(_sql("SELECT {}, {} FROM accessTokens WHERE id = ?".format(_now_sql(), _token_columns(cur))),
                        (token_id,))
            row = cur.fetchone()
    except _MissingTables:
        return None
    if row is None:
        return None
    result = _token(row[1:], _parse_time(row[0]))
    if access is not None:
        result["ownerId"] = found[2]
    return result


def delete_token(token_id, access=None, actor=None):
    """Delete a revoked or expired token, its owner row, and forget which nodes used it.

    Returns "deleted", "not_found" (with access, for one the viewer may not see too) or "active" (revoke it
    first). With access, deleting the last client token row needs _may_switch_client_tokens (NotPermitted)."""
    if not 1 <= token_id <= _MAX_TOKEN_ID:
        return "not_found"
    switch = _may_switch_client_tokens(access)
    try:
        with _session() as cur:
            lock = _lock(cur)  # held across check and delete
            if not switch:
                # Every client row, locked in id order before anything else: two people deleting the last two at
                # once would otherwise each count the other's as still there. In one order, they wait in turn.
                cur.execute("SELECT id FROM accessTokens WHERE kind = 'client' ORDER BY id" + lock)
                clients = [row[0] for row in cur.fetchall()]
            found = _visible_token(cur, token_id, access, lock)
            if found is None:
                return "not_found"
            cur.execute(_sql("SELECT {}, expiresAt, revokedAt FROM accessTokens WHERE id = ?".format(_now_sql())),
                        (token_id,))
            if _token_status(*(_parse_time(v) for v in cur.fetchone())) == "active":
                return "active"
            if found[0] == "client" and not switch and not any(i != token_id for i in clients):
                raise NotPermitted(LAST_CLIENT_TOKEN, permission="tokens.all")
            if "tokenid" in _columns(cur, "nodes"):
                cur.execute(_sql("UPDATE nodes SET tokenId = NULL WHERE tokenId = ?"), (token_id,))
            cur.execute(_sql("DELETE FROM accessTokens WHERE id = ?"), (token_id,))
            _drop_item_rows(cur, "token", token_id)
            _audit(cur, actor, "token.deleted", "token", token_id, found[1])
            return "deleted"
    except _MissingTables:
        return "not_found"


def client_token(token):
    """The active managed client token `token` is, as {"id", "name", "ownerId", "scope"}, or None. ownerId is the
    users.id of the person who made it, or None when an admin did (with the admin token, or before there were
    users): checks using it act as an admin. scope is None for a token made without one; otherwise the frozenset
    of its scope ids this version knows, which may be empty (nothing is allowed). Stamps the token's lastUsedAt,
    as the hub does when a check connects with it.

    Raises TokensUnavailable when the database has users but no itemOwners table (or the UI's database user can't
    see it): people may have made tokens, and without their owner rows none can be told from an admin's, so none
    is taken for one."""
    if not token:
        return None
    try:
        with _session() as cur:
            scope_sql = "scope" if "scope" in _columns(cur, "accessTokens") else "NULL"
            cur.execute(_sql("SELECT {}, id, name, expiresAt, revokedAt, tokenHash, {} FROM accessTokens "
                             "WHERE tokenHash IN (?, ?) AND kind = 'client'".format(_now_sql(), scope_sql)),
                        (token_hash(token), scoped_token_hash(token)))
            rows = cur.fetchall()
            if len(rows) != 1:
                if rows:  # one token stored both ways: only someone writing to the table does that
                    warn_once(_TWO_TOKEN_ROWS.format(rows[0][1], rows[1][1]))
                return None
            row = rows[0]
            if _token_status(_parse_time(row[0]), _parse_time(row[3]), _parse_time(row[4])) != "active":
                return None
            # The row says which form it is: stored as scoped, it has a scope; stored by the plain hash, it has
            # none. Anything else was changed in the database, or the scope column is gone: it lets nothing in.
            if str(row[5]).startswith(SCOPED_HASH_PREFIX) != (row[6] is not None):
                warn_once(_TOKEN_FORM.format(row[1]))
                return None
            tables = _tables(cur)
            if "users" in tables and "itemowners" not in tables:
                raise TokensUnavailable(_NO_TOKEN_OWNERS)
            cur.execute(_sql("UPDATE accessTokens SET lastUsedAt = {} WHERE id = ?".format(_now_sql())), (row[1],))
            return {"id": row[1], "name": row[2], "ownerId": _owner(cur, "token", row[1], tables),
                    "scope": None if row[6] is None else permissions.parse_scope(row[6])}
    except _MissingTables:
        return None


def client_token_name(token):
    """The name of the active managed client token `token` is, or None (see client_token)."""
    return (client_token(token) or {}).get("name")


# ---- MFA secrets ----------------------------------------------------------------
#
# Authenticator-app enrolments (TOTP seeds) for checks that sign in with two-step verification. The
# seed is encrypted before it reaches the database (see mfa.py) and never leaves this process: checks
# and people get codes. Each time step is handed out once, like sites accept each code once, so checks
# that sign in to the same account at the same time never get the same code. Whoever adds a secret owns
# it; it is used by them, by those they share it with, and by whoever may see every secret (mfa.all).

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
    """The row lock for a read-then-write transaction: FOR UPDATE on MySQL, the write lock on sqlite (which a
    transaction that changes access holds already)."""
    if DB_ENGINE == "mysql":
        return " FOR UPDATE"
    if not cur.connection.in_transaction:
        cur.execute("BEGIN IMMEDIATE")
    return ""


def list_mfa(access=None):
    """{"ready", "generated", "secrets": [...], "issues": [the newest MFA_LOG_LIMIT hand-outs]}, without seeds.

    ready is False while the database has no MFA tables. With access (who asks), only the secrets it may see
    and their hand-outs are listed, each secret with who has it (_ownership)."""
    ensure_schema()
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            tables = _tables(cur)
            if not {"mfasecrets", "mfaissues"} <= tables:
                return {"ready": False, "generated": _iso(now), "secrets": [], "issues": []}
            visible, params = ("", []) if access is None else _visible_clause("mfa", "s", access, tables)
            cur.execute(_sql("SELECT " + _MFA_COLUMNS + " FROM mfaSecrets s WHERE 1 = 1" + visible + " ORDER BY name"),
                        params)
            secrets_ = [_mfa(row) for row in cur.fetchall()]
            # The page says "last 7 days": rows not purged yet don't count.
            cur.execute(_sql("""
                SELECT i.id, i.secretId, s.name, i.step, i.issuedAt, i.issuedTo, i.label
                FROM mfaIssues i JOIN mfaSecrets s ON s.id = i.secretId
                WHERE i.issuedAt >= {}{}
                ORDER BY i.id DESC LIMIT {}
            """.format(_days_ago_sql(MFA_LOG_DAYS), visible, MFA_LOG_LIMIT)), params)
            issues = [{"id": i, "secretId": sid, "name": name, "step": step, "issuedAt": _iso(_parse_time(at)),
                       "issuedTo": to, "label": label} for i, sid, name, step, at, to, label in cur.fetchall()]
            if access is not None:
                _ownership(cur, "mfa", secrets_, access)
    except _MissingTables:
        return {"ready": False, "generated": None, "secrets": [], "issues": []}
    return {"ready": True, "generated": _iso(now), "secrets": secrets_, "issues": issues}


def _mfa_taken(cur, name, access):
    """The refusal of a name another secret has. It says how to replace that secret to whoever may see it, and
    only that the name is taken to anyone else: names are shared by everyone on the grid."""
    cur.execute(_sql("SELECT id FROM mfaSecrets WHERE name = ?"), (name,))
    row = cur.fetchone()
    if access is None or (row is not None and _item_access(cur, "mfa", row[0], access)[1]):
        return mfa.InvalidSecret("There is already a secret called {!r}. Delete it first to replace it.".format(name))
    return mfa.InvalidSecret("The name {!r} is taken. Names are shared by everyone on the grid: choose another."
                             .format(name))


def create_mfa(name, secret, issuer=None, account=None, algorithm=None, digits=None, period=None, owner_id=None,
               actor=None, access=None):
    """Store a secret, owned by owner_id (a users.id; None: the admins'). `secret` is a base32 seed or an
    otpauth://totp/ URI (whose issuer, account and settings are used unless given here). Returns the secret as
    list_mfa shows it without access, never the seed. access (who asks) decides how a taken name is refused;
    actor is who added it, for the audit log.

    Raises mfa.InvalidSecret, mfa.SecretsKeyMissing or MfaUnavailable (also for a secret someone owns, on a
    database without itemOwners)."""
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
    ensure_schema()
    try:
        with _session() as cur:
            # No locking read: on MySQL, FOR UPDATE on a name nobody has locks a gap, and two creates
            # holding it deadlock on their INSERTs. uq_mfa_name decides a race; this is the common case.
            cur.execute(_sql("SELECT id FROM mfaSecrets WHERE name = ?"), (name,))
            if cur.fetchone() is not None:
                raise _mfa_taken(cur, name, access)
            if owner_id is not None and not _owner_tables(cur)[0]:
                raise MfaUnavailable(_NO_OWNER_TABLE)
            try:
                cur.execute(_sql("""
                    INSERT INTO mfaSecrets (name, issuer, account, algorithm, digits, period, seedCipher, createdAt)
                    VALUES (?, ?, ?, ?, ?, ?, ?, {})
                """.format(_now_sql())), (name, issuer, account, algorithm, digits, period, cipher))
            except Exception as e:
                if not _is_duplicate_key(e):
                    raise
                raise _mfa_taken(cur, name, access) from None
            secret_id = cur.lastrowid
            if owner_id is not None:
                _add_owner(cur, "mfa", secret_id, owner_id)
            _audit(cur, actor, "mfa.created", "mfa", secret_id, name)
            cur.execute(_sql("SELECT " + _MFA_COLUMNS + " FROM mfaSecrets WHERE id = ?"), (secret_id,))
            return _mfa(cur.fetchone())
    except _MissingTables as e:
        raise MfaUnavailable(_NO_MFA_TABLE) from e


def delete_mfa(secret_id, access=None, actor=None):
    """Delete a secret, its hand-out log, and its owner and share rows. Returns False if there is no such secret
    (or, with access, none the viewer may see); NotPermitted when the viewer may see it but not delete it. actor
    is who deleted it, for the audit log."""
    if not 1 <= secret_id <= _MAX_TOKEN_ID:
        return False
    try:
        with _session() as cur:
            lock = _lock(cur)
            cur.execute(_sql("SELECT name FROM mfaSecrets WHERE id = ?" + lock), (secret_id,))
            row = cur.fetchone()
            if row is None or not _managed(cur, "mfa", secret_id, access, "delete"):
                return False
            cur.execute(_sql("DELETE FROM mfaSecrets WHERE id = ?"), (secret_id,))
            cur.execute(_sql("DELETE FROM mfaIssues WHERE secretId = ?"), (secret_id,))
            _drop_item_rows(cur, "mfa", secret_id)
            _audit(cur, actor, "mfa.deleted", "mfa", secret_id, row[0])
            return True
    except _MissingTables:
        return False


def _window(t, step, period):
    """(seconds until the step's window opens (0 if it has), seconds from now until it closes)."""
    return round(max(0.0, step * period - t), 3), round((step + 1) * period - t, 3)


def _not_shared(what, name):
    return NotShared("The {} {!r} belongs to someone else and isn't shared with this client token's owner."
                     .format(what, name))


def issue_mfa_code(issued_to, name=None, secret_id=None, label=None, min_valid_s=None, now=None, access=None,
                   actor=None):
    """Hand out the code of a time step nobody has had yet, for the secret called `name` (or with id
    `secret_id`): the current step if it has at least min_valid_s left and is still free, else the next
    free one. A code from a later step is handed out before its window opens: waitS says how long to
    wait before typing it, validForS how long until it stops working (both from now, by this server's
    clock, which must be in sync with the site's).

    With access (who asks), a secret it may not see is answered before its seed is even decrypted, as if it
    weren't there when asked by id (the page), and with NotShared when asked by name (a check). actor is who
    took a code on the page, for the audit log (checks' hand-outs are in mfaIssues already).

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
            if access is not None and not _item_access(cur, "mfa", row[0], access)[1]:
                if secret_id is not None:
                    return None
                raise _not_shared("MFA secret", name)
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
            _audit(cur, actor, "mfa.code_used", "mfa", sid, name)
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


def peek_mfa_code(secret_id, now=None, access=None):
    """The current step's code for someone to look at on the page, using nothing up. "issued" says whether that
    step was already handed out (a site would refuse it from a second sign-in).

    Returns {"id", "name", "code", "step", "secondsLeft", "period", "digits", "issued"} or None (also, with
    access, for a secret the viewer may not see: before its seed is even decrypted)."""
    if not 1 <= secret_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            cur.execute(_sql("SELECT id, name, algorithm, digits, period, seedCipher, lastStep FROM mfaSecrets WHERE id = ?"),
                        (secret_id,))
            row = cur.fetchone()
            if row is not None and access is not None and not _item_access(cur, "mfa", secret_id, access)[1]:
                return None
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
# encrypted before it reaches the database (see mfa.seal) and only checks get it back: people see what is
# saved and who used it, never a cookie. When nothing usable is saved for a name, one check at a time signs
# in (it holds the lease) and the others wait for the state it saves.
#
# A sign-in is owned by the owner of the client token whose check first checked its name out (the admins',
# for a token without an owner). Names are shared by everyone on the grid, so a check uses a sign-in only
# when its token's owner is a steward of it (the owner, or for the admins' one, whoever may see every
# sign-in) or it is shared with them. Only stewards' checks sign in fresh and save: nobody can make another
# person's checks type their credentials into a row they control, or save a state over the owner's.

_SIGNIN_COLUMNS = ("id, name, version, stateCipher IS NOT NULL, domains, cookieCount, originCount, savedAt, savedBy, "
                   "expiresAt, signInCount, refreshCount, useCount, lastUsedAt, lastUsedBy, rejectedCount, lastRejectedAt, "
                   "leaseHash IS NOT NULL, leaseUntil, leaseHolder")
# Checks add names themselves (a name's first checkout adds it), so their number is capped, in all and for each
# person who doesn't see every sign-in. Checks adding names at the very same moment can get a few past it.
SIGNIN_MAX_COUNT = 200
SIGNIN_MAX_PER_OWNER = 50
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


def list_signins(access=None):
    """{"ready", "generated", "signins": [...]}: what is saved for each name and who used it, never a state.

    ready is False while the database has no savedSignins table. With access (who asks), only the sign-ins it
    may see are listed, each with who has it and whether checks acting as it may use it (_ownership)."""
    ensure_schema()
    try:
        with _session() as cur:
            cur.execute("SELECT " + _now_sql())
            now = _parse_time(cur.fetchone()[0])
            tables = _tables(cur)
            if "savedsignins" not in tables:
                return {"ready": False, "generated": _iso(now), "signins": []}
            visible, params = ("", []) if access is None else _visible_clause("signin", "s", access, tables)
            cur.execute(_sql("SELECT " + _SIGNIN_COLUMNS + " FROM savedSignins s WHERE 1 = 1" + visible + " ORDER BY name"),
                        params)
            signins = [_signin(row, now) for row in cur.fetchall()]
            if access is not None:
                _ownership(cur, "signin", signins, access)
    except _MissingTables:
        return {"ready": False, "generated": None, "signins": []}
    return {"ready": True, "generated": _iso(now), "signins": signins}


def _may_add_signins(access, may_add=True):
    # A name's first checkout adds it, for checks whose token's owner may add sign-ins (and the store's own callers),
    # unless the token's scope leaves adding out (may_add).
    return may_add and (access is None or "signins.add" in access.permissions)


def _add_signin(name, access=None, actor=None, may_add=True):
    """Drop every state past its expiresAt, and add the row of a name no check used yet when the check may
    (_may_add_signins), owned by its token's owner (access.user_id; None: the admins'). SigninsFull past
    SIGNIN_MAX_COUNT names, or past SIGNIN_MAX_PER_OWNER for an owner who doesn't see every sign-in.
    actor is the check, for the audit log."""
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
        if cur.fetchone() is not None or not _may_add_signins(access, may_add):
            return
        owner_id = access.user_id if access is not None else None
        if owner_id is not None and not _owner_tables(cur)[0]:
            raise SigninsUnavailable(_NO_OWNER_TABLE)
        cur.execute("SELECT COUNT(*) FROM savedSignins")
        if cur.fetchone()[0] >= SIGNIN_MAX_COUNT:
            raise SigninsFull("There are {} saved sign-ins already, the most the grid keeps. Delete some on the Grid UI "
                              "to use new names.".format(SIGNIN_MAX_COUNT))
        if owner_id is not None and "signins.all" not in access.permissions:
            cur.execute(_sql("SELECT COUNT(*) FROM itemOwners WHERE itemKind = 'signin' AND userId = ?"), (owner_id,))
            if cur.fetchone()[0] >= SIGNIN_MAX_PER_OWNER:
                raise SigninsFull("This client token's owner has {} saved sign-ins already, the most one person's checks "
                                  "may add. Delete some on the Grid UI to use new names.".format(SIGNIN_MAX_PER_OWNER))
        try:
            cur.execute(_sql("INSERT INTO savedSignins (name, createdAt) VALUES (?, {})".format(_now_sql())), (name,))
        except Exception as e:
            if not _is_duplicate_key(e):
                raise
            return
        signin_id = cur.lastrowid
        if owner_id is not None:
            _add_owner(cur, "signin", signin_id, owner_id)
        _audit(cur, actor, "signin.created", "signin", signin_id, name, {"owner": owner_id})


def checkout_signin(used_by, name, rejected_version=None, lease_s=None, signin_id=None, access=None, actor=None,
                    may_add=True):
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

    With access (who the check acts as), a sign-in it may not use raises NotShared, and two more answers can
    come: {"status": "missing"} for a name nobody added when it may not add names, and {"status":
    "owner-signs-in", "name"} when nothing usable is saved and it isn't a steward, so it may not sign in for
    it. actor is the check, for the audit log. may_add is False when the asking client token's scope leaves out
    signins.add: a name nobody added then stays missing, whatever access holds.
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
            _add_signin(name, access, actor, may_add)
            with _session() as cur:
                lock = _lock(cur)
                cur.execute("SELECT " + _now_sql())
                now = _parse_time(cur.fetchone()[0])
                cur.execute(_sql("SELECT id, name, version, stateCipher, savedAt, expiresAt, leaseHash, leaseUntil "
                                 "FROM savedSignins WHERE name = ?" + lock), (name,))
                row = cur.fetchone()
                if row is not None:
                    steward = True
                    if access is not None:
                        # Before anything else, the drop of a rejected state included; and never round the loop
                        # again: a row someone else owns is there, it just isn't this check's.
                        _, _, _, steward, usable = _item_access(cur, "signin", row[0], access)
                        if not usable:
                            raise _not_shared("saved sign-in", name)
                    return _checkout(cur, now, row, used_by, rejected_version, signin_id, lease_s, steward)
                if not _may_add_signins(access, may_add):
                    return {"status": "missing"}
            # An admin deleted it in between: add it again.
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e


def _checkout(cur, now, row, used_by, rejected_version, rejected_id, lease_s, may_lease=True):
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
    if not may_lease:
        # Returned, not raised: the drop of a rejected state above must commit.
        return {"status": "owner-signs-in", "name": name}
    token = secrets.token_urlsafe(32)
    cur.execute(_sql("UPDATE savedSignins SET leaseHash = ?, leaseUntil = ?, leaseHolder = ? WHERE id = ?"),
                (token_hash(token), _db_time(now + datetime.timedelta(seconds=lease_s)), used_by, signin_id))
    return {"status": "sign-in", "id": signin_id, "name": name, "version": version, "leaseToken": token,
            "leaseForS": lease_s}


def save_signin(saved_by, name, state, lease_token=None, base_version=None, max_age_s=None, signin_id=None,
                access=None):
    """Keep the state a check is signed in with, for max_age_s seconds (SIGNIN_MAX_AGE_S by default):

    with lease_token, the state of the sign-in that lease was for, which ends the lease;
    with base_version and signin_id instead (the version and id the context was opened with), the newer
    state of that context (cookies roll over), kept only while that version of that id is still the saved
    one and nobody is signing in. The id tells a state from one of the same version an admin deleted:
    a name added again starts over at version 0.

    With access (who the check acts as), a sign-in it may not use raises NotShared, and only a steward's
    check saves a newer state: anyone else's is answered with the reason "shared", and the owner's state
    stays as it was. A check that held a lease as a steward and is no longer one (its token's owner lost
    “Every saved sign-in”, say) saves nothing either: its lease ends, and it gets LeaseLost, or NotShared when it
    may no longer use the sign-in at all.

    Returns {"saved": True, "version"}, {"saved": False, "reason": "newer", "signing-in" or "shared",
    "version"}, or None for an unknown name. Raises InvalidSignin (SigninTooLarge), LeaseLost,
    mfa.SecretsKeyMissing and SigninsUnavailable."""
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
    ended = None  # what a check that held the lease and may no longer save with it is told, once its lease ends
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
            steward, refused = True, None  # refused: why a check that may not use it at all is turned away
            if access is not None:
                _, _, _, steward, usable = _item_access(cur, "signin", row[0], access)
                if not usable:
                    refused = _not_shared("saved sign-in", name)
            row_id, name, version, lease_hash, lease_until = row
            leased = _lease_live(lease_hash, _parse_time(lease_until), now)
            held = lease_token is not None and leased and _holds(lease_hash, lease_token)
            if held and (refused is not None or not steward):
                # Only a steward's check is given a lease, and only a steward's saves with it: one that has stopped
                # being a steward since (it lost “Every saved sign-in”, say, and maybe the share that reached it
                # too) ends it here, so that the next steward's check signs in at once rather than wait for it.
                # Committed, then raised.
                cur.execute(_sql("UPDATE savedSignins SET leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL "
                                 "WHERE id = ?"), (row_id,))
                ended = refused or LeaseLost("Checks using this client token may no longer sign in fresh for {!r}, so "
                                             "its lease ended. Nothing was saved.".format(name))
            elif refused is not None:
                raise refused
            elif lease_token is not None:
                if not held:
                    raise LeaseLost("The lease for {!r} isn't yours any more: it ran out, or an admin forgot the sign-in. "
                                    "Nothing was saved.".format(name))
                counter = "signInCount"
            elif not steward:
                return {"saved": False, "reason": "shared", "version": version}
            elif base_version != version or signin_id != row_id:
                return {"saved": False, "reason": "newer", "version": version}
            elif leased:
                return {"saved": False, "reason": "signing-in", "version": version}
            else:
                counter = "refreshCount"
            if ended is None:
                expires = now + datetime.timedelta(seconds=int(max_age_s))
                cur.execute(_sql("""
                    UPDATE savedSignins SET stateCipher = ?, version = version + 1, domains = ?, cookieCount = ?,
                           originCount = ?, savedAt = ?, savedBy = ?, expiresAt = ?, {counter} = {counter} + 1,
                           leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL
                    WHERE id = ?
                """.format(counter=counter)), (mfa.seal(packed, name, "signin"), domains, cookies, origins,
                                               _db_time(now), saved_by, _db_time(expires), row_id))
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e
    if ended is not None:
        raise ended
    if lease_token is not None:
        log.info("saved sign-in %r: signed in by %s (version %d)", name, saved_by, version + 1)
    return {"saved": True, "version": version + 1}


def release_signin(name, lease_token, access=None):
    """End a lease without saving (the sign-in failed), so the next check signs in at once. Returns whether the
    lease was the token's, or None for an unknown name. With access (who the check acts as), a sign-in it may
    not use raises NotShared, unless the token holds its lease: a check that may no longer use it still ends
    the lease it was given, and ending it is all that token can do."""
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
                if access is not None and not _item_access(cur, "signin", row[0], access)[4]:
                    raise _not_shared("saved sign-in", name)
                return False
            cur.execute(_sql("UPDATE savedSignins SET leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL WHERE id = ?"),
                        (row[0],))
            return True
    except _MissingTables as e:
        raise SigninsUnavailable(_NO_SIGNIN_TABLE) from e


def forget_signin(signin_id, access=None, actor=None):
    """Drop the saved state and any lease, so the next check signs in again. The version goes up, so a check
    still using the old state can't save it back. Its owner and its shares stay. Returns the sign-in as
    list_signins shows it, or None (with access, also for one the viewer may not see); NotPermitted when the
    viewer may see it but not forget it. actor is who forgot it, for the audit log."""
    if not 1 <= signin_id <= _MAX_TOKEN_ID:
        return None
    try:
        with _session() as cur:
            lock = _lock(cur)
            cur.execute(_sql("SELECT name FROM savedSignins WHERE id = ?" + lock), (signin_id,))
            found = cur.fetchone()
            if found is None or not _managed(cur, "signin", signin_id, access, "forget"):
                return None
            # expiresAt goes too: nothing is left to expire, and the page tells a forgotten state from an expired
            # one by it.
            cur.execute(_sql("UPDATE savedSignins SET stateCipher = NULL, version = version + 1, expiresAt = NULL, "
                             "leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL WHERE id = ?"), (signin_id,))
            _audit(cur, actor, "signin.forgotten", "signin", signin_id, found[0])
            cur.execute(_sql("SELECT {}, {} FROM savedSignins WHERE id = ?".format(_now_sql(), _SIGNIN_COLUMNS)),
                        (signin_id,))
            row = cur.fetchone()
            signin = _signin(row[1:], _parse_time(row[0]))
            if access is not None:
                _ownership(cur, "signin", [signin], access)
            return signin
    except _MissingTables:
        return None


def delete_signin(signin_id, access=None, actor=None):
    """Delete a sign-in, its state, and its owner and share rows. Returns False if there is no such sign-in (with
    access, also for one the viewer may not see); NotPermitted when the viewer may see it but not delete it.
    A check that checks out the name again adds a new row, starting over at version 0 under a new id (ids
    aren't reused), so a check still holding the old state can't save it back over the new one's version:
    save_signin compares both. actor is who deleted it, for the audit log."""
    if not 1 <= signin_id <= _MAX_TOKEN_ID:
        return False
    try:
        with _session() as cur:
            lock = _lock(cur)
            cur.execute(_sql("SELECT name FROM savedSignins WHERE id = ?" + lock), (signin_id,))
            found = cur.fetchone()
            if found is None or not _managed(cur, "signin", signin_id, access, "delete"):
                return False
            cur.execute(_sql("DELETE FROM savedSignins WHERE id = ?"), (signin_id,))
            _drop_item_rows(cur, "signin", signin_id)
            _audit(cur, actor, "signin.deleted", "signin", signin_id, found[0])
            return True
    except _MissingTables:
        return False
