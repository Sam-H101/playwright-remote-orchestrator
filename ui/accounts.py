"""People on the Grid UI: the users single sign-on brings, their sessions, the sign-ins on their way through the
identity provider, and what each person may do.

oidc.py checks what the identity provider says; this keeps it. A user is the provider's issuer and subject, made
at their first sign-in. Their roles come from the Admin page (userRoles), from the groups the provider said they
are in at their last sign-in (groupRoles; '*' is everyone) and from GRID_OIDC_ADMINS, and are worked out again on
every request, so a change takes effect at once. A session is the sha256 of the value of a browser's cookie: the
value itself is never stored. Everything goes through store.py's connection and SQL helpers, so it works on
MySQL and on the demo's sqlite alike.
"""
import base64
import collections
import datetime
import hmac
import json
import os
import unicodedata

from flask import g, has_app_context

from . import oidc, permissions, store

# A sign-in has this long to come back from the identity provider. Older pending ones are deleted as new ones
# start.
LOGIN_TTL_S = 600
# Pending sign-ins in all, past which no more start: a flood of starts can't grow oidcLogins without end.
MAX_PENDING_LOGINS = 10000
# A session's lastSeenAt, and its user's, is written at most this often: an open page polls every few seconds.
LAST_SEEN_EVERY_S = 60
# Sessions that ended (revoked or expired) this long ago are deleted, at most _PURGE_LIMIT at a time, as new ones
# start.
ENDED_SESSIONS_KEPT_DAYS = 7
_PURGE_LIMIT = 500
# How many groups an audit row lists as added, and as removed, when someone's groups change (or those they were in
# at their first sign-in), and in how much of its JSON.
MAX_GROUPS_CHANGED = 50
_GROUPS_LISTED_LENGTH = (store.AUDIT_DETAILS_LIMIT - 100) // 2
NO_USER_TABLES = ("This database has no users, userSessions or oidcLogins table yet, so nobody can sign in with single "
                  "sign-on. The Grid UI creates them on MySQL when its database user may create tables, and so does the "
                  "gateway hub when it starts.")
# Where a role someone has came from, in the order their roles are listed.
SOURCES = ("manual", "group", "everyone", "env")
# Whom the page offers to share something with or give it to: every role, and the enabled users whose name or
# email holds what was typed, once it is DIRECTORY_MIN_QUERY characters, at most DIRECTORY_LIMIT of them.
DIRECTORY_MIN_QUERY = 2
DIRECTORY_LIMIT = 20
NO_SHARING_TABLES = ("This database has no users, itemOwners or itemShares table yet, so nothing can be shared or given "
                     "to someone. The Grid UI creates them on MySQL when its database user may create tables, and so "
                     "does the gateway hub when it starts.")
NO_PEOPLE_TABLES = ("This database has no users, roles, userRoles or groupRoles table yet, so nobody's access can be "
                    "changed. The Grid UI creates them on MySQL when its database user may create tables, and so does "
                    "the gateway hub when it starts.")
# What the Admin page lists, and how: the most people one page holds, the statuses and orders it asks for.
USERS_LIMIT = 200
USER_STATUSES = ("active", "disabled", "all")
USER_SORTS = ("recent", "name")
# How many audit entries a user's sheet shows, and the most one page of the audit log holds.
HISTORY_LIMIT = 50
AUDIT_LIMIT = 200
# The audit log's categories (the part of an action before the dot), what a target names, and the actorMethods of
# checks (the admin token's is "token").
AUDIT_CATEGORIES = ("auth", "user", "role", "group", "token", "mfa", "signin", "grid", "access")
AUDIT_TARGETS = ("user", "role", "group", "token", "mfa", "signin", "slot", "node")
CHECK_METHODS = ("client-token", "client-bootstrap")
# How many of the groups people were last seen in the group mappings tab suggests.
SEEN_GROUPS_LIMIT = 500
ROLE_NAME_LENGTH = 64
ROLE_DESCRIPTION_LENGTH = 255
GROUP_LENGTH = 255
NOTE_LENGTH = 128

# The refusals of the rules on who may change whose access (rule is what the audit log records of each).
SELF = "You can't change your own access. Ask another admin."
OUTRANKED = "You can't change {}'s access: they have permissions you don't have."
ROLE_BEYOND = "You can't give or change the role “{}”: it has permissions you don't have ({})."
SHARES_BEYOND = "You can't give the role “{}”: {} you can't see are shared with it."
SHARED_ITEMS = {"mfa": "MFA secrets", "signin": "saved sign-ins"}
SIGNIN_BEYOND = ("You can't share a saved sign-in with {}: they have permissions you don't have, and their checks would "
                 "run as whatever account is saved for it. Ask someone whose roles include “Every saved sign-in”.")
BUILT_IN = "Built-in roles can't be changed. Duplicate one to make your own."
RESERVED = "“{}” is reserved: choose another name."
EVERYONE = "Everyone who signs in can't be given a role that manages people."
LAST_ADMIN = "That would leave no admin who can sign in. Make someone else an admin first."
ROLE_TAKEN = "There is already a role called {!r}."

_tables_ok = False  # the sign-in tables were there: tables don't disappear, so this process doesn't ask again


class UsersUnavailable(Exception):
    """The database has no users tables (and the UI couldn't create them)."""


class UserDisabled(Exception):
    """An admin disabled the user who is signing in."""


class LoginsBusy(Exception):
    """MAX_PENDING_LOGINS sign-ins are on their way through the identity provider already."""


class AccessBusy(Exception):
    """Another change to who may do what holds the access lock: try again."""


class InvalidRequest(ValueError):
    """A request to the Admin page's, the sharing or the audit log's API that doesn't pass validation (a query
    argument, its body, a flag or an id); the message says why. The API answers it with 400."""


class InvalidRole(InvalidRequest):
    """A role, a group mapping, or whom something is shared with or given to, that doesn't pass validation; the
    message says why. rule is set when one of the rules on who may change whose access refused it (a built-in
    role, a reserved name, a role that manages people for everyone), and permission names what it was about, for
    the audit log, as with store.NotPermitted."""

    def __init__(self, message, rule=None, permission=None):
        super().__init__(message)
        self.rule = rule
        self.permission = permission


class NotTransferable(Exception):
    """A token that can't be given to someone: an agent token, or a client token someone owns already."""


class LastAdmin(Exception):
    """A change that would leave no enabled user who may do everything, so that nobody but the admin token could
    put things right."""


class HeldElsewhere(Exception):
    """Taking a role from someone who has it through a group mapping, the mapping for everyone or GRID_OIDC_ADMINS,
    which no change on their user can take away; the message says where it comes from."""


def _access_lock(cur):
    """The first statement of every transaction that can change anyone's permissions, so that no two run at once
    (two admins taking each other's Admin role would otherwise both succeed). MySQL: a named lock, which goes with
    the connection; AccessBusy after 5 s. sqlite: the write lock, unless the transaction holds it already."""
    if store.DB_ENGINE == "mysql":
        cur.execute("SELECT GET_LOCK('grid-ui-access', 5)")
        if cur.fetchone()[0] != 1:
            raise AccessBusy("Another change to access is in progress. Try again.")
    elif not cur.connection.in_transaction:
        cur.execute("BEGIN IMMEDIATE")


def _actor_permissions(cur, actor):
    """What the actor of a change to access may do now, worked out again inside its transaction, under the access
    lock: a request still on its way from someone just demoted gets what they have now, not what the gate saw.
    Whoever has no user (the admin token) keeps what the gate saw."""
    if actor.user_id is None:
        return actor.permissions
    found = user_access(actor.user_id, cur)
    return found["permissions"] if found else frozenset()


def _still_allowed(cur, actor, *needed):
    """What the actor may do now (_actor_permissions), refusing the change (rule "actor") unless that still holds
    every one of needed."""
    held = _actor_permissions(cur, actor)
    for permission in needed:
        if permission not in held:
            raise store.NotPermitted("Your roles no longer include “{}”.".format(permissions.PERMISSIONS[permission][1]),
                                     rule="actor", permission=permission)
    return held


def _first(perms):
    """The first of perms in the role editor's order."""
    return next(p for p in permissions.PERMISSIONS if p in perms)


def _may_give(held, name, perms):
    """Refuse (rule "role") giving, taking, changing, mapping or deleting the role `name` with perms (before
    expand()) unless whoever does it holds everything it brings: nobody hands out more than they have."""
    missing = permissions.expand(perms) - held
    if missing:
        labels = ", ".join(label for p, (_, label, _) in permissions.PERMISSIONS.items() if p in missing)
        raise store.NotPermitted(ROLE_BEYOND.format(name, labels), rule="role", permission=_first(missing))


def _actor_access(cur, actor, held):
    """What the actor may see of MFA secrets and saved sign-ins now (store.Access, as auth.access works it out),
    held being what they may do now (_actor_permissions)."""
    found = user_access(actor.user_id, cur) if actor.user_id is not None else None
    roles = tuple(r["id"] for r in found["roles"]) if found and not found["disabled"] else ()
    if held == permissions.ALL and permissions.ADMIN not in roles:
        roles += (permissions.ADMIN,)
    return store.Access(actor.user_id, roles, held)


def _may_give_shares(cur, tables, actor, held, role_id, name):
    """Refuse (rule "role") giving the role `name`, or mapping a group to it, while MFA secrets or saved sign-ins
    the actor can't see are shared with it: a role brings what is shared with it as well as its permissions, and
    nobody hands out what they can't use themselves (a group they are in would hand it to them too)."""
    if "itemshares" not in tables:
        return
    viewer = None
    for kind, table in store._ITEM_TABLES.items():
        group = store._PERMISSION_GROUPS[kind]
        if group + ".all" in held or table.lower() not in tables:
            continue
        shared = ("SELECT COUNT(*) FROM {} i WHERE i.id IN (SELECT itemId FROM itemShares WHERE itemKind = ? "
                  "AND granteeKind = 'role' AND granteeId = ?)".format(table))
        cur.execute(store._sql(shared), (kind, role_id))
        total = cur.fetchone()[0]
        if not total:
            continue
        viewer = viewer or _actor_access(cur, actor, held)
        clause, params = store._visible_clause(kind, "i", viewer, tables)
        cur.execute(store._sql(shared + clause), [kind, role_id] + params)
        if cur.fetchone()[0] < total:
            raise store.NotPermitted(SHARES_BEYOND.format(name, SHARED_ITEMS[kind]), rule="role",
                                     permission=group + ".all")


def _changeable(cur, actor, held, user_id, own=False):
    """The user whose access the actor changes, as user_access gives it as if they were enabled, or None when
    there is no such user. Refuses changing your own access (rule "self"; own allows it, for what anyone may do
    to themselves) and the access of anyone with a permission the actor doesn't hold now (rule "target"): a
    disabled admin is still an admin here, so only an admin enables one again."""
    target = user_access(user_id, cur, as_enabled=True) if _may_be("user", user_id) else None
    if target is None:
        return None
    if actor.user_id is not None and actor.user_id == user_id:
        if own:
            return target
        raise store.NotPermitted(SELF, rule="self")
    missing = target["permissions"] - held
    if missing:
        raise store.NotPermitted(OUTRANKED.format(target["name"]), rule="target", permission=_first(missing))
    return target


def _cannot_manage(viewer, target):
    """Why the viewer (a store.Actor) may not change the target's access (user_access as if enabled), as the Admin
    page says it, or None when they may."""
    if "users.manage" not in viewer.permissions:
        return "Your roles don't include “{}”.".format(permissions.PERMISSIONS["users.manage"][1])
    if viewer.user_id is not None and viewer.user_id == target["id"]:
        return SELF
    if not target["permissions"] <= viewer.permissions:
        return OUTRANKED.format(target["name"])
    return None


def _admin_count(cur, tables):
    """How many enabled users who can sign in may do everything (whatever gives it them: a role by hand, a group,
    GRID_OIDC_ADMINS). Only users of the configured identity provider can sign in: those another issuer made, before
    GRID_OIDC_ISSUER changed, don't count, and with no provider configured nobody does. The admin token doesn't
    count either: it is for when nobody can sign in."""
    issuer = oidc.settings().issuer
    if not issuer:
        return 0
    cur.execute(store._sql("SELECT " + _USER_COLUMNS + " FROM users WHERE disabledAt IS NULL AND issuer = ?"),
                (issuer,))
    return sum(1 for found in _accesses(cur, tables, cur.fetchall()) if found["permissions"] == permissions.ALL)


def _keep_an_admin(cur, tables, before):
    """Refuse (LastAdmin) a change that took the enabled admins from `before` to none. Taken before and after the
    change in its transaction, under the access lock; a grid that had none (it is being set up) may go on."""
    if before and not _admin_count(cur, tables):
        raise LastAdmin(LAST_ADMIN)


