"""Users, roles, sessions, the Admin page's API and the audit log: accounts.py and the API against sqlite.
Run from the repo root: python -m pytest ui/tests"""
import collections
import contextlib
import datetime
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import types
import urllib.parse

import flask
import pytest

from ui import accounts, app as grid_app, auth, demo, oidc, permissions, recorder, store

ISSUER = "https://login.example.com"
TESTER = permissions.expand(permissions.BUILTIN_ROLES[permissions.TESTER][2])
OPERATOR = permissions.expand(permissions.BUILTIN_ROLES[permissions.OPERATOR][2])
VIEWER = permissions.expand(permissions.BUILTIN_ROLES[permissions.VIEWER][2])
ADMIN = {"X-Grid-Token": "s3cret"}  # what the db fixture sets GRID_ADMIN_TOKEN to
BOOTSTRAP = "bootstrap-client-token-for-tests"
# Who made a change, as the audit log sees the admin token.
ADMIN_ACTOR = store.Actor(None, "Admin", "token", "10.0.0.7", permissions.ALL)
OIDC_SETTINGS = ("GRID_OIDC_ISSUER", "GRID_OIDC_CLIENT_ID", "GRID_OIDC_CLIENT_SECRET", "GRID_UI_PUBLIC_URL",
                 "GRID_OIDC_ADMINS", "GRID_OIDC_GROUPS_CLAIM", "GRID_SESSION_HOURS", "GRID_SESSION_IDLE_MINUTES",
                 "GRID_AUDIT_DAYS")


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.create_schema(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    for name in OIDC_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    return path


def run(path, sql, params=()):
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute(sql, params).fetchall()


def oidc_on(monkeypatch, issuer=ISSUER):
    """Single sign-on settings with no provider behind them: enough for whatever doesn't fetch from one."""
    monkeypatch.setenv("GRID_OIDC_ISSUER", issuer)
    monkeypatch.setenv("GRID_OIDC_CLIENT_ID", "grid-ui")
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", "s3cret-client")
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", "http://localhost/")


def make_user(path, name, roles=(), groups=(), email=None, verified=True, disabled=False, issuer=None, subject=None):
    """A user as their last sign-in left them, with roles given on the Admin page. Returns their id. They signed in
    at issuer, by default the GRID_OIDC_ISSUER set now, else ISSUER: a session counts only at the issuer set."""
    issuer = issuer or os.environ.get("GRID_OIDC_ISSUER") or ISSUER
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        cur = con.execute(
            "INSERT INTO users (issuer, subject, email, emailVerified, name, idpGroups, createdAt, lastSignInAt, disabledAt) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (issuer, subject or name.lower().replace(" ", "."), email, int(verified), name, json.dumps(list(groups)),
             ts(86400), ts(3600), ts(60) if disabled else None))
        for role_id in roles:
            con.execute("INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (cur.lastrowid, role_id, ts(60)))
        return cur.lastrowid


def add_role(path, name, perms):
    """A custom role; perms is its roles.permissions value. Returns its id."""
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute("INSERT INTO roles (name, permissions, createdAt) VALUES (?, ?, ?)",
                           (name, perms, ts(60))).lastrowid


def add_mapping(path, group, role_id):
    run(path, "INSERT INTO groupRoles (groupName, roleId, createdAt) VALUES (?, ?, ?)", (group, role_id, ts(60)))


def add_session(path, user_id, seen_ago=0, expires_in=43200, revoked_ago=None):
    """A session of the user's (times in seconds; a negative expires_in has passed). Returns the cookie's value."""
    value = secrets.token_urlsafe(32)
    run(path, "INSERT INTO userSessions (tokenHash, userId, createdAt, lastSeenAt, expiresAt, revokedAt) "
              "VALUES (?, ?, ?, ?, ?, ?)",
        (store.token_hash(value), user_id, ts(seen_ago + 60), ts(seen_ago), ts(-expires_in),
         None if revoked_ago is None else ts(revoked_ago)))
    return value


def sign_in_as(client, user_id):
    """Sign the test client in as the user, as single sign-on would. Returns the session cookie's value."""
    value = add_session(store.SQLITE_PATH, user_id)
    client.set_cookie(oidc.cookie_names().session, value)
    return value


def unparsed(perms):
    """A set of permissions as itemShares.vouchedFor keeps it, or None."""
    return None if perms is None else permissions.unparse(perms)


def add_item(path, kind, name, owner=None, shares=(), vouched_for=None):
    """An MFA secret ('mfa') or a saved sign-in ('signin') with that owner (a users.id, or None: the admins') and
    those shares ((granteeKind, granteeId) pairs), made by someone who may see every one when vouched_for is what
    its owner could do then (a set of permissions). Returns its id."""
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        if kind == "mfa":
            cur = con.execute("INSERT INTO mfaSecrets (name, seedCipher, createdAt) VALUES (?, 'x', ?)", (name, ts(60)))
        else:
            cur = con.execute("INSERT INTO savedSignins (name, createdAt) VALUES (?, ?)", (name, ts(60)))
        if owner is not None:
            con.execute("INSERT INTO itemOwners (itemKind, itemId, userId, createdAt) VALUES (?, ?, ?, ?)",
                        (kind, cur.lastrowid, owner, ts(60)))
        for grantee_kind, grantee_id in shares:
            con.execute("INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt, vouchedFor) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (kind, cur.lastrowid, grantee_kind, grantee_id, ts(60), unparsed(vouched_for)))
        return cur.lastrowid


def add_client_token(path, name, owner=None):
    """A managed client token, made by that user (None: with the admin token). Returns (its id, its value)."""
    value = "pwgc_" + name
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        cur = con.execute("INSERT INTO accessTokens (name, kind, tokenHash, prefix, createdAt) VALUES (?, 'client', ?, ?, ?)",
                          (name, hashed(value), value[:12], ts(3600)))
        if owner is not None:
            con.execute("INSERT INTO itemOwners (itemKind, itemId, userId, createdAt) VALUES ('token', ?, ?, ?)",
                        (cur.lastrowid, owner, ts(3600)))
        return cur.lastrowid, value


def audit_rows(path):
    return run(path, "SELECT actorId, actorName, actorMethod, address, action, targetKind, targetId, targetName, details "
                     "FROM auditLog ORDER BY id")


def identity(subject="dana.lee", name="Dana Lee", email="dana@example.com", verified=True, groups=(), overage=False):
    """What oidc.identity() makes of a verified ID token."""
    return {"subject": subject, "email": email, "emailVerified": verified, "name": name, "groups": list(groups),
            "groupsOverage": overage}


# ---- permissions.py ----------------------------------------------------------------------


def test_the_fourteen_permissions_in_order():
    assert list(permissions.PERMISSIONS) == [
        "grid.view", "grid.operate", "tokens.own", "tokens.agents", "tokens.all", "mfa.use", "mfa.add", "mfa.all",
        "signins.use", "signins.add", "signins.all", "users.view", "users.manage", "audit.view"]
    assert permissions.ALL == frozenset(permissions.PERMISSIONS)
    assert permissions.OPEN_GRID == {"grid.view", "grid.operate"}
    assert all(p in permissions.ALL for p in permissions.IMPLIES)
    assert all(p in permissions.ALL for implied in permissions.IMPLIES.values() for p in implied)


@pytest.mark.parametrize("given, expanded", [
    ({"mfa.all"}, {"mfa.all", "mfa.add", "mfa.use"}),
    ({"signins.all"}, {"signins.all", "signins.add", "signins.use"}),
    ({"tokens.all"}, {"tokens.all", "tokens.own", "tokens.agents"}),
    ({"users.manage"}, {"users.manage", "users.view"}),
    ({"grid.operate"}, {"grid.operate", "grid.view"}),
    ({"audit.view", "mfa.use"}, {"audit.view", "mfa.use"}),
    ({"mfa.add", "made.up", ""}, {"mfa.add", "mfa.use"}),  # ids this version doesn't know count for nothing
    (set(), set()),
])
def test_a_permission_brings_what_it_implies(given, expanded):
    assert permissions.expand(given) == expanded
    assert isinstance(permissions.expand(given), frozenset)


def test_a_custom_role_ignores_permissions_it_does_not_know():
    assert permissions.parse(" mfa.add  made.up\tgrid.view mfa.add ") == {"mfa.add", "grid.view"}  # not expanded
    assert permissions.parse("") == permissions.parse(None) == frozenset()
    assert permissions.unparse({"users.view", "grid.view", "audit.view"}) == "audit.view grid.view users.view"
    assert permissions.parse(permissions.unparse(permissions.ALL)) == permissions.ALL


def test_the_built_in_roles():
    assert (permissions.ADMIN, permissions.OPERATOR, permissions.TESTER, permissions.VIEWER) == (-1, -2, -3, -4)
    roles = {role_id: (name, permissions.expand(perms))
             for role_id, (name, _, perms) in permissions.BUILTIN_ROLES.items()}
    assert roles == {
        -1: ("Admin", permissions.ALL),
        -2: ("Operator", {"grid.operate", "grid.view", "tokens.own", "tokens.agents", "mfa.add", "mfa.use",
                          "signins.add", "signins.use"}),
        -3: ("Tester", {"grid.view", "tokens.own", "mfa.add", "mfa.use", "signins.add", "signins.use"}),
        -4: ("Viewer", {"grid.view"}),
    }
    # Only Admin sees people, everyone's tokens and secrets, or the audit log.
    everything = {"users.view", "tokens.all", "mfa.all", "signins.all", "audit.view"}
    assert all(not perms & everything for name, perms in roles.values() if name != "Admin")
    # The trust that adding hosts implies is said wherever it is given.
    assert "see what checks do" in permissions.BUILTIN_ROLES[permissions.OPERATOR][1]
    assert "sign-ins and codes" in permissions.PERMISSIONS["tokens.agents"][2]


def test_the_catalogue_for_the_role_editor():
    catalogue = permissions.catalogue()
    assert [p["id"] for p in catalogue] == list(permissions.PERMISSIONS)
    found = {p["id"]: p for p in catalogue}
    assert found["mfa.all"] == {"id": "mfa.all", "group": "MFA secrets", "label": "Every MFA secret",
                                "description": "See, use, share, delete and change the owner of every MFA secret.",
                                "implies": ["mfa.use", "mfa.add"]}
    assert found["tokens.all"]["implies"] == ["tokens.own", "tokens.agents"]
    assert found["signins.add"]["implies"] == ["signins.use"]
    assert found["audit.view"]["implies"] == found["grid.view"]["implies"] == []
    groups = [p["group"] for p in catalogue]
    assert groups == sorted(groups, key=groups.index)  # each group's permissions together, for one fieldset each
    assert list(dict.fromkeys(groups)) == ["Grid", "Access tokens", "MFA secrets", "Saved sign-ins", "People"]


def test_names_a_custom_role_cannot_take():
    """The built-in roles' names, and "Admins", the owner shown for items nobody owns."""
    assert permissions.reserved_names() == {"admin", "operator", "tester", "viewer", "admins"}
    assert "ADMINS".casefold() in permissions.reserved_names()


# ---- store.py: owners and sharing ------------------------------------------------------------

BOB, DANA, ERIN, CAROL, FRANK = 1, 2, 3, 4, 6
READERS = 7  # a custom role's id
VIEWERS = {
    "the owner": store.Access(BOB, (permissions.TESTER,), TESTER),
    "a grantee": store.Access(DANA, (permissions.TESTER,), TESTER),
    "a grantee by role": store.Access(ERIN, (READERS,), permissions.expand({"mfa.use", "signins.use"})),
    "a stranger": store.Access(CAROL, (permissions.TESTER,), TESTER),
    "a grantee without the permission": store.Access(DANA, (permissions.VIEWER,), permissions.expand({"grid.view"})),
    "an admin": store.Access(5, (permissions.ADMIN,), permissions.ALL),
    "the admin token": store.Access(None, (permissions.ADMIN,), permissions.ALL),
}


def test_which_tables_say_who_owns_what(db):
    """_tables() gives lower-case names, so the checks compare with lower-case ones, or they'd never find them."""
    with store._session() as cur:
        assert store._owner_tables(cur) == (True, True)
    run(db, "DROP TABLE itemShares")
    with store._session() as cur:
        assert store._owner_tables(cur) == (True, False)
    run(db, "DROP TABLE itemOwners")
    with store._session() as cur:
        assert store._owner_tables(cur) == (False, False)


@pytest.mark.parametrize("who, rights", [
    ("the owner", (True, True, True, True)),
    ("a grantee", (True, False, False, True)),
    ("a grantee by role", (True, False, False, True)),
    ("a stranger", (False, False, False, False)),
    ("a grantee without the permission", (False, False, False, False)),
    # Admins see and manage it, but their checks don't use a sign-in someone else controls.
    ("an admin", (True, True, False, False)),
    ("the admin token", (True, True, False, False)),
])
def test_what_each_viewer_may_do_with_a_sign_in_someone_owns(db, who, rights):
    """(owner, visible, manageable, steward, usable)."""
    assert make_user(db, "Bob Stone", [permissions.TESTER]) == BOB
    signin = add_item(db, "signin", "prod-admin", owner=BOB, shares=[("user", DANA), ("role", READERS)])
    with store._session() as cur:
        assert store._item_access(cur, "signin", signin, VIEWERS[who]) == (BOB,) + rights


@pytest.mark.parametrize("who, rights", [
    ("an admin", (True, True, True, True)),  # admins' checks are its stewards
    ("the admin token", (True, True, True, True)),
    ("a grantee", (True, False, False, True)),
    ("the owner", (False, False, False, False)),  # Bob owns other things, not this
])
def test_a_sign_in_nobody_owns_is_the_admins(db, who, rights):
    signin = add_item(db, "signin", "shared-admin", shares=[("user", DANA)])
    with store._session() as cur:
        assert store._item_access(cur, "signin", signin, VIEWERS[who]) == (None,) + rights


def test_a_sign_in_shared_with_the_admin_role_reaches_every_admin(db):
    """Only someone who may see every sign-in shares one with the Admin role (accounts._grantee), so the share is
    vouched for, with what Bob may do then."""
    assert make_user(db, "Bob Stone", [permissions.TESTER]) == BOB
    signin = add_item(db, "signin", "bob-reports", owner=BOB, shares=[("role", permissions.ADMIN)], vouched_for=TESTER)
    with store._session() as cur:
        for who in ("an admin", "the admin token"):
            assert store._item_access(cur, "signin", signin, VIEWERS[who]) == (BOB, True, True, False, True)


def test_a_sign_ins_shares_reach_only_who_may_do_no_more_than_its_owner(db):
    """Or a share someone who may see every sign-in made, while its owner may still do all they could then: checks
    it reaches run as whatever account its owner's checks saved. Bob shares his with Viewer, a role everyone may
    have through the '*' mapping: it reaches a Tester, but not an Operator or an admin who is a Viewer too, nor
    anyone once he is gone. An MFA secret's shares reach everyone they are for."""
    assert make_user(db, "Bob Stone", [permissions.TESTER]) == BOB
    signin = add_item(db, "signin", "prod-admin", owner=BOB, shares=[("role", permissions.VIEWER)])
    secret = add_item(db, "mfa", "intranet", owner=BOB, shares=[("role", permissions.VIEWER)])
    viewers = (store.Access(DANA, (permissions.TESTER, permissions.VIEWER), TESTER),
               store.Access(CAROL, (permissions.OPERATOR, permissions.VIEWER), OPERATOR),
               store.Access(5, (permissions.ADMIN, permissions.VIEWER), permissions.ALL))

    def usable(kind="signin", item=signin, vouches=()):
        with store._session() as cur:
            assert [store._shared_with(cur, kind, item, access) for access in viewers] == [(True, list(vouches))] * 3
            return [store._item_access(cur, kind, item, access)[4] for access in viewers]

    def bob_is(role_id):
        run(db, "DELETE FROM userRoles WHERE userId = ?", (BOB,))
        run(db, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (BOB, role_id, ts(5)))

    assert usable() == [True, False, False]
    assert usable("mfa", secret) == [True, True, True]
    run(db, "UPDATE users SET disabledAt = ? WHERE id = ?", (ts(5), BOB))  # what he'd have counts: his shares stay
    assert usable() == [True, False, False]
    # Shared by someone who may see every sign-in while Bob was a Tester: it reaches everyone it is for while he
    # may still do all a Tester may, more or not.
    run(db, "UPDATE itemShares SET vouchedFor = ? WHERE itemKind = 'signin'", (permissions.unparse(TESTER),))
    assert usable(vouches=[TESTER]) == [True, True, True]
    bob_is(permissions.OPERATOR)
    assert usable(vouches=[TESTER]) == [True, True, True]
    # Once he may do less, it reaches only who may do no more than he may now.
    bob_is(permissions.VIEWER)
    assert usable(vouches=[TESTER]) == [False, False, False]
    bob_is(permissions.TESTER)
    run(db, "DELETE FROM users")
    assert usable(vouches=[TESTER]) == [False, False, False]


@pytest.mark.parametrize("who, visible, manageable", [
    ("the owner", True, True),
    ("a grantee", True, False),
    ("a grantee by role", True, False),
    ("a stranger", False, False),
    ("a grantee without the permission", False, False),
    ("an admin", True, True),
    ("the admin token", True, True),
])
def test_what_each_viewer_may_do_with_an_mfa_secret_someone_owns(db, who, visible, manageable):
    secret = add_item(db, "mfa", "intranet", owner=BOB, shares=[("user", DANA), ("role", READERS)])
    add_item(db, "signin", "unrelated", shares=[("user", CAROL)])  # the same id, another kind: none of the secret's
    with store._session() as cur:
        assert store._item_access(cur, "mfa", secret, VIEWERS[who])[:3] == (BOB, visible, manageable)


def test_without_the_owner_tables_everything_is_the_admins(db):
    signin = add_item(db, "signin", "prod-admin", owner=BOB, shares=[("user", DANA)])
    run(db, "DROP TABLE itemOwners")
    run(db, "DROP TABLE itemShares")
    with store._session() as cur:
        assert store._owner(cur, "signin", signin) is None
        assert store._shared_with(cur, "signin", signin, VIEWERS["a grantee"]) == (False, [])
        assert store._item_access(cur, "signin", signin, VIEWERS["the owner"]) == (None, False, False, False, False)
        assert store._item_access(cur, "signin", signin, VIEWERS["an admin"]) == (None, True, True, True, True)


def visible_names(kind, access):
    table = {"mfa": "mfaSecrets", "signin": "savedSignins"}[kind]
    with store._session() as cur:
        clause, params = store._visible_clause(kind, "s", access, store._tables(cur))
        cur.execute("SELECT s.name FROM {} s WHERE 1 = 1{} ORDER BY s.name".format(table, clause), params)
        return [name for (name,) in cur.fetchall()]


def test_listings_show_each_viewer_what_it_may_see(db):
    add_item(db, "mfa", "bobs", owner=BOB, shares=[("user", DANA)])
    add_item(db, "mfa", "readers", owner=FRANK, shares=[("role", READERS)])
    add_item(db, "mfa", "admins")
    add_item(db, "signin", "carols", owner=CAROL, shares=[("user", DANA)])  # another kind: not an MFA secret's rows
    assert visible_names("mfa", VIEWERS["the owner"]) == ["bobs"]
    assert visible_names("mfa", VIEWERS["a grantee"]) == ["bobs"]
    assert visible_names("mfa", VIEWERS["a grantee by role"]) == ["readers"]
    assert visible_names("mfa", VIEWERS["a stranger"]) == []
    assert visible_names("mfa", VIEWERS["a grantee without the permission"]) == []
    assert visible_names("mfa", VIEWERS["an admin"]) == visible_names("mfa", VIEWERS["the admin token"]) == [
        "admins", "bobs", "readers"]
    assert visible_names("signin", VIEWERS["a grantee"]) == ["carols"]
    assert store._visible_clause("mfa", "s", VIEWERS["an admin"], set()) == ("", [])
    # Nobody to share with: no user and no roles.
    assert visible_names("mfa", store.Access(None, (), TESTER)) == []

    run(db, "DROP TABLE itemShares")
    assert visible_names("mfa", VIEWERS["the owner"]) == ["bobs"]
    assert visible_names("mfa", VIEWERS["a grantee"]) == []
    run(db, "DROP TABLE itemOwners")
    assert visible_names("mfa", VIEWERS["the owner"]) == []
    assert visible_names("mfa", VIEWERS["an admin"]) == ["admins", "bobs", "readers"]


def test_what_a_listing_says_about_who_has_each_item(db):
    assert make_user(db, "Bob Stone", [permissions.TESTER]) == BOB
    bobs = add_item(db, "signin", "bobs", owner=BOB, shares=[("user", DANA), ("role", READERS)])
    admins = add_item(db, "signin", "admins")

    def rows_for(who, kind="signin"):
        with store._session() as cur:
            rows = store._ownership(cur, kind, [{"id": bobs, "name": "bobs"}, {"id": admins, "name": "admins"}],
                                    VIEWERS[who])
        return {row.pop("name"): row for row in rows}

    shares = [{"kind": "user", "id": DANA, "vouchedFor": None}, {"kind": "role", "id": READERS, "vouchedFor": None}]
    assert rows_for("the owner")["bobs"] == {"id": bobs, "ownerId": BOB, "access": "owner", "canManage": True,
                                             "usable": True, "shares": shares}
    assert rows_for("a grantee")["bobs"] == {"id": bobs, "ownerId": BOB, "access": "shared", "canManage": False,
                                             "usable": True, "shares": []}
    assert rows_for("a grantee by role")["bobs"]["access"] == "shared"
    assert rows_for("an admin") == {
        "bobs": {"id": bobs, "ownerId": BOB, "access": "all", "canManage": True, "usable": False, "shares": shares},
        "admins": {"id": admins, "ownerId": None, "access": "all", "canManage": True, "usable": True, "shares": []}}
    # MFA secrets have no stewards: checks get codes for whatever they see.
    assert "usable" not in rows_for("an admin", kind="mfa")["bobs"]
    with store._session() as cur:
        assert store._ownership(cur, "signin", [], VIEWERS["an admin"]) == []


def test_deleting_an_item_takes_its_owner_and_share_rows(db):
    gone = add_item(db, "mfa", "gone", owner=BOB, shares=[("user", DANA)])
    kept = add_item(db, "mfa", "kept", owner=BOB, shares=[("user", DANA)])
    namesake = add_item(db, "signin", "gone", owner=BOB, shares=[("user", DANA)])
    assert namesake == gone  # the same id in another table: its rows must stay
    with store._session() as cur:
        store._drop_item_rows(cur, "mfa", gone)
    assert run(db, "SELECT itemKind, itemId FROM itemOwners ORDER BY itemKind") == [("mfa", kept), ("signin", namesake)]
    assert run(db, "SELECT itemKind, itemId FROM itemShares ORDER BY itemKind") == [("mfa", kept), ("signin", namesake)]
    run(db, "DROP TABLE itemOwners")
    run(db, "DROP TABLE itemShares")
    with store._session() as cur:
        store._drop_item_rows(cur, "mfa", kept)  # nothing to delete, and no error


def test_a_client_token_says_who_made_it(db):
    legacy_id, legacy = add_client_token(db, "legacy")
    owned_id, owned = add_client_token(db, "dana-ci", owner=DANA)
    assert store.client_token(legacy) == {"id": legacy_id, "name": "legacy", "ownerId": None}
    assert store.client_token(owned) == {"id": owned_id, "name": "dana-ci", "ownerId": DANA}
    assert store.client_token_name(owned) == "dana-ci"
    assert run(db, "SELECT COUNT(*) FROM accessTokens WHERE lastUsedAt IS NOT NULL") == [(2,)]
    assert store.client_token("pwgc_wrong") is None and store.client_token("") is None
    assert store.client_token_name("pwgc_wrong") is None
    run(db, "UPDATE accessTokens SET revokedAt = ? WHERE id = ?", (ts(5), owned_id))
    assert store.client_token(owned) is None


# ---- store.py: the audit log -----------------------------------------------------------------


def test_a_change_and_its_audit_row_commit_together(db):
    with store._session() as cur:
        store._audit(cur, ADMIN_ACTOR, "token.revoked", "token", 7, "ci", {"why": "leaked"})
    assert audit_rows(db) == [(None, "Admin", "token", "10.0.0.7", "token.revoked", "token", 7, "ci", '{"why":"leaked"}')]
    occurred = store._parse_time(run(db, "SELECT occurredAt FROM auditLog")[0][0])
    assert abs((occurred - utcnow()).total_seconds()) < 5


def test_a_change_that_fails_takes_its_audit_row_with_it(db):
    with pytest.raises(RuntimeError):
        with store._session() as cur:
            cur.execute("INSERT INTO roles (name, permissions, createdAt) VALUES ('Auditors', 'audit.view', datetime('now'))")
            store._audit(cur, ADMIN_ACTOR, "role.created", "role", cur.lastrowid, "Auditors", {"permissions": ["audit.view"]})
            raise RuntimeError("the next statement failed")
    assert run(db, "SELECT COUNT(*) FROM roles") == [(0,)]
    assert audit_rows(db) == []


def test_nothing_is_recorded_without_an_actor(db):
    """A grid with no sign-in configured has nobody to name."""
    with store._session() as cur:
        store._audit(cur, None, "grid.slot_released", "slot", 1)
    store.audit_alone(None, "auth.breakglass")
    assert audit_rows(db) == []


def test_without_an_audit_log_table_changes_go_unrecorded_and_say_so_once(db, caplog):
    run(db, "DROP TABLE auditLog")
    with caplog.at_level(logging.WARNING, logger="ui.store"):
        for _ in range(2):
            with store._session() as cur:
                store._audit(cur, ADMIN_ACTOR, "token.revoked", "token", 7, "ci")
    assert caplog.text.count("the database has no auditLog table yet; changes aren't being recorded") == 1


def test_long_names_and_details_are_cut_to_fit(db):
    with store._session() as cur:
        store._audit(cur, store.Actor(3, "D" * 300, "oidc", "a" * 80, frozenset()), "user.groups_changed", "user", 3,
                     "N" * 300, {"added": ["g" * 5000], "removed": []})
    _, name, _, address, _, _, _, target_name, details = audit_rows(db)[0]
    assert (len(name), len(address), len(target_name), len(details)) == (255, 64, 255, store.AUDIT_DETAILS_LIMIT)
    assert details.startswith('{"added":["ggg')


def test_audit_rows_older_than_grid_audit_days_go_as_new_ones_come(db, monkeypatch):
    monkeypatch.setenv("GRID_AUDIT_DAYS", "30")
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        for days, action in ((40, "old"), (31, "older than 30 days"), (29, "recent")):
            con.execute("INSERT INTO auditLog (occurredAt, actorName, actorMethod, action) VALUES (?, 'Admin', 'token', ?)",
                        (ts(days * 86400), action))
    monkeypatch.setattr(store, "_AUDIT_PURGE_LIMIT", 1)  # by id, a few at a time
    with store._session() as cur:
        store._audit(cur, ADMIN_ACTOR, "token.deleted", "token", 7, "ci")
    assert [a for (a,) in run(db, "SELECT action FROM auditLog ORDER BY id")] == [
        "older than 30 days", "recent", "token.deleted"]
    with store._session() as cur:
        store._audit(cur, ADMIN_ACTOR, "token.deleted", "token", 8, "ci-2")
    assert [a for (a,) in run(db, "SELECT action FROM auditLog ORDER BY id")] == ["recent", "token.deleted", "token.deleted"]


@pytest.mark.parametrize("value, days", [
    (None, 365), ("30", 30), (" 90 ", 90), ("3650", 3650), ("29", 365), ("3651", 365), ("a year", 365), ("90.5", 365),
])
def test_how_long_the_audit_log_keeps(monkeypatch, value, days):
    if value is None:
        monkeypatch.delenv("GRID_AUDIT_DAYS", raising=False)
    else:
        monkeypatch.setenv("GRID_AUDIT_DAYS", value)
    assert store.audit_days() == days


def test_a_bad_grid_audit_days_is_logged_once(monkeypatch, caplog):
    monkeypatch.setenv("GRID_AUDIT_DAYS", "forever")
    with caplog.at_level(logging.WARNING, logger="ui.store"):
        assert store.audit_days() == store.audit_days() == 365
    assert caplog.text.count("GRID_AUDIT_DAYS is a number of days from 30 to 3650") == 1


def test_a_row_written_alone_never_fails_what_it_records(db, monkeypatch, caplog):
    store.audit_alone(ADMIN_ACTOR, "auth.breakglass")
    assert [row[4] for row in audit_rows(db)] == ["auth.breakglass"]
    monkeypatch.setattr(store, "SQLITE_PATH", db + ".missing")
    with caplog.at_level(logging.WARNING, logger="ui.store"):
        store.audit_alone(ADMIN_ACTOR, "mfa.code_viewed", "mfa", 3, "intranet")
    assert "couldn't record mfa.code_viewed in the audit log" in caplog.text


# ---- accounts.py: locks ----------------------------------------------------------------------


def test_the_access_lock_and_the_row_lock_in_one_transaction(db):
    """On sqlite both are the write lock (BEGIN IMMEDIATE): whichever comes second must not start it again."""
    with store._session() as cur:
        accounts._access_lock(cur)
        assert cur.connection.in_transaction
        cur.execute("SELECT id FROM users" + store._lock(cur))
        cur.execute("INSERT INTO roles (name, permissions, createdAt) VALUES ('Auditors', 'audit.view', datetime('now'))")
    assert run(db, "SELECT name FROM roles") == [("Auditors",)]


def test_the_row_lock_starts_the_transaction_when_nothing_has(db):
    with store._session() as cur:
        assert not cur.connection.in_transaction
        assert store._lock(cur) == ""
        assert cur.connection.in_transaction
        accounts._access_lock(cur)


def test_sharing_and_giving_things_away_take_both_locks_in_one_transaction(db, monkeypatch):
    """Each takes the access lock, then locks its item's row (or rows): on sqlite both are the write lock, taken
    once."""
    bob, dana = make_user(db, "Bob Stone", [permissions.TESTER]), make_user(db, "Dana Lee", [permissions.TESTER])
    secret = add_item(db, "mfa", "intranet", owner=bob)
    signin = add_item(db, "signin", "prod-admin", owner=bob, shares=[("user", dana)])
    later = add_item(db, "signin", "staging", owner=bob)
    token_id, _ = add_client_token(db, "legacy")
    admins = store.Access(None, (permissions.ADMIN,), permissions.ALL)
    begun, connect = [], sqlite3.connect

    def traced(*args, **kwargs):
        con, statements = connect(*args, **kwargs), []
        begun.append(statements)
        con.set_trace_callback(lambda statement: statements.append(statement) if statement.startswith("BEGIN") else None)
        return con

    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", traced)
        for change in (lambda: accounts.share_item("mfa", secret, "user", dana, admins, ADMIN_ACTOR),
                       lambda: accounts.unshare_item("mfa", secret, "user", dana, admins, ADMIN_ACTOR),
                       lambda: accounts.set_item_owner("mfa", secret, dana, admins, ADMIN_ACTOR),
                       lambda: accounts.set_item_owner("signin", signin, None, admins, ADMIN_ACTOR),
                       lambda: accounts.set_token_owner(token_id, dana, ADMIN_ACTOR),
                       lambda: accounts.move_items(bob, dana, ADMIN_ACTOR)):
            begun.clear()
            assert change() is not None
            assert begun == [["BEGIN IMMEDIATE"]]
    assert run(db, "SELECT itemKind, itemId, userId FROM itemOwners ORDER BY itemKind") == [
        ("mfa", secret, dana), ("signin", later, dana), ("token", token_id, dana)]
    assert [row[4] for row in audit_rows(db)] == ["mfa.shared", "mfa.unshared", "mfa.owner_changed",
                                                  "signin.owner_changed", "token.owner_set", "user.items_moved"]


# ---- accounts.py: who may do what ------------------------------------------------------------


def test_roles_given_on_the_admin_page(db, monkeypatch):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee", roles=[permissions.TESTER], email="dana@example.com")
    assert accounts.user_access(dana) == {
        "id": dana, "name": "Dana Lee", "email": "dana@example.com", "emailVerified": True, "subject": "dana.lee",
        "issuer": ISSUER, "groups": [], "groupsOverage": False, "disabled": False,
        "roles": [{"id": permissions.TESTER, "name": "Tester", "source": "manual"}],
        "permissions": {"grid.view", "tokens.own", "mfa.add", "mfa.use", "signins.add", "signins.use"}}


def test_roles_from_groups_match_exactly(db, monkeypatch):
    oidc_on(monkeypatch)
    add_mapping(db, "grid-operators", permissions.OPERATOR)
    add_mapping(db, "Grid-Viewers", permissions.VIEWER)  # the user is in grid-viewers: another group
    add_mapping(db, "*", permissions.VIEWER)
    dana = make_user(db, "Dana Lee", groups=["grid-operators", "grid-viewers"])
    access = accounts.user_access(dana)
    assert access["roles"] == [
        {"id": permissions.OPERATOR, "name": "Operator", "source": "group", "group": "grid-operators"},
        {"id": permissions.VIEWER, "name": "Viewer", "source": "everyone"}]
    assert access["permissions"] == permissions.expand(permissions.BUILTIN_ROLES[permissions.OPERATOR][2])
    assert access["groups"] == ["grid-operators", "grid-viewers"]


@pytest.mark.parametrize("admins, email, verified, admin", [
    ("dana@example.com", "Dana@Example.COM", True, True),
    ("dana@example.com", "dana@example.com", False, False),  # an email the provider didn't verify
    ("sub:dana.lee", None, False, True),
    ("sub:Dana.Lee", None, False, False),  # subjects match exactly
    ("ops@example.com, sub:dana.lee", None, False, True),
    ("", "dana@example.com", True, False),
])
def test_people_who_are_always_admins(db, monkeypatch, admins, email, verified, admin):
    oidc_on(monkeypatch)
    monkeypatch.setenv("GRID_OIDC_ADMINS", admins)
    access = accounts.user_access(make_user(db, "Dana Lee", email=email, verified=verified))
    assert (access["roles"] == [{"id": permissions.ADMIN, "name": "Admin", "source": "env"}]) is admin
    assert (access["permissions"] == permissions.ALL) is admin


def test_grid_oidc_admins_names_people_at_the_configured_provider(db, monkeypatch):
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:dana.lee")
    dana = make_user(db, "Dana Lee", issuer="https://old.example.com")
    oidc_on(monkeypatch)
    assert accounts.user_access(dana)["roles"] == []
    oidc_on(monkeypatch, issuer="https://old.example.com")
    assert accounts.user_access(dana)["permissions"] == permissions.ALL
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    assert accounts.user_access(dana)["roles"] == []


def test_a_custom_role_ignores_permissions_it_does_not_know(db, monkeypatch):
    oidc_on(monkeypatch)
    auditors = add_role(db, "Auditors", "audit.view made.up mfa.all")
    dana = make_user(db, "Dana Lee", roles=[auditors, 999, -9])  # roles that don't exist count for nothing
    access = accounts.user_access(dana)
    assert access["roles"] == [{"id": auditors, "name": "Auditors", "source": "manual"}]
    assert access["permissions"] == {"audit.view", "mfa.all", "mfa.add", "mfa.use"}


def test_a_role_from_several_places_is_listed_for_each(db, monkeypatch):
    oidc_on(monkeypatch)
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:dana.lee")
    auditors = add_role(db, "Auditors", "audit.view")
    add_mapping(db, "qa", permissions.TESTER)
    add_mapping(db, "ops", permissions.TESTER)
    add_mapping(db, "*", auditors)
    dana = make_user(db, "Dana Lee", roles=[auditors, permissions.TESTER], groups=["qa", "ops"])
    assert [(r["name"], r["source"], r.get("group")) for r in accounts.user_access(dana)["roles"]] == [
        ("Tester", "manual", None), ("Auditors", "manual", None), ("Tester", "group", "ops"), ("Tester", "group", "qa"),
        ("Auditors", "everyone", None), ("Admin", "env", None)]


def test_groups_the_provider_left_out_count_for_nothing(db, monkeypatch):
    oidc_on(monkeypatch)
    add_mapping(db, "ops", permissions.OPERATOR)
    dana = make_user(db, "Dana Lee", groups=["ops"])
    run(db, "UPDATE users SET groupsOverage = 1 WHERE id = ?", (dana,))
    access = accounts.user_access(dana)
    assert (access["groups"], access["groupsOverage"], access["roles"]) == ([], True, [])


def test_a_disabled_user_may_do_nothing_but_keeps_their_roles(db, monkeypatch):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee", roles=[permissions.ADMIN], disabled=True)
    access = accounts.user_access(dana)
    assert access["disabled"] is True and access["permissions"] == set()
    assert access["roles"] == [{"id": permissions.ADMIN, "name": "Admin", "source": "manual"}]  # for the Admin page
    # What they'd have if enabled again: only someone who has all of it may do that.
    assert accounts.user_access(dana, as_enabled=True)["permissions"] == permissions.ALL


def test_no_user_has_no_access(db):
    assert accounts.user_access(999) is None and accounts.user_access(None) is None
    run(db, "INSERT INTO users (issuer, subject, createdAt) VALUES (?, 'nameless', ?)", (ISSUER, ts(60)))
    nameless = run(db, "SELECT id FROM users")[0][0]
    assert accounts.user_access(nameless)["name"] == "user {}".format(nameless)
    dana = make_user(db, "Dana Lee", roles=[permissions.TESTER])
    for table in ("userRoles", "groupRoles", "roles"):
        run(db, "DROP TABLE " + table)
    assert accounts.user_access(dana)["roles"] == []
    run(db, "DROP TABLE users")
    assert accounts.user_access(dana) is None


def test_the_sign_in_tables_are_looked_for_until_they_are_found(db):
    assert accounts.tables_problem() is None  # _tables() names are lower case
    run(db, "DROP TABLE oidcLogins")
    assert accounts.tables_problem() is None  # tables don't disappear: this process doesn't ask again
    accounts.reset_caches()
    assert accounts.tables_problem() == accounts.NO_USER_TABLES


@pytest.mark.parametrize("table", ["users", "userSessions", "oidcLogins"])
def test_each_missing_sign_in_table_is_a_problem(db, table):
    run(db, "DROP TABLE " + table)
    assert accounts.tables_problem() == accounts.NO_USER_TABLES
    assert "nobody can sign in with single sign-on" in accounts.tables_problem()


def test_meeting_a_missing_table_looks_for_the_tables_again(db):
    assert accounts.tables_problem() is None
    run(db, "DROP TABLE userSessions")
    assert accounts.session_access("anything") is None
    assert accounts.tables_problem() == accounts.NO_USER_TABLES


def test_an_unreachable_database_is_no_table_problem(db, monkeypatch, tmp_path):
    """The request's next use of the database reports that; /api/session learns it from g."""
    monkeypatch.setattr(store, "SQLITE_PATH", db + ".missing")
    with grid_app.app.app_context():
        assert accounts.tables_problem() is None
        assert flask.g.db_unavailable is True
    assert accounts.tables_problem() is None  # outside a request too
    empty = str(tmp_path / "empty.db")
    sqlite3.connect(empty).close()
    monkeypatch.setattr(store, "SQLITE_PATH", empty)
    assert accounts.tables_problem() == accounts.NO_USER_TABLES  # the outage wasn't taken for "no problem"


# ---- accounts.py: signing in -----------------------------------------------------------------


def test_a_first_sign_in_makes_the_user_and_a_session(db, monkeypatch):
    oidc_on(monkeypatch)
    add_mapping(db, "ops", permissions.OPERATOR)
    value, access = accounts.sign_in(ISSUER, identity(groups=["ops"]), "10.0.0.7", "Mozilla/5.0 " + "x" * 300)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", value)
    assert (access["name"], access["subject"]) == ("Dana Lee", "dana.lee")
    assert access["roles"] == [{"id": permissions.OPERATOR, "name": "Operator", "source": "group", "group": "ops"}]
    assert run(db, "SELECT id, issuer, subject, email, emailVerified, name, idpGroups, groupsOverage, "
                   "lastSignInAt = lastSeenAt FROM users") == [
        (access["id"], ISSUER, "dana.lee", "dana@example.com", 1, "Dana Lee", '["ops"]', 0, 1)]
    [(token_hash, user_id, address, agent, created, expires, revoked)] = run(
        db, "SELECT tokenHash, userId, address, userAgent, createdAt, expiresAt, revokedAt FROM userSessions")
    assert (token_hash, user_id, address, len(agent), revoked) == (hashed(value), access["id"], "10.0.0.7", 255, None)
    # GRID_SESSION_HOURS (12 by default), by the database's clock.
    assert store._parse_time(expires) - store._parse_time(created) == datetime.timedelta(hours=12)
    # The groups are on the row that makes the user: there were none before, so no change of groups is recorded.
    assert [row[4] for row in audit_rows(db)] == ["user.created", "auth.signin"]
    assert {row[:4] for row in audit_rows(db)} == {(access["id"], "Dana Lee", "oidc", "10.0.0.7")}
    assert json.loads(audit_rows(db)[0][8]) == {"groups": ["ops"]}
    # Only the value's hash is kept.
    stored = run(db, "SELECT * FROM userSessions") + run(db, "SELECT * FROM auditLog") + run(db, "SELECT * FROM users")
    assert not any(value in str(v) for row in stored for v in row)


def test_a_session_lasts_grid_session_hours(db, monkeypatch):
    oidc_on(monkeypatch)
    monkeypatch.setenv("GRID_SESSION_HOURS", "2")
    accounts.sign_in(ISSUER, identity())
    created, expires = run(db, "SELECT createdAt, expiresAt FROM userSessions")[0]
    assert store._parse_time(expires) - store._parse_time(created) == datetime.timedelta(hours=2)


def test_signing_in_again_keeps_what_the_provider_says_now(db, monkeypatch):
    oidc_on(monkeypatch)
    first, access = accounts.sign_in(ISSUER, identity(groups=["ops", "qa"]))
    second, again = accounts.sign_in(ISSUER, identity(name="Dana Smith", email="dana@corp.example", verified=False,
                                                      groups=["qa", "dev"]))
    accounts.sign_in(ISSUER, identity(name="Dana Smith", groups=["dev", "qa"]))  # the same groups: nothing to record
    assert again["id"] == access["id"] and second != first
    assert run(db, "SELECT name, email, emailVerified, idpGroups FROM users") == [
        ("Dana Smith", "dana@example.com", 1, '["dev", "qa"]')]
    assert [json.loads(row[8]) for row in audit_rows(db) if row[4] in ("user.created", "user.groups_changed")] == [
        {"groups": ["ops", "qa"]}, {"added": ["dev"], "removed": ["ops"]}]
    assert [row[4] for row in audit_rows(db)].count("user.created") == 1
    # The first session still works: it just isn't in the browser any more.
    assert accounts.session_access(first)["id"] == access["id"]
    assert accounts.session_access(first)["session_id"] != accounts.session_access(second)["session_id"]


def test_a_first_sign_in_records_its_groups_once(db, monkeypatch):
    """A user without groups is made with no details. One made at a first sign-in that didn't go through (another
    browser's, still on its way) has no groups yet, and the groups user.created has may not be these: the next
    sign-in records all of its own as added."""
    oidc_on(monkeypatch)
    accounts.sign_in(ISSUER, identity(subject="erin", name="Erin"))
    run(db, "INSERT INTO users (issuer, subject, name, createdAt) VALUES (?, 'lee', 'Lee', ?)", (ISSUER, ts(60)))
    accounts.sign_in(ISSUER, identity(subject="lee", name="Lee", groups=["qa"]))
    accounts.sign_in(ISSUER, identity(subject="lee", name="Lee", groups=["qa", "ops"]))
    assert [(row[1], row[4], row[8] and json.loads(row[8])) for row in audit_rows(db)] == [
        ("Erin", "user.created", None), ("Erin", "auth.signin", None),
        ("Lee", "user.groups_changed", {"added": ["qa"], "removed": []}), ("Lee", "auth.signin", None),
        ("Lee", "user.groups_changed", {"added": ["ops"], "removed": []}), ("Lee", "auth.signin", None)]


def test_a_first_sign_in_that_failed_leaves_the_groups_of_the_next_in_the_audit_log(db, monkeypatch):
    """The first try made the user, and user.created has its groups, but its session failed: the next sign-in,
    with other groups, records them, though nothing was kept of the first try's on the user."""
    oidc_on(monkeypatch)
    start = accounts._start_session

    def failing(*args):
        raise store.DatabaseUnavailable("The database went away.")

    monkeypatch.setattr(accounts, "_start_session", failing)
    with pytest.raises(store.DatabaseUnavailable):
        accounts.sign_in(ISSUER, identity(groups=["ops"]))
    assert run(db, "SELECT idpGroups FROM users") == [(None,)]
    monkeypatch.setattr(accounts, "_start_session", start)
    accounts.sign_in(ISSUER, identity(groups=["qa"]))
    assert [(row[4], row[8] and json.loads(row[8])) for row in audit_rows(db)] == [
        ("user.created", {"groups": ["ops"]}), ("user.groups_changed", {"added": ["qa"], "removed": []}),
        ("auth.signin", None)]


def test_the_same_subject_elsewhere_is_someone_else(db, monkeypatch):
    oidc_on(monkeypatch)
    _, dana = accounts.sign_in(ISSUER, identity(subject="abc"))
    _, upper = accounts.sign_in(ISSUER, identity(subject="ABC"))
    _, other = accounts.sign_in("https://other.example.com", identity(subject="abc"))
    assert len({dana["id"], upper["id"], other["id"]}) == 3


def test_groups_the_provider_left_out_are_not_kept(db, monkeypatch):
    oidc_on(monkeypatch)
    accounts.sign_in(ISSUER, identity(groups=["ops"]))
    accounts.sign_in(ISSUER, identity(groups=["ops", "qa"], overage=True))
    assert run(db, "SELECT idpGroups, groupsOverage FROM users") == [("[]", 1)]


def test_500_groups_of_255_characters_are_kept(db, monkeypatch):
    oidc_on(monkeypatch)
    groups = ["{:03d}".format(i) + "é" * 252 for i in range(500)]
    _, access = accounts.sign_in(ISSUER, identity(groups=groups + ["one too many"]))
    assert access["groups"] == groups
    # The audit rows list the first few, as whole JSON within the limit: those of the new user, then those added
    # and those removed.
    details = [row[8] for row in audit_rows(db) if row[4] == "user.created"][0]
    listed = json.loads(details)["groups"]
    assert len(details) <= store.AUDIT_DETAILS_LIMIT and listed and listed == groups[:len(listed)]
    others = [group.replace("é", "e") for group in groups]
    accounts.sign_in(ISSUER, identity(groups=others))
    details = [row[8] for row in audit_rows(db) if row[4] == "user.groups_changed"][0]
    changed = json.loads(details)
    assert len(details) <= store.AUDIT_DETAILS_LIMIT and changed["added"] and changed["removed"]
    assert changed["added"] == others[:len(changed["added"])] and changed["removed"] == groups[:len(changed["removed"])]


def test_a_user_without_a_name_is_shown_by_their_id(db, monkeypatch):
    oidc_on(monkeypatch)
    _, access = accounts.sign_in(ISSUER, identity(name=None))
    assert access["name"] == "user {}".format(access["id"])
    assert {row[1] for row in audit_rows(db)} == {"user {}".format(access["id"])}


def test_a_disabled_user_cannot_sign_in(db, monkeypatch):
    oidc_on(monkeypatch)
    make_user(db, "Dana Lee", disabled=True)
    with pytest.raises(accounts.UserDisabled):
        accounts.sign_in(ISSUER, identity(name="Dana Smith", groups=["ops"]), "10.0.0.7")
    assert run(db, "SELECT COUNT(*) FROM userSessions") == [(0,)]
    assert run(db, "SELECT name, idpGroups FROM users") == [("Dana Lee", "[]")]  # nothing the provider said is kept
    assert [(row[4], row[3], json.loads(row[8])) for row in audit_rows(db)] == [
        ("auth.signin_refused", "10.0.0.7", {"reason": "disabled"})]


def test_sessions_that_ended_a_week_ago_go_as_new_ones_start(db, monkeypatch):
    oidc_on(monkeypatch)
    erin = make_user(db, "Erin")
    gone = [add_session(db, erin, revoked_ago=8 * 86400), add_session(db, erin, expires_in=-8 * 86400)]
    kept = [add_session(db, erin, revoked_ago=6 * 86400), add_session(db, erin, expires_in=-6 * 86400),
            add_session(db, erin)]
    monkeypatch.setattr(accounts, "_PURGE_LIMIT", 1)  # by id, a few at a time
    accounts.sign_in(ISSUER, identity())
    accounts.sign_in(ISSUER, identity())
    hashes = {h for (h,) in run(db, "SELECT tokenHash FROM userSessions")}
    assert hashes.isdisjoint(store.token_hash(v) for v in gone)
    assert {store.token_hash(v) for v in kept} <= hashes and len(hashes) == len(kept) + 2


# ---- accounts.py: sign-ins on their way through the identity provider -------------------------


def test_a_sign_in_on_its_way_is_taken_once_by_the_browser_that_started_it(db):
    accounts.start_login(hashed("browser"), hashed("state"), "the-nonce", "the-verifier", "#/mfa")
    assert run(db, "SELECT stateHash, browserHash, nonce, codeVerifier, returnTo FROM oidcLogins") == [
        (hashed("state"), hashed("browser"), "the-nonce", "the-verifier", "#/mfa")]
    # Another browser with the state can't finish it, nor spoil it for the one that started it.
    assert accounts.take_login(hashed("state"), hashed("another browser")) is None
    assert accounts.take_login(hashed("state"), hashed("browser")) == {
        "nonce": "the-nonce", "verifier": "the-verifier", "return_to": "#/mfa", "others": False}
    assert accounts.take_login(hashed("state"), hashed("browser")) is None  # used up
    assert accounts.take_login(hashed("made up"), hashed("browser")) is None
    assert run(db, "SELECT COUNT(*) FROM oidcLogins") == [(0,)]


def test_other_tabs_signing_in_from_the_same_browser(db):
    for state in ("one", "two"):
        accounts.start_login(hashed("browser"), hashed(state), "n", "v", None)
    accounts.start_login(hashed("another browser"), hashed("three"), "n", "v", None)
    assert accounts.take_login(hashed("one"), hashed("browser"))["others"] is True
    assert accounts.take_login(hashed("two"), hashed("browser"))["others"] is False


def test_a_sign_in_older_than_ten_minutes_is_gone(db):
    for state in ("old", "older"):
        accounts.start_login(hashed("browser"), hashed(state), "n", "v", None)
    run(db, "UPDATE oidcLogins SET createdAt = ?", (ts(accounts.LOGIN_TTL_S + 1),))
    assert accounts.take_login(hashed("old"), hashed("browser")) is None
    assert run(db, "SELECT stateHash FROM oidcLogins") == [(hashed("older"),)]  # taken all the same
    accounts.start_login(hashed("browser"), hashed("new"), "n", "v", None)  # and a new one clears the rest
    assert run(db, "SELECT stateHash FROM oidcLogins") == [(hashed("new"),)]


def test_at_most_max_pending_logins_at_once(db, monkeypatch):
    monkeypatch.setattr(accounts, "MAX_PENDING_LOGINS", 3)
    for i in range(3):
        accounts.start_login(hashed("browser {}".format(i)), hashed("state {}".format(i)), "n", "v", None)
    with pytest.raises(accounts.LoginsBusy):
        accounts.start_login(hashed("browser 9"), hashed("state 9"), "n", "v", None)
    run(db, "UPDATE oidcLogins SET createdAt = ? WHERE stateHash = ?", (ts(accounts.LOGIN_TTL_S + 1), hashed("state 0")))
    accounts.start_login(hashed("browser 9"), hashed("state 9"), "n", "v", None)  # an old one made room
    assert run(db, "SELECT COUNT(*) FROM oidcLogins") == [(3,)]


def test_signing_in_without_the_tables(db, monkeypatch):
    oidc_on(monkeypatch)
    run(db, "DROP TABLE oidcLogins")
    with pytest.raises(accounts.UsersUnavailable) as e:
        accounts.start_login(hashed("browser"), hashed("state"), "n", "v", None)
    assert str(e.value) == accounts.NO_USER_TABLES
    with pytest.raises(accounts.UsersUnavailable):
        accounts.take_login(hashed("state"), hashed("browser"))
    run(db, "DROP TABLE users")
    with pytest.raises(accounts.UsersUnavailable):
        accounts.sign_in(ISSUER, identity())


# ---- accounts.py: sessions -------------------------------------------------------------------


@pytest.mark.parametrize("session, user, reason", [
    ({}, {}, None),
    ({"revoked_ago": 5}, {}, "revoked"),
    ({}, {"disabled": True}, "disabled"),
    ({"revoked_ago": 5}, {"disabled": True}, "disabled"),  # disabling someone ends their sessions too
    ({"expires_in": -1}, {}, "expired"),
    ({"seen_ago": 121 * 60}, {}, "idle"),  # GRID_SESSION_IDLE_MINUTES: 120 by default
    ({"seen_ago": 119 * 60}, {}, None),
])
def test_why_a_session_stops_working(db, monkeypatch, session, user, reason):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee", roles=[permissions.TESTER], **user)
    found = accounts.session_access(add_session(db, dana, **session))
    if reason is None:
        session_id = run(db, "SELECT id FROM userSessions")[0][0]
        assert found == dict(accounts.user_access(dana), session_id=session_id)
    else:
        assert found == oidc.Ended(reason)


def test_a_cookie_that_is_no_session(db):
    assert accounts.session_access("made-up") == oidc.Ended("unknown")


def test_sessions_started_through_another_issuer_stop_when_grid_oidc_issuer_changes(db, monkeypatch):
    """Nobody signs in through the old provider any more, so what was signed in through it stops at once (a grid
    moved off a tenant someone broke into), even an admin's, and signs nobody out either. Moving back brings them
    back, as they hadn't ended."""
    oidc_on(monkeypatch)
    robin = make_user(db, "Robin Hale", roles=[permissions.ADMIN])
    value = add_session(db, robin)
    client = grid_app.app.test_client()
    client.set_cookie(oidc.cookie_names().session, value)
    assert client.get("/api/session").get_json()["admin"] is True
    oidc_on(monkeypatch, issuer="https://login.example.org")
    assert accounts.session_access(value) == oidc.Ended("unknown")
    r = client.get("/api/session")
    assert (r.get_json()["signedIn"], r.get_json()["admin"]) == (False, False)
    assert client.get("/api/tokens").status_code == 403
    assert accounts.end_session(value, everywhere=True) == 0
    assert run(db, "SELECT COUNT(*) FROM userSessions WHERE revokedAt IS NOT NULL") == [(0,)]
    oidc_on(monkeypatch)
    assert accounts.session_access(value)["id"] == robin


def test_idle_sessions_end_after_grid_session_idle_minutes(db, monkeypatch):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee")
    value = add_session(db, dana, seen_ago=6 * 60)
    monkeypatch.setenv("GRID_SESSION_IDLE_MINUTES", "5")
    assert accounts.session_access(value) == oidc.Ended("idle")
    monkeypatch.setenv("GRID_SESSION_IDLE_MINUTES", "0")  # never
    assert accounts.session_access(value)["id"] == dana


def test_last_seen_is_written_at_most_once_a_minute(db, monkeypatch):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee")
    value = add_session(db, dana, seen_ago=30)

    def seen():
        return run(db, "SELECT s.lastSeenAt, u.lastSeenAt FROM userSessions s JOIN users u ON u.id = s.userId")[0]

    before = seen()
    accounts.session_access(value)
    assert seen() == before  # 30 s ago: not yet
    run(db, "UPDATE userSessions SET lastSeenAt = ?", (ts(61),))
    accounts.session_access(value)
    session_seen, user_seen = (store._parse_time(v) for v in seen())
    assert abs((session_seen - utcnow()).total_seconds()) < 5 and user_seen == session_seen


def test_signing_out_ends_the_session_or_every_session(db, monkeypatch):
    oidc_on(monkeypatch)
    dana, erin = make_user(db, "Dana Lee"), make_user(db, "Erin")
    here, there, laptop, elsewhere = (add_session(db, dana), add_session(db, dana), add_session(db, dana),
                                      add_session(db, erin))
    assert accounts.end_session(here, address="10.0.0.7") == 1
    assert accounts.session_access(here) == oidc.Ended("revoked")
    assert accounts.session_access(there)["id"] == dana
    assert accounts.end_session(here) == 0  # once
    assert accounts.end_session(there, everywhere=True, address="10.0.0.8") == 2
    assert accounts.session_access(laptop) == oidc.Ended("revoked")
    assert accounts.session_access(elsewhere)["id"] == erin  # someone else's
    assert run(db, "SELECT DISTINCT revokedBy FROM userSessions WHERE userId = ?", (dana,)) == [("signed out",)]
    assert [(row[0], row[1], row[3], row[4], json.loads(row[8])) for row in audit_rows(db)] == [
        (dana, "Dana Lee", "10.0.0.7", "auth.signout", {"everywhere": False}),
        (dana, "Dana Lee", "10.0.0.8", "auth.signout", {"everywhere": True})]
    assert accounts.end_session("") == accounts.end_session("made-up") == 0


def test_a_session_that_stopped_working_signs_nobody_out(db, monkeypatch):
    """A cookie that can't sign anyone in any more (expired, idle, or its user disabled) can't end its user's
    other sessions either, nor write an audit row in their name."""
    oidc_on(monkeypatch)
    dana, erin = make_user(db, "Dana Lee"), make_user(db, "Erin", disabled=True)
    laptop = add_session(db, dana)
    for dead in (add_session(db, dana, expires_in=-1), add_session(db, dana, seen_ago=121 * 60),
                 add_session(db, erin)):
        assert accounts.end_session(dead, everywhere=True, address="10.0.0.7") == 0
    assert accounts.session_access(laptop)["id"] == dana
    assert run(db, "SELECT COUNT(*) FROM userSessions WHERE revokedAt IS NOT NULL") == [(0,)]
    assert audit_rows(db) == []


def test_who_checks_using_a_client_token_act_as(db, monkeypatch):
    oidc_on(monkeypatch)
    dana = make_user(db, "Dana Lee", roles=[permissions.TESTER])
    erin = make_user(db, "Erin", roles=[permissions.TESTER], disabled=True)
    legacy_id, legacy = add_client_token(db, "legacy")
    _, danas = add_client_token(db, "dana-ci", owner=dana)
    _, erins = add_client_token(db, "erin-ci", owner=erin)
    gone_id, gones = add_client_token(db, "gone-ci", owner=999)
    assert accounts.client_token_access(legacy) == {"id": legacy_id, "name": "legacy", "ownerId": None, "owner": None}
    found = accounts.client_token_access(danas)
    assert (found["name"], found["ownerId"], found["owner"]["name"], found["owner"]["permissions"]) == (
        "dana-ci", dana, "Dana Lee", TESTER)
    assert accounts.client_token_access(erins)["owner"]["disabled"] is True
    assert accounts.client_token_access(erins)["owner"]["permissions"] == set()
    # An owner nobody can find leaves no owner, but the token still has an owner id: never an admin's token.
    assert accounts.client_token_access(gones) == {"id": gone_id, "name": "gone-ci", "ownerId": 999, "owner": None}
    run(db, "DROP TABLE users")
    assert accounts.client_token_access(danas)["ownerId"] == dana
    assert accounts.client_token_access(danas)["owner"] is None
    assert accounts.client_token_access("pwgc_wrong") is None


# ---- accounts.py: changing people's access -------------------------------------------------------


# What People leads hold: managing people, with an Operator's permissions besides (and nothing of everyone's).
LEADS = "users.manage grid.operate tokens.own tokens.agents mfa.add signins.add"


@pytest.fixture
def staff(db, monkeypatch):
    """Robin is an admin. Lee leads people with less than an admin has; Dana is a Tester, Ann has the Auditors
    role, and Pat is an admin who is disabled."""
    oidc_on(monkeypatch)
    leads, auditors = add_role(db, "People leads", LEADS), add_role(db, "Auditors", "audit.view")
    return types.SimpleNamespace(
        path=db, leads=leads, auditors=auditors,
        robin=make_user(db, "Robin Hale", [permissions.ADMIN], email="robin@example.com"),
        lee=make_user(db, "Lee Park", [leads]),
        dana=make_user(db, "Dana Lee", [permissions.TESTER], email="dana@example.com"),
        ann=make_user(db, "Ann Auditor", [auditors]),
        pat=make_user(db, "Pat Quinn", [permissions.ADMIN], disabled=True))


def actor_of(user_id):
    """A person as the actor of a change, with what the gate would have let through: what they may do now."""
    found = accounts.user_access(user_id)
    return store.Actor(user_id, found["name"], "oidc", "10.0.0.9", found["permissions"])


def refused(call):
    """The refusal call raises: (its type, its message, its rule)."""
    with pytest.raises((store.NotPermitted, accounts.InvalidRole, accounts.LastAdmin, accounts.HeldElsewhere)) as e:
        call()
    return type(e.value), str(e.value), getattr(e.value, "rule", None)


def manual_roles(path):
    return run(path, "SELECT userId, roleId FROM userRoles ORDER BY userId, roleId")


def add_token(path, name, kind, owner, revoked=False):
    """A managed token of either kind that owner made. Returns its id."""
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        cur = con.execute("INSERT INTO accessTokens (name, kind, tokenHash, prefix, createdAt, revokedAt) "
                          "VALUES (?, ?, ?, ?, ?, ?)", (name, kind, hashed(name), name[:12], ts(3600),
                                                        ts(60) if revoked else None))
        con.execute("INSERT INTO itemOwners (itemKind, itemId, userId, createdAt) VALUES ('token', ?, ?, ?)",
                    (cur.lastrowid, owner, ts(3600)))
        return cur.lastrowid


ADMIN_LABELS = "Every access token, Every MFA secret, Every saved sign-in, Read the audit log"


def test_whoever_changes_a_role_must_hold_everything_it_brings(staff):
    """Giving or taking it, making, changing (what it brought and what it will bring), deleting or mapping it: nobody
    hands out more than they have, so only admins give Admin."""
    lee = actor_of(staff.lee)
    helpers = add_role(staff.path, "Helpers", "grid.view")
    add_mapping(staff.path, "admins", permissions.ADMIN)
    ((mapping,),), before = run(staff.path, "SELECT id FROM groupRoles"), manual_roles(staff.path)
    beyond = "You can't give or change the role “{}”: it has permissions you don't have ({})."
    for call, name, labels in [
            (lambda: accounts.give_role(staff.dana, permissions.ADMIN, lee), "Admin", ADMIN_LABELS),
            (lambda: accounts.give_role(staff.dana, staff.auditors, lee), "Auditors", "Read the audit log"),
            (lambda: accounts.take_role(staff.dana, staff.auditors, lee), "Auditors", "Read the audit log"),
            (lambda: accounts.create_role("Approvers", None, ["users.view", "audit.view"], lee), "Approvers",
             "Read the audit log"),
            (lambda: accounts.update_role(helpers, {"permissions": ["grid.view", "mfa.all"]}, lee), "Helpers",
             "Every MFA secret"),
            (lambda: accounts.update_role(staff.auditors, {"permissions": []}, lee), "Auditors", "Read the audit log"),
            (lambda: accounts.delete_role(staff.auditors, lee), "Auditors", "Read the audit log"),
            (lambda: accounts.add_mapping("auditors", staff.auditors, None, lee), "Auditors", "Read the audit log"),
            (lambda: accounts.delete_mapping(mapping, lee), "Admin", ADMIN_LABELS)]:
        assert refused(call) == (store.NotPermitted, beyond.format(name, labels), "role")
    assert manual_roles(staff.path) == before
    assert run(staff.path, "SELECT name, permissions FROM roles ORDER BY id") == [
        ("People leads", LEADS), ("Auditors", "audit.view"), ("Helpers", "grid.view")]
    assert run(staff.path, "SELECT id FROM groupRoles") == [(mapping,)]
    assert audit_rows(staff.path) == []
    # What they hold themselves, they may hand out.
    assert [r["name"] for r in accounts.give_role(staff.dana, permissions.OPERATOR, lee)["user"]["roles"]] == [
        "Operator", "Tester"]
    assert accounts.create_role("Watchers", None, ["grid.view"], lee)["canEdit"] is True
    assert accounts.update_role(helpers, {"permissions": ["grid.operate"]}, lee)["effective"] == ["grid.operate",
                                                                                                 "grid.view"]


def test_whoever_changes_someones_access_must_hold_everything_they_have(staff):
    """Disabling or enabling them, giving or taking their roles, ending their sessions, revoking their tokens or
    moving what they own. A disabled admin is still an admin here: only an admin enables one again."""
    lee = actor_of(staff.lee)
    stewards = add_role(staff.path, "Stewards", "users.manage mfa.all signins.all")
    sam = actor_of(make_user(staff.path, "Sam Steward", [stewards]))
    outranked = "You can't change {}'s access: they have permissions you don't have."
    for call, name in [(lambda: accounts.disable_user(staff.robin, lee), "Robin Hale"),
                       (lambda: accounts.enable_user(staff.pat, lee), "Pat Quinn"),
                       (lambda: accounts.give_role(staff.robin, permissions.VIEWER, lee), "Robin Hale"),
                       (lambda: accounts.take_role(staff.robin, permissions.ADMIN, lee), "Robin Hale"),
                       (lambda: accounts.end_user_sessions(staff.robin, lee), "Robin Hale"),
                       (lambda: accounts.revoke_user_tokens(staff.ann, lee, client_tokens=True), "Ann Auditor"),
                       (lambda: accounts.move_items(staff.robin, None, sam), "Robin Hale")]:
        assert refused(call) == (store.NotPermitted, outranked.format(name), "target")
    assert run(staff.path, "SELECT name FROM users WHERE disabledAt IS NOT NULL") == [("Pat Quinn",)]
    # Someone they outrank they may change, and an admin may change anyone but themselves.
    assert accounts.disable_user(staff.dana, lee)["user"]["disabled"] is True
    assert accounts.enable_user(staff.dana, lee)["user"]["disabled"] is False
    assert accounts.enable_user(staff.pat, actor_of(staff.robin))["user"]["admin"] is True


def test_nobody_changes_their_own_access(staff):
    """Not their roles, not by disabling themselves, not by revoking their own tokens here (the Access page does
    that); they may end their own sessions, though. The admin token has no user, so it may act on anyone."""
    robin = actor_of(staff.robin)
    add_session(staff.path, staff.robin)
    own = "You can't change your own access. Ask another admin."
    for call in (lambda: accounts.give_role(staff.robin, permissions.VIEWER, robin),
                 lambda: accounts.take_role(staff.robin, permissions.ADMIN, robin),
                 lambda: accounts.disable_user(staff.robin, robin),
                 lambda: accounts.revoke_user_tokens(staff.robin, robin, client_tokens=True)):
        assert refused(call) == (store.NotPermitted, own, "self")
    assert accounts.end_user_sessions(staff.robin, robin) == 1
    assert accounts.give_role(staff.robin, permissions.VIEWER, ADMIN_ACTOR)["user"]["roles"] == [
        {"id": permissions.ADMIN, "name": "Admin", "source": "manual"},
        {"id": permissions.VIEWER, "name": "Viewer", "source": "manual"}]
    assert accounts.enable_user(staff.pat, ADMIN_ACTOR)["user"]["disabled"] is False
    assert accounts.disable_user(staff.robin, ADMIN_ACTOR)["user"]["disabled"] is True


NO_NAME = "A role's name is 1 to 64 characters, without control or invisible characters."


def test_built_in_roles_never_change_and_nothing_else_takes_their_names(staff):
    robin = actor_of(staff.robin)
    built_in = "Built-in roles can't be changed. Duplicate one to make your own."
    assert refused(lambda: accounts.update_role(permissions.OPERATOR, {"name": "Ops"}, robin)) == (
        accounts.InvalidRole, built_in, "built-in")
    assert refused(lambda: accounts.delete_role(permissions.VIEWER, robin)) == (accounts.InvalidRole, built_in,
                                                                               "built-in")
    # Full-width letters are the same letters.
    for name in ("admin", "VIEWER", "Admins", " tester ", "Ａｄｍｉｎ"):
        assert refused(lambda: accounts.create_role(name, None, [], robin)) == (
            accounts.InvalidRole, "“{}” is reserved: choose another name.".format(name.strip()), "built-in")
    assert refused(lambda: accounts.update_role(staff.auditors, {"name": "Operator"}, robin))[1] == (
        "“Operator” is reserved: choose another name.")
    # A name with an invisible character in it would look like a reserved one, or say something else than it reads.
    for name in ("Admin​", "Admins⁠", "‮Admin", "Op­erator"):
        assert refused(lambda: accounts.create_role(name, None, [], robin)) == (accounts.InvalidRole, NO_NAME, None)
        assert refused(lambda: accounts.update_role(staff.auditors, {"name": name}, robin))[1] == NO_NAME
    assert run(staff.path, "SELECT name FROM roles ORDER BY id") == [("People leads",), ("Auditors",)]


def test_role_names_are_unique_ignoring_case(staff, monkeypatch):
    """A duplicate the unique key finds (MySQL's folds accents too) is refused as one found taken."""
    robin = actor_of(staff.robin)
    qa = accounts.create_role("QA leads", "Look after the checks.", ["mfa.add"], robin)["id"]
    taken = "There is already a role called {!r}."
    assert refused(lambda: accounts.create_role("qa LEADS", None, [], robin)) == (
        accounts.InvalidRole, taken.format("qa LEADS"), None)
    assert refused(lambda: accounts.create_role("ＱＡ ｌｅａｄｓ", None, [], robin))[1] == taken.format("ＱＡ ｌｅａｄｓ")
    assert refused(lambda: accounts.update_role(staff.auditors, {"name": "Qa Leads"}, robin))[1] == taken.format(
        "Qa Leads")
    assert accounts.update_role(qa, {"name": "QA Leads"}, robin)["name"] == "QA Leads"  # its own name, recased
    monkeypatch.setattr(accounts, "_name_free", lambda cur, name, role_id=None: None)
    assert refused(lambda: accounts.create_role("QA Leads", None, [], robin)) == (
        accounts.InvalidRole, taken.format("QA Leads"), None)
    assert refused(lambda: accounts.update_role(staff.auditors, {"name": "QA Leads"}, robin))[1] == taken.format(
        "QA Leads")
    assert run(staff.path, "SELECT name FROM roles ORDER BY id") == [("People leads",), ("Auditors",), ("QA Leads",)]


@pytest.mark.parametrize("fields, message", [
    ({"name": ""}, NO_NAME),
    ({"name": "x" * 65}, NO_NAME),
    ({"name": "Line\nbreak"}, NO_NAME),
    ({"name": "Lone \ud800"}, NO_NAME),
    ({"name": 7}, NO_NAME),
    ({"description": "x" * 256}, "A role's description is at most 255 characters, without control characters."),
    ({"permissions": "grid.view"}, "Send permissions as a list of permission ids."),
    ({"permissions": ["grid.view", "grid.fly"]}, "There is no permission 'grid.fly'."),
])
def test_what_a_role_may_be(staff, fields, message):
    robin = actor_of(staff.robin)
    role = dict({"name": "Night shift", "description": None, "permissions": ["grid.view"]}, **fields)
    assert refused(lambda: accounts.create_role(role["name"], role["description"], role["permissions"], robin)) == (
        accounts.InvalidRole, message, None)
    assert refused(lambda: accounts.update_role(staff.auditors, fields, robin))[1] == message


def test_everyone_who_signs_in_never_gets_a_role_that_manages_people(staff):
    """Neither by mapping '*' to such a role, nor by changing a role '*' maps to so that it manages people."""
    robin = actor_of(staff.robin)
    everyone = "Everyone who signs in can't be given a role that manages people."
    assert refused(lambda: accounts.add_mapping("*", staff.leads, None, robin)) == (accounts.InvalidRole, everyone,
                                                                                  "everyone")
    assert refused(lambda: accounts.add_mapping("*", permissions.ADMIN, None, robin))[1] == everyone
    helpers = add_role(staff.path, "Helpers", "grid.view")
    accounts.add_mapping("*", helpers, "Everyone", robin)
    assert refused(lambda: accounts.update_role(helpers, {"permissions": ["users.manage"]}, robin)) == (
        accounts.InvalidRole, everyone, "everyone")
    assert accounts.update_role(helpers, {"permissions": ["users.view"]}, robin)["permissions"] == ["users.view"]
    # A group's members may get one: they are people the identity provider vouches for.
    assert accounts.add_mapping("leads", staff.leads, None, robin)[1] is True


def test_a_role_is_handed_out_only_by_someone_who_sees_what_is_shared_with_it(staff):
    """A role brings what is shared with it as well as its permissions. Lee holds every permission the DBA role
    has, but can't see the secret shared with it: mapping a group he is in to it would give it to him, without
    changing his own user, and giving it to Dana would give it to her. Once he sees it, he may; an admin sees
    everything."""
    lee, robin = actor_of(staff.lee), actor_of(staff.robin)
    run(staff.path, "UPDATE users SET idpGroups = ? WHERE id = ?", (json.dumps(["helpdesk"]), staff.lee))
    dba = add_role(staff.path, "DBA", "mfa.use")
    secret = add_item(staff.path, "mfa", "prod-db", shares=[("role", dba)])
    beyond = "You can't give the role “DBA”: MFA secrets you can't see are shared with it."
    for change in (lambda: accounts.add_mapping("helpdesk", dba, None, lee),
                   lambda: accounts.add_mapping("*", dba, None, lee),
                   lambda: accounts.give_role(staff.dana, dba, lee)):
        assert refused(change) == (store.NotPermitted, beyond, "role")
    grid = grid_app.app.test_client()
    sign_in_as(grid, staff.lee)
    r = grid.post("/api/admin/groups", json={"group": "helpdesk", "roleId": dba})
    assert (r.status_code, r.get_json()) == (403, {"error": beyond, "forbidden": True})
    assert [(row[4], json.loads(row[8])) for row in audit_rows(staff.path)] == [
        ("access.denied", {"route": "POST /api/admin/groups", "rule": "role", "permission": "mfa.all"})]
    assert run(staff.path, "SELECT COUNT(*) FROM groupRoles") == run(
        staff.path, "SELECT COUNT(*) FROM userRoles WHERE roleId = ?", (dba,)) == [(0,)]
    run(staff.path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) "
                    "VALUES ('mfa', ?, 'user', ?, ?)", (secret, staff.lee, ts(60)))
    assert accounts.add_mapping("helpdesk", dba, None, lee)[1] is True
    # Saved sign-ins the same.
    readers = add_role(staff.path, "Sign-in readers", "signins.use")
    add_item(staff.path, "signin", "prod-admin", owner=staff.robin, shares=[("role", readers)])
    assert refused(lambda: accounts.give_role(staff.dana, readers, lee)) == (
        store.NotPermitted, "You can't give the role “Sign-in readers”: saved sign-ins you can't see are shared with "
                            "it.", "role")
    assert accounts.give_role(staff.dana, readers, robin)["user"]["roles"][-1]["name"] == "Sign-in readers"


def test_someone_demoted_while_changing_access_is_refused(staff, monkeypatch):
    """The gate let the request through, but the actor lost the role it needs before the change ran: what they may
    do is worked out again under the access lock, so nothing changes, and the refusal is recorded."""
    grid = grid_app.app.test_client()
    sign_in_as(grid, staff.lee)
    lock = accounts._access_lock

    def demoted_first(cur):
        run(staff.path, "DELETE FROM userRoles WHERE userId = ?", (staff.lee,))
        lock(cur)

    monkeypatch.setattr(accounts, "_access_lock", demoted_first)
    r = grid.post("/api/admin/users/{}/roles/add".format(staff.dana), json={"role": permissions.VIEWER})
    assert (r.status_code, r.get_json()) == (403, {
        "error": "Your roles no longer include “Manage people and roles”.", "forbidden": True})
    assert manual_roles(staff.path) == [(staff.robin, permissions.ADMIN), (staff.dana, permissions.TESTER),
                                        (staff.ann, staff.auditors), (staff.pat, permissions.ADMIN)]
    assert [(row[4], json.loads(row[8])) for row in audit_rows(staff.path)] == [
        ("access.denied", {"route": "POST /api/admin/users/{}/roles/add".format(staff.dana), "rule": "actor",
                           "permission": "users.manage"})]


def test_the_last_admin_who_can_sign_in_stays_whatever_the_change(staff):
    """Disabling them, taking their role, deleting the mapping that gives it, or changing or deleting the role:
    each is refused while they are the only enabled admin, the admin token's changes too. Pat, disabled, doesn't
    count."""
    last = (accounts.LastAdmin, "That would leave no admin who can sign in. Make someone else an admin first.", None)
    assert refused(lambda: accounts.disable_user(staff.robin, ADMIN_ACTOR)) == last
    assert refused(lambda: accounts.take_role(staff.robin, permissions.ADMIN, ADMIN_ACTOR)) == last
    # An admin through a group mapping.
    run(staff.path, "UPDATE users SET idpGroups = '[\"grid-admins\"]' WHERE id = ?", (staff.robin,))
    accounts.add_mapping("grid-admins", permissions.ADMIN, None, ADMIN_ACTOR)
    accounts.take_role(staff.robin, permissions.ADMIN, ADMIN_ACTOR)
    (mapping,) = run(staff.path, "SELECT id FROM groupRoles")
    assert refused(lambda: accounts.delete_mapping(mapping[0], ADMIN_ACTOR)) == last
    # An admin through a custom role that holds everything.
    everything = add_role(staff.path, "Everything", permissions.unparse(permissions.ALL))
    accounts.give_role(staff.robin, everything, ADMIN_ACTOR)
    assert accounts.delete_mapping(mapping[0], ADMIN_ACTOR) is True
    assert refused(lambda: accounts.update_role(everything, {"permissions": ["users.manage"]}, ADMIN_ACTOR)) == last
    assert refused(lambda: accounts.delete_role(everything, ADMIN_ACTOR)) == last
    assert accounts.user_access(staff.robin)["permissions"] == permissions.ALL
    # With another admin, each goes through.
    accounts.give_role(staff.dana, permissions.ADMIN, ADMIN_ACTOR)
    assert accounts.update_role(everything, {"permissions": ["users.manage"]}, ADMIN_ACTOR)["name"] == "Everything"
    assert accounts.delete_role(everything, ADMIN_ACTOR) == {"users": 1, "mappings": 0, "shares": 0}
    assert accounts.disable_user(staff.robin, ADMIN_ACTOR)["user"]["disabled"] is True


def test_a_grid_without_an_admin_yet_can_be_set_up(staff):
    """The rule only keeps the last admin: with none (Robin and Pat are disabled), nothing it covers is refused."""
    run(staff.path, "UPDATE users SET disabledAt = ? WHERE id = ?", (ts(60), staff.robin))
    accounts.update_role(staff.leads, {"permissions": ["users.manage"]}, ADMIN_ACTOR)
    accounts.take_role(staff.dana, permissions.TESTER, ADMIN_ACTOR)
    accounts.disable_user(staff.ann, ADMIN_ACTOR)
    assert accounts.give_role(staff.dana, permissions.ADMIN, ADMIN_ACTOR)["user"]["admin"] is True
    assert refused(lambda: accounts.take_role(staff.dana, permissions.ADMIN, ADMIN_ACTOR))[0] is accounts.LastAdmin


def test_admins_another_issuer_made_are_no_admins_who_can_sign_in(staff, monkeypatch):
    """GRID_OIDC_ISSUER changed since Olga signed in: she is a user of the old provider, and can't sign in any more.
    She doesn't keep Robin from being the last admin; with no provider configured, nobody can sign in at all."""
    olga = make_user(staff.path, "Olga Old", [permissions.ADMIN], issuer="https://old-login.example.com")
    assert refused(lambda: accounts.disable_user(staff.robin, ADMIN_ACTOR))[0] is accounts.LastAdmin
    assert refused(lambda: accounts.take_role(staff.robin, permissions.ADMIN, ADMIN_ACTOR))[0] is accounts.LastAdmin
    monkeypatch.setenv("GRID_OIDC_ISSUER", "https://old-login.example.com")
    assert accounts.take_role(staff.robin, permissions.ADMIN, ADMIN_ACTOR)["user"]["admin"] is False
    assert refused(lambda: accounts.disable_user(olga, ADMIN_ACTOR))[0] is accounts.LastAdmin
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    assert accounts.disable_user(olga, ADMIN_ACTOR)["user"]["disabled"] is True


def test_a_role_from_elsewhere_is_taken_away_there(staff, monkeypatch):
    """Taking a role someone has through a group, the mapping for everyone or GRID_OIDC_ADMINS changes nothing on
    their user, so it is refused saying where it comes from. One they also have by hand goes; one they don't
    have at all changes nothing."""
    robin = actor_of(staff.robin)
    monkeypatch.setenv("GRID_OIDC_ADMINS", "dana@example.com")
    run(staff.path, "UPDATE users SET idpGroups = '[\"grid-operators\"]' WHERE id = ?", (staff.dana,))
    accounts.add_mapping("grid-operators", permissions.OPERATOR, None, robin)
    accounts.add_mapping("*", permissions.VIEWER, None, robin)
    for role_id, message in [
            (permissions.OPERATOR, "Dana Lee has Operator through the group 'grid-operators'. Remove the mapping, or "
                                   "their membership at the identity provider, instead."),
            (permissions.VIEWER, "Dana Lee has Viewer through the mapping for everyone who signs in. Remove that "
                                 "mapping instead."),
            (permissions.ADMIN, "Dana Lee has Admin through GRID_OIDC_ADMINS. Take them out of it instead.")]:
        assert refused(lambda: accounts.take_role(staff.dana, role_id, ADMIN_ACTOR)) == (
            accounts.HeldElsewhere, message, None)
    accounts.give_role(staff.dana, permissions.OPERATOR, ADMIN_ACTOR)
    assert accounts.take_role(staff.dana, permissions.OPERATOR, ADMIN_ACTOR)["user"]["roles"][1] == {
        "id": permissions.OPERATOR, "name": "Operator", "source": "group", "group": "grid-operators"}
    assert accounts.take_role(staff.ann, permissions.OPERATOR, ADMIN_ACTOR)["activeClientTokens"] == 0
    assert [row[4] for row in audit_rows(staff.path)] == ["group.mapped", "group.mapped", "user.role_added",
                                                         "user.role_removed"]


def test_disabling_someone_ends_their_sessions_and_revokes_the_tokens_chosen(staff):
    """By id, revokedAt set and the rows kept: the Access page still shows them, and the hub still requires a
    token. What they own stays theirs."""
    robin = actor_of(staff.robin)
    first, second = add_session(staff.path, staff.dana), add_session(staff.path, staff.dana)
    add_session(staff.path, staff.dana, revoked_ago=600)
    ci, ci2 = (add_token(staff.path, name, "client", staff.dana) for name in ("dana-ci", "dana-ci2"))
    host = add_token(staff.path, "dana-host", "agent", staff.dana)
    old = add_token(staff.path, "dana-old", "client", staff.dana, revoked=True)
    robins = add_token(staff.path, "robin-ci", "client", staff.robin)
    answer = accounts.disable_user(staff.dana, robin, client_tokens=True)
    assert (answer["sessionsEnded"], answer["tokensRevoked"]) == (2, {"client": 2, "agent": 0})
    assert (answer["user"]["disabled"], answer["user"]["sessions"], answer["user"]["permissions"]) == (True, 0, [])
    assert run(staff.path, "SELECT disabledBy FROM users WHERE id = ?", (staff.dana,)) == [("Robin Hale",)]
    assert run(staff.path, "SELECT COUNT(*) FROM userSessions WHERE userId = ? AND revokedAt IS NULL",
               (staff.dana,)) == [(0,)]
    assert run(staff.path, "SELECT revokedBy FROM userSessions WHERE userId = ? ORDER BY id", (staff.dana,))[:2] == [
        ("Robin Hale",), ("Robin Hale",)]
    assert accounts.session_access(first) == oidc.Ended("disabled") == accounts.session_access(second)
    revoked = dict(run(staff.path, "SELECT id, revokedAt IS NOT NULL FROM accessTokens"))
    assert revoked == {ci: 1, ci2: 1, host: 0, old: 1, robins: 0}
    assert [row[4:] for row in audit_rows(staff.path)] == [
        ("user.disabled", "user", staff.dana, "Dana Lee", '{"sessions":2,"clientTokens":2,"agentTokens":0}')]
    # Disabling again ends nothing more, but may still revoke what is left.
    answer = accounts.disable_user(staff.dana, robin, agent_tokens=True)
    assert (answer["sessionsEnded"], answer["tokensRevoked"]) == (0, {"client": 0, "agent": 1})
    assert accounts.disable_user(staff.dana, robin)["tokensRevoked"] == {"client": 0, "agent": 0}
    assert len(audit_rows(staff.path)) == 2


def test_moving_what_someone_owns(staff):
    """Their MFA secrets keep their shares; their saved sign-ins lose theirs, any sign-in under way and what was
    saved. Tokens stay with whoever made them. Moving needs every MFA secret and every saved sign-in."""
    robin = actor_of(staff.robin)
    secret = add_item(staff.path, "mfa", "intranet", owner=staff.dana, shares=[("user", staff.ann)])
    signin = add_item(staff.path, "signin", "prod-admin", owner=staff.dana, shares=[("role", permissions.TESTER)])
    run(staff.path, "UPDATE savedSignins SET leaseHash = 'x', leaseUntil = ?, leaseHolder = 'a check', "
                    "stateCipher = 'x', version = 3, expiresAt = ? WHERE id = ?", (ts(-60), ts(-3600), signin))
    idle = add_item(staff.path, "signin", "staging", owner=staff.dana)
    token = add_token(staff.path, "dana-ci", "client", staff.dana)
    add_item(staff.path, "mfa", "robins", owner=staff.robin)
    assert accounts.move_items(staff.dana, staff.ann, robin) == {
        "moved": {"mfa": 1, "signins": 2}, "sharesRemoved": 1, "leasesCleared": 1, "statesDropped": 1}
    assert run(staff.path, "SELECT itemKind, itemId, userId FROM itemOwners ORDER BY itemKind, itemId") == [
        ("mfa", secret, staff.ann), ("mfa", secret + 1, staff.robin), ("signin", signin, staff.ann),
        ("signin", idle, staff.ann), ("token", token, staff.dana)]
    assert run(staff.path, "SELECT itemKind, granteeKind, granteeId FROM itemShares") == [("mfa", "user", staff.ann)]
    assert run(staff.path, "SELECT leaseHash, leaseUntil, leaseHolder, stateCipher, version, expiresAt FROM savedSignins "
                           "WHERE id = ?", (signin,)) == [(None, None, None, None, 4, None)]
    assert [row[4:] for row in audit_rows(staff.path)] == [
        ("user.items_moved", "user", staff.dana, "Dana Lee",
         '{"to":{"id":%d,"name":"Ann Auditor"},"mfa":1,"signins":2,"statesDropped":1}' % staff.ann)]
    assert accounts.move_items(staff.ann, None, robin)["moved"] == {"mfa": 1, "signins": 2}
    assert run(staff.path, "SELECT itemKind, userId FROM itemOwners ORDER BY itemKind, itemId") == [
        ("mfa", staff.robin), ("token", staff.dana)]
    assert accounts.move_items(staff.ann, staff.dana, robin)["moved"] == {"mfa": 0, "signins": 0}
    assert len(audit_rows(staff.path)) == 2
    assert refused(lambda: accounts.move_items(staff.robin, staff.pat, ADMIN_ACTOR))[1] == (
        "User {} is disabled.".format(staff.pat))
    assert refused(lambda: accounts.move_items(staff.dana, None, actor_of(staff.lee))) == (
        store.NotPermitted, "Your roles no longer include “Every MFA secret”.", "actor")


def test_a_change_to_access_and_its_audit_row_commit_or_roll_back_together(staff, monkeypatch):
    """A failure after the audit row is written takes the change with it, and the row too."""
    recorded = store._audit

    def then_failed(cur, *args, **kwargs):
        recorded(cur, *args, **kwargs)
        raise store.DatabaseUnavailable("the connection dropped")

    monkeypatch.setattr(store, "_audit", then_failed)
    with pytest.raises(store.DatabaseUnavailable):
        accounts.give_role(staff.dana, permissions.OPERATOR, ADMIN_ACTOR)
    assert manual_roles(staff.path) == [(staff.robin, permissions.ADMIN), (staff.lee, staff.leads),
                                        (staff.dana, permissions.TESTER), (staff.ann, staff.auditors),
                                        (staff.pat, permissions.ADMIN)]
    assert audit_rows(staff.path) == []


# ---- auth.py: principals and the gate ----------------------------------------------------------


# A person signed in with the Tester role, as auth sees them.
TESS = auth.Principal("Tess Tester", "oidc", 4, TESTER, (permissions.TESTER,))


def test_whoever_may_do_everything_holds_the_admin_role_for_shares():
    """What is shared with the Admin role reaches the admin token, ownerless client tokens and full admins
    through a custom role alike, and nobody with less."""
    assert auth.access(auth.Principal("Admin", "token")) == store.Access(None, (permissions.ADMIN,), permissions.ALL)
    full = auth.Principal("Dana Lee", "oidc", 3, permissions.ALL, (READERS,))
    assert auth.access(full).role_ids == (READERS, permissions.ADMIN)
    assert auth.access(full._replace(role_ids=(permissions.ADMIN, READERS))).role_ids == (permissions.ADMIN, READERS)
    assert auth.access(TESS) == store.Access(4, (permissions.TESTER,), TESTER)


def test_what_a_principal_may_do():
    """None is a grid with no sign-in configured: grid actions are open, nothing else is."""
    assert auth.can(None, "grid.view") and auth.can(None, "grid.operate")
    assert not auth.can(None, "mfa.use")
    assert auth.can(TESS, "mfa.use") and not auth.can(TESS, "users.view")
    assert auth.can(auth.Principal("Admin", "token"), "users.manage")


def test_who_makes_a_change_for_the_audit_log():
    with grid_app.app.test_request_context(environ_base={"REMOTE_ADDR": "10.0.0.8"}):
        assert auth.actor(TESS) == store.Actor(4, "Tess Tester", "oidc", "10.0.0.8", TESTER)
        assert auth.actor(auth.Principal("Admin", "token")) == store.Actor(None, "Admin", "token", "10.0.0.8",
                                                                          permissions.ALL)
        assert auth.actor(None) is None


def test_a_route_needs_any_one_of_its_permissions(db, monkeypatch):
    """Holding one of them is enough. Anyone signed in with none is forbidden, which the page doesn't answer by
    asking for sign-in again; no permissions at all means anyone signed in; and without any sign-in configured,
    the route is open, as grid actions always were, unless it refuses then."""
    oidc_on(monkeypatch)
    app = flask.Flask(__name__)

    @app.get("/either")
    @auth.permission_required("users.manage", "mfa.use")
    def either():
        return flask.jsonify(principal=getattr(flask.g.principal, "name", None),
                             actor=getattr(flask.g.actor, "name", None))

    @app.get("/anyone")
    @auth.permission_required()
    def anyone():
        return flask.jsonify(principal=flask.g.principal.name)

    @app.get("/strict")
    @auth.permission_required("mfa.use", refuse_when_unconfigured=True, unconfigured_error="Set it up first.")
    def strict():
        return flask.jsonify(ok=True)

    client = app.test_client()
    r = client.get("/either")
    assert r.status_code == 403 and r.get_json() == {"error": "Sign in to continue.", "token_required": True}
    sign_in_as(client, make_user(db, "Tess Tester", [permissions.TESTER]))
    assert client.get("/either").get_json() == {"principal": "Tess Tester", "actor": "Tess Tester"}
    sign_in_as(client, make_user(db, "Vic Viewer", [permissions.VIEWER]))
    r = client.get("/either")
    assert r.status_code == 403 and r.get_json() == {
        "error": "Your roles don't include “Manage people and roles”, which this needs. Ask an admin for a role "
                 "that does.", "forbidden": True, "permission": "users.manage"}
    sign_in_as(client, make_user(db, "Nora Nobody"))
    assert client.get("/anyone").get_json() == {"principal": "Nora Nobody"}
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    assert client.get("/either").get_json() == {"principal": None, "actor": None}
    r = client.get("/strict")
    assert r.status_code == 403 and r.get_json() == {"error": "Set it up first.", "admin_token_unset": True}


# ---- auth.py: checks act as their client token's owner -----------------------------------------


def test_a_check_acts_as_the_owner_of_its_client_token(db, monkeypatch):
    """Whether a token acts as a person goes by its owner id. One an admin made, and the bootstrap token, act as an
    admin under the names checks have always seen."""
    oidc_on(monkeypatch)
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "b" * auth.MIN_BOOTSTRAP_TOKEN_LENGTH)
    dana = make_user(db, "Dana Lee", roles=[permissions.TESTER], email="dana@example.com")
    _, legacy = add_client_token(db, "legacy")
    _, danas = add_client_token(db, "dana-ci", owner=dana)

    def principal(token):
        with grid_app.app.test_request_context(headers={"Authorization": "Bearer " + token}):
            return auth.check_principal(accounts.client_token_access)

    assert principal("b" * auth.MIN_BOOTSTRAP_TOKEN_LENGTH) == auth.Principal("GATEWAY_CLIENT_TOKEN", "client-bootstrap")
    assert principal(legacy) == auth.Principal("client token 'legacy'", "client-token")
    assert auth.is_admin(principal(legacy))
    assert principal(danas) == auth.Principal("client token 'dana-ci' (Dana Lee)", "client-token", dana, TESTER,
                                              (permissions.TESTER,), "dana@example.com")
    run(db, "UPDATE userRoles SET roleId = ? WHERE userId = ?", (permissions.VIEWER, dana))
    assert principal(danas).permissions == {"grid.view"}  # at each use: a demotion counts at once


