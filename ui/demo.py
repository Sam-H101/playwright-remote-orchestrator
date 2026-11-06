"""Build a sqlite database with sample nodes and sessions, for demos and screenshots.

    python -m ui.demo demo.db               # sample data (UTC timestamps relative to now)
    python -m ui.demo demo.db --empty       # a database with no tables yet
    python -m ui.demo demo.db --heartbeat   # sample data, then keep the online nodes' lastSeen fresh
    python -m ui.demo demo.db --heartbeat --activity   # ... and run short bursts of checks
                                                       # (or GRID_DEMO_ACTIVITY=1)

Then: GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app
"""
import argparse
import contextlib
import datetime
import hashlib
import json
import os
import random
import secrets
import sqlite3
import time

from . import permissions

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
    failCount INTEGER NOT NULL DEFAULT 0,
    claimedBy TEXT NULL
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
CREATE TABLE IF NOT EXISTS hubs (
    instance TEXT NOT NULL PRIMARY KEY,
    host TEXT NULL,
    port INTEGER NULL,
    startedAt TEXT NULL,
    lastSeen TEXT NULL
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
CREATE TABLE IF NOT EXISTS mfaSecrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    issuer TEXT NULL,
    account TEXT NULL,
    algorithm TEXT NOT NULL DEFAULT 'SHA1',
    digits INTEGER NOT NULL DEFAULT 6,
    period INTEGER NOT NULL DEFAULT 30,
    seedCipher TEXT NOT NULL,
    createdAt TEXT NOT NULL,
    lastStep INTEGER NULL,
    lastIssuedAt TEXT NULL,
    lastIssuedTo TEXT NULL,
    issuedCount INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS mfaIssues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    secretId INTEGER NOT NULL,
    step INTEGER NOT NULL,
    issuedAt TEXT NOT NULL,
    issuedTo TEXT NOT NULL,
    label TEXT NULL
);
CREATE TABLE IF NOT EXISTS savedSignins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    stateCipher TEXT NULL,
    version INTEGER NOT NULL DEFAULT 0,
    domains TEXT NULL,
    cookieCount INTEGER NOT NULL DEFAULT 0,
    originCount INTEGER NOT NULL DEFAULT 0,
    savedAt TEXT NULL,
    savedBy TEXT NULL,
    expiresAt TEXT NULL,
    signInCount INTEGER NOT NULL DEFAULT 0,
    refreshCount INTEGER NOT NULL DEFAULT 0,
    useCount INTEGER NOT NULL DEFAULT 0,
    lastUsedAt TEXT NULL,
    lastUsedBy TEXT NULL,
    rejectedCount INTEGER NOT NULL DEFAULT 0,
    lastRejectedAt TEXT NULL,
    leaseHash TEXT NULL,
    leaseUntil TEXT NULL,
    leaseHolder TEXT NULL,
    createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer TEXT NOT NULL,
    subject TEXT NOT NULL,
    email TEXT NULL,
    emailVerified INTEGER NOT NULL DEFAULT 0,
    name TEXT NULL,
    idpGroups TEXT NULL,
    groupsOverage INTEGER NOT NULL DEFAULT 0,
    createdAt TEXT NOT NULL,
    lastSignInAt TEXT NULL,
    lastSeenAt TEXT NULL,
    disabledAt TEXT NULL,
    disabledBy TEXT NULL,
    UNIQUE (issuer, subject)
);
CREATE TABLE IF NOT EXISTS roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT NULL,
    permissions TEXT NOT NULL DEFAULT '',
    createdAt TEXT NOT NULL,
    createdBy TEXT NULL,
    updatedAt TEXT NULL,
    updatedBy TEXT NULL
);
CREATE TABLE IF NOT EXISTS userRoles (
    userId INTEGER NOT NULL,
    roleId INTEGER NOT NULL,
    grantedAt TEXT NOT NULL,
    grantedBy TEXT NULL,
    PRIMARY KEY (userId, roleId)
);
CREATE TABLE IF NOT EXISTS groupRoles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    groupName TEXT NOT NULL,
    roleId INTEGER NOT NULL,
    note TEXT NULL,
    createdAt TEXT NOT NULL,
    createdBy TEXT NULL,
    UNIQUE (groupName, roleId)
);
CREATE TABLE IF NOT EXISTS userSessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tokenHash TEXT NOT NULL UNIQUE,
    userId INTEGER NOT NULL,
    createdAt TEXT NOT NULL,
    lastSeenAt TEXT NOT NULL,
    expiresAt TEXT NOT NULL,
    revokedAt TEXT NULL,
    revokedBy TEXT NULL,
    address TEXT NULL,
    userAgent TEXT NULL
);
CREATE TABLE IF NOT EXISTS oidcLogins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stateHash TEXT NOT NULL UNIQUE,
    browserHash TEXT NOT NULL,
    nonce TEXT NOT NULL,
    codeVerifier TEXT NOT NULL,
    returnTo TEXT NULL,
    createdAt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS itemOwners (
    itemKind TEXT NOT NULL,
    itemId INTEGER NOT NULL,
    userId INTEGER NOT NULL,
    createdAt TEXT NOT NULL,
    PRIMARY KEY (itemKind, itemId)
);
CREATE TABLE IF NOT EXISTS itemShares (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    itemKind TEXT NOT NULL,
    itemId INTEGER NOT NULL,
    granteeKind TEXT NOT NULL,
    granteeId INTEGER NOT NULL,
    sharedAt TEXT NOT NULL,
    sharedBy TEXT NULL,
    vouchedFor TEXT NULL,
    UNIQUE (itemKind, itemId, granteeKind, granteeId)
);
CREATE TABLE IF NOT EXISTS auditLog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurredAt TEXT NOT NULL,
    actorId INTEGER NULL,
    actorName TEXT NOT NULL,
    actorMethod TEXT NOT NULL,
    address TEXT NULL,
    action TEXT NOT NULL,
    targetKind TEXT NULL,
    targetId INTEGER NULL,
    targetName TEXT NULL,
    details TEXT NULL
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