def _now(cur):
    cur.execute("SELECT " + store._now_sql())
    return store._parse_time(cur.fetchone()[0]).replace(microsecond=0)


def _forget_tables():
    global _tables_ok
    _tables_ok = False


def reset_caches():
    """Forget that the sign-in tables were there: tests drop them, and point store at other databases."""
    _forget_tables()


def _person(user_id, name, address):
    """The audit log's actor for someone who signs themselves in or out."""
    return store.Actor(user_id, name, "oidc", address, frozenset())


def _groups(text):
    """A users.idpGroups value as a list ([] when empty or unreadable)."""
    try:
        groups = json.loads(text) if text else []
    except ValueError:
        return []
    return [group for group in groups if isinstance(group, str)] if isinstance(groups, list) else []


# ---- sign-in ----------------------------------------------------------------------------


def start_login(browser_hash, state_hash, nonce, verifier, return_to):
    """Keep a sign-in on its way through the identity provider, for whichever worker the callback reaches: the
    hashes of the login cookie's value and of the state (neither value is stored), and the nonce, PKCE verifier
    and page hash it needs to finish. Pending sign-ins older than LOGIN_TTL_S go first.

    Raises LoginsBusy past MAX_PENDING_LOGINS, UsersUnavailable and DatabaseUnavailable."""
    store.ensure_schema()
    try:
        with store._session() as cur:
            now = _now(cur)
            # By id: on MySQL, a DELETE by createdAt would lock every row it scans, the rows other starts add too.
            cur.execute(store._sql("SELECT id FROM oidcLogins WHERE createdAt < ?"),
                        (store._db_time(now - datetime.timedelta(seconds=LOGIN_TTL_S)),))
            old = [str(int(i)) for (i,) in cur.fetchall()]
            if old:
                cur.execute("DELETE FROM oidcLogins WHERE id IN ({})".format(", ".join(old)))
            cur.execute("SELECT COUNT(*) FROM oidcLogins")
            busy = cur.fetchone()[0] >= MAX_PENDING_LOGINS
            if not busy:  # the old ones stay deleted either way
                cur.execute(store._sql("""
                    INSERT INTO oidcLogins (stateHash, browserHash, nonce, codeVerifier, returnTo, createdAt)
                    VALUES (?, ?, ?, ?, ?, ?)
                """), (state_hash, browser_hash, nonce, verifier, return_to, store._db_time(now)))
    except store._MissingTables as e:
        _forget_tables()
        raise UsersUnavailable(NO_USER_TABLES) from e
    if busy:
        raise LoginsBusy("{} sign-ins are on their way through the identity provider already.".format(MAX_PENDING_LOGINS))


def take_login(state_hash, browser_hash):
    """The pending sign-in the identity provider sent a browser back with, used up: {"nonce", "verifier",
    "return_to", "others"}, where others says whether that browser has more on their way (in other tabs). None
    when there is none, when it is older than LOGIN_TTL_S, or when another callback took it first. None as well
    when another browser started it, and then it stays, so nobody can spoil someone's sign-in by sending its
    state first.

    Raises UsersUnavailable and DatabaseUnavailable."""
    store.ensure_schema()
    try:
        with store._session() as cur:
            now = _now(cur)
            cur.execute(store._sql("SELECT id, browserHash, nonce, codeVerifier, returnTo, createdAt FROM oidcLogins "
                                   "WHERE stateHash = ?"), (state_hash,))
            row = cur.fetchone()
            if row is None or not hmac.compare_digest(str(row[1]), browser_hash):
                return None
            cur.execute(store._sql("DELETE FROM oidcLogins WHERE id = ?"), (row[0],))
            if cur.rowcount != 1:  # another callback with the same state took it
                return None
            oldest = now - datetime.timedelta(seconds=LOGIN_TTL_S)
            created = store._parse_time(row[5])
            if created is None or created < oldest:
                return None
            cur.execute(store._sql("SELECT id FROM oidcLogins WHERE browserHash = ? AND createdAt >= ? LIMIT 1"),
                        (browser_hash, store._db_time(oldest)))
            return {"nonce": row[2], "verifier": row[3], "return_to": row[4], "others": cur.fetchone() is not None}
    except store._MissingTables as e:
        _forget_tables()
        raise UsersUnavailable(NO_USER_TABLES) from e


def _add_user(issuer, identity, address, groups):
    """Make the user at their first sign-in, and record that with the groups they signed in with. Returns whether
    this call made them. A transaction of its own with no locking read: on MySQL, FOR UPDATE on an identity nobody
    has locks a gap, and two first sign-ins holding it deadlock on their INSERTs. uq_user_identity decides a race."""
    with store._session() as cur:
        cur.execute(store._sql("SELECT id FROM users WHERE issuer = ? AND subject = ?"), (issuer, identity["subject"]))
        if cur.fetchone() is not None:
            return False
        try:
            cur.execute(store._sql("INSERT INTO users (issuer, subject, createdAt) VALUES (?, ?, {})"
                                   .format(store._now_sql())), (issuer, identity["subject"]))
        except Exception as e:
            if not store._is_duplicate_key(e):
                raise
            return False
        user_id = cur.lastrowid
        name = identity.get("name") or "user {}".format(user_id)
        store._audit(cur, _person(user_id, name, address), "user.created", "user", user_id, name,
                     {"groups": _listed(groups)} if groups else None)
        return True


def _purge_sessions(cur, now):
    """Delete sessions that ended more than ENDED_SESSIONS_KEPT_DAYS ago, at most _PURGE_LIMIT. By id: on MySQL, a
    DELETE by date would lock every row it scans, the sessions in use too."""
    ended = store._db_time(now - datetime.timedelta(days=ENDED_SESSIONS_KEPT_DAYS))
    cur.execute(store._sql("SELECT id FROM userSessions WHERE revokedAt < ? OR expiresAt < ? LIMIT {}"
                           .format(_PURGE_LIMIT)), (ended, ended))
    old = [str(int(i)) for (i,) in cur.fetchall()]
    if old:
        cur.execute("DELETE FROM userSessions WHERE id IN ({})".format(", ".join(old)))


def _listed(groups):
    """Groups for an audit row: the first MAX_GROUPS_CHANGED, or fewer when they are long, so that a row listing
    those added and those removed (or a new user's) stays whole JSON within the audit log's limit on details."""
    kept, length = [], 0
    for group in groups[:MAX_GROUPS_CHANGED]:
        length += len(json.dumps(group)) + 1
        if length > _GROUPS_LISTED_LENGTH:
            break
        kept.append(group)
    return kept


def _random_value():
    """A session cookie's value: 32 random bytes as URL-safe text, as secrets.token_urlsafe(32) makes it. Made from
    os.urandom here because code scanning takes whatever a function named for tokens returns for a secret, and
    the cookie has to hold the value itself."""
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")


def _start_session(cur, now, person, identity, groups, before, user_agent, value_hash):
    """Keep what the provider says about the person now, and start their session, kept by the sha256 of its
    cookie's value. before is the groups they had as far as the audit log knows (sign_in works them out), and any
    change from those is recorded."""
    user_id = person.user_id
    cur.execute(store._sql("""
        UPDATE users SET email = ?, emailVerified = ?, name = ?, idpGroups = ?, groupsOverage = ?, lastSignInAt = ?,
               lastSeenAt = ?
        WHERE id = ?
    """), (identity.get("email"), int(bool(identity.get("emailVerified"))), identity.get("name"), json.dumps(groups),
           int(bool(identity.get("groupsOverage"))), store._db_time(now), store._db_time(now), user_id))
    old, new = set(before), set(groups)
    added = [group for group in groups if group not in old]
    removed = [group for group in before if group not in new]
    if added or removed:
        store._audit(cur, person, "user.groups_changed", "user", user_id, person.name,
                     {"added": _listed(added), "removed": _listed(removed)})
    expires = now + datetime.timedelta(hours=oidc.settings().session_hours)
    cur.execute(store._sql("""
        INSERT INTO userSessions (tokenHash, userId, createdAt, lastSeenAt, expiresAt, address, userAgent)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """), (value_hash, user_id, store._db_time(now), store._db_time(now), store._db_time(expires),
           person.address[:64] if person.address else None, user_agent[:255] if user_agent else None))
    _purge_sessions(cur, now)
    store._audit(cur, person, "auth.signin", "user", user_id, person.name)


def sign_in(issuer, identity, address=None, user_agent=None):
    """Sign in the person a verified ID token names (oidc.identity() of it): make their user at the first sign-in,
    keep what the provider says about them now, and start a session that lasts GRID_SESSION_HOURS. Returns (the
    session cookie's value, their access as user_access gives it); only the value's sha256 is kept.

    Raises UserDisabled (recorded in the audit log), UsersUnavailable and DatabaseUnavailable."""
    store.ensure_schema()
    groups = [] if identity.get("groupsOverage") else list(identity.get("groups") or [])[:oidc.MAX_GROUPS]
    try:
        created = _add_user(issuer, identity, address, groups)
        with store._session() as cur:
            lock = store._lock(cur)
            now = _now(cur)
            cur.execute(store._sql("SELECT id, idpGroups, disabledAt FROM users WHERE issuer = ? AND subject = ?" + lock),
                        (issuer, identity["subject"]))
            user_id, before, disabled_at = cur.fetchone()
            person = _person(user_id, identity.get("name") or "user {}".format(user_id), address)
            if disabled_at is not None:
                # In this transaction, which changes nothing else: it commits.
                store._audit(cur, person, "auth.signin_refused", "user", user_id, person.name, {"reason": "disabled"})
                value = None
            else:
                value = _random_value()
                # idpGroups is NULL until someone's first sign-in goes through. If this call made the user, its
                # user.created has these groups. If not, the one that made them failed (or is still on its way,
                # in another browser) with groups of its own: all of these count as added, so the log has them,
                # at worst twice.
                previous = _groups(before) if before is not None else groups if created else []
                _start_session(cur, now, person, identity, groups, previous, user_agent, store.token_hash(value))
                access = user_access(user_id, cur)
    except store._MissingTables as e:
        _forget_tables()
        raise UsersUnavailable(NO_USER_TABLES) from e
    if value is None:
        raise UserDisabled("{} is disabled on this grid.".format(person.name))
    return value, access


# ---- users and sessions -----------------------------------------------------------------


def _defined_roles(cur, tables, role_ids):
    """{role id: (name, its permissions before expand())} of the ids that name a role, built-in or custom."""
    found = {}
    for role_id in role_ids:
        if role_id in permissions.BUILTIN_ROLES:
            name, _, perms = permissions.BUILTIN_ROLES[role_id]
            found[role_id] = (name, perms)
    custom = sorted(int(r) for r in role_ids if r > 0)
    if custom and "roles" in tables:
        cur.execute("SELECT id, name, permissions FROM roles WHERE id IN ({})".format(", ".join(str(r) for r in custom)))
        for role_id, name, perms in cur.fetchall():
            found[role_id] = (name, permissions.parse(perms))
    return found


def _role_order(role_id, name):
    """Built-in roles first, from Admin to Viewer, then custom roles by name."""
    return (0, -role_id, "") if role_id < 0 else (1, 0, name.casefold())


def user_access(user_id, cur=None, as_enabled=False):
    """What a user may do now: {"id", "name", "email", "emailVerified", "subject", "issuer", "groups",
    "groupsOverage", "disabled", "roles", "permissions"}, or None when there is no such user (or no users table).

    roles are every role they have and where it came from: {"id", "name", "source"}, source being "manual" (given
    on the Admin page), "group" (with "group", the claim value it was mapped from), "everyone" (the '*' mapping)
    or "env" (GRID_OIDC_ADMINS). A role that comes from several places is listed once for each. permissions is
    everything those roles hold, with what that implies; nothing at all while the user is disabled, unless
    as_enabled asks what they would have (for the rules on who may change whose access). Never cached: changes
    take effect on the user's next request. cur is a transaction's own cursor, for callers inside one."""
    if cur is None:
        store.ensure_schema()
        try:
            with store._session() as cur:
                return user_access(user_id, cur, as_enabled)
        except store._MissingTables:
            _forget_tables()
            return None
    tables = store._tables(cur)
    if user_id is None or "users" not in tables:
        return None
    cur.execute(store._sql("SELECT " + _USER_COLUMNS + " FROM users WHERE id = ?"), (user_id,))
    row = cur.fetchone()
    return None if row is None else _accesses(cur, tables, [row], as_enabled)[0]