def test_a_token_whose_owner_is_gone_or_disabled_gets_nothing(db, monkeypatch):
    """Never an admin's access, and not a guess either: the token is right, its owner just can't use it."""
    oidc_on(monkeypatch)
    _, erins = add_client_token(db, "erin-ci", owner=make_user(db, "Erin", disabled=True))
    _, gones = add_client_token(db, "gone-ci", owner=999)
    _, danas = add_client_token(db, "dana-ci", owner=make_user(db, "Dana Lee", roles=[permissions.TESTER]))
    client = grid_app.app.test_client()

    def ask(token):
        return client.post("/api/mfa/code", json={"name": "nope"}, headers={"Authorization": "Bearer " + token})

    r = ask(erins)
    assert r.status_code == 403 and r.get_json() == {
        "error": "This client token belongs to Erin, whose account is disabled, so it gets nothing from the Grid UI. "
                 "An admin can enable them, or revoke the token.", "owner_disabled": True}
    r = ask(gones)
    assert r.status_code == 403 and r.get_json() == {
        "error": "This client token belongs to a person this grid no longer knows, so it gets nothing from the Grid "
                 "UI. An admin can revoke it.", "owner_disabled": True}
    assert ask(danas).status_code == 404  # on to the secret, which there isn't
    assert not auth._failures
    run(db, "DROP TABLE users")
    assert ask(danas).get_json()["owner_disabled"] is True