# The hub marks the claims it makes for /connect sessions; the demo's checks all come through it,
# except the static host's oldest session, claimed by something else long ago.
HUB_INSTANCE = "5c1e09a2"
HUB_CLAIM = "hub:" + HUB_INSTANCE + ":"
DIRECT_CLAIMS = {("pw-static.lab.local", 3000)}

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

# People who signed in with single sign-on, for the Admin page: name, subject, email, whether the provider
# verified it, the groups their last sign-in carried, and first sign-in / last sign-in / disabled as seconds ago
# (None: never). Roles given by hand are the audit log's user.role_added rows below. They own nothing and have
# no sessions: the tokens above stay the admins'.
ISSUER = "https://login.example.com"
PEOPLE = [
    ("Robin Hale", "00udemo1", "robin.hale@example.com", True, [], 30 * DAY + 600, 3600, None),
    ("Dana Lee", "00udemo2", "dana.lee@example.com", True, ["grid-operators", "qa"], 31 * DAY, 3 * DAY, None),
    ("Priya Kumar", "00udemo3", "priya.kumar@example.com", True, [], 30 * DAY - 600, 26 * 3600, None),
    ("Lee Chen", "00udemo4", "lee.chen@example.com", True, [], 9 * DAY + 600, 2 * DAY + 3600, 2 * DAY),
    ("Alex Moreno", "00udemo5", "alex.moreno@example.com", False, [], 5 * 3600, 5 * 3600, None),
]
# A custom role: name, description, permissions. Then the groups that give roles: group, role name.
ROLES = [("QA leads", "Leads testing: sees the grid, uses shared MFA secrets and sign-ins, and reads the audit log.",
          "grid.view mfa.use signins.use audit.view")]