def user_accesses(cur, user_ids, as_enabled=False):
    """{users.id: user_access} of those of user_ids that are users, in the same few queries however many there are
    (_accesses): a listing of sign-ins judges each one's owner, and the Share dialog everyone it is shared with.
    cur is a transaction's own cursor."""
    ids = sorted({int(i) for i in user_ids if i is not None})
    tables = store._tables(cur) if ids else set()
    if "users" not in tables:
        return {}
    cur.execute("SELECT " + _USER_COLUMNS + " FROM users WHERE id IN ({})".format(_ids(ids)))
    return {found["id"]: found for found in _accesses(cur, tables, cur.fetchall(), as_enabled)}


# The columns of users that user_access works from, in this order.
_USER_COLUMNS = "id, issuer, subject, email, emailVerified, name, idpGroups, groupsOverage, disabledAt"


def _accesses(cur, tables, rows, as_enabled=False):
    """user_access of each users row (_USER_COLUMNS first, anything after), in their order, in the same few queries
    however many there are: the Admin page lists everyone, and the last admin rule counts them all."""
    manual = collections.defaultdict(set)
    if rows and "userroles" in tables:
        if len(rows) == 1:
            cur.execute(store._sql("SELECT userId, roleId FROM userRoles WHERE userId = ?"), (rows[0][0],))
        else:
            cur.execute("SELECT userId, roleId FROM userRoles")
        for user_id, role_id in cur.fetchall():
            manual[user_id].add(role_id)
    mappings = []
    if rows and "grouproles" in tables:
        cur.execute("SELECT groupName, roleId FROM groupRoles")
        mappings = cur.fetchall()
    s = oidc.settings()
    reached = []  # each user's {(role id, source, group)}
    for user_id, issuer, subject, email, verified, _, groups, overage, _ in (row[:9] for row in rows):
        member = set() if overage else set(_groups(groups))  # the provider didn't list them all, so none count
        found = {(role_id, "manual", None) for role_id in manual[user_id]}
        for group, role_id in mappings:
            # Compared here, exactly: MySQL's default collation would take "Ops" for "ops".
            if group == "*":
                found.add((role_id, "everyone", None))
            elif group in member:
                found.add((role_id, "group", group))
        # GRID_OIDC_ADMINS names people at the configured provider: a subject means nothing at another.
        if s.issuer and issuer == s.issuer and oidc.admin_match(
                {"subject": subject, "email": email, "emailVerified": bool(verified)}, s.admins):
            found.add((permissions.ADMIN, "env", None))
        reached.append(found)
    defined = _defined_roles(cur, tables, {r[0] for found in reached for r in found})  # unknown ids count for nothing
    return [_access(row[:9], found, defined, as_enabled) for row, found in zip(rows, reached)]


def _access(row, reached, defined, as_enabled):
    """user_access of a users row (_USER_COLUMNS), from the roles it reached and what those roles are."""
    user_id, issuer, subject, email, verified, name, groups, overage, disabled_at = row
    roles, held = [], set()
    for role_id, source, group in sorted(
            (r for r in reached if r[0] in defined),
            key=lambda r: (SOURCES.index(r[1]), _role_order(r[0], defined[r[0]][0]), r[2] or "")):
        role = {"id": role_id, "name": defined[role_id][0], "source": source}
        if group is not None:
            role["group"] = group
        roles.append(role)
        held |= defined[role_id][1]
    disabled = disabled_at is not None
    return {"id": user_id, "name": name or "user {}".format(user_id), "email": email, "emailVerified": bool(verified),
            "subject": subject, "issuer": issuer, "groups": [] if overage else _groups(groups),
            "groupsOverage": bool(overage), "disabled": disabled, "roles": roles,
            "permissions": frozenset() if disabled and not as_enabled else permissions.expand(held)}


def _ended(now, seen, expires, revoked, disabled, idle_minutes):
    """Why a session no longer works (a key of auth.ENDED), or None. Disabled comes first: disabling someone ends
    their sessions too, and they should be told why they can't sign in again, not asked to."""
    if disabled is not None:
        return "disabled"
    if revoked is not None:
        return "revoked"
    if expires is None or expires <= now:
        return "expired"
    if idle_minutes and (seen is None or seen < now - datetime.timedelta(minutes=idle_minutes)):
        return "idle"
    return None


def session_access(token):
    """Who the session cookie's value `token` signs in: user_access of their user plus "session_id", or
    oidc.Ended(reason) for a session that doesn't work (any more): unknown (also one made through another
    identity provider, before GRID_OIDC_ISSUER changed), revoked, disabled, expired or idle (unused for
    GRID_SESSION_IDLE_MINUTES). None while the database has no sessions table. Renews lastSeenAt at most once
    every LAST_SEEN_EVERY_S. One connection: it runs on every request that sends the cookie.

    Raises DatabaseUnavailable."""
    store.ensure_schema()
    s = oidc.settings()
    try:
        with store._session() as cur:
            cur.execute(store._sql("SELECT s.id, s.userId, u.issuer, s.lastSeenAt, s.expiresAt, s.revokedAt, "
                                   "u.disabledAt, {} FROM userSessions s JOIN users u ON u.id = s.userId "
                                   "WHERE s.tokenHash = ?".format(store._now_sql())), (store.token_hash(token),))
            row = cur.fetchone()
            # Nobody signs in through another provider, so a session started through one doesn't count either: a
            # grid moved off a tenant someone broke into is rid of what was signed in through it at once.
            if row is None or row[2] != s.issuer:
                return oidc.Ended("unknown")
            session_id, user_id = row[:2]
            seen, expires, revoked, disabled, now = (store._parse_time(v) for v in row[3:])
            ended = _ended(now, seen, expires, revoked, disabled, s.idle_minutes)
            if ended:
                return oidc.Ended(ended)
            stale = store._db_time(now - datetime.timedelta(seconds=LAST_SEEN_EVERY_S))
            cur.execute(store._sql("UPDATE userSessions SET lastSeenAt = ? WHERE id = ? AND lastSeenAt < ?"),
                        (store._db_time(now), session_id, stale))
            if cur.rowcount:
                cur.execute(store._sql("UPDATE users SET lastSeenAt = ? WHERE id = ? AND (lastSeenAt IS NULL OR "
                                       "lastSeenAt < ?)"), (store._db_time(now), user_id, stale))
            access = user_access(user_id, cur)
    except store._MissingTables:
        _forget_tables()
        return None
    if access is None:
        return oidc.Ended("unknown")
    access["session_id"] = session_id
    return access


def end_session(token, everywhere=False, address=None):
    """Sign a browser out: end the session the cookie's value `token` is, or with everywhere every session its
    user has, and record it with the browser's address. Returns how many ended: 0 for a value that is no session,
    or one that no longer works (ended already, expired, idle, its user disabled, or started through another
    identity provider): a cookie that can't sign anyone in can't sign anyone out either."""
    if not token:
        return 0
    store.ensure_schema()
    s = oidc.settings()
    try:
        with store._session() as cur:
            cur.execute(store._sql("SELECT s.id, u.id, u.name, u.issuer, s.lastSeenAt, s.expiresAt, u.disabledAt, {} "
                                   "FROM userSessions s JOIN users u ON u.id = s.userId "
                                   "WHERE s.tokenHash = ? AND s.revokedAt IS NULL".format(store._now_sql())),
                        (store.token_hash(token),))
            row = cur.fetchone()
            if row is None or row[3] != s.issuer:
                return 0
            session_id, user_id, name = row[:3]
            seen, expires, disabled, now = (store._parse_time(v) for v in row[4:])
            if _ended(now, seen, expires, None, disabled, s.idle_minutes):
                return 0
            which, arg = ("userId = ?", user_id) if everywhere else ("id = ?", session_id)
            cur.execute(store._sql("UPDATE userSessions SET revokedAt = ?, revokedBy = 'signed out' "
                                   "WHERE {} AND revokedAt IS NULL".format(which)), (store._db_time(now), arg))
            ended = cur.rowcount
            name = name or "user {}".format(user_id)
            store._audit(cur, _person(user_id, name, address), "auth.signout", "user", user_id, name,
                         {"everywhere": bool(everywhere)})
            return ended
    except store._MissingTables:
        _forget_tables()
        return 0


def client_token_access(token):
    """The active managed client token `token` is, and who checks using it act as: {"id", "name", "ownerId",
    "owner", "scope"}, or None. owner is user_access of the person who made it. It is None when ownerId is (an admin made
    the token, and checks using it act as an admin), but also when the owner can't be found: auth.py goes by
    ownerId, so that a token whose owner is gone is never taken for an admin's. Raises TokensUnavailable when
    whose it is can't be told (store.client_token)."""
    found = store.client_token(token)
    if found is None:
        return None
    found["owner"] = None if found["ownerId"] is None else user_access(found["ownerId"])
    return found


def tables_problem():
    """Why nobody can sign in with single sign-on for want of tables (users, userSessions or oidcLogins is
    missing), or None. None too when the database can't be reached: the request's next use of it reports that,
    and g.db_unavailable tells /api/session. Once the tables are there, this process doesn't ask again."""
    global _tables_ok
    if _tables_ok:
        return None
    try:
        store.ensure_schema()
        with store._session() as cur:
            missing = {"users", "usersessions", "oidclogins"} - store._tables(cur)
    except store.DatabaseUnavailable:
        if has_app_context():
            g.db_unavailable = True
        return None
    if missing:
        return NO_USER_TABLES
    _tables_ok = True
    return None


# The Admin page. Everyone who signed in is listed, with what they may do and where it comes from. Changing anyone's
# access runs under the access lock with the rules that keep people from handing out more than they have: whoever
# changes it must hold every permission the role brings (and that its target has, as if they were enabled), may not
# change their own, and may not leave the grid without an enabled admin. What the actor may do is worked out again
# inside the lock, so a request from someone demoted a moment ago is judged by what they have now.

# The columns of users the Admin page lists, user_access's first.
_LISTED_COLUMNS = _USER_COLUMNS + ", createdAt, lastSignInAt, lastSeenAt, disabledBy"
# The Admin page's name for what someone owns, the itemKind, and the table its items are rows of.
_OWNED = (("mfa", "mfa", "mfaSecrets"), ("signins", "signin", "savedSignins"), ("tokens", "token", "accessTokens"))


def _when(value):
    return store._iso(store._parse_time(value))


def _ids(ids):
    """Ids as SQL's IN list: numbers, never parameters, so the list can be any length."""
    return ", ".join(str(int(i)) for i in ids)


def _people(cur, tables, where="", params=()):
    """[(users row of _LISTED_COLUMNS, its user_access as if enabled)] of the users that match where."""
    cur.execute(store._sql("SELECT " + _LISTED_COLUMNS + " FROM users" + where), params)
    rows = cur.fetchall()
    return list(zip(rows, _accesses(cur, tables, rows, as_enabled=True)))


def _working(now):
    """(condition, params) of the sessions that still work, bar their user being disabled or of another issuer
    (_signs_in): not ended, not past expiresAt, and used within GRID_SESSION_IDLE_MINUTES."""
    clause, params = "revokedAt IS NULL AND expiresAt > ?", [store._db_time(now)]
    idle_minutes = oidc.settings().idle_minutes
    if idle_minutes:
        clause += " AND lastSeenAt >= ?"
        params.append(store._db_time(now - datetime.timedelta(minutes=idle_minutes)))
    return clause, params


def _signs_in(found):
    """Whether a user's sessions can work at all (user_access as if enabled): not while they are disabled, nor for
    a user of another issuer than GRID_OIDC_ISSUER, as session_access takes none of theirs."""
    return not found["disabled"] and found["issuer"] == oidc.settings().issuer


def _user_entries(cur, tables, now, people, viewer):
    """The Admin page's User of each of people (_people): who they are, their roles, what they may do now, how many
    sessions they have working (none while their sessions can't work: _signs_in), how many things they own, and
    whether the viewer (a store.Actor) may change their access."""
    ids = [row[0] for row, _ in people]
    sessions, owns = {}, {user_id: {key: 0 for key, _, _ in _OWNED} for user_id in ids}
    if ids and "usersessions" in tables:
        clause, params = _working(now)
        cur.execute(store._sql("SELECT userId, COUNT(*) FROM userSessions WHERE userId IN ({}) AND {} GROUP BY userId"
                               .format(_ids(ids), clause)), params)
        sessions = dict(cur.fetchall())
    for key, kind, table in _OWNED:
        if ids and {"itemowners", table.lower()} <= tables:
            # Joined with the items: an older version that deleted one left its owner row behind.
            cur.execute(store._sql("SELECT o.userId, COUNT(*) FROM itemOwners o JOIN {} t ON t.id = o.itemId "
                                   "WHERE o.itemKind = ? AND o.userId IN ({}) GROUP BY o.userId"
                                   .format(table, _ids(ids))), (kind,))
            for user_id, count in cur.fetchall():
                owns[user_id][key] = count
    entries = []
    for row, found in people:
        held = frozenset() if found["disabled"] else found["permissions"]
        entries.append({
            "id": found["id"], "name": found["name"], "email": found["email"], "emailVerified": found["emailVerified"],
            "subject": found["subject"], "disabled": found["disabled"], "createdAt": _when(row[9]),
            "lastSignInAt": _when(row[10]), "lastSeenAt": _when(row[11]),
            "sessions": sessions.get(found["id"], 0) if _signs_in(found) else 0, "roles": found["roles"],
            "permissions": sorted(held), "admin": held == permissions.ALL, "groupsOverage": found["groupsOverage"],
            "owns": owns[found["id"]], "canManage": _cannot_manage(viewer, found) is None})
    return entries