# ---- app.py: refusals --------------------------------------------------------------------------


def test_a_refusal_by_a_rule_is_recorded_alone_at_most_once_a_minute(db, monkeypatch):
    """The change it refused rolled back, so its row is written in a transaction of its own; someone trying again
    and again writes one a minute. A refusal no rule made (seen, but not theirs to change) isn't recorded."""
    oidc_on(monkeypatch)
    tess = make_user(db, "Tess Tester", [permissions.TESTER])
    grid = grid_app.app.test_client()
    sign_in_as(grid, tess)
    clock = [1000.0]
    monkeypatch.setattr(grid_app, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    refusals = [store.NotPermitted("Your roles no longer include “Add MFA secrets”.", rule="actor", permission="mfa.add")]

    def refused(*args):
        raise refusals[0]

    monkeypatch.setattr(store, "delete_mfa", refused)
    for secret_id in (7, 7, 8):
        r = grid.post("/api/mfa/{}/delete".format(secret_id), json={})
        assert (r.status_code, r.get_json()) == (403, {"error": "Your roles no longer include “Add MFA secrets”.",
                                                       "forbidden": True})
        assert r.headers["Cache-Control"] == "no-store"
    denied = (tess, "Tess Tester", "oidc", "127.0.0.1", "access.denied", None, None, None)
    assert audit_rows(db) == [denied + ('{"route":"POST /api/mfa/7/delete","rule":"actor","permission":"mfa.add"}',)]
    clock[0] += 60
    grid.post("/api/mfa/8/delete", json={})
    assert audit_rows(db)[1] == denied + ('{"route":"POST /api/mfa/8/delete","rule":"actor","permission":"mfa.add"}',)
    refusals[0] = store.NotPermitted("Only its owner or an admin can delete it.")
    clock[0] += 60
    assert grid.post("/api/mfa/8/delete", json={}).get_json() == {"error": "Only its owner or an admin can delete it.",
                                                                  "forbidden": True}
    assert len(audit_rows(db)) == 2



# ---- the API: reading the grid -----------------------------------------------------------------

READS = ("/status", "/api/status", "/api/events")
SIGN_IN_TO_CONTINUE = {"error": "Sign in to continue.", "token_required": True}
NO_GRID_VIEW = {"error": "Your roles don't include “See the grid”, which this needs. Ask an admin for a role that "
                         "does.", "forbidden": True, "permission": "grid.view"}


def bearer(token):
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def seeded(db, monkeypatch):
    """The db fixture's database with the demo grid in it, and a recorder of the app's own, without the background
    thread: the tests drive it."""
    with contextlib.closing(sqlite3.connect(db)) as con, con:
        demo.seed(con)
    monkeypatch.setattr(store, "FAIL_THRESHOLD", 3)
    monkeypatch.setattr(grid_app, "RECORDER", recorder.Recorder(store.snapshot, store.db_identity, interval_s=0))
    return db


def test_where_reading_needs_sign_in_only_those_who_may_see_the_grid_read_it(db, monkeypatch):
    """With single sign-on the overview, activity and sessions need sign-in, unless GRID_UI_READS=open: a person
    with "See the grid", the admin token, or a client token whose owner has it (a script watching the grid). Anyone
    else is refused as the gate refuses, and a wrong client token is a guess, as a check's is."""
    oidc_on(monkeypatch)
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    tess = make_user(db, "Tess Tester", [permissions.TESTER])
    nora = make_user(db, "Nora Nobody")
    _, tess_ci = add_client_token(db, "tess-ci", owner=tess)
    _, nora_ci = add_client_token(db, "nora-ci", owner=nora)
    _, legacy = add_client_token(db, "legacy")
    anyone = grid_app.app.test_client()
    for url in READS:
        r = anyone.get(url)
        assert (r.status_code, r.get_json()) == (403, SIGN_IN_TO_CONTINUE), url
        for headers in (ADMIN, bearer(tess_ci), bearer(legacy), bearer(BOOTSTRAP)):
            assert anyone.get(url, headers=headers).status_code == 200, (url, headers)
        r = anyone.get(url, headers=bearer(nora_ci))
        assert (r.status_code, r.get_json()) == (403, NO_GRID_VIEW), url
    person = grid_app.app.test_client()
    sign_in_as(person, nora)
    for url in READS:
        r = person.get(url)
        assert (r.status_code, r.get_json()) == (403, NO_GRID_VIEW), url
    sign_in_as(person, tess)
    assert all(person.get(url).status_code == 200 for url in READS)
    assert not auth._failures
    r = anyone.get("/api/events", headers=bearer("pwgc_wrong"))
    assert r.status_code == 401 and r.get_json()["client_token_required"] is True
    assert len(auth._failures["127.0.0.1"]) == 1
    monkeypatch.setenv("GRID_UI_READS", "open")
    assert all(grid_app.app.test_client().get(url).status_code == 200 for url in READS)


def test_the_pages_status_poll_is_refused_with_data_and_nothing_of_the_grid(db, monkeypatch):
    """?soft=1 answers a refusal 200 with why ("signedout" or "forbidden") and the refusal's words, so the browser
    logs no HTTP error at every poll. Only the status poll is soft, and a client token's own refusals never are."""
    oidc_on(monkeypatch)
    anyone = grid_app.app.test_client()
    r = anyone.get("/api/status?soft=1&events=0")
    assert (r.status_code, r.get_json()) == (200, {"refused": "signedout", "error": "Sign in to continue."})
    assert r.headers["Cache-Control"] == "no-store"
    assert anyone.get("/status?soft=1").get_json() == {"refused": "signedout", "error": "Sign in to continue."}
    assert anyone.get("/api/events?soft=1").status_code == 403
    nora = make_user(db, "Nora Nobody")
    person = grid_app.app.test_client()
    sign_in_as(person, nora)
    r = person.get("/api/status?soft=1&events=0")
    assert (r.status_code, r.get_json()) == (200, {"refused": "forbidden", "error": NO_GRID_VIEW["error"]})
    person.set_cookie(oidc.cookie_names().session, add_session(db, nora, expires_in=-60))
    r = person.get("/api/status?soft=1")
    assert (r.status_code, r.get_json()) == (200, {"refused": "signedout", "error": auth.ENDED["expired"]})
    assert any(c.startswith("grid-session=;") for c in r.headers.getlist("Set-Cookie"))  # the browser forgets it
    assert anyone.get("/api/status?soft=1", headers=bearer("pwgc_wrong")).status_code == 401
    _, gones = add_client_token(db, "gone-ci", owner=999)
    r = anyone.get("/api/status?soft=1", headers=bearer(gones))
    assert r.status_code == 403 and r.get_json()["owner_disabled"] is True
    # Where only the admin token signs in, in its words.
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    monkeypatch.setenv("GRID_UI_READS", "signed-in")
    assert anyone.get("/api/status?soft=1").get_json() == {"refused": "signedout",
                                                           "error": "The admin token is missing or wrong."}


def test_an_answer_says_when_the_admin_token_sent_with_it_was_wrong(db, monkeypatch):
    """A wrong admin token beside a session cookie, or on a read that's open to anyone, doesn't stop the request,
    but it counts as a guess: the answer says so, and a page still holding a token from before GRID_ADMIN_TOKEN
    changed forgets it rather than lock its address out. A token that wasn't compared says nothing."""
    oidc_on(monkeypatch)
    refused = lambda r: r.headers.get("X-Grid-Token-Refused")
    person = grid_app.app.test_client()
    sign_in_as(person, make_user(db, "Tess Tester", [permissions.TESTER]))
    r = person.get("/api/tokens", headers={"X-Grid-Token": "old"})
    assert r.status_code == 200 and refused(r) == "1"
    for headers in ({}, {"X-Grid-Token": ""}, ADMIN):
        r = person.get("/api/tokens", headers=headers)
        assert r.status_code == 200 and refused(r) is None, headers
    monkeypatch.setenv("GRID_UI_READS", "open")
    r = grid_app.app.test_client().get("/api/status?soft=1&events=0", headers={"X-Grid-Token": "old"})
    assert r.status_code == 200 and "nodes" in r.get_json() and refused(r) == "1"
    while len(auth._failures["127.0.0.1"]) < auth.FAILURE_LIMIT:
        person.get("/api/tokens", headers={"X-Grid-Token": "old"})
    r = person.get("/api/tokens", headers={"X-Grid-Token": "old"})
    assert r.status_code == 429 and refused(r) is None  # past the limit, nothing is compared


def test_an_outage_in_the_read_gate_is_the_status_polls_outage_answer(db, monkeypatch, tmp_path):
    """The session can't be looked at: the soft poll gets what an outage in the snapshot gets, and nothing of the
    grid, with the session looked up once; anything else gets the 503 it always has."""
    oidc_on(monkeypatch)
    provider = next(p for p in auth.PROVIDERS if isinstance(p, auth.OidcSessionProvider))
    lookups, lookup = [], provider.lookup

    def counted(token):
        lookups.append(token)
        return lookup(token)

    monkeypatch.setattr(provider, "lookup", counted)
    person = grid_app.app.test_client()
    sign_in_as(person, make_user(db, "Vic Viewer", [permissions.VIEWER]))
    monkeypatch.setattr(store, "SQLITE_PATH", str(tmp_path))  # a directory: every query fails
    r = person.get("/api/status?soft=1&events=0")
    body = r.get_json()
    assert r.status_code == 200 and set(body) == {"error", "unavailable"} and body["unavailable"] is True
    assert body["error"] and str(tmp_path) not in body["error"]
    assert r.headers["Cache-Control"] == "no-store"
    assert len(lookups) == 1  # the error's words didn't look again
    for url in READS:
        assert person.get(url).status_code == 503, url


def test_a_bad_grid_ui_reads_means_reading_needs_sign_in(db, monkeypatch, caplog):
    """It says so once in the log. Without any sign-in configured, nobody could sign in to read, so reading stays
    open whatever GRID_UI_READS says."""
    monkeypatch.setenv("GRID_UI_READS", "Sometimes")
    anyone = grid_app.app.test_client()
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            r = anyone.get("/api/status")
            assert (r.status_code, r.get_json()) == (403, {"error": "The admin token is missing or wrong.",
                                                           "token_required": True})
        assert anyone.get("/api/session").get_json()["readsOpen"] is False
    assert caplog.text.count("GRID_UI_READS is open or signed-in, not 'sometimes'") == 1
    assert anyone.get("/api/status", headers=ADMIN).status_code == 200
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    assert anyone.get("/api/status").status_code == 200
    assert anyone.get("/api/session").get_json()["readsOpen"] is True


def admin_actions(client, headers=None):
    """{event type: by} of the actions the recorder holds, as this reader gets them from /api/status and from
    /api/events (which must agree)."""
    seen = [{e["type"]: e["by"] for e in client.get(url, headers=headers or {}).get_json()["events"]
             if e["kind"] == "admin"}
            for url in ("/api/status?events=0", "/api/events")]
    assert seen[0] == seen[1]
    return seen[0]


def test_an_action_is_recorded_as_taken_by_the_display_name(seeded, monkeypatch):
    """What the page shows a person by: not their subject or their email address."""
    oidc_on(monkeypatch)
    person = grid_app.app.test_client()
    sign_in_as(person, make_user(seeded, "Dana Lee", [permissions.OPERATOR], email="dana@example.com",
                                 subject="00u1ab2cd3"))
    nodes = person.get("/api/status").get_json()["nodes"]
    failing = next(s for n in nodes for s in n["slots"] if s["fails"] >= 3)
    r = person.post("/api/slots/{}/reset-failures".format(failing["id"]), json={})
    assert r.status_code == 200 and len(r.get_json()["events"]) == 1
    assert admin_actions(person) == {"reset": "Dana Lee"}


def test_where_reading_is_open_only_people_signed_in_see_who_took_an_action(seeded, monkeypatch):
    """With single sign-on, who took an action is a person's name, which a reader who hasn't signed in gets as "".
    Reading still asks who the reader is, which keeps their session from going idle while the page is open.
    Without single sign-on nothing changes: the names are "Admin" or "", for everyone."""
    path = seeded
    admin = grid_app.app.test_client()
    nodes = admin.get("/api/status").get_json()["nodes"]
    failing = next(s for n in nodes for s in n["slots"] if s["fails"] >= 3)
    offline = next(n for n in nodes if n["kind"] == "gateway" and not n["online"])
    assert admin.post("/api/slots/{}/reset-failures".format(failing["id"]), json={}, headers=ADMIN).status_code == 200
    assert admin_actions(grid_app.app.test_client()) == {"reset": "Admin"}

    oidc_on(monkeypatch)
    monkeypatch.setenv("GRID_UI_READS", "open")
    dana = grid_app.app.test_client()
    sign_in_as(dana, make_user(path, "Dana Lee", [permissions.OPERATOR]))
    assert dana.post("/api/nodes/forget", json={"host": offline["host"]}).get_json()["forgotten"] is True
    named = {"reset": "Admin", "node-left": "Dana Lee"}
    assert admin_actions(dana) == named
    anyone = grid_app.app.test_client()
    assert admin_actions(anyone) == {"reset": "", "node-left": ""}
    _, legacy = add_client_token(path, "legacy")
    # A client token is for checks, and for reading where that needs sign-in: here it says nothing.
    assert admin_actions(anyone, bearer(legacy)) == {"reset": "", "node-left": ""}
    assert admin_actions(admin, ADMIN) == named

    nora = grid_app.app.test_client()
    value = add_session(path, make_user(path, "Nora Nobody"), seen_ago=600)
    nora.set_cookie(oidc.cookie_names().session, value)
    assert admin_actions(nora) == named  # signed in, though she may do nothing
    ((seen,),) = run(path, "SELECT lastSeenAt FROM userSessions WHERE tokenHash = ?", (store.token_hash(value),))
    assert abs((store._parse_time(seen) - utcnow()).total_seconds()) < 5


# ---- the API: the permission catalogue ---------------------------------------------------------


def test_the_permission_catalogue_and_the_built_in_roles(db, monkeypatch):
    """For the role editor and for saying what a refusal was missing. It says nothing about anyone, so anyone may
    have it, signed in or not."""
    oidc_on(monkeypatch)
    r = grid_app.app.test_client().get("/api/permissions")
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.get_json() == {
        "permissions": permissions.catalogue(),
        "builtInRoles": [
            {"id": -1, "name": "Admin", "description": "Everything, including people, roles and every secret.",
             "permissions": sorted(permissions.ALL)},
            {"id": -2, "name": "Operator",
             "description": "Runs the grid and writes checks: grid actions, adding hosts (which see what checks do "
                            "on them), their own client tokens, and the MFA secrets and sign-ins they add or that "
                            "are shared with them.",
             "permissions": ["grid.operate", "grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use",
                             "tokens.agents", "tokens.own"]},
            {"id": -3, "name": "Tester",
             "description": "Writes checks: their own client tokens, and the MFA secrets and sign-ins they add or "
                            "that are shared with them.",
             "permissions": ["grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use", "tokens.own"]},
            {"id": -4, "name": "Viewer",
             "description": "Sees the grid and changes nothing. Matters only when the grid needs sign-in to view it.",
             "permissions": ["grid.view"]},
        ]}
    assert r.get_json()["permissions"][0] == {
        "id": "grid.view", "group": "Grid", "label": "See the grid",
        "description": "Open the overview, activity and sessions when the grid needs sign-in to view them "
                       "(GRID_UI_READS).", "implies": []}
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    assert grid_app.app.test_client().get("/api/permissions").get_json() == r.get_json()


# ---- the API: the Admin page -------------------------------------------------------------------


@pytest.fixture
def admin(db, monkeypatch):
    """Robin, an admin, signed in on the test client. Dana is a Tester whose group qa maps to the QA role; she owns
    an MFA secret shared with QA, and a client token. Pat is an admin who is disabled. Times are fixed."""
    oidc_on(monkeypatch)
    qa = add_role(db, "QA", "mfa.add signins.add")
    add_mapping(db, "qa", qa)
    robin = make_user(db, "Robin Hale", [permissions.ADMIN], email="robin@example.com")
    dana = make_user(db, "Dana Lee", [permissions.TESTER], groups=["qa"], email="dana@example.com")
    pat = make_user(db, "Pat Quinn", [permissions.ADMIN], disabled=True)
    for user_id, day in ((robin, 1), (dana, 2), (pat, 3)):
        run(db, "UPDATE users SET createdAt = ?, lastSignInAt = ? WHERE id = ?",
            ("2026-01-0{} 09:00:00".format(day), "2026-02-0{} 09:00:00".format(day), user_id))
    run(db, "UPDATE groupRoles SET createdAt = '2026-01-01 08:00:00'")
    secret = add_item(db, "mfa", "intranet", owner=dana, shares=[("role", qa)])
    token, _ = add_client_token(db, "dana-ci", owner=dana)
    grid = grid_app.app.test_client()
    sign_in_as(grid, robin)
    return types.SimpleNamespace(path=db, grid=grid, qa=qa, robin=robin, dana=dana, pat=pat, secret=secret,
                                 token=token)


TESTERS = ["grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use", "tokens.own"]
OPERATORS = ["grid.operate", "grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use", "tokens.agents",
             "tokens.own"]
NO_OWNS = {"mfa": 0, "signins": 0, "tokens": 0}


def manual(role_id):
    return {"id": role_id, "name": permissions.BUILTIN_ROLES[role_id][0], "source": "manual"}


def listed(a, who, **changes):
    """What the Admin page says of Robin, Dana or Pat (the admin fixture's), with changes."""
    people = {
        "robin": {"id": a.robin, "name": "Robin Hale", "email": "robin@example.com", "emailVerified": True,
                  "subject": "robin.hale", "disabled": False, "createdAt": "2026-01-01T09:00:00Z",
                  "lastSignInAt": "2026-02-01T09:00:00Z", "lastSeenAt": None, "sessions": 1,
                  "roles": [manual(permissions.ADMIN)], "permissions": sorted(permissions.ALL), "admin": True,
                  "groupsOverage": False, "owns": NO_OWNS, "canManage": False},
        "dana": {"id": a.dana, "name": "Dana Lee", "email": "dana@example.com", "emailVerified": True,
                 "subject": "dana.lee", "disabled": False, "createdAt": "2026-01-02T09:00:00Z",
                 "lastSignInAt": "2026-02-02T09:00:00Z", "lastSeenAt": None, "sessions": 0,
                 "roles": [manual(permissions.TESTER), {"id": a.qa, "name": "QA", "source": "group", "group": "qa"}],
                 "permissions": TESTERS, "admin": False, "groupsOverage": False,
                 "owns": {"mfa": 1, "signins": 0, "tokens": 1}, "canManage": True},
        "pat": {"id": a.pat, "name": "Pat Quinn", "email": None, "emailVerified": True, "subject": "pat.quinn",
                "disabled": True, "createdAt": "2026-01-03T09:00:00Z", "lastSignInAt": "2026-02-03T09:00:00Z",
                "lastSeenAt": None, "sessions": 0, "roles": [manual(permissions.ADMIN)], "permissions": [],
                "admin": False, "groupsOverage": False, "owns": NO_OWNS, "canManage": True},
    }
    return dict(people[who], **changes)


def post(a, path, body=None):
    return a.grid.post(path, json={} if body is None else body)


def ok(r, status=200):
    """The JSON of an answer that is status, never cached."""
    assert (r.status_code, r.headers["Cache-Control"]) == (status, "no-store"), r.get_data(as_text=True)
    return r.get_json()


def generated(body):
    """body without its generated time, which must be one."""
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", body.pop("generated"))
    return body


def test_the_people_who_signed_in(admin):
    a = admin
    assert generated(ok(a.grid.get("/api/admin/users"))) == {
        "ready": True, "total": 2, "counts": {"active": 2, "disabled": 1},
        "users": [listed(a, "dana"), listed(a, "robin")], "problem": None, "ssoConfigured": True}
    assert generated(ok(a.grid.get("/api/admin/users?status=all")))["users"] == [
        listed(a, "pat"), listed(a, "dana"), listed(a, "robin")]
    body = generated(ok(a.grid.get("/api/admin/users?status=disabled")))
    assert (body["total"], body["counts"], body["users"]) == (1, {"active": 2, "disabled": 1}, [listed(a, "pat")])


def test_one_persons_sheet(admin):
    a = admin
    assert ok(a.grid.get("/api/admin/users/{}".format(a.dana))) == {
        "user": listed(a, "dana", issuer=ISSUER, groups=["qa"], disabledAt=None, disabledBy=None),
        "permissions": [{"id": "grid.view", "via": [permissions.TESTER]},
                        {"id": "tokens.own", "via": [permissions.TESTER]},
                        {"id": "mfa.use", "via": [permissions.TESTER, a.qa]},
                        {"id": "mfa.add", "via": [permissions.TESTER, a.qa]},
                        {"id": "signins.use", "via": [permissions.TESTER, a.qa]},
                        {"id": "signins.add", "via": [permissions.TESTER, a.qa]}],
        "sessions": [], "owns": {"mfa": [{"id": a.secret, "name": "intranet"}], "signins": [],
                                 "tokens": [{"id": a.token, "name": "dana-ci", "kind": "client", "status": "active"}]},
        "history": [], "canManage": True, "cannotManage": None}
    # Their own: the session this browser is, and why they can't change their own access.
    mine = ok(a.grid.get("/api/admin/users/{}".format(a.robin)))
    (session,) = mine["sessions"]
    (session_id,) = run(a.path, "SELECT id FROM userSessions WHERE userId = ?", (a.robin,))[0]
    assert {k: v for k, v in session.items() if not k.endswith("At")} == {
        "id": session_id, "address": None, "userAgent": None, "current": True}
    assert (mine["canManage"], mine["cannotManage"]) == (False, "You can't change your own access. Ask another admin.")
    # A disabled admin, and what they did and had done to them, for whoever may read the audit log.
    post(a, "/api/admin/users/{}/enable".format(a.pat))
    post(a, "/api/admin/users/{}/disable".format(a.pat))
    pat = ok(a.grid.get("/api/admin/users/{}".format(a.pat)))
    assert (pat["user"]["disabledBy"], pat["permissions"], pat["sessions"]) == ("Robin Hale", [], [])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", pat["user"]["disabledAt"])
    assert [(e["action"], e["actorName"], e["targetName"]) for e in pat["history"]] == [
        ("user.disabled", "Robin Hale", "Pat Quinn"), ("user.enabled", "Robin Hale", "Pat Quinn")]
    # Someone who may see people but not read the audit log, nor change anyone.
    viewer = grid_app.app.test_client()
    sign_in_as(viewer, make_user(a.path, "Vera Viewer", [add_role(a.path, "People viewers", "users.view")]))
    seen = ok(viewer.get("/api/admin/users/{}".format(a.dana)))
    assert "history" not in seen
    assert (seen["canManage"], seen["cannotManage"], seen["user"]["canManage"]) == (
        False, "Your roles don't include “Manage people and roles”.", False)
    assert ok(a.grid.get("/api/admin/users/999"), 404) == {"error": "There is no user 999."}


def test_the_sessions_of_someone_another_issuer_made_count_for_nothing(admin):
    """GRID_OIDC_ISSUER changed since Olga signed in: her session, still within its hours, signs nobody in
    (session_access), so the Admin page neither counts nor lists it."""
    a = admin
    olga = make_user(a.path, "Olga Old", [permissions.TESTER], issuer="https://old-login.example.com")
    add_session(a.path, olga)
    add_session(a.path, a.dana)
    users = {u["name"]: u for u in ok(a.grid.get("/api/admin/users"))["users"]}
    assert (users["Olga Old"]["sessions"], users["Dana Lee"]["sessions"]) == (0, 1)
    assert ok(a.grid.get("/api/admin/users/{}".format(olga)))["sessions"] == []
    assert len(ok(a.grid.get("/api/admin/users/{}".format(a.dana)))["sessions"]) == 1


def test_giving_and_taking_roles(admin):
    a = admin
    give, take = "/api/admin/users/{}/roles/add".format(a.dana), "/api/admin/users/{}/roles/remove".format(a.dana)
    operator = listed(a, "dana", roles=[manual(permissions.OPERATOR), manual(permissions.TESTER),
                                        {"id": a.qa, "name": "QA", "source": "group", "group": "qa"}],
                      permissions=OPERATORS)
    assert ok(post(a, give, {"role": permissions.OPERATOR})) == {"user": operator}
    assert ok(post(a, give, {"role": permissions.OPERATOR})) == {"user": operator}  # again: nothing changes
    assert ok(post(a, take, {"role": permissions.TESTER})) == {
        "user": dict(operator, roles=[operator["roles"][0], operator["roles"][2]]), "activeClientTokens": 0}
    # Without “Make client tokens” any more, the client tokens they made still open sessions at the hub.
    assert ok(post(a, take, {"role": permissions.OPERATOR})) == {
        "user": listed(a, "dana", roles=[{"id": a.qa, "name": "QA", "source": "group", "group": "qa"}],
                       permissions=["mfa.add", "mfa.use", "signins.add", "signins.use"]), "activeClientTokens": 1}
    assert ok(post(a, take, {"role": permissions.OPERATOR}))["activeClientTokens"] == 1  # a role they don't have
    assert [(row[4], json.loads(row[8])) for row in audit_rows(a.path)] == [
        ("user.role_added", {"role": {"id": permissions.OPERATOR, "name": "Operator"}}),
        ("user.role_removed", {"role": {"id": permissions.TESTER, "name": "Tester"}}),
        ("user.role_removed", {"role": {"id": permissions.OPERATOR, "name": "Operator"}})]
    assert ok(post(a, take, {"role": a.qa}), 409) == {
        "error": "Dana Lee has QA through the group 'qa'. Remove the mapping, or their membership at the identity "
                 "provider, instead."}
    assert ok(post(a, give, {"role": 99}), 400) == {"error": "There is no role 99."}
    assert ok(post(a, give, {"role": "Operator"}), 400) == {"error": 'Send {"role": <id>}.'}
    assert ok(post(a, give, []), 400) == {"error": "Send a JSON object."}
    assert ok(post(a, "/api/admin/users/999/roles/add", {"role": -4}), 404) == {"error": "There is no user 999."}
    assert ok(post(a, "/api/admin/users/{}/roles/remove".format(a.robin), {"role": -1}), 403) == {
        "error": "You can't change your own access. Ask another admin.", "forbidden": True}


def test_disabling_and_enabling_someone(admin):
    """Their browser is told why at its next request: disabled, though their sessions were ended too, and once
    they are enabled again, that their session was ended."""
    a = admin
    danas = grid_app.app.test_client()
    cookie = sign_in_as(danas, a.dana)
    r = post(a, "/api/admin/users/{}/disable".format(a.dana), {"revokeClientTokens": True, "revokeAgentTokens": False})
    assert ok(r) == {"user": listed(a, "dana", disabled=True, permissions=[]), "sessionsEnded": 1,
                     "tokensRevoked": {"client": 1, "agent": 0}}
    assert run(a.path, "SELECT revokedAt IS NOT NULL FROM accessTokens WHERE id = ?", (a.token,)) == [(1,)]
    assert ok(danas.get("/api/mfa"), 403) == {"error": auth.ENDED["disabled"], "token_required": True,
                                              "reason": "disabled"}
    assert ok(post(a, "/api/admin/users/{}/enable".format(a.dana))) == {"user": listed(a, "dana")}
    danas.set_cookie(oidc.cookie_names().session, cookie)  # the answer above dropped it
    assert danas.get("/api/mfa").get_json()["reason"] == "revoked"
    assert ok(post(a, "/api/admin/users/{}/disable".format(a.dana), {"revokeClientTokens": "yes"}), 400) == {
        "error": "revokeClientTokens is true or false."}
    # The last enabled admin.
    assert ok(post(a, "/api/admin/users/{}/disable".format(a.robin)), 403)["error"] == (
        "You can't change your own access. Ask another admin.")
    r = a.grid.post("/api/admin/users/{}/disable".format(a.robin), json={}, headers=ADMIN)
    assert ok(r, 409) == {"error": "That would leave no admin who can sign in. Make someone else an admin first.",
                          "last_admin": True}


def test_ending_someones_sessions(admin):
    a = admin
    first, second = add_session(a.path, a.dana), add_session(a.path, a.dana)
    first_id, second_id = (i for (i,) in run(a.path, "SELECT id FROM userSessions WHERE userId = ? ORDER BY id",
                                             (a.dana,)))
    path = "/api/admin/users/{}/sessions/revoke".format(a.dana)
    assert ok(post(a, path, {"sessionId": first_id})) == {"ended": 1}
    assert ok(post(a, path, {"sessionId": first_id})) == {"ended": 0}
    assert ok(post(a, path, {"sessionId": 99999999999})) == {"ended": 0}
    assert ok(post(a, path)) == {"ended": 1}
    assert accounts.session_access(first) == accounts.session_access(second) == oidc.Ended("revoked")
    assert [(row[4], json.loads(row[8])) for row in audit_rows(a.path)] == [
        ("user.sessions_ended", {"sessions": 1}), ("user.sessions_ended", {"sessions": 1})]
    # Anyone may end their own: this browser is signed out at its next request.
    assert ok(post(a, "/api/admin/users/{}/sessions/revoke".format(a.robin))) == {"ended": 1}
    assert a.grid.get("/api/admin/users").get_json() == {
        "error": "Your session was ended, by signing out elsewhere or by an admin. Sign in again.",
        "token_required": True, "reason": "revoked"}


def test_revoking_someones_tokens(admin):
    a = admin
    host = add_token(a.path, "dana-host", "agent", a.dana)
    path = "/api/admin/users/{}/tokens/revoke".format(a.dana)
    assert ok(post(a, path, {"client": True})) == {"revoked": {"client": 1, "agent": 0}}
    assert ok(post(a, path, {"client": True, "agent": True})) == {"revoked": {"client": 0, "agent": 1}}
    assert ok(post(a, path, {})) == {"revoked": {"client": 0, "agent": 0}}
    assert run(a.path, "SELECT id, revokedAt IS NOT NULL FROM accessTokens ORDER BY id") == [(a.token, 1), (host, 1)]
    assert [(row[4], row[8]) for row in audit_rows(a.path)] == [
        ("user.tokens_revoked", '{"client":1,"agent":0}'), ("user.tokens_revoked", '{"client":0,"agent":1}')]
    assert ok(post(a, path, {"agent": 1}), 400) == {"error": "agent is true or false."}


def test_moving_what_someone_owns_to_someone_else(admin):
    a = admin
    bob = make_user(a.path, "Bob Stone", [permissions.TESTER])
    path = "/api/admin/users/{}/move-items".format(a.dana)
    assert ok(post(a, path, {"toUserId": bob})) == {"moved": {"mfa": 1, "signins": 0}, "sharesRemoved": 0,
                                                    "leasesCleared": 0, "statesDropped": 0}
    assert ok(post(a, "/api/admin/users/{}/move-items".format(bob), {"toUserId": None}))["moved"] == {"mfa": 1,
                                                                                                      "signins": 0}
    assert run(a.path, "SELECT itemKind, userId FROM itemOwners ORDER BY itemKind") == [("token", a.dana)]
    assert ok(post(a, path, {"toUserId": a.pat}), 400) == {"error": "User {} is disabled.".format(a.pat)}
    assert ok(post(a, path, {}), 400) == {"error": 'Send {"toUserId": <id, or null>}.'}
    # It takes seeing every MFA secret and every saved sign-in, besides managing people.
    lead = grid_app.app.test_client()
    sign_in_as(lead, make_user(a.path, "Lee Park", [add_role(a.path, "People leads", LEADS)]))
    assert lead.post(path, json={"toUserId": None}).get_json() == {
        "error": "Your roles don't include “Every MFA secret”, which this needs. Ask an admin for a role that "
                 "does.", "forbidden": True, "permission": "mfa.all"}


def built_in(role_id, members, can_edit=False):
    name, description, perms = permissions.BUILTIN_ROLES[role_id]
    return {"id": role_id, "name": name, "description": description, "builtIn": True, "permissions": sorted(perms),
            "effective": sorted(permissions.expand(perms)), "members": members, "mappings": [], "shares": 0,
            "canEdit": can_edit}


def test_the_roles(admin):
    a = admin
    (mapping,) = run(a.path, "SELECT id FROM groupRoles")[0]
    assert ok(a.grid.get("/api/admin/roles")) == {"ready": True, "permissions": permissions.catalogue(), "roles": [
        built_in(permissions.ADMIN, {"manual": 2, "groups": 0}),
        built_in(permissions.OPERATOR, {"manual": 0, "groups": 0}),
        built_in(permissions.TESTER, {"manual": 1, "groups": 0}),
        built_in(permissions.VIEWER, {"manual": 0, "groups": 0}),
        {"id": a.qa, "name": "QA", "description": None, "builtIn": False, "permissions": ["mfa.add", "signins.add"],
         "effective": ["mfa.add", "mfa.use", "signins.add", "signins.use"], "members": {"manual": 0, "groups": 1},
         "mappings": [{"id": mapping, "group": "qa"}], "shares": 1, "canEdit": True}]}


def test_making_changing_and_deleting_a_role(admin):
    a = admin
    r = post(a, "/api/admin/roles", {"name": " Night shift ", "description": "Runs the grid at night.",
                                     "permissions": ["grid.operate", "mfa.use"]})
    role = {"id": a.qa + 1, "name": "Night shift", "description": "Runs the grid at night.", "builtIn": False,
            "permissions": ["grid.operate", "mfa.use"], "effective": ["grid.operate", "grid.view", "mfa.use"],
            "members": {"manual": 0, "groups": 0}, "mappings": [], "shares": 0, "canEdit": True}
    assert ok(r, 201) == {"role": role}
    path = "/api/admin/roles/{}".format(role["id"])
    assert ok(post(a, path, {"name": "Late shift", "permissions": ["grid.view"]})) == {
        "role": dict(role, name="Late shift", permissions=["grid.view"], effective=["grid.view"])}
    assert ok(post(a, path, {"description": None}))["role"]["description"] is None
    post(a, "/api/admin/users/{}/roles/add".format(a.dana), {"role": role["id"]})
    mapping = ok(post(a, "/api/admin/groups", {"group": "night", "roleId": role["id"]}), 201)["mapping"]["id"]
    run(a.path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) VALUES ('mfa', ?, 'role', "
                "?, ?)", (a.secret, role["id"], ts(60)))
    listed_role = ok(a.grid.get("/api/admin/roles"))["roles"][-2]
    assert (listed_role["name"], listed_role["members"], listed_role["mappings"], listed_role["shares"]) == (
        "Late shift", {"manual": 1, "groups": 0}, [{"id": mapping, "group": "night"}], 1)
    assert ok(post(a, path + "/delete")) == {"deleted": True, "id": role["id"],
                                             "removed": {"users": 1, "mappings": 1, "shares": 1}}
    assert run(a.path, "SELECT COUNT(*) FROM itemShares WHERE granteeKind = 'role' AND granteeId = ?",
               (role["id"],)) == [(0,)]
    assert [(row[4], row[6], row[7], json.loads(row[8])) for row in audit_rows(a.path)
            if row[4].startswith("role.")] == [
        ("role.created", role["id"], "Night shift", {"permissions": ["grid.operate", "mfa.use"]}),
        ("role.updated", role["id"], "Late shift", {"before": {"name": "Night shift",
                                                               "permissions": ["grid.operate", "mfa.use"]},
                                                    "after": {"name": "Late shift", "permissions": ["grid.view"]}}),
        ("role.updated", role["id"], "Late shift", {"before": {"name": "Late shift", "permissions": ["grid.view"]},
                                                    "after": {"name": "Late shift", "permissions": ["grid.view"]}}),
        ("role.deleted", role["id"], "Late shift", {"users": 1, "mappings": 1, "shares": 1})]
    assert ok(post(a, path), 404) == ok(post(a, path + "/delete"), 404) == {
        "error": "There is no role {}.".format(role["id"])}
    built_in_refused = {"error": "Built-in roles can't be changed. Duplicate one to make your own."}
    assert ok(post(a, "/api/admin/roles/-2", {"name": "Ops"}), 400) == built_in_refused
    assert ok(post(a, "/api/admin/roles/-2/delete"), 400) == built_in_refused
    assert ok(post(a, "/api/admin/roles", {"name": "qa", "permissions": []}), 400) == {
        "error": "There is already a role called 'qa'."}
    assert ok(post(a, "/api/admin/roles", {"name": "Admins", "permissions": []}), 400) == {
        "error": "“Admins” is reserved: choose another name."}


def test_the_group_mappings(admin):
    a = admin
    (mapping,) = run(a.path, "SELECT id FROM groupRoles")[0]
    qa = {"id": mapping, "group": "qa", "roleId": a.qa, "role": "QA", "note": None, "people": 1,
          "createdAt": "2026-01-01T08:00:00Z", "createdBy": None}
    assert ok(a.grid.get("/api/admin/groups")) == {"ready": True, "claims": ["groups"], "mappings": [qa],
                                                   "seen": [{"group": "qa", "people": 1}]}
    r = post(a, "/api/admin/groups", {"group": "*", "roleId": permissions.VIEWER, "note": " Everyone "})
    made = ok(r, 201)["mapping"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", made.pop("createdAt"))
    assert made == {"id": mapping + 1, "group": "*", "roleId": permissions.VIEWER, "role": "Viewer",
                    "note": "Everyone", "people": 2, "createdBy": "Robin Hale"}
    again = ok(post(a, "/api/admin/groups", {"group": "*", "roleId": permissions.VIEWER}), 200)["mapping"]
    assert dict(again, createdAt=None) == dict(made, createdAt=None)
    listing = ok(a.grid.get("/api/admin/groups"))["mappings"]
    assert [m["group"] for m in listing] == ["*", "qa"]  # everyone first
    assert ok(post(a, "/api/admin/groups/{}/delete".format(made["id"]))) == {"deleted": True}
    assert ok(post(a, "/api/admin/groups/{}/delete".format(made["id"])), 404) == {
        "error": "There is no group mapping {}.".format(made["id"])}
    assert [(row[4], row[5], row[6], row[7], json.loads(row[8])) for row in audit_rows(a.path)] == [
        ("group.mapped", "group", made["id"], "*", {"group": "*", "role": {"id": -4, "name": "Viewer"}}),
        ("group.unmapped", "group", made["id"], "*", {"group": "*", "role": {"id": -4, "name": "Viewer"}})]
    no_group = "A group is 1 to 255 characters, without control characters."
    for body, error in [({"group": "", "roleId": -4}, no_group), ({"group": "a\tb", "roleId": -4}, no_group),
                        ({"group": "x" * 256, "roleId": -4}, no_group),
                        ({"group": "ops", "roleId": -4, "note": "x" * 129},
                         "A note is at most 128 characters, without control characters."),
                        ({"group": "ops", "roleId": 99}, "There is no role 99."),
                        ({"group": "ops"}, 'Send {"roleId": <id>}.')]:
        assert ok(post(a, "/api/admin/groups", body), 400) == {"error": error}


def test_the_seen_groups_are_the_most_common_first(admin):
    a = admin
    for name, groups in (("Bob", ["ops", "qa"]), ("Cy", ["ops"]), ("Di", ["zz", "ops"]), ("Ed", ["gone"])):
        make_user(a.path, name, groups=groups, disabled=name == "Ed")
    assert ok(a.grid.get("/api/admin/groups"))["seen"] == [
        {"group": "ops", "people": 3}, {"group": "qa", "people": 2}, {"group": "zz", "people": 1}]


def audited(path, actor, action, target=(None, None, None), details=None, when="2026-03-01 10:00:00"):
    """An audit row as a change would write it, at a fixed time. Returns its id."""
    store.audit_alone(actor, action, *target, details=details)
    ((row_id,),) = run(path, "SELECT MAX(id) FROM auditLog")
    run(path, "UPDATE auditLog SET occurredAt = ? WHERE id = ?", (when, row_id))
    return row_id


def test_the_audit_log(admin):
    a = admin
    robin = store.Actor(a.robin, "Robin Hale", "oidc", "10.0.0.8", permissions.ALL)
    check = store.Actor(a.dana, "client token 'dana-ci' (Dana Lee)", "client-token", "10.0.0.9", TESTER)
    first = audited(a.path, ADMIN_ACTOR, "auth.breakglass")
    second = audited(a.path, robin, "user.role_added", ("user", a.dana, "Dana Lee"),
                     {"role": {"id": -2, "name": "Operator"}}, "2026-03-01 10:05:00")
    third = audited(a.path, check, "signin.created", ("signin", 4, "prod-admin"), {"owner": {"id": a.dana}})
    fourth = audited(a.path, robin, "mfa.shared", ("mfa", a.secret, "intranet"))
    run(a.path, "UPDATE auditLog SET details = ? WHERE id = ?", ('{"grantee":{"kind":"user","na', fourth))
    entries = {
        first: {"id": first, "occurredAt": "2026-03-01T10:00:00Z", "actorId": None, "actorName": "Admin",
                "actorMethod": "token", "address": "10.0.0.7", "action": "auth.breakglass", "targetKind": None,
                "targetId": None, "targetName": None, "details": {}},
        second: {"id": second, "occurredAt": "2026-03-01T10:05:00Z", "actorId": a.robin, "actorName": "Robin Hale",
                 "actorMethod": "oidc", "address": "10.0.0.8", "action": "user.role_added", "targetKind": "user",
                 "targetId": a.dana, "targetName": "Dana Lee", "details": {"role": {"id": -2, "name": "Operator"}}},
        third: {"id": third, "occurredAt": "2026-03-01T10:00:00Z", "actorId": a.dana,
                "actorName": "client token 'dana-ci' (Dana Lee)", "actorMethod": "client-token",
                "address": "10.0.0.9", "action": "signin.created", "targetKind": "signin", "targetId": 4,
                "targetName": "prod-admin", "details": {"owner": {"id": a.dana}}},
        # Cut at the audit log's limit, it is no longer JSON: what is left comes as text.
        fourth: {"id": fourth, "occurredAt": "2026-03-01T10:00:00Z", "actorId": a.robin, "actorName": "Robin Hale",
                 "actorMethod": "oidc", "address": "10.0.0.8", "action": "mfa.shared", "targetKind": "mfa",
                 "targetId": a.secret, "targetName": "intranet",
                 "details": {"text": '{"grantee":{"kind":"user","na'}},
    }
    assert ok(a.grid.get("/api/admin/audit")) == {
        "ready": True, "entries": [entries[i] for i in (fourth, third, second, first)], "next": None, "keptDays": 365}
    # Newest first, a page at a time: the next page starts below the last id shown.
    page = ok(a.grid.get("/api/admin/audit?limit=3"))
    assert ([e["id"] for e in page["entries"]], page["next"]) == ([fourth, third, second], second)
    page = ok(a.grid.get("/api/admin/audit?limit=3&before={}".format(page["next"])))
    assert ([e["id"] for e in page["entries"]], page["next"]) == ([first], None)

    def ids(query):
        return [e["id"] for e in ok(a.grid.get("/api/admin/audit?" + query))["entries"]]

    assert ids("actor=token") == [first]
    assert ids("actor=checks") == [third]
    assert ids("actor={}".format(a.robin)) == [fourth, second]
    assert ids("category=user") == [second]
    assert ids("category=signin&actor=checks") == [third]
    assert ids("target=user:{}".format(a.dana)) == [second]
    assert ids("target=mfa:{}".format(a.secret)) == [fourth]
    for query, error in [("limit=0", "limit is a whole number from 1 to 200."),
                         ("limit=201", "limit is a whole number from 1 to 200."),
                         ("before=x", "before is a whole number from 1 to 2147483647."),
                         ("actor=someone", "actor is a whole number from 1 to 2147483647."),
                         ("category=users", "category is one of auth, user, role, group, token, mfa, signin, grid, "
                                            "access."),
                         ("target=user", "target is <kind>:<id>, the kind one of user, role, group, token, mfa, "
                                         "signin, slot, node."),
                         ("target=people:3", "target is <kind>:<id>, the kind one of user, role, group, token, mfa, "
                                             "signin, slot, node.")]:
        assert ok(a.grid.get("/api/admin/audit?" + query), 400) == {"error": error}


def test_the_single_sign_on_settings(admin, monkeypatch):
    """What the Grid UI reads from its settings, with the problem in full: never the client secret."""
    a = admin
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:robin.hale, Dana@Example.com, nobody@example.com")
    r = a.grid.get("/api/admin/sign-in")
    assert ok(r) == {
        "configured": True, "problem": None, "label": "single sign-on", "issuer": ISSUER, "clientId": "grid-ui",
        "clientAuth": "auto", "redirectUri": "http://localhost/auth/callback", "scopes": ["openid", "email", "profile"],
        "groupsClaims": ["groups"], "admins": [{"entry": "sub:robin.hale", "userId": a.robin, "name": "Robin Hale"},
                                               {"entry": "Dana@Example.com", "userId": a.dana, "name": "Dana Lee"},
                                               {"entry": "nobody@example.com", "userId": None, "name": None}],
        "session": {"hours": 12, "idleMinutes": 120}, "readsOpen": False, "adminToken": True, "endSession": False}
    assert "s3cret-client" not in r.get_data(as_text=True)
    monkeypatch.setenv("GRID_OIDC_CA_FILE", "/nowhere/ca.pem")
    monkeypatch.setenv("GRID_OIDC_SCOPES", "groups")
    body = a.grid.get("/api/admin/sign-in", headers=ADMIN).get_json()
    assert (body["problem"], body["scopes"]) == ("GRID_OIDC_CA_FILE can't be read: there is no file /nowhere/ca.pem.",
                                                 ["openid", "email", "profile", "groups"])
    assert a.grid.get("/api/admin/users", headers=ADMIN).get_json()["problem"] == body["problem"]
    # A grid with only the admin token: single sign-on isn't set up, and says so.
    for name in OIDC_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("GRID_OIDC_CA_FILE")
    monkeypatch.delenv("GRID_OIDC_SCOPES")
    assert grid_app.app.test_client().get("/api/admin/sign-in", headers=ADMIN).get_json() == {
        "configured": False, "problem": None, "label": "single sign-on", "issuer": None, "clientId": None,
        "clientAuth": "auto", "redirectUri": None, "scopes": ["openid", "email", "profile"], "groupsClaims": ["groups"],
        "admins": [], "session": {"hours": 12, "idleMinutes": 120}, "readsOpen": True, "adminToken": True,
        "endSession": False}


def names(a, query, status="all"):
    return [u["name"] for u in ok(a.grid.get("/api/admin/users?sort=name&status={}&{}".format(status, query)))["users"]]


def test_finding_people(admin):
    """By part of their name, email or subject, whatever the case. What is typed is only ever text: '%' and '_'
    mean nothing to the search, and '!', its escape, is escaped too."""
    a = admin
    for name in ("Ann 100% Sure", "Ann 1000 Times", "Bo_sun", "Bobsun", "Wow!!", "Wowza"):
        make_user(a.path, name)
    q = urllib.parse.urlencode
    assert names(a, q({"q": "0%"})) == ["Ann 100% Sure"]
    assert names(a, q({"q": "o_s"})) == ["Bo_sun"]
    assert names(a, q({"q": "w!"})) == ["Wow!!"]
    assert names(a, q({"q": "%%"})) == names(a, q({"q": "__"})) == names(a, q({"q": "!%"})) == []
    assert names(a, q({"q": "ANN"})) == ["Ann 100% Sure", "Ann 1000 Times"]
    assert names(a, q({"q": "ROBIN@"})) == ["Robin Hale"]
    assert names(a, q({"q": " dana.lee "})) == ["Dana Lee"]  # the subject
    assert len(names(a, q({"q": ""}))) == 9


def test_people_by_role_however_they_have_it(admin, monkeypatch):
    """By hand, through a group, the mapping for everyone or GRID_OIDC_ADMINS; or no role at all."""
    a = admin
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:erin.wu")
    make_user(a.path, "Erin Wu")
    make_user(a.path, "Nora Nobody")
    make_user(a.path, "Quinn QA", groups=["qa"])
    assert names(a, "role=-1") == ["Erin Wu", "Pat Quinn", "Robin Hale"]
    assert names(a, "role=-1", status="active") == ["Erin Wu", "Robin Hale"]
    assert names(a, "role={}".format(a.qa)) == ["Dana Lee", "Quinn QA"]
    assert names(a, "role=none") == ["Nora Nobody"]
    assert names(a, "role=-4") == []
    add_mapping(a.path, "*", permissions.VIEWER)
    assert len(names(a, "role=-4")) == 6
    assert names(a, "role=none") == []
    body = ok(a.grid.get("/api/admin/users?role=-1"))
    assert (body["total"], body["counts"]) == (2, {"active": 2, "disabled": 1})
    for query, error in [("role=admin", "role is a whole number from -4 to 2147483647."),
                         ("status=any", "status is active, disabled or all."),
                         ("sort=newest", "sort is recent or name."),
                         ("limit=0", "limit is a whole number from 1 to 200."),
                         ("offset=-1", "offset is a whole number from 0 to 2147483647.")]:
        assert ok(a.grid.get("/api/admin/users?" + query), 400) == {"error": error}


def test_people_a_page_at_a_time_by_name_or_latest_sign_in(admin):
    a = admin
    for i in range(5):
        tess = make_user(a.path, "Tess {}".format(i))
        run(a.path, "UPDATE users SET lastSignInAt = ? WHERE id = ?",
            (None if i == 2 else "2026-03-0{} 09:00:00".format(i + 1), tess))
    page = ok(a.grid.get("/api/admin/users?limit=3"))
    assert (page["total"], [u["name"] for u in page["users"]]) == (7, ["Tess 4", "Tess 3", "Tess 1"])
    page = ok(a.grid.get("/api/admin/users?limit=3&offset=3"))
    assert [u["name"] for u in page["users"]] == ["Tess 0", "Dana Lee", "Robin Hale"]
    page = ok(a.grid.get("/api/admin/users?limit=3&offset=6"))
    assert [u["name"] for u in page["users"]] == ["Tess 2"]  # never signed in: last
    assert [u["name"] for u in ok(a.grid.get("/api/admin/users?sort=name&limit=4&offset=1"))["users"]] == [
        "Robin Hale", "Tess 0", "Tess 1", "Tess 2"]


def test_a_refusal_by_a_rule_on_the_admin_page_is_recorded(admin, monkeypatch):
    """Through the routes: once a minute per person, route and rule, in a transaction of its own. A built-in role
    is refused by a rule too, though with a 400."""
    a = admin
    clock = [1000.0]
    monkeypatch.setattr(grid_app, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    lead = grid_app.app.test_client()
    lee = make_user(a.path, "Lee Park", [add_role(a.path, "People leads", LEADS)])
    sign_in_as(lead, lee)
    path = "/api/admin/users/{}/roles/add".format(a.dana)
    for _ in range(3):
        r = lead.post(path, json={"role": permissions.ADMIN})
        assert ok(r, 403) == {"error": "You can't give or change the role “Admin”: it has permissions you don't "
                                       "have ({}).".format(ADMIN_LABELS), "forbidden": True}
    assert ok(lead.post("/api/admin/roles/-3", json={"name": "Testers"}), 400) == {
        "error": "Built-in roles can't be changed. Duplicate one to make your own."}
    clock[0] += 60
    lead.post(path, json={"role": permissions.ADMIN})
    assert [(row[0], row[4], json.loads(row[8])) for row in audit_rows(a.path)] == [
        (lee, "access.denied", {"route": "POST " + path, "rule": "role", "permission": "tokens.all"}),
        (lee, "access.denied", {"route": "POST /api/admin/roles/-3", "rule": "built-in"}),
        (lee, "access.denied", {"route": "POST " + path, "rule": "role", "permission": "tokens.all"})]


def test_without_the_people_tables_nothing_can_be_changed(admin):
    """Reading says there is nobody yet; changing anyone's access says which tables are missing."""
    a = admin
    for table in ("userRoles", "groupRoles", "roles"):
        run(a.path, "DROP TABLE " + table)
    r = a.grid.get("/api/admin/roles", headers=ADMIN)
    assert ok(r)["ready"] is False and [role["id"] for role in r.get_json()["roles"]] == [-1, -2, -3, -4]
    assert ok(a.grid.get("/api/admin/groups", headers=ADMIN))["ready"] is False
    for path, body in [("/api/admin/users/{}/roles/add".format(a.dana), {"role": -4}),
                       ("/api/admin/roles", {"name": "QA", "permissions": []}),
                       ("/api/admin/groups", {"group": "qa", "roleId": -4})]:
        assert ok(a.grid.post(path, json=body, headers=ADMIN), 503) == {"error": accounts.NO_PEOPLE_TABLES}
    run(a.path, "DROP TABLE users")
    body = generated(ok(a.grid.get("/api/admin/users", headers=ADMIN)))
    assert (body["ready"], body["users"], body["problem"]) == (False, [], accounts.NO_USER_TABLES)
    assert ok(a.grid.get("/api/admin/users/{}".format(a.dana), headers=ADMIN), 404) == {
        "error": "There is no user {}.".format(a.dana)}


def test_the_demo_has_people_roles_and_an_audit_log_and_owns_nothing(tmp_path, monkeypatch):
    """What the demo's Admin page shows: five people, a custom role, two group mappings and an audit log. Its tokens
    stay the admins' and nobody has a session, so the demo's other counts are as they were."""
    path = str(tmp_path / "demo.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.seed(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    users = accounts.list_users(ADMIN_ACTOR, status="all", sort="name")["users"]
    assert [(u["name"], [(r["name"], r["source"]) for r in u["roles"]], u["disabled"]) for u in users] == [
        ("Alex Moreno", [], False), ("Dana Lee", [("Operator", "group"), ("QA leads", "group")], False),
        ("Lee Chen", [("Viewer", "manual")], True), ("Priya Kumar", [("Tester", "manual")], False),
        ("Robin Hale", [("Admin", "manual")], False)]
    assert [(m["group"], m["role"]) for m in accounts.list_mappings()["mappings"]] == [("grid-operators", "Operator"),
                                                                                       ("qa", "QA leads")]
    assert len(accounts.audit_log()["entries"]) >= 12
    for table in ("itemOwners", "itemShares", "userSessions", "mfaSecrets", "savedSignins"):
        assert run(path, "SELECT COUNT(*) FROM " + table) == [(0,)], table
    assert run(path, "SELECT COUNT(*) FROM accessTokens") == [(len(demo.TOKENS),)]


# ---- the API: who may call what ----------------------------------------------------------------
#
# Every route, how it is gated, and what it answers whoever the gate lets through: (method, path, body, gate,
# status). The gate is ("open",); ("sign-in",) for single sign-on's own routes, which aren't there without it;
# ("read", "grid.view") for the grid's reads, which need sign-in with single sign-on; ("grid", *perms) for grid
# actions, open where no sign-in is configured; ("strict", *perms) for what is refused there; and ("check",
# *perms) for the routes checks call with their client token. perms is any one of. Ids and bodies are chosen so
# that everyone let through gets the same answer. A route the app gains gets a row here
# (test_every_route_is_in_the_permission_matrix says so).

OPEN, SIGN_IN, READ = ("open",), ("sign-in",), ("read", "grid.view")
GRID = ("grid", "grid.operate")
TOKENS = ("strict",) + grid_app.TOKEN_PERMISSIONS
VIEW_PEOPLE, MANAGE_PEOPLE = ("strict", "users.view"), ("strict", "users.manage")
ROUTES = [
    ("GET", "/", None, OPEN, 200),
    ("GET", "/ui", None, OPEN, 200),
    ("GET", "/api/session", None, OPEN, 200),
    ("GET", "/api/permissions", None, OPEN, 200),
    ("POST", "/api/session/signout", {}, OPEN, 200),
    ("GET", "/auth/login", None, SIGN_IN, 303),
    ("GET", "/auth/callback", None, SIGN_IN, 303),
    ("GET", "/status", None, READ, 200),
    ("GET", "/api/status", None, READ, 200),
    ("GET", "/api/events", None, READ, 200),
    ("POST", "/api/slots/999999/release", {}, GRID, 200),
    ("POST", "/api/slots/999999/reset-failures", {}, GRID, 200),
    ("POST", "/api/nodes/reset-failures", {"host": "nowhere"}, GRID, 200),
    ("POST", "/api/nodes/forget", {"host": "nowhere"}, GRID, 404),
    ("GET", "/api/tokens", None, TOKENS, 200),
    ("POST", "/api/tokens", [], ("strict", "tokens.own", "tokens.agents"), 400),
    ("POST", "/api/tokens/999999/revoke", {}, TOKENS, 404),
    ("POST", "/api/tokens/999999/delete", {}, TOKENS, 404),
    ("GET", "/api/mfa", None, ("strict", "mfa.use"), 200),
    ("POST", "/api/mfa", [], ("strict", "mfa.add"), 400),
    ("POST", "/api/mfa/999999/delete", {}, ("strict", "mfa.add"), 404),
    ("GET", "/api/mfa/999999/code", None, ("strict", "mfa.use"), 404),
    ("POST", "/api/mfa/999999/use", {}, ("strict", "mfa.use"), 404),
    ("POST", "/api/mfa/code", [], ("check", "mfa.use"), 400),
    ("GET", "/api/signins", None, ("strict", "signins.use"), 200),
    ("POST", "/api/signins/999999/forget", {}, ("strict", "signins.add"), 404),
    ("POST", "/api/signins/999999/delete", {}, ("strict", "signins.add"), 404),
    ("POST", "/api/signins/checkout", [], ("check", "signins.use"), 400),
    ("POST", "/api/signins/save", [], ("check", "signins.use"), 400),
    ("POST", "/api/signins/release", [], ("check", "signins.use"), 400),
    ("GET", "/api/directory", None, ("strict", "mfa.add", "signins.add", "tokens.all", "users.view"), 200),
    ("POST", "/api/mfa/999999/shares/add", {"user": 1}, ("strict", "mfa.add"), 404),
    ("POST", "/api/mfa/999999/shares/remove", {"role": -3}, ("strict", "mfa.add"), 404),
    ("POST", "/api/mfa/999999/owner", {"userId": None}, ("strict", "mfa.all"), 404),
    ("POST", "/api/signins/999999/shares/add", {"role": -1}, ("strict", "signins.add"), 404),
    ("POST", "/api/signins/999999/shares/remove", {"user": 1}, ("strict", "signins.add"), 404),
    ("POST", "/api/signins/999999/owner", {"userId": 1}, ("strict", "signins.all"), 404),
    ("POST", "/api/tokens/999999/owner", {"userId": 1}, ("strict", "tokens.all"), 404),
    ("GET", "/api/admin/users", None, VIEW_PEOPLE, 200),
    ("GET", "/api/admin/users/999999", None, VIEW_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/roles/add", {"role": -4}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/roles/remove", {"role": -4}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/disable", {}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/enable", {}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/sessions/revoke", {}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/tokens/revoke", {"client": True}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/users/999999/move-items", {"toUserId": None}, MANAGE_PEOPLE, 404),
    ("GET", "/api/admin/roles", None, VIEW_PEOPLE, 200),
    ("POST", "/api/admin/roles", [], MANAGE_PEOPLE, 400),
    ("POST", "/api/admin/roles/999999", {}, MANAGE_PEOPLE, 404),
    ("POST", "/api/admin/roles/999999/delete", {}, MANAGE_PEOPLE, 404),
    ("GET", "/api/admin/groups", None, VIEW_PEOPLE, 200),
    ("POST", "/api/admin/groups", [], MANAGE_PEOPLE, 400),
    ("POST", "/api/admin/groups/999999/delete", {}, MANAGE_PEOPLE, 404),
    ("GET", "/api/admin/audit", None, ("strict", "audit.view"), 200),
    ("GET", "/api/admin/sign-in", None, VIEW_PEOPLE, 200),
]

# Who asks: whether any sign-in is configured, what the gate sees them hold (None: nobody it knows), and what the
# client token they send holds (None: they send none; OWNER_REFUSED: its owner can't use it).
OWNER_REFUSED = "owner refused"
Asker = collections.namedtuple("Asker", "configured held token")
ASKERS = {
    "nothing configured": Asker(False, None, None),
    "the admin token": Asker(True, permissions.ALL, None),
    "an Admin": Asker(True, permissions.ALL, None),
    "an Operator": Asker(True, OPERATOR, None),
    "a Tester": Asker(True, TESTER, None),
    "a Viewer": Asker(True, VIEWER, None),
    "someone with no role": Asker(True, frozenset(), None),
    "a disabled user's cookie": Asker(True, None, None),
    "nobody signed in": Asker(True, None, None),
    "the bootstrap client token": Asker(True, None, permissions.ALL),
    "a legacy client token": Asker(True, None, permissions.ALL),
    "a Tester's client token": Asker(True, None, TESTER),
    "a disabled owner's client token": Asker(True, None, OWNER_REFUSED),
    "an unknown owner's client token": Asker(True, None, OWNER_REFUSED),
}
# What a refusal's body says it is, one flag each. 401 is a check's missing or wrong client token.
REFUSALS = {"forbidden": 403, "token_required": 403, "admin_token_unset": 403, "client_token_required": 401,
            "owner_disabled": 403}


def expected(asker, gate, status):
    """(status, the refusal's flag or None) for asker on a route with that gate, which answers status to whoever it
    lets through."""
    kind, perms = gate[0], gate[1:]
    if kind == "open":
        return status, None
    if kind == "sign-in":
        return (status, None) if asker.configured else (404, None)
    if kind == "check":
        held = asker.token
        if held is None:
            return 401, "client_token_required"
    else:
        if not asker.configured:
            return (403, "admin_token_unset") if kind == "strict" else (status, None)
        held = asker.held
        if held is None and kind == "read":  # a client token reads where reading needs sign-in
            held = asker.token
        if held is None:
            return 403, "token_required"
    if held == OWNER_REFUSED:
        return 403, "owner_disabled"
    return (status, None) if any(p in held for p in perms) else (403, "forbidden")


@pytest.fixture
def matrix(db, monkeypatch):
    """The db fixture's grid with single sign-on, the admin token and the hub's bootstrap client token, and no
    identity provider behind it (starting a sign-in finds it unavailable)."""
    oidc_on(monkeypatch)
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)

    def unavailable(refresh=False):
        raise oidc.Unavailable("there is no identity provider in these tests")

    monkeypatch.setattr(oidc, "discovery", unavailable)
    monkeypatch.setattr(grid_app, "RECORDER", recorder.Recorder(store.snapshot, store.db_identity, interval_s=0))
    return db


def caller(path, who, monkeypatch):
    """A test client that asks as who (a key of ASKERS), and the headers it sends."""
    client, headers = grid_app.app.test_client(), {}
    people = {"an Admin": [permissions.ADMIN], "an Operator": [permissions.OPERATOR], "a Tester": [permissions.TESTER],
              "a Viewer": [permissions.VIEWER], "someone with no role": []}
    if who == "nothing configured":
        monkeypatch.delenv("GRID_OIDC_ISSUER")
        monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    elif who == "the admin token":
        headers = ADMIN
    elif who in people:
        sign_in_as(client, make_user(path, "Dana Lee", people[who]))
    elif who == "a disabled user's cookie":
        sign_in_as(client, make_user(path, "Dana Lee", [permissions.ADMIN], disabled=True))
    elif who == "the bootstrap client token":
        headers = bearer(BOOTSTRAP)
    elif who == "a legacy client token":
        headers = bearer(add_client_token(path, "legacy")[1])
    elif who == "a Tester's client token":
        headers = bearer(add_client_token(path, "tess-ci", owner=make_user(path, "Tess", [permissions.TESTER]))[1])
    elif who == "a disabled owner's client token":
        erin = make_user(path, "Erin", [permissions.TESTER], disabled=True)
        headers = bearer(add_client_token(path, "erin-ci", owner=erin)[1])
    elif who == "an unknown owner's client token":
        headers = bearer(add_client_token(path, "gone-ci", owner=999)[1])
    return client, headers


def send(client, method, path, body=None, headers=None):
    if method == "GET":
        return client.get(path, headers=headers or {})
    return client.post(path, json=body, headers=headers or {})


def refusal(r):
    """The flags of the refusal a response is, if it is one."""
    body = r.get_json(silent=True)
    return {flag for flag in REFUSALS if isinstance(body, dict) and body.get(flag)}


def route_id(route):
    return "{} {}".format(route[0], route[1])


def test_every_route_is_in_the_permission_matrix():
    """Or a new route would go without its gate being tested."""
    def pattern(rule):
        parts = re.split(r"(<[^>]+>)", rule)
        return "".join(r"-?\d+" if i % 2 else re.escape(part) for i, part in enumerate(parts))

    rules = {(method, rule.rule) for rule in grid_app.app.url_map.iter_rules() if rule.endpoint != "static"
             for method in rule.methods - {"HEAD", "OPTIONS"}}
    missing = {(method, rule) for method, rule in rules
               if not any(m == method and re.fullmatch(pattern(rule), path) for m, path, _, _, _ in ROUTES)}
    assert missing == set()


@pytest.mark.parametrize("route", ROUTES, ids=route_id)
@pytest.mark.parametrize("who", list(ASKERS))
def test_who_may_call_what(matrix, monkeypatch, who, route):
    """Each route answers each kind of caller with what its gate says: let through, or refused with exactly one of
    forbidden, token_required, admin_token_unset, client_token_required or owner_disabled."""
    method, path, body, gate, status = route
    client, headers = caller(matrix, who, monkeypatch)
    r = send(client, method, path, body, headers)
    want_status, flag = expected(ASKERS[who], gate, status)
    assert (r.status_code, refusal(r)) == (want_status, {flag} if flag else set()), r.get_data(as_text=True)


def answer(r):
    """Everything a response says, to compare two byte for byte."""
    return r.status_code, r.get_data(), r.headers.get("Content-Type"), r.headers.get("Cache-Control")


@pytest.mark.parametrize("route", [r for r in ROUTES if r[3][0] in ("grid", "strict")], ids=route_id)
def test_a_client_token_is_no_way_in_to_what_people_manage(matrix, route):
    """Only the check routes, and reads where they need sign-in, take one. Elsewhere it is never even looked at:
    one that acts as an admin gets the answer no credentials get, and a wrong one isn't a guess."""
    method, path, body, _, _ = route
    client = grid_app.app.test_client()
    _, legacy = add_client_token(matrix, "legacy")
    nobody = answer(send(client, method, path, body))
    assert nobody[0] == 403 and json.loads(nobody[1]) == SIGN_IN_TO_CONTINUE
    for token in (BOOTSTRAP, legacy, "pwgc_wrong"):
        assert answer(send(client, method, path, body, bearer(token))) == nobody, token
    assert not auth._failures


def test_only_an_admin_gets_past_admin_required(db, monkeypatch):
    """Every permission, however it is held: a custom role that ticks every box is an admin's, one that leaves out
    a single permission isn't, and the refusal is forbidden, not a request to sign in again. A client token is
    never let in. (The grid's own routes ask for the permissions they need; this one is registered here.)"""
    oidc_on(monkeypatch)
    app = flask.Flask(__name__)

    @app.get("/admins-only")
    @auth.admin_required()
    def admins_only():
        return flask.jsonify(principal=flask.g.principal.name)

    client = app.test_client()
    everything = add_role(db, "Everything", permissions.unparse(permissions.ALL))
    all_but_audit = add_role(db, "All but the audit log", permissions.unparse(permissions.ALL - {"audit.view"}))
    for name, roles in (("Tess Tester", [permissions.TESTER]), ("Otto Both", [permissions.OPERATOR, permissions.TESTER]),
                        ("Robin Hale", [all_but_audit, permissions.OPERATOR])):
        sign_in_as(client, make_user(db, name, roles))
        r = client.get("/admins-only")
        assert (r.status_code, r.get_json()) == (403, {"error": "Only an admin can do this. Ask one for the Admin role.",
                                                       "forbidden": True}), name
    sign_in_as(client, make_user(db, "Sam Hale", [everything]))
    assert client.get("/admins-only").get_json() == {"principal": "Sam Hale"}
    sign_in_as(client, make_user(db, "Ada Admin", [permissions.ADMIN]))
    assert client.get("/admins-only").get_json() == {"principal": "Ada Admin"}
    _, legacy = add_client_token(db, "legacy")
    anyone = app.test_client()
    assert anyone.get("/admins-only", headers=ADMIN).get_json() == {"principal": "Admin"}
    assert anyone.get("/admins-only", headers=bearer(legacy)).get_json() == SIGN_IN_TO_CONTINUE


# Every POST the app answers, ids filled in.
POSTS = sorted({re.sub(r"<[^>]+>", "1", rule.rule) for rule in grid_app.app.url_map.iter_rules()
                if "POST" in rule.methods})
GUARD_REFUSALS = {
    "a cross-site Origin": (dict(json={}, headers={"Origin": "https://evil.example"}), "Cross-origin request refused."),
    "Sec-Fetch-Site cross-site": (dict(json={}, headers={"Sec-Fetch-Site": "cross-site"}),
                                  "Cross-site request refused."),
    "a form body": (dict(data={"name": "x"}),
                    "Send actions from the grid page (X-Grid-Token header or a JSON body required)."),
    "an unknown Host": (dict(json={}, base_url="http://evil.example"),
                        "Actions aren't accepted through the host name 'evil.example'. Add it to "
                        "GRID_UI_ALLOWED_HOSTS to manage the grid through it."),
}


@pytest.mark.parametrize("path", POSTS)
def test_every_action_comes_from_the_grid_page(db, monkeypatch, path):
    """The same-origin guard refuses a cross-site or rebound request, or a form post, before the gate looks at who
    sends it (an Admin, signed in). Only the four check paths skip it: checks call them by the UI's compose name,
    and their client token can't be sent cross-site without a preflight."""
    assert grid_app.CHECK_PATHS == {"/api/mfa/code", "/api/signins/checkout", "/api/signins/save",
                                    "/api/signins/release"}
    oidc_on(monkeypatch)
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())
    client = grid_app.app.test_client()
    sign_in_as(client, make_user(db, "Robin Hale", [permissions.ADMIN]))
    for why, (kwargs, error) in GUARD_REFUSALS.items():
        r = client.post(path, **kwargs)
        if path in grid_app.CHECK_PATHS:
            assert r.status_code == 401 and r.get_json()["client_token_required"] is True, why
        else:
            assert (r.status_code, r.get_json()) == (403, {"error": error}), why