MAPPINGS = [("grid-operators", "Operator"), ("qa", "QA leads")]
# The audit log: seconds ago, who (a person's name, or None for the admin token), their address, the action,
# what it was about (its kind, and its name, by which its id is looked up), and details, in which a role is
# named and stands for {"id", "name"}.
ADMIN_ADDRESS, ROBIN_ADDRESS = "10.20.4.2", "10.20.4.31"
AUDIT = [
    (32 * DAY - 60, None, ADMIN_ADDRESS, "role.created", "role", "QA leads",
     {"permissions": ["audit.view", "grid.view", "mfa.use", "signins.use"]}),
    (32 * DAY - 120, None, ADMIN_ADDRESS, "group.mapped", "group", "grid-operators",
     {"group": "grid-operators", "role": "Operator"}),
    (32 * DAY - 180, None, ADMIN_ADDRESS, "group.mapped", "group", "qa", {"group": "qa", "role": "QA leads"}),
    (30 * DAY, None, ADMIN_ADDRESS, "user.role_added", "user", "Robin Hale", {"role": "Admin"}),
    (30 * DAY - 300, "Robin Hale", ROBIN_ADDRESS, "user.role_added", "user", "Priya Kumar", {"role": "Tester"}),
    (12 * DAY, None, ADMIN_ADDRESS, "token.created", "token", "Example checks", {"kind": "client", "owner": None}),
    (9 * DAY, "Robin Hale", ROBIN_ADDRESS, "user.role_added", "user", "Lee Chen", {"role": "Viewer"}),
    (3 * DAY, "Dana Lee", "10.20.6.12", "auth.signin", "user", "Dana Lee", None),
    (2 * DAY, "Robin Hale", ROBIN_ADDRESS, "user.disabled", "user", "Lee Chen",
     {"sessions": 1, "clientTokens": 0, "agentTokens": 0}),
    (26 * 3600, "Lee Chen", "10.20.7.3", "auth.signin_refused", "user", "Lee Chen", {"reason": "disabled"}),
    (5 * 3600, "Alex Moreno", "10.20.6.40", "user.created", "user", "Alex Moreno", None),
    (5 * 3600, "Alex Moreno", "10.20.6.40", "auth.signin", "user", "Alex Moreno", None),
    (3600, "Robin Hale", ROBIN_ADDRESS, "auth.signin", "user", "Robin Hale", None),
    (1800, "Robin Hale", ROBIN_ADDRESS, "grid.slot_reset", "slot", "runner-b81e04:3", None),
]


def _ts(now, seconds_ago):
    return None if seconds_ago is None else (now - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def create_schema(con):
    con.executescript(SCHEMA)


def _add_slot(cur, server, number, status, in_use_since, fails, now):
    cur.execute("INSERT INTO portStatus (PortNumber, PortStatus, PortServer, LastUpdateTime) VALUES (?, ?, ?, ?)",
                (number, status, server, _ts(now, 0)))
    slot_id = cur.lastrowid
    claimed_by = None if in_use_since is None or (server, number) in DIRECT_CLAIMS else HUB_CLAIM + str(slot_id)
    cur.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate, failCount, claimedBy) VALUES (?, ?, ?, ?, ?, ?)",
                (slot_id, number, int(in_use_since is not None), _ts(now, in_use_since), fails, claimed_by))


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
    # The hub's lease, which --heartbeat keeps renewing like a running hub does.
    cur.execute("INSERT INTO hubs (instance, host, port, startedAt, lastSeen) VALUES (?, 'hub', 4500, ?, ?)",
                (HUB_INSTANCE, _ts(now, 2 * DAY), _ts(now, 0)))
    for name, kind, node_name, created, expires, used, revoked, users in TOKENS:
        token = ("pwga_" if kind == "agent" else "pwgc_") + secrets.token_urlsafe(32)  # thrown away: a demo
        cur.execute("""
            INSERT INTO accessTokens (name, kind, tokenHash, prefix, nodeName, createdAt, expiresAt, lastUsedAt, revokedAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (name, kind, hashlib.sha256(token.encode()).hexdigest(), token[:12], node_name,
              _ts(now, created), _ts(now, expires), _ts(now, used), _ts(now, revoked)))
        cur.executemany("UPDATE nodes SET tokenId = ? WHERE name = ?", [(cur.lastrowid, user) for user in users])
    _add_people(cur, now)
    con.commit()


def _add_people(cur, now):
    """The Admin page's people, the custom role and the group mappings, and the audit log's rows about them."""
    roles = {name: role_id for role_id, (name, _, _) in permissions.BUILTIN_ROLES.items()}
    for name, description, perms in ROLES:
        cur.execute("INSERT INTO roles (name, description, permissions, createdAt, createdBy) VALUES (?, ?, ?, ?, 'Admin')",
                    (name, description, perms, _ts(now, 32 * DAY)))
        roles[name] = cur.lastrowid
    groups = {}
    for group, role in MAPPINGS:
        cur.execute("INSERT INTO groupRoles (groupName, roleId, createdAt, createdBy) VALUES (?, ?, ?, 'Admin')",
                    (group, roles[role], _ts(now, 32 * DAY)))
        groups[group] = cur.lastrowid
    people = {}
    for name, subject, email, verified, idp_groups, created, signed_in, disabled in PEOPLE:
        cur.execute("""
            INSERT INTO users (issuer, subject, email, emailVerified, name, idpGroups, createdAt, lastSignInAt, lastSeenAt,
                               disabledAt, disabledBy)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (ISSUER, subject, email, int(verified), name, json.dumps(idp_groups), _ts(now, created), _ts(now, signed_in),
              _ts(now, signed_in), _ts(now, disabled), "Robin Hale" if disabled else None))
        people[name] = cur.lastrowid

    def target_id(kind, name):
        if kind in ("user", "role", "group"):
            return {"user": people, "role": roles, "group": groups}[kind][name]
        if kind == "token":
            return cur.execute("SELECT id FROM accessTokens WHERE name = ?", (name,)).fetchone()[0]
        if kind == "slot":
            host, port = name.split(":")
            return cur.execute("SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?", (host, int(port))).fetchone()[0]
        return None

    for when, by, address, action, kind, target, details in AUDIT:
        if details and isinstance(details.get("role"), str):
            details = dict(details, role={"id": roles[details["role"]], "name": details["role"]})
        cur.execute("""
            INSERT INTO auditLog (occurredAt, actorId, actorName, actorMethod, address, action, targetKind, targetId, targetName, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (_ts(now, when), people.get(by), by or "Admin", "oidc" if by else "token", address, action, kind,
              target_id(kind, target), target, json.dumps(details, separators=(",", ":")) if details else None))
        if action == "user.role_added":
            cur.execute("INSERT INTO userRoles (userId, roleId, grantedAt, grantedBy) VALUES (?, ?, ?, ?)",
                        (people[target], details["role"]["id"], _ts(now, when), by or "Admin"))


def _selectable(con, fail_threshold=3):
    """Free slots a check could claim right now: on an online, healthy node, not failing."""
    return [r[0] for r in con.execute("""
        SELECT ps.id FROM portStatus ps JOIN portUsage pu ON ps.id = pu.id JOIN nodes n ON n.name = ps.PortServer
        WHERE pu.inUse = 0 AND ps.PortStatus = 1 AND n.online = 1 AND pu.failCount < ?
    """, (fail_threshold,))]


def burst(con, rng, now=None):
    """Claim 3 to 7 selectable slots, the way the hub does for /connect sessions. Returns the claimed ids."""
    ids = _selectable(con)
    rng.shuffle(ids)
    claimed = ids[:rng.randint(3, 7)]
    stamp = _ts(now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), 0)
    con.executemany("UPDATE portUsage SET inUse = 1, LastRunDate = ?, claimedBy = ? WHERE id = ? AND inUse = 0",
                    [(stamp, HUB_CLAIM + str(slot_id), slot_id) for slot_id in claimed])
    return claimed


def finish(con, slot_ids, rng, now=None):
    """Release them like the hub does when a session ends; now and then a check couldn't connect first."""
    stamp = _ts(now or datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None), 0)
    for slot_id in slot_ids:
        failed = rng.random() < 0.08
        con.execute("UPDATE portUsage SET inUse = 0, LastRunDate = ?, claimedBy = NULL, failCount = {} WHERE id = ?".format(
            "failCount + 1" if failed else "0"), (stamp, slot_id))