def _user_entry(cur, tables, now, user_id, viewer):
    return _user_entries(cur, tables, now, _people(cur, tables, " WHERE id = ?", (user_id,)), viewer)[0]


def _signed_in(entry):
    """When someone (a _people entry) last signed in, to sort by: someone who never did comes last."""
    return store._parse_time(entry[0][10]) or datetime.datetime.min


def list_users(viewer, q="", status="active", role=None, sort="recent", offset=0, limit=50):
    """The people who signed in, for the Admin page: {"ready", "generated", "total", "counts": {"active",
    "disabled"}, "users": [User]} (_user_entries; viewer, a store.Actor, decides canManage).

    q finds people whose name, email or subject holds it, ignoring case; status is active, disabled or all; role a
    role id they have in any way (by hand, through a group, the mapping for everyone or GRID_OIDC_ADMINS) or
    "none" for no role at all; sort "recent" (latest sign-in first) or "name". counts are of everyone q and role
    find, whatever their status; total of those status finds too, of which offset and limit pick the page.
    ready is False while the database has no users table."""
    text = q.strip().lower()
    store.ensure_schema()
    try:
        with store._session() as cur:
            now = _now(cur)
            tables = store._tables(cur)
            if "users" not in tables:
                return _no_users(now)
            where, params = "", ()
            if text:
                # What was typed is only ever a parameter, its '%' and '_' escaped: text to find, never a pattern.
                where = (" WHERE LOWER(name) LIKE ? ESCAPE '!' OR LOWER(email) LIKE ? ESCAPE '!' "
                         "OR LOWER(subject) LIKE ? ESCAPE '!'")
                params = (_like(text),) * 3
            people = _people(cur, tables, where, params)
            if role == "none":
                people = [(row, found) for row, found in people if not found["roles"]]
            elif role is not None:
                people = [(row, found) for row, found in people if any(r["id"] == role for r in found["roles"])]
            counts = {"active": sum(1 for _, found in people if not found["disabled"])}
            counts["disabled"] = len(people) - counts["active"]
            if status != "all":
                people = [(row, found) for row, found in people if found["disabled"] == (status == "disabled")]
            if sort == "name":
                people.sort(key=lambda entry: (entry[1]["name"].casefold(), entry[0][0]))
            else:
                people.sort(key=lambda entry: entry[0][0], reverse=True)
                people.sort(key=_signed_in, reverse=True)  # stable: the newest user first among equals
            users = _user_entries(cur, tables, now, people[offset:offset + limit], viewer)
    except store._MissingTables:  # dropped in between
        _forget_tables()
        return _no_users(datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None, microsecond=0))
    return {"ready": True, "generated": store._iso(now), "total": len(people), "counts": counts, "users": users}


def _no_users(now):
    return {"ready": False, "generated": store._iso(now), "total": 0, "counts": {"active": 0, "disabled": 0},
            "users": []}


_AUDIT_COLUMNS = ("id, occurredAt, actorId, actorName, actorMethod, address, action, targetKind, targetId, targetName, "
                  "details")


def _audit_entry(row):
    """An auditLog row (_AUDIT_COLUMNS) as the API answers with it. details is an object; one cut at
    store.AUDIT_DETAILS_LIMIT characters is no longer JSON, and comes as {"text": what is left of it}."""
    entry_id, occurred, actor_id, actor_name, method, address, action, target_kind, target_id, target_name, text = row
    try:
        details = json.loads(text) if text else {}
    except ValueError:
        details = None
    if not isinstance(details, dict):
        details = {"text": text}
    return {"id": entry_id, "occurredAt": _when(occurred), "actorId": actor_id, "actorName": actor_name,
            "actorMethod": method, "address": address, "action": action, "targetKind": target_kind,
            "targetId": target_id, "targetName": target_name, "details": details}


def user_detail(user_id, viewer, history=False, session_id=None):
    """One person for the Admin page's sheet, or None when there is no such user: {"user": User plus "issuer",
    "groups", "disabledAt" and "disabledBy", "permissions": [{"id", "via": [the ids of the roles that give it]}]
    (what they may do now, in the role editor's order), "sessions": the ones still working (none while their
    sessions can't work: _signs_in), "current" being session_id (the viewer's), "owns": {"mfa", "signins":
    [{"id", "name"}], "tokens": [{"id", "name", "kind", "status"}]}, "history": the last HISTORY_LIMIT audit
    entries they made or were the target of (only with history: whoever may read the audit log), "canManage",
    "cannotManage": why not, or None}."""
    if not _may_be("user", user_id):
        return None
    store.ensure_schema()
    try:
        with store._session() as cur:
            now = _now(cur)
            tables = store._tables(cur)
            if "users" not in tables:
                return None
            people = _people(cur, tables, " WHERE id = ?", (user_id,))
            if not people:
                return None
            row, found = people[0]
            user = _user_entries(cur, tables, now, people, viewer)[0]
            user.update({"issuer": found["issuer"], "groups": found["groups"], "disabledAt": _when(row[8]),
                         "disabledBy": row[12]})
            defined = _defined_roles(cur, tables, {r["id"] for r in found["roles"]})
            via = collections.OrderedDict((p, []) for p in permissions.PERMISSIONS if p in user["permissions"])
            for role in found["roles"]:
                for perm in permissions.expand(defined[role["id"]][1]):
                    if perm in via and role["id"] not in via[perm]:
                        via[perm].append(role["id"])
            sessions = []
            if "usersessions" in tables and _signs_in(found):
                clause, params = _working(now)
                cur.execute(store._sql("SELECT id, createdAt, lastSeenAt, expiresAt, address, userAgent "
                                       "FROM userSessions WHERE userId = ? AND {} ORDER BY lastSeenAt DESC, id DESC"
                                       .format(clause)), [user_id] + params)
                sessions = [{"id": i, "createdAt": _when(created), "lastSeenAt": _when(seen),
                             "expiresAt": _when(expires), "address": address, "userAgent": agent,
                             "current": i == session_id}
                            for i, created, seen, expires, address, agent in cur.fetchall()]
            owns = {key: [] for key, _, _ in _OWNED}
            for key, kind, table in _OWNED:
                if {"itemowners", table.lower()} <= tables:
                    extra = ", t.kind, t.expiresAt, t.revokedAt" if kind == "token" else ""
                    cur.execute(store._sql("SELECT t.id, t.name{} FROM {} t JOIN itemOwners o ON o.itemKind = ? "
                                           "AND o.itemId = t.id WHERE o.userId = ? ORDER BY t.name, t.id"
                                           .format(extra, table)), (kind, user_id))
                    for item in cur.fetchall():
                        entry = {"id": item[0], "name": item[1]}
                        if kind == "token":
                            entry["kind"] = item[2]
                            entry["status"] = store._token_status(now, store._parse_time(item[3]),
                                                                  store._parse_time(item[4]))
                        owns[key].append(entry)
            reason = _cannot_manage(viewer, found)
            detail = {"user": user, "permissions": [{"id": p, "via": roles} for p, roles in via.items()],
                      "sessions": sessions, "owns": owns, "canManage": reason is None, "cannotManage": reason}
            if history:
                entries = []
                if "auditlog" in tables:
                    cur.execute(store._sql("SELECT " + _AUDIT_COLUMNS + " FROM auditLog WHERE actorId = ? OR "
                                           "(targetKind = 'user' AND targetId = ?) ORDER BY id DESC LIMIT {}"
                                           .format(HISTORY_LIMIT)), (user_id, user_id))
                    entries = [_audit_entry(r) for r in cur.fetchall()]
                detail["history"] = entries
            return detail
    except store._MissingTables:  # dropped in between
        _forget_tables()
        return None


def _people_tables(cur):
    """The tables a change to someone's access needs (_tables' names); UsersUnavailable without them."""
    return _need_tables(cur, {"users", "roles", "userroles", "grouproles"}, NO_PEOPLE_TABLES)


def _role(cur, tables, role_id):
    """(name, permissions before expand()) of a role, built-in or custom; InvalidRole when there is no such role."""
    found = _defined_roles(cur, tables, {role_id}).get(role_id) if _may_be("role", role_id) else None
    if found is None:
        raise InvalidRole("There is no role {}.".format(role_id))
    return found


def _held_elsewhere(name, role, held):
    """Why taking a role someone has some other way than by hand changes nothing (HeldElsewhere's message)."""
    if held["source"] == "group":
        return ("{} has {} through the group {!r}. Remove the mapping, or their membership at the identity provider, "
                "instead.".format(name, role, held["group"]))
    if held["source"] == "everyone":
        return ("{} has {} through the mapping for everyone who signs in. Remove that mapping instead."
                .format(name, role))
    return "{} has {} through GRID_OIDC_ADMINS. Take them out of it instead.".format(name, role)


def _active_tokens(cur, tables, now, user_id, kind):
    """The ids of the user's tokens of a kind ('client' or 'agent') that still work."""
    if not {"accesstokens", "itemowners"} <= tables:
        return []
    cur.execute(store._sql("SELECT t.id FROM accessTokens t JOIN itemOwners o ON o.itemKind = 'token' "
                           "AND o.itemId = t.id WHERE o.userId = ? AND t.kind = ? AND t.revokedAt IS NULL "
                           "AND (t.expiresAt IS NULL OR t.expiresAt > ?)"), (user_id, kind, store._db_time(now)))
    return [row[0] for row in cur.fetchall()]


def _revoke_tokens(cur, tables, now, user_id, kinds):
    """Revoke the user's working tokens of kinds: revokedAt set, never deleted, so the Access page still shows
    them and the hub keeps requiring a token. Returns {"client": n, "agent": n}."""
    revoked = {}
    for kind in ("client", "agent"):
        ids = _active_tokens(cur, tables, now, user_id, kind) if kind in kinds else []
        if ids:
            cur.execute(store._sql("UPDATE accessTokens SET revokedAt = ? WHERE revokedAt IS NULL AND id IN ({})"
                                   .format(_ids(ids))), (store._db_time(now),))
        revoked[kind] = cur.rowcount if ids else 0
    return revoked


def _end_sessions(cur, actor, now, user_id, session_id=None):
    """End the user's sessions that haven't ended (only session_id, if given). Returns how many."""
    which, params = "", [store._db_time(now), str(actor.name)[:255], user_id, store._db_time(now)]
    if session_id is not None:
        which = " AND id = ?"
        params.append(session_id)
    cur.execute(store._sql("UPDATE userSessions SET revokedAt = ?, revokedBy = ? WHERE userId = ? AND revokedAt IS NULL "
                           "AND expiresAt > ?" + which), params)
    return cur.rowcount


def _change_access(user_id, actor, change, *needed, own=False):
    """Run change(cur, tables, now, held, target) on a user's access, in one transaction under the access lock,
    once the rules let the actor make it: they still hold needed (and users.manage), and may change this user's
    access (_changeable). Returns what change does, or None when there is no such user."""
    if not _may_be("user", user_id):
        return None
    store.ensure_schema()
    try:
        with store._session() as cur:
            _access_lock(cur)
            held = _still_allowed(cur, actor, "users.manage", *needed)
            tables = _people_tables(cur)
            target = _changeable(cur, actor, held, user_id, own)
            if target is None:
                return None
            return change(cur, tables, _now(cur), held, target)
    except store._MissingTables as e:
        _forget_tables()
        raise UsersUnavailable(NO_PEOPLE_TABLES) from e


def give_role(user_id, role_id, actor):
    """Give a user a role (a built-in role's negative id, or a roles.id) on the Admin page, recorded as the actor's
    doing. Giving it again changes nothing. Returns {"user": User} as the actor now sees them, or None when there
    is no such user.

    Raises NotPermitted when the rules forbid it (the actor's own access, a user or a role with permissions the
    actor doesn't hold, a role that items the actor can't see are shared with, or the actor's own permissions gone
    since the request came in), InvalidRole for no such role, AccessBusy and UsersUnavailable."""
    def give(cur, tables, now, held, target):
        name, perms = _role(cur, tables, role_id)
        _may_give(held, name, perms)
        _may_give_shares(cur, tables, actor, held, role_id, name)
        if not any(r["id"] == role_id and r["source"] == "manual" for r in target["roles"]):
            cur.execute(store._sql("INSERT INTO userRoles (userId, roleId, grantedAt, grantedBy) VALUES (?, ?, ?, ?)"),
                        (user_id, role_id, store._db_time(now), str(actor.name)[:255]))
            store._audit(cur, actor, "user.role_added", "user", user_id, target["name"],
                         {"role": {"id": role_id, "name": name}})
        return {"user": _user_entry(cur, tables, now, user_id, actor._replace(permissions=held))}

    return _change_access(user_id, actor, give)


def take_role(user_id, role_id, actor):
    """Take a role given on the Admin page from a user. Taking one they don't have changes nothing. Returns {"user":
    User, "activeClientTokens": n}, n being how many client tokens of theirs still work when they may no longer
    make them (the hub still takes those until they are revoked), else 0; None when there is no such user.

    Raises as give_role does, and HeldElsewhere when they have the role only through a group, the mapping for
    everyone or GRID_OIDC_ADMINS, and LastAdmin when it would leave the grid without an enabled admin."""
    def take(cur, tables, now, held, target):
        name, perms = _role(cur, tables, role_id)
        _may_give(held, name, perms)
        sources = [r for r in target["roles"] if r["id"] == role_id]
        if sources and not any(r["source"] == "manual" for r in sources):
            raise HeldElsewhere(_held_elsewhere(target["name"], name, sources[0]))
        if sources:
            before = _admin_count(cur, tables)
            cur.execute(store._sql("DELETE FROM userRoles WHERE userId = ? AND roleId = ?"), (user_id, role_id))
            _keep_an_admin(cur, tables, before)
            store._audit(cur, actor, "user.role_removed", "user", user_id, target["name"],
                         {"role": {"id": role_id, "name": name}})
        left = user_access(user_id, cur, as_enabled=True)["permissions"]
        tokens = 0 if "tokens.own" in left else len(_active_tokens(cur, tables, now, user_id, "client"))
        return {"user": _user_entry(cur, tables, now, user_id, actor._replace(permissions=held)),
                "activeClientTokens": tokens}

    return _change_access(user_id, actor, take)


def disable_user(user_id, actor, client_tokens=False, agent_tokens=False):
    """Disable a user: they can't sign in, their sessions end now, and their client tokens get nothing from the Grid
    UI (the hub still takes them for /connect, which is what revoking them with client_tokens, and agent_tokens
    for their hosts, is for). What they own stays theirs. Returns {"user": User, "sessionsEnded": n,
    "tokensRevoked": {"client": n, "agent": n}}, or None when there is no such user.

    Raises NotPermitted (as give_role), LastAdmin, AccessBusy and UsersUnavailable."""
    kinds = {kind for kind, chosen in (("client", client_tokens), ("agent", agent_tokens)) if chosen}

    def disable(cur, tables, now, held, target):
        before = _admin_count(cur, tables)
        cur.execute(store._sql("UPDATE users SET disabledAt = ?, disabledBy = ? WHERE id = ? AND disabledAt IS NULL"),
                    (store._db_time(now), str(actor.name)[:255], user_id))
        disabled = cur.rowcount
        _keep_an_admin(cur, tables, before)
        ended = _end_sessions(cur, actor, now, user_id) if "usersessions" in tables else 0
        revoked = _revoke_tokens(cur, tables, now, user_id, kinds)
        if disabled or ended or any(revoked.values()):
            store._audit(cur, actor, "user.disabled", "user", user_id, target["name"],
                         {"sessions": ended, "clientTokens": revoked["client"], "agentTokens": revoked["agent"]})
        return {"user": _user_entry(cur, tables, now, user_id, actor._replace(permissions=held)),
                "sessionsEnded": ended, "tokensRevoked": revoked}

    return _change_access(user_id, actor, disable)


def enable_user(user_id, actor):
    """Let a disabled user sign in again, with the roles they have. Only someone who holds everything they would
    have may (a disabled admin is enabled by an admin). Returns {"user": User}, or None when there is no such
    user. Raises NotPermitted, AccessBusy and UsersUnavailable."""
    def enable(cur, tables, now, held, target):
        cur.execute(store._sql("UPDATE users SET disabledAt = NULL, disabledBy = NULL WHERE id = ? "
                               "AND disabledAt IS NOT NULL"), (user_id,))
        if cur.rowcount:
            store._audit(cur, actor, "user.enabled", "user", user_id, target["name"])
        return {"user": _user_entry(cur, tables, now, user_id, actor._replace(permissions=held))}

    return _change_access(user_id, actor, enable)


def end_user_sessions(user_id, actor, session_id=None):
    """End a user's sessions (only session_id, if given): their browsers are signed out at their next request.
    Anyone may end their own. Returns how many ended (0 for a session_id that isn't theirs, or ended already), or
    None when there is no such user. Raises NotPermitted, AccessBusy and UsersUnavailable."""
    def end(cur, tables, now, held, target):
        ended = _end_sessions(cur, actor, now, user_id, session_id) if "usersessions" in tables else 0
        if ended:
            store._audit(cur, actor, "user.sessions_ended", "user", user_id, target["name"], {"sessions": ended})
        return ended

    return _change_access(user_id, actor, end, own=True)


def revoke_user_tokens(user_id, actor, client_tokens=False, agent_tokens=False):
    """Revoke a user's working client tokens and, with agent_tokens, their agent tokens: the hub turns them away
    within seconds. Returns {"client": n, "agent": n}, or None when there is no such user. Raises NotPermitted
    (your own tokens are revoked on the Access page, not here), AccessBusy and UsersUnavailable."""
    kinds = {kind for kind, chosen in (("client", client_tokens), ("agent", agent_tokens)) if chosen}

    def revoke(cur, tables, now, held, target):
        revoked = _revoke_tokens(cur, tables, now, user_id, kinds)
        if any(revoked.values()):
            store._audit(cur, actor, "user.tokens_revoked", "user", user_id, target["name"], revoked)
        return revoked

    return _change_access(user_id, actor, revoke)


def move_items(user_id, to_user_id, actor):
    """Give everything a user owns of MFA secrets and saved sign-ins to another user (a users.id), or with None to
    the admins, as set_item_owner gives each one: the sign-ins lose their shares, any sign-in under way and what
    was saved for them, the MFA secrets keep their shares. Tokens stay with whoever made them. Needs mfa.all and
    signins.all, worked out again under the access lock. Returns {"moved": {"mfa": n, "signins": n},
    "sharesRemoved": n, "leasesCleared": n, "statesDropped": n}, or None when there is no such user.

    Raises NotPermitted, InvalidRole (no such user to give them to, or a disabled one), AccessBusy and
    UsersUnavailable."""
    def move(cur, tables, now, held, target):
        _need_tables(cur, {"itemowners", "itemshares"})
        to_name = None if to_user_id is None else _enabled_user(cur, to_user_id)
        moved, removed, cleared, dropped = {"mfa": 0, "signins": 0}, 0, 0, 0
        for key, kind, table in _OWNED[:2]:
            if to_user_id == user_id or table.lower() not in tables:
                continue
            cur.execute(store._sql("SELECT id FROM {} WHERE id IN (SELECT itemId FROM itemOwners WHERE itemKind = ? "
                                   "AND userId = ?) ORDER BY id".format(table) + store._lock(cur)), (kind, user_id))
            for (item_id,) in cur.fetchall():
                _replace_owner(cur, kind, item_id, to_user_id)
                if kind == "signin":
                    handed = _hand_over_sign_in(cur, item_id)
                    removed += handed["sharesRemoved"]
                    cleared += handed["leaseCleared"]
                    dropped += handed["stateDropped"]
                moved[key] += 1
        if moved["mfa"] or moved["signins"]:
            store._audit(cur, actor, "user.items_moved", "user", user_id, target["name"],
                         {"to": None if to_user_id is None else {"id": to_user_id, "name": to_name}, **moved,
                          "statesDropped": dropped})
        return {"moved": moved, "sharesRemoved": removed, "leasesCleared": cleared, "statesDropped": dropped}

    return _change_access(user_id, actor, move, "mfa.all", "signins.all", own=True)


def named_admins():
    """GRID_OIDC_ADMINS for the Admin page: [{"entry", "userId", "name"}], each entry with the user it names (the
    first, if several share a verified email), or None for both until they sign in."""
    s = oidc.settings()
    if not s.admins:
        return []
    subjects = [entry[4:] for entry in s.admins if entry.startswith("sub:")]
    emails = [entry.lower() for entry in s.admins if not entry.startswith("sub:")]
    rows = []
    store.ensure_schema()
    try:
        with store._session() as cur:
            if "users" in store._tables(cur):
                # Narrowed here; oidc.admin_match decides, as it does for their access.
                found = [("subject IN ({})", subjects), ("LOWER(email) IN ({})", emails)]
                where = " OR ".join(clause.format(", ".join("?" * len(values))) for clause, values in found if values)
                cur.execute(store._sql("SELECT id, name, subject, email, emailVerified FROM users WHERE issuer = ? "
                                       "AND ({}) ORDER BY id".format(where)), [s.issuer] + subjects + emails)
                rows = cur.fetchall()
    except store._MissingTables:  # dropped in between
        _forget_tables()
    named = []
    for entry in s.admins:
        match = next((row for row in rows if oidc.admin_match(
            {"subject": row[2], "email": row[3], "emailVerified": bool(row[4])}, [entry])), None)
        named.append({"entry": entry, "userId": match[0] if match else None,
                      "name": (match[1] or "user {}".format(match[0])) if match else None})
    return named


# ---- roles and group mappings -----------------------------------------------------------
#
# The built-in roles are in permissions.py and never change; custom roles are rows of roles, which people with
# users.manage make, change and delete, with permissions they hold themselves. A group mapping gives everyone whose
# latest sign-in carried a group (or everyone, '*') a role.


def _role_entries(cur, tables, held):
    """Every role for the Admin page: the built-in ones from Admin to Viewer, then the custom ones by name, each
    {"id", "name", "description", "builtIn", "permissions": [as ticked], "effective": [with what they imply],
    "members": {"manual": people who have it by hand, "groups": people who have it only some other way},
    "mappings": [{"id", "group"}], "shares": how many items are shared with it, "canEdit"}. canEdit is whether
    someone who holds `held` may change and delete it: never a built-in role."""
    roles = [(role_id, name, description, perms, True) for role_id, (name, description, perms)
             in permissions.BUILTIN_ROLES.items()]
    if "roles" in tables:
        cur.execute("SELECT id, name, description, permissions FROM roles")
        roles += sorted(((role_id, name, description, permissions.parse(perms), False)
                         for role_id, name, description, perms in cur.fetchall()),
                        key=lambda r: _role_order(r[0], r[1]))
    members = collections.defaultdict(lambda: {"manual": 0, "groups": 0})
    if "users" in tables:
        for _, found in _people(cur, tables):
            sources = collections.defaultdict(set)
            for role in found["roles"]:
                sources[role["id"]].add(role["source"])
            for role_id, how in sources.items():
                members[role_id]["manual" if "manual" in how else "groups"] += 1
    mappings = collections.defaultdict(list)
    if "grouproles" in tables:
        cur.execute("SELECT id, groupName, roleId FROM groupRoles ORDER BY id")
        for mapping_id, group, role_id in cur.fetchall():
            mappings[role_id].append({"id": mapping_id, "group": group})
    shares = {}
    if "itemshares" in tables:
        cur.execute("SELECT granteeId, COUNT(*) FROM itemShares WHERE granteeKind = 'role' GROUP BY granteeId")
        shares = dict(cur.fetchall())
    return [{"id": role_id, "name": name, "description": description, "builtIn": built_in,
             "permissions": sorted(perms), "effective": sorted(permissions.expand(perms)),
             "members": dict(members[role_id]), "mappings": mappings[role_id], "shares": shares.get(role_id, 0),
             "canEdit": not built_in and "users.manage" in held and permissions.expand(perms) <= held}
            for role_id, name, description, perms, built_in in roles]


def list_roles(viewer):
    """Every role, as _role_entries gives them to the viewer (a store.Actor): {"ready", "permissions": the
    catalogue, "roles"}. ready is False while the database has no roles table: there are only the built-in
    roles then."""
    store.ensure_schema()
    try:
        with store._session() as cur:
            tables = store._tables(cur)
            roles = _role_entries(cur, tables, viewer.permissions)
    except store._MissingTables:  # dropped in between
        _forget_tables()
        tables, roles = set(), []
    return {"ready": "roles" in tables, "permissions": permissions.catalogue(), "roles": roles}