def heartbeat(path, interval=5, activity=False, seed=None):
    """Keep online nodes online. With activity, every ~8 s run a burst of 2-5 s checks."""
    rng = random.Random(seed)
    next_beat = next_burst = time.monotonic()
    running, release_at = [], None
    while True:
        now = time.monotonic()
        with contextlib.closing(sqlite3.connect(path)) as con, con:  # commits, then closes
            if now >= next_beat:
                con.execute("UPDATE nodes SET lastSeen = datetime('now') WHERE online = 1")
                con.execute("UPDATE hubs SET lastSeen = datetime('now')")
                next_beat = now + interval
            if activity and running and now >= release_at:
                finish(con, running, rng)
                running = []
            if activity and not running and now >= next_burst:
                running = burst(con, rng)
                release_at = now + rng.uniform(2, 5)
                next_burst = now + rng.uniform(6.5, 9.5)
        time.sleep(0.25 if activity else interval)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path")
    parser.add_argument("--empty", action="store_true", help="create the file but no tables")
    parser.add_argument("--heartbeat", action="store_true", help="keep online nodes online until interrupted")
    parser.add_argument("--activity", action="store_true",
                        help="with --heartbeat, also run short bursts of checks (or set GRID_DEMO_ACTIVITY=1)")
    args = parser.parse_args()
    if os.path.exists(args.path):
        os.remove(args.path)
    con = sqlite3.connect(args.path)
    if not args.empty:
        seed(con)
    con.close()
    print("wrote", args.path)
    if args.heartbeat:
        heartbeat(args.path, activity=args.activity or os.environ.get("GRID_DEMO_ACTIVITY") == "1")


if __name__ == "__main__":
    main()