def _text(value, what, length, required=True, visible=False):
    """A name, description, group or note as sent, stripped: at most length characters, and none of them a control
    character or a lone surrogate (JSON can carry one, but no database can store it), nor with visible a format
    character (a zero-width space, a right-to-left override), which would let a name pass for another; None for an
    optional one left empty. InvalidRole, saying what it may be, otherwise."""
    if value is None and not required:
        return None
    text = value.strip() if isinstance(value, str) else None
    if text is None or len(text) > length or not oidc._control_free(text) or (required and not text) or (
            visible and any(unicodedata.category(ch) == "Cf" for ch in text)):
        raise InvalidRole("{} is {}{} characters, without control{} characters.".format(
            what, "1 to " if required else "at most ", length, " or invisible" if visible else ""))
    return text or None


def _folded(name):
    """A role's name as it is compared with others: case ignored, and compatibility forms (full-width letters,
    ligatures) as the letters they stand for, so that no role passes for another on the page."""
    return unicodedata.normalize("NFKC", name).casefold()


def _permission_list(value):
    """The permissions a role is to have, as sent: a list of known permission ids."""
    if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
        raise InvalidRole("Send permissions as a list of permission ids.")
    for perm in value:
        if perm not in permissions.PERMISSIONS:
            raise InvalidRole("There is no permission {!r}.".format(perm))
    return frozenset(value)


def _name_free(cur, name, role_id=None):
    """Refuse a role name that is reserved (rule "built-in") or another role's, as _folded compares them. MySQL
    folds accents too, which its unique key catches when the role is written (_written)."""
    if _folded(name) in permissions.reserved_names():
        raise InvalidRole(RESERVED.format(name), rule="built-in")
    cur.execute("SELECT id, name FROM roles")
    if any(_folded(other) == _folded(name) and other_id != role_id for other_id, other in cur.fetchall()):
        raise InvalidRole(ROLE_TAKEN.format(name))


def _written(cur, sql, params, name):
    """Run an INSERT or UPDATE of roles; a name another role has by the database's unique key (MySQL takes
    "Résumé" for "Resume") is refused as one found taken."""
    try:
        cur.execute(store._sql(sql), params)
    except Exception as e:
        if not store._is_duplicate_key(e):
            raise
        raise InvalidRole(ROLE_TAKEN.format(name)) from None


def _change_roles(actor, change):
    """Run change(cur, tables, now, held) on the roles or the group mappings, in one transaction under the access
    lock, once the actor still holds users.manage (held is what they hold now). Returns what change does."""
    store.ensure_schema()
    try:
        with store._session() as cur:
            _access_lock(cur)
            held = _still_allowed(cur, actor, "users.manage")
            return change(cur, _people_tables(cur), _now(cur), held)
    except store._MissingTables as e:
        _forget_tables()
        raise UsersUnavailable(NO_PEOPLE_TABLES) from e


def _a_role(cur, tables, held, role_id):
    return next(role for role in _role_entries(cur, tables, held) if role["id"] == role_id)


def create_role(name, description, perms, actor):
    """Make a custom role with a name (1 to ROLE_NAME_LENGTH characters, no other role's ignoring case, nor a
    reserved one), an optional description and permissions (permission ids), recorded as the actor's doing.
    Returns the role as list_roles shows it to them.

    Raises InvalidRole for any of that, NotPermitted for permissions the actor doesn't hold (or no longer holds
    users.manage), AccessBusy and UsersUnavailable."""
    name = _text(name, "A role's name", ROLE_NAME_LENGTH, visible=True)
    description = _text(description, "A role's description", ROLE_DESCRIPTION_LENGTH, required=False)
    perms = _permission_list(perms)

    def create(cur, tables, now, held):
        _name_free(cur, name)
        _may_give(held, name, perms)
        _written(cur, "INSERT INTO roles (name, description, permissions, createdAt, createdBy) VALUES (?, ?, ?, ?, ?)",
                 (name, description, permissions.unparse(perms), store._db_time(now), str(actor.name)[:255]), name)
        role_id = cur.lastrowid
        store._audit(cur, actor, "role.created", "role", role_id, name, {"permissions": sorted(perms)})
        return _a_role(cur, tables, held, role_id)

    return _change_roles(actor, create)


def _custom_role(cur, role_id):
    """(name, description, permissions) of a custom role, its row locked for the change, or None."""
    if not _may_be("role", role_id):
        return None
    cur.execute(store._sql("SELECT name, description, permissions FROM roles WHERE id = ?" + store._lock(cur)),
                (role_id,))
    row = cur.fetchone()
    return None if row is None else (row[0], row[1], permissions.parse(row[2]))


def update_role(role_id, changes, actor):
    """Change a custom role: changes holds any of "name", "description" and "permissions" (as create_role takes
    them). The actor must hold everything it brought and everything it will bring; a role the mapping for everyone
    points at may not come to manage people; and it may not leave the grid without an enabled admin. Changes reach
    everyone with the role at their next request. Returns the role as list_roles shows it, or None when there is no
    such role.

    Raises InvalidRole (for a built-in role too: they never change), NotPermitted, LastAdmin, AccessBusy and
    UsersUnavailable."""
    if role_id in permissions.BUILTIN_ROLES:
        raise InvalidRole(BUILT_IN, rule="built-in")
    name = _text(changes["name"], "A role's name", ROLE_NAME_LENGTH, visible=True) if "name" in changes else None
    description = (_text(changes["description"], "A role's description", ROLE_DESCRIPTION_LENGTH, required=False)
                   if "description" in changes else None)
    perms = _permission_list(changes["permissions"]) if "permissions" in changes else None

    def update(cur, tables, now, held):
        found = _custom_role(cur, role_id)
        if found is None:
            return None
        old_name, old_description, old_perms = found
        new_name = old_name if name is None else name
        new_description = description if "description" in changes else old_description
        new_perms = old_perms if perms is None else perms
        if new_name != old_name:
            _name_free(cur, new_name, role_id)
        _may_give(held, new_name, old_perms | new_perms)
        if "users.manage" in permissions.expand(new_perms):
            cur.execute(store._sql("SELECT 1 FROM groupRoles WHERE groupName = '*' AND roleId = ?"), (role_id,))
            if cur.fetchone() is not None:
                raise InvalidRole(EVERYONE, rule="everyone", permission="users.manage")
        if (new_name, new_description, new_perms) != (old_name, old_description, old_perms):
            before = _admin_count(cur, tables)
            _written(cur, "UPDATE roles SET name = ?, description = ?, permissions = ?, updatedAt = ?, updatedBy = ? "
                          "WHERE id = ?", (new_name, new_description, permissions.unparse(new_perms),
                                           store._db_time(now), str(actor.name)[:255], role_id), new_name)
            _keep_an_admin(cur, tables, before)
            store._audit(cur, actor, "role.updated", "role", role_id, new_name,
                         {"before": {"name": old_name, "permissions": sorted(old_perms)},
                          "after": {"name": new_name, "permissions": sorted(new_perms)}})
        return _a_role(cur, tables, held, role_id)

    return _change_roles(actor, update)


def delete_role(role_id, actor):
    """Delete a custom role, and with it who has it by hand, the group mappings to it and what is shared with it:
    everyone loses what it gave them at their next request. The actor must hold everything it brings, and it may
    not leave the grid without an enabled admin. Returns {"users", "mappings", "shares"}: how many of each went;
    None when there is no such role.

    Raises InvalidRole for a built-in role, NotPermitted, LastAdmin, AccessBusy and UsersUnavailable."""
    if role_id in permissions.BUILTIN_ROLES:
        raise InvalidRole(BUILT_IN, rule="built-in")

    def delete(cur, tables, now, held):
        found = _custom_role(cur, role_id)
        if found is None:
            return None
        name, _, perms = found
        _may_give(held, name, perms)
        before = _admin_count(cur, tables)
        removed = {}
        for key, sql in (("users", "DELETE FROM userRoles WHERE roleId = ?"),
                         ("mappings", "DELETE FROM groupRoles WHERE roleId = ?"),
                         ("shares", "DELETE FROM itemShares WHERE granteeKind = 'role' AND granteeId = ?")):
            removed[key] = 0
            if key != "shares" or "itemshares" in tables:
                cur.execute(store._sql(sql), (role_id,))
                removed[key] = cur.rowcount
        cur.execute(store._sql("DELETE FROM roles WHERE id = ?"), (role_id,))
        _keep_an_admin(cur, tables, before)
        store._audit(cur, actor, "role.deleted", "role", role_id, name, removed)
        return removed

    return _change_roles(actor, delete)


def _mapping_entries(cur, tables, mapping_id=None):
    """The group mappings (or the one mapping_id is) for the Admin page: {"id", "group", "roleId", "role", "note",
    "people": how many enabled users it gives the role, "createdAt", "createdBy"}, the mapping for everyone ('*')
    first, then by group; and how many enabled users were last seen in each group ({group: n})."""
    seen, everyone = collections.Counter(), 0
    if "users" in tables:
        cur.execute("SELECT idpGroups, groupsOverage FROM users WHERE disabledAt IS NULL")
        for groups, overage in cur.fetchall():
            everyone += 1
            seen.update(set() if overage else set(_groups(groups)))
    if "grouproles" not in tables:
        return [], seen
    where, params = ("", ()) if mapping_id is None else (" WHERE id = ?", (mapping_id,))
    cur.execute(store._sql("SELECT id, groupName, roleId, note, createdAt, createdBy FROM groupRoles" + where), params)
    rows = list(cur.fetchall())
    defined = _defined_roles(cur, tables, {row[2] for row in rows})
    names = {role_id: name for role_id, (name, _) in defined.items()}
    rows.sort(key=lambda r: (r[1] != "*", r[1], _role_order(r[2], names.get(r[2], ""))))
    return [{"id": i, "group": group, "roleId": role_id, "role": names.get(role_id, "role {}".format(role_id)),
             "note": note, "people": everyone if group == "*" else seen[group], "createdAt": _when(created),
             "createdBy": by} for i, group, role_id, note, created, by in rows], seen


def list_mappings():
    """The group mappings for the Admin page: {"ready", "claims": the ID token claims groups are read from
    (GRID_OIDC_GROUPS_CLAIM), "mappings": [as _mapping_entries gives them], "seen": [{"group", "people"}]: the
    groups enabled users were in at their latest sign-in, most common first, at most SEEN_GROUPS_LIMIT}. ready is
    False while the database has no groupRoles table."""
    store.ensure_schema()
    try:
        with store._session() as cur:
            tables = store._tables(cur)
            mappings, seen = _mapping_entries(cur, tables)
    except store._MissingTables:  # dropped in between
        _forget_tables()
        tables, mappings, seen = set(), [], collections.Counter()
    common = sorted(seen.items(), key=lambda item: (-item[1], item[0]))[:SEEN_GROUPS_LIMIT]
    return {"ready": "grouproles" in tables, "claims": list(oidc.settings().groups_claims), "mappings": mappings,
            "seen": [{"group": group, "people": people} for group, people in common]}


def add_mapping(group, role_id, note, actor):
    """Give everyone whose latest sign-in carried group (a value of the groups claim, compared exactly; '*' for
    everyone who signs in) the role role_id, with an optional note saying what the group is. The actor must hold
    everything the role brings, its permissions and what is shared with it, and the mapping for everyone may not
    give a role that manages people. Returns
    (the mapping as list_mappings shows it, whether it is new): mapping it again changes nothing.

    Raises InvalidRole (for the group, the note, no such role, or a role that manages people for everyone),
    NotPermitted, AccessBusy and UsersUnavailable."""
    group = _text(group, "A group", GROUP_LENGTH)
    note = _text(note, "A note", NOTE_LENGTH, required=False)

    def add(cur, tables, now, held):
        name, perms = _role(cur, tables, role_id)
        _may_give(held, name, perms)
        _may_give_shares(cur, tables, actor, held, role_id, name)
        if group == "*" and "users.manage" in permissions.expand(perms):
            raise InvalidRole(EVERYONE, rule="everyone", permission="users.manage")
        cur.execute(store._sql("SELECT id FROM groupRoles WHERE groupName = ? AND roleId = ?"), (group, role_id))
        found = cur.fetchone()
        if found is None:
            try:
                cur.execute(store._sql("INSERT INTO groupRoles (groupName, roleId, note, createdAt, createdBy) "
                                       "VALUES (?, ?, ?, ?, ?)"),
                            (group, role_id, note, store._db_time(now), str(actor.name)[:255]))
            except Exception as e:
                if not store._is_duplicate_key(e):
                    raise
            else:
                mapping_id = cur.lastrowid
                store._audit(cur, actor, "group.mapped", "group", mapping_id, group,
                             {"group": group, "role": {"id": role_id, "name": name}})
                return _mapping_entries(cur, tables, mapping_id)[0][0], True
            cur.execute(store._sql("SELECT id FROM groupRoles WHERE groupName = ? AND roleId = ?"), (group, role_id))
            found = cur.fetchone()  # mapped at the same moment by someone else
        return _mapping_entries(cur, tables, found[0])[0][0], False

    return _change_roles(actor, add)


def delete_mapping(mapping_id, actor):
    """Stop a group mapping giving its role: the actor must hold everything the role brings, and it may not leave
    the grid without an enabled admin. Returns whether there was such a mapping. Raises NotPermitted, LastAdmin,
    AccessBusy and UsersUnavailable."""
    if not 1 <= mapping_id <= store._MAX_TOKEN_ID:
        return False

    def delete(cur, tables, now, held):
        cur.execute(store._sql("SELECT groupName, roleId FROM groupRoles WHERE id = ?" + store._lock(cur)),
                    (mapping_id,))
        row = cur.fetchone()
        if row is None:
            return False
        group, role_id = row
        name, perms = _defined_roles(cur, tables, {role_id}).get(role_id, ("role {}".format(role_id), frozenset()))
        _may_give(held, name, perms)
        before = _admin_count(cur, tables)
        cur.execute(store._sql("DELETE FROM groupRoles WHERE id = ?"), (mapping_id,))
        _keep_an_admin(cur, tables, before)
        store._audit(cur, actor, "group.unmapped", "group", mapping_id, group,
                     {"group": group, "role": {"id": role_id, "name": name}})
        return True

    return _change_roles(actor, delete)


# ---- sharing ------------------------------------------------------------------------------


def _owner(user_id, people):
    if user_id is None:
        return None
    name, _, disabled = people.get(user_id, ("user {}".format(user_id), None, False))
    return {"id": user_id, "name": name, "disabled": disabled}


def annotate(kind, rows):
    """MFA secrets ('mfa'), saved sign-ins ('signin') or access tokens ('token') as the API answers with them,
    from the raw facts store adds (store._ownership, or a token's ownerId): ownerId becomes owner, {"id",
    "name", "disabled"} or None (the admins'). An MFA secret's or a sign-in's shares become sharedWith, [{"kind",
    "id", "name", "email"?, "canUse", "outranksOwner"?, "vouchLapsed"?}], where canUse says whether that user's
    permissions, or that role's, let them use it (a share does nothing without them); store lists shares only for
    those who may manage the item.

    For a saved sign-in, a share reaches someone who may do something its owner may not only when someone who
    could see every sign-in made it, and only while its owner may still do all they could then (store._reaches).
    outranksOwner (true, else left out) says the user, or the role, may use it but may do more than its owner,
    and no share reaches them (a user is also reached through a role of theirs that such a share still reaches);
    canUse is false then. vouchLapsed (true, else left out) says that share was made by someone who could see
    every sign-in, but its owner may now do less than they could then. A token gains actsAs: who checks using it
    act as, "owner" or "admin" for a client token, None for an agent token. Returns rows."""
    shares = [share for row in rows for share in row.get("shares", ())]
    user_ids = {row["ownerId"] for row in rows if row.get("ownerId") is not None}
    grantees = {share["id"] for share in shares if share["kind"] == "user"}
    role_ids = {share["id"] for share in shares if share["kind"] == "role"}
    # The shared sign-ins' owners: what they may do, as store._reaches asks.
    judged = {row["ownerId"] for row in rows if kind == "signin" and row.get("ownerId") is not None
              and row.get("shares")}
    people, accesses, roles, owners = {}, {}, {}, {}
    if user_ids | grantees or role_ids:
        store.ensure_schema()
        try:
            with store._session() as cur:
                tables = store._tables(cur)
                if user_ids | grantees and "users" in tables:
                    cur.execute("SELECT id, name, email, disabledAt FROM users WHERE id IN ({})".format(
                        _ids(sorted(user_ids | grantees))))
                    people = {user_id: (name or "user {}".format(user_id), email, disabled is not None)
                              for user_id, name, email, disabled in cur.fetchall()}
                accesses = user_accesses(cur, grantees)
                owners = {user_id: found["permissions"]
                          for user_id, found in user_accesses(cur, judged, as_enabled=True).items()}
                roles = {role_id: (name, permissions.expand(perms))
                         for role_id, (name, perms) in _defined_roles(cur, tables, role_ids).items()}
        except store._MissingTables:  # dropped in between: whoever isn't found is shown by their id
            _forget_tables()
    use = store._PERMISSION_GROUPS.get(kind, "") + ".use"
    for row in rows:
        owner_id = row.get("ownerId")
        if "ownerId" in row:
            row["owner"] = _owner(row.pop("ownerId"), people)
        if kind == "token":
            row["actsAs"] = None if row["kind"] == "agent" else "owner" if row.get("owner") else "admin"
            continue
        item_shares = row.pop("shares", ())
        reach = permissions.ALL if owner_id is None else owners.get(owner_id)  # None: no such user
        # The roles a share from someone who could see every sign-in still reaches, whatever their members may do.
        vouched_roles = {share["id"] for share in item_shares
                         if share["kind"] == "role" and store._vouch_holds(share["vouchedFor"], reach)}
        shared_with = []
        for share in item_shares:
            through = set()  # a user's roles as their checks have them (auth.access): Admin too, with every permission
            if share["kind"] == "user":
                name, email, _ = people.get(share["id"], ("user {}".format(share["id"]), None, False))
                found = accesses.get(share["id"])
                perms = found["permissions"] if found else frozenset()
                through = {role["id"] for role in found["roles"]} if found else set()
                if perms == permissions.ALL:
                    through.add(permissions.ADMIN)
                entry = {"kind": "user", "id": share["id"], "name": name, "canUse": use in perms}
                if email:
                    entry["email"] = email
            else:
                name, perms = roles.get(share["id"], ("role {}".format(share["id"]), frozenset()))
                entry = {"kind": "role", "id": share["id"], "name": name, "canUse": use in perms}
            reached = (reach is not None and perms <= reach) or store._vouch_holds(share["vouchedFor"], reach) \
                or bool(through & vouched_roles)
            if kind == "signin" and use in perms and not reached:
                entry.update(canUse=False, outranksOwner=True)
                if share["vouchedFor"] is not None:
                    entry["vouchLapsed"] = True
            shared_with.append(entry)
        row["sharedWith"] = shared_with
    return rows


def _like(text):
    """A LIKE pattern, with ESCAPE '!', for text anywhere in a value: '!', '%' and '_' stand for themselves."""
    return "%" + text.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def directory(q=""):
    """Whom something can be shared with or given to: {"roles": [{"id", "name", "builtIn", "permissions"}],
    "users": [{"id", "name", "email"}]}. roles are every role, the built-in ones first, with the permissions each
    brings (what they imply too). users are the enabled users whose name or email holds q, ignoring case: at most
    DIRECTORY_LIMIT, and none until q has DIRECTORY_MIN_QUERY characters, so nobody lists everyone by typing
    nothing. A database without the roles or users table has no custom roles or no users."""
    text = q.strip().lower()
    roles = [{"id": role_id, "name": name, "builtIn": True, "permissions": sorted(permissions.expand(perms))}
             for role_id, (name, _, perms) in permissions.BUILTIN_ROLES.items()]
    users = []
    store.ensure_schema()
    try:
        with store._session() as cur:
            tables = store._tables(cur)
            if "roles" in tables:
                cur.execute("SELECT id, name, permissions FROM roles")
                roles += [{"id": role_id, "name": name, "builtIn": False,
                           "permissions": sorted(permissions.expand(permissions.parse(perms)))}
                          for role_id, name, perms in sorted(cur.fetchall(), key=lambda r: _role_order(r[0], r[1]))]
            if len(text) >= DIRECTORY_MIN_QUERY and "users" in tables:
                # What was typed is only ever a parameter, its '%' and '_' escaped: text to find, never a pattern.
                pattern = _like(text)
                cur.execute(store._sql("SELECT id, name, email FROM users WHERE disabledAt IS NULL "
                                       "AND (LOWER(name) LIKE ? ESCAPE '!' OR LOWER(email) LIKE ? ESCAPE '!') "
                                       "ORDER BY name, id LIMIT {}".format(DIRECTORY_LIMIT)), (pattern, pattern))
                users = [{"id": user_id, "name": name or "user {}".format(user_id), "email": email}
                         for user_id, name, email in cur.fetchall()]
    except store._MissingTables:  # dropped in between
        _forget_tables()
    return {"roles": roles, "users": users}


def _may_be(grantee_kind, grantee_id):
    """Whether the id could be a user's ('user') or a role's ('role') at all: sqlite can't even bind a much larger
    number."""
    if grantee_kind == "role" and grantee_id in permissions.BUILTIN_ROLES:
        return True
    return 1 <= grantee_id <= store._MAX_TOKEN_ID


def _named_user(cur, user_id):
    """(name, disabled) of a user, or None when there is no such user."""
    if not _may_be("user", user_id):
        return None
    cur.execute(store._sql("SELECT name, disabledAt FROM users WHERE id = ?"), (user_id,))
    row = cur.fetchone()
    return None if row is None else (row[0] or "user {}".format(user_id), row[1] is not None)


def _enabled_user(cur, user_id):
    """The name of the user something is shared with or given to; InvalidRole when there is no such user, or they
    are disabled. A disabled user is named by their id: the directory leaves them out, and anyone who may share
    their own things could otherwise read their names off one id after another."""
    found = _named_user(cur, user_id)
    if found is None:
        raise InvalidRole("There is no user {}.".format(user_id))
    name, disabled = found
    if disabled:
        raise InvalidRole("User {} is disabled.".format(user_id))
    return name


def _named_role(cur, role_id):
    """The name of a role, built-in or custom, or None when there is no such role."""
    if not _may_be("role", role_id):
        return None
    found = _defined_roles(cur, store._tables(cur), {role_id}).get(role_id)
    return None if found is None else found[0]


def _name_of(cur, grantee_kind, grantee_id):
    """The name of a user ('user') or a role ('role'), as an answer or an audit row gives it: by their id when
    they are gone."""
    if grantee_kind == "user":
        found = _named_user(cur, grantee_id)
        return found[0] if found else "user {}".format(grantee_id)
    return _named_role(cur, grantee_id) or "role {}".format(grantee_id)


def _user_ref(cur, user_id):
    """A user as an audit row names them, {"id", "name"}, or None for the admins: the row reads the same after a
    rename."""
    return None if user_id is None else {"id": user_id, "name": _name_of(cur, "user", user_id)}


def _grantee(cur, kind, grantee_kind, grantee_id, owner_id, actor):
    """(name, vouched_for) of whom an item that owner_id owns (None: the admins) may be shared with, or InvalidRole
    saying why it may not be.

    A saved sign-in goes only to people and roles whose permissions are within the actor's, unless the actor may
    see every sign-in (NotPermitted, rule "grantee", otherwise): checks it is shared with run as whatever account
    its owner's checks saved, so nobody pushes an account of their own on to the checks of someone who has more
    than they do, the admins' least of all. When the actor may, vouched_for is what its owner may do now, as if
    enabled (every permission for the admins'), as itemShares.vouchedFor keeps it, else None: only such a share
    goes on reaching people who may do more than its owner, as checks use it (store._reaches), whatever they are
    given later, and only while its owner may still do all of that. An MFA secret needs no such rule: a code for
    the wrong seed signs nobody in."""
    if grantee_kind == "user":
        name = _enabled_user(cur, grantee_id)
        if grantee_id == actor.user_id:
            raise InvalidRole("You can't share it with yourself.")
        if grantee_id == owner_id:
            raise InvalidRole("It's {}'s already: owners always have it.".format(name))
        reach, who = user_access(grantee_id, cur)["permissions"], name
    else:
        name, perms = _role(cur, store._tables(cur), grantee_id)
        if kind == "mfa" and grantee_id == permissions.ADMIN:
            # Unlike a sign-in: seeing every saved sign-in doesn't let an admin's checks use one (store.py says why).
            raise InvalidRole("Admins see every MFA secret already.")
        reach, who = permissions.expand(perms), "the role “{}”".format(name)
    vouched_for = None
    if kind == "signin":
        held = _actor_permissions(cur, actor)
        missing = reach - held
        if "signins.all" in held:
            owner_may = permissions.ALL if owner_id is None else store._owner_permissions(cur, owner_id)
            vouched_for = permissions.unparse(owner_may or ())
        elif missing:
            raise store.NotPermitted(SIGNIN_BEYOND.format(who), rule="grantee", permission=_first(missing))
    return name, vouched_for


def _add_share(cur, share, actor, vouched_for=None):
    """Insert the itemShares row of share, (itemKind, itemId, granteeKind, granteeId), with vouched_for (_grantee),
    unless it is there. Returns whether it was inserted. No locking read: uq_item_share decides a race."""
    cur.execute(store._sql("SELECT 1 FROM itemShares WHERE itemKind = ? AND itemId = ? AND granteeKind = ? "
                           "AND granteeId = ?"), share)
    if cur.fetchone() is not None:
        return False
    try:
        cur.execute(store._sql("INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt, sharedBy, "
                               "vouchedFor) VALUES (?, ?, ?, ?, {}, ?, ?)".format(store._now_sql())),
                    share + (str(actor.name)[:255], vouched_for))
    except Exception as e:
        if not store._is_duplicate_key(e):
            raise
        return False  # shared with them at the same moment by someone else
    return True


def _drop_share(cur, share):
    """Delete the itemShares row of share, if it is there. Returns whether it was."""
    if not _may_be(share[2], share[3]):
        return False
    cur.execute(store._sql("DELETE FROM itemShares WHERE itemKind = ? AND itemId = ? AND granteeKind = ? "
                           "AND granteeId = ?"), share)
    return cur.rowcount > 0


def _need_tables(cur, needed, message=NO_SHARING_TABLES):
    """The database's tables (_tables' lower-case names); UsersUnavailable, with message, unless they include those
    a change needs."""
    tables = store._tables(cur)
    if needed - tables:
        raise UsersUnavailable(message)
    return tables


def _locked_item(cur, kind, item_id):
    """The name of the MFA secret or saved sign-in, its row locked for the change, or None when there is none."""
    cur.execute(store._sql("SELECT name FROM {} WHERE id = ?".format(store._ITEM_TABLES[kind]) + store._lock(cur)),
                (item_id,))
    row = cur.fetchone()
    return None if row is None else row[0]


def _change_shares(kind, item_id, grantee_kind, grantee_id, access, actor, add):
    """share_item (add) or unshare_item."""
    if not 1 <= item_id <= store._MAX_TOKEN_ID:
        return None
    store.ensure_schema()
    try:
        with store._session() as cur:
            # Under the access lock: who may use what is someone's access too.
            _access_lock(cur)
            _need_tables(cur, {"users", "itemowners", "itemshares"})
            name = _locked_item(cur, kind, item_id)
            if name is None:
                return None
            owner_id, visible, manageable, _, _ = store._item_access(cur, kind, item_id, access)
            if not visible:
                return None
            if not manageable:
                raise store.NotPermitted("Only its owner or an admin can share it.")
            share = (kind, item_id, grantee_kind, grantee_id)
            if add:
                grantee, vouched_for = _grantee(cur, kind, grantee_kind, grantee_id, owner_id, actor)
                changed = _add_share(cur, share, actor, vouched_for)
            else:
                changed = _drop_share(cur, share)
                grantee = _name_of(cur, grantee_kind, grantee_id) if changed else None
            if changed:
                store._audit(cur, actor, "{}.{}".format(kind, "shared" if add else "unshared"), kind, item_id, name,
                             {"grantee": {"kind": grantee_kind, "id": grantee_id, "name": grantee}})
            return store._listed_item(cur, kind, item_id, access)
    except store._MissingTables:  # the item's own table: there is no such item
        return None


def share_item(kind, item_id, grantee_kind, grantee_id, access, actor):
    """Share an MFA secret ('mfa') or a saved sign-in ('signin') with a user (grantee_kind 'user', a users.id) or
    with everyone who has a role ('role', a role id), so that they and their checks may use it: never manage it,
    nor share it on, and only with the permission to use what it is. Sharing it again changes nothing. access is
    who asks, actor who that is for the audit log.

    Returns the item as its listing shows it to them (store._ownership; accounts.annotate puts names to it), or
    None when there is no such item or they may not see it. Raises NotPermitted when they may see it but not
    manage it, or for a saved sign-in, when whom it is for has permissions they don't (_grantee), InvalidRole for
    whom it can't be shared with (themselves, its owner, a disabled user, no such user or role, and for an MFA
    secret the Admin role, whose members see every one already), AccessBusy and UsersUnavailable."""
    return _change_shares(kind, item_id, grantee_kind, grantee_id, access, actor, True)


def unshare_item(kind, item_id, grantee_kind, grantee_id, access, actor):
    """Stop sharing an MFA secret or a saved sign-in with a user or a role (see share_item). Stopping again, or a
    share that isn't there, changes nothing. Returns as share_item does, and raises NotPermitted, AccessBusy and
    UsersUnavailable."""
    return _change_shares(kind, item_id, grantee_kind, grantee_id, access, actor, False)


def _replace_owner(cur, kind, item_id, user_id):
    """Make user_id (a users.id, or None: the admins) the item's owner."""
    cur.execute(store._sql("DELETE FROM itemOwners WHERE itemKind = ? AND itemId = ?"), (kind, item_id))
    if user_id is not None:
        store._add_owner(cur, kind, item_id, user_id)


def _hand_over_sign_in(cur, signin_id):
    """What a saved sign-in loses with its owner: its shares, since the new owner decides who has it; any lease,
    so that nobody waits for a sign-in the old owner's check started; and its saved state, as forget_signin drops
    it, since that is an account the old owner's checks signed in as: the new owner's checks would otherwise run
    as it (a name squatted to catch the admins' checks would catch them once given to the admins). The version
    goes up, so no check still holding the old state saves it back. The audit row's {"sharesRemoved",
    "leaseCleared", "stateDropped"}; leaseCleared says whether a sign-in was under way."""
    cur.execute(store._sql("DELETE FROM itemShares WHERE itemKind = 'signin' AND itemId = ?"), (signin_id,))
    removed = cur.rowcount
    cur.execute(store._sql("SELECT leaseHash, leaseUntil, stateCipher IS NOT NULL, {} FROM savedSignins WHERE id = ?"
                           .format(store._now_sql())), (signin_id,))
    lease_hash, lease_until, saved, now = cur.fetchone()
    cur.execute(store._sql("UPDATE savedSignins SET stateCipher = NULL, version = version + 1, expiresAt = NULL, "
                           "leaseHash = NULL, leaseUntil = NULL, leaseHolder = NULL WHERE id = ?"), (signin_id,))
    return {"sharesRemoved": removed,
            "leaseCleared": store._lease_live(lease_hash, store._parse_time(lease_until), store._parse_time(now)),
            "stateDropped": bool(saved)}


def set_item_owner(kind, item_id, user_id, access, actor):
    """Give an MFA secret ('mfa') or a saved sign-in ('signin') to a user (a users.id), or with None to the admins.
    Needs mfa.all or signins.all, worked out again under the access lock (NotPermitted, rule "actor"). A sign-in
    loses its shares, any lease and its saved state with it (_hand_over_sign_in); an MFA secret keeps its shares.
    Giving it to whoever has it already changes nothing. access is who asks, actor who that is for the audit log.

    Returns the item as its listing shows it to them, with "warning" when the new owner's roles don't let them use
    it, or None when there is no such item. Raises NotPermitted, InvalidRole (no such user, or a disabled one),
    AccessBusy and UsersUnavailable."""
    if not 1 <= item_id <= store._MAX_TOKEN_ID:
        return None
    group = store._PERMISSION_GROUPS[kind]
    store.ensure_schema()
    try:
        with store._session() as cur:
            _access_lock(cur)
            _still_allowed(cur, actor, group + ".all")
            _need_tables(cur, {"users", "itemowners", "itemshares"})
            name = _locked_item(cur, kind, item_id)
            if name is None:
                return None
            to_name = None if user_id is None else _enabled_user(cur, user_id)
            before = store._owner(cur, kind, item_id)
            if before != user_id:
                _replace_owner(cur, kind, item_id, user_id)
                details = {"from": _user_ref(cur, before), "to": _user_ref(cur, user_id)}
                if kind == "signin":
                    details.update(_hand_over_sign_in(cur, item_id))
                store._audit(cur, actor, kind + ".owner_changed", kind, item_id, name, details)
            row = store._listed_item(cur, kind, item_id, access)
            use = group + ".use"
            if user_id is not None and use not in user_access(user_id, cur)["permissions"]:
                row["warning"] = "{}'s roles don't include “{}”, so they can't use it until they get one.".format(
                    to_name, permissions.PERMISSIONS[use][1])
            return row
    except store._MissingTables:  # the item's own table: there is no such item
        return None


def set_token_owner(token_id, user_id, actor):
    """Give a client token nobody owns to a user (a users.id): checks using it then act as them, rather than as an
    admin (as a token made before single sign-on, or with the admin token, does). Needs tokens.all, worked out
    again under the access lock (NotPermitted, rule "actor"). Only ever that way round: a token someone owns never
    changes hands, so nobody can pass a token on to give its checks more than their own maker has.

    Returns the token as revoke_token answers it, with its "ownerId", or None when there is no such token. Raises
    NotTransferable for an agent token or one someone owns, InvalidRole (no such user, or a disabled one),
    NotPermitted, AccessBusy and UsersUnavailable."""
    if not 1 <= token_id <= store._MAX_TOKEN_ID:
        return None
    store.ensure_schema()
    try:
        with store._session() as cur:
            _access_lock(cur)
            _still_allowed(cur, actor, "tokens.all")
            _need_tables(cur, {"users", "itemowners"})
            cur.execute(store._sql("SELECT kind, name FROM accessTokens WHERE id = ?" + store._lock(cur)), (token_id,))
            row = cur.fetchone()
            if row is None:
                return None
            kind, name = row
            if kind != "client":
                raise NotTransferable("Only client tokens act as someone.")
            owner_id = store._owner(cur, "token", token_id)
            if owner_id is not None:
                raise NotTransferable("It belongs to {} already; tokens don't change hands.".format(
                    _name_of(cur, "user", owner_id)))
            _enabled_user(cur, user_id)
            store._add_owner(cur, "token", token_id, user_id)
            store._audit(cur, actor, "token.owner_set", "token", token_id, name, {"to": _user_ref(cur, user_id)})
            cur.execute(store._sql("SELECT {}, {} FROM accessTokens WHERE id = ?".format(
                store._now_sql(), store._token_columns(cur))), (token_id,))
            row = cur.fetchone()
    except store._MissingTables:  # the accessTokens table: there is no such token
        return None
    token = store._token(row[1:], store._parse_time(row[0]))
    token["ownerId"] = user_id
    return token


# ---- audit log ----------------------------------------------------------------------------


def audit_log(before=None, limit=50, actor=None, category=None, target=None):
    """The audit log for the Admin page, newest first: {"ready", "entries": [as _audit_entry gives them], "next":
    the id to pass as before for the page after, or None, "keptDays": store.audit_days()}.

    before is the id the page starts below (keyset paging: a row written meanwhile never shifts a page); actor a
    users.id, "token" (the admin token) or "checks" (client tokens); category one of AUDIT_CATEGORIES; target
    (kind, id) of what was changed. ready is False while the database has no auditLog table."""
    clauses, params = [], []
    if before is not None:
        clauses.append("id < ?")
        params.append(before)
    if actor == "token":
        clauses.append("actorMethod = 'token'")
    elif actor == "checks":
        clauses.append("actorMethod IN ({})".format(", ".join("'{}'".format(m) for m in CHECK_METHODS)))
    elif actor is not None:
        clauses.append("actorId = ?")
        params.append(actor)
    if category is not None:
        clauses.append("action LIKE ?")  # a category has no '%' or '_' in it
        params.append(category + ".%")
    if target is not None:
        clauses.append("targetKind = ? AND targetId = ?")
        params.extend(target)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    store.ensure_schema()
    try:
        with store._session() as cur:
            if "auditlog" not in store._tables(cur):
                return {"ready": False, "entries": [], "next": None, "keptDays": store.audit_days()}
            # One more than a page, to tell whether there is a page after it.
            cur.execute(store._sql("SELECT {} FROM auditLog{} ORDER BY id DESC LIMIT ?".format(_AUDIT_COLUMNS, where)),
                        params + [int(limit) + 1])
            rows = cur.fetchall()
    except store._MissingTables:  # dropped in between
        return {"ready": False, "entries": [], "next": None, "keptDays": store.audit_days()}
    entries = [_audit_entry(row) for row in rows[:limit]]
    return {"ready": True, "entries": entries, "next": entries[-1]["id"] if len(rows) > limit else None,
            "keptDays": store.audit_days()}
