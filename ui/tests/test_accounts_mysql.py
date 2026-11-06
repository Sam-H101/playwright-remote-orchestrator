"""Users, roles, sessions, sharing and the audit log against a real MySQL. Opt in with
ORCHESTRATOR_TEST_DB=<database to wipe>, plus DB_HOST/DB_PORT/DB_USER/DB_PASSWORD. The users, roles, sessions,
sharing and audit tables in that database are dropped, and the MFA, sign-in and token tables by the test that
uses them."""
import base64
import json
import os
import threading

import pytest

from ui import accounts, app as grid_app, permissions, store
from ui.tests.test_accounts import ADMIN, ADMIN_ACTOR, ISSUER, OPERATOR, TESTER, hashed, identity

TEST_DB = os.environ.get("ORCHESTRATOR_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORCHESTRATOR_TEST_DB to run against MySQL")

SEED = base64.b32encode(b"12345678901234567890").decode()
TABLES = ["users", "roles", "userRoles", "groupRoles", "userSessions", "oidcLogins", "itemOwners", "itemShares",
          "auditLog"]


def sql(statement, args=None):
    with store._session() as cur:
        cur.execute(statement, args)
        return list(cur.fetchall())


@pytest.fixture
def mysql(monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB or "")
    for name in ("GRID_OIDC_ISSUER", "GRID_OIDC_ADMINS", "GRID_SESSION_HOURS", "GRID_SESSION_IDLE_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "_schema_attempted", False)
    sql("DROP TABLE IF EXISTS " + ", ".join(TABLES))
    accounts.reset_caches()


@pytest.fixture
def at_issuer(mysql, monkeypatch):
    """mysql, with GRID_OIDC_ISSUER set to the issuer the tests' people sign in at: a session counts only there."""
    monkeypatch.setenv("GRID_OIDC_ISSUER", ISSUER)


def from_threads(count, call):
    """call() from `count` threads at once: (what they returned, what they raised)."""
    got, raised = [], []
    start = threading.Barrier(count)

    def ask():
        start.wait()
        try:
            got.append(call())
        except Exception as e:
            raised.append(e)

    threads = [threading.Thread(target=ask) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return got, raised


def test_the_schema_makes_the_tables_on_a_new_database(mysql):
    """A statement that fails is only logged (a read-only user still gets a working UI), so look at what was
    made: utf8mb4 tables, whatever the database's default, exact comparisons where people's ids and groups are
    kept, and room for 500 groups."""
    store.ensure_schema()  # the tables don't exist yet
    made = dict(sql("SELECT t.table_name, c.character_set_name FROM information_schema.tables t "
                    "JOIN information_schema.collation_character_set_applicability c ON c.collation_name = t.table_collation "
                    "WHERE t.table_schema = DATABASE()"))
    assert {name.lower(): made.get(name) for name in TABLES} == {name.lower(): "utf8mb4" for name in TABLES}
    assert len(made) == 17
    columns = {(table.lower(), column): (kind, collation) for table, column, kind, collation in sql(
        "SELECT table_name, column_name, data_type, collation_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND column_name IN ('issuer', 'subject', 'groupName', 'idpGroups', 'name')")}
    assert columns[("users", "issuer")] == columns[("users", "subject")] == ("varchar", "utf8mb4_bin")
    assert columns[("grouproles", "groupName")] == ("varchar", "utf8mb4_bin")
    assert columns[("users", "idpGroups")][0] == "mediumtext"
    assert columns[("roles", "name")][1].startswith("utf8mb4_")  # folds case and accents, as the names check does
    assert accounts.tables_problem() is None  # its lower-case names find MySQL's tables


def test_subjects_and_groups_are_compared_exactly(at_issuer):
    """MySQL's default collation would take "ABC" for "abc", and "Ops" for "ops"."""
    lower, _ = accounts.sign_in(ISSUER, identity(subject="abc", groups=["ops"]))
    upper, _ = accounts.sign_in(ISSUER, identity(subject="ABC", groups=["Ops"]))
    assert sql("SELECT COUNT(*) FROM users") == [(2,)]
    sql("INSERT INTO groupRoles (groupName, roleId, createdAt) VALUES ('Ops', -2, UTC_TIMESTAMP()), "
        "('ops', -2, UTC_TIMESTAMP()), ('ops', -4, UTC_TIMESTAMP())")
    assert [(r["name"], r["group"]) for r in accounts.session_access(lower)["roles"]] == [
        ("Operator", "ops"), ("Viewer", "ops")]
    assert [(r["name"], r["group"]) for r in accounts.session_access(upper)["roles"]] == [("Operator", "Ops")]


def test_a_user_with_500_long_non_ascii_groups_signs_in(at_issuer):
    groups = ["{:03d}".format(i) + "é" * 252 for i in range(500)]
    value, access = accounts.sign_in(ISSUER, identity(groups=groups))
    assert access["groups"] == groups
    assert accounts.session_access(value)["groups"] == groups


def test_a_first_sign_in_from_two_browsers_at_once_makes_one_user(mysql):
    store.ensure_schema()
    got, raised = from_threads(2, lambda: accounts.sign_in(ISSUER, identity(groups=["ops"])))
    assert raised == []
    assert len({access["id"] for _, access in got}) == 1
    assert sql("SELECT COUNT(*) FROM users") == [(1,)]
    assert sql("SELECT COUNT(*) FROM userSessions") == [(2,)]
    # The new user's row has the groups. If the other browser's session starts before the one that made the user,
    # it records them as added too: at worst twice, never missing.
    created = ("user.created", {"groups": ["ops"]})
    assert [(action, json.loads(details)) for action, details in sql(
        "SELECT action, details FROM auditLog WHERE action IN ('user.created', 'user.groups_changed') ORDER BY id")] in (
        [created], [created, ("user.groups_changed", {"added": ["ops"], "removed": []})])


def test_two_callbacks_with_the_same_state_take_it_once(mysql):
    accounts.start_login(hashed("browser"), hashed("state"), "n", "v", "#/mfa")
    got, raised = from_threads(2, lambda: accounts.take_login(hashed("state"), hashed("browser")))
    assert raised == []
    assert sorted(got, key=bool) == [None, {"nonce": "n", "verifier": "v", "return_to": "#/mfa", "others": False}]


def test_a_change_to_access_waits_for_the_one_in_progress(mysql):
    """GET_LOCK waits 5 s, then AccessBusy, rather than two changes deciding on the same counts."""
    store.ensure_schema()
    with store._session() as held:
        accounts._access_lock(held)
        with pytest.raises(accounts.AccessBusy):
            with store._session() as cur:
                accounts._access_lock(cur)
    with store._session() as cur:  # it went with the first connection
        accounts._access_lock(cur)


def test_purging_ended_sessions_does_not_lock_the_sessions_in_use(at_issuer):
    """By id: a DELETE by date would lock every row it scans, and the next request's lastSeenAt write, or a new
    session, would wait for it."""
    _, old = accounts.sign_in(ISSUER, identity(subject="old"))
    value, dana = accounts.sign_in(ISSUER, identity())
    sql("UPDATE userSessions SET revokedAt = UTC_TIMESTAMP() - INTERVAL 8 DAY WHERE userId = %s", (old["id"],))
    sql("UPDATE userSessions SET lastSeenAt = UTC_TIMESTAMP() - INTERVAL 2 MINUTE WHERE userId = %s", (dana["id"],))
    purging, done = threading.Event(), threading.Event()

    def purge():
        with store._session() as cur:
            accounts._purge_sessions(cur, accounts._now(cur))
            purging.set()
            done.wait(20)  # holds the purge's locks until the others are through

    def use():
        finished.append(accounts.session_access(value)["id"])
        sql("INSERT INTO userSessions (tokenHash, userId, createdAt, lastSeenAt, expiresAt) VALUES "
            "(%s, %s, UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP() + INTERVAL 1 HOUR)", (hashed("new"), dana["id"]))
        finished.append("inserted")

    finished = []
    purger = threading.Thread(target=purge)
    purger.start()
    try:
        assert purging.wait(10)
        user = threading.Thread(target=use)
        user.start()
        user.join(10)
        assert finished == [dana["id"], "inserted"]
    finally:
        done.set()
        purger.join()
    assert sql("SELECT COUNT(*) FROM userSessions") == [(2,)]


def test_a_database_user_that_cannot_make_the_tables(mysql, monkeypatch):
    """A read-only user: the UI can't make the tables, so single sign-on says why nobody can sign in. The admin
    token still gets in, and finds nobody; changing anyone's access says which tables are missing."""
    monkeypatch.setattr(store, "_schema_attempted", True)  # as if the CREATEs had been refused
    assert accounts.tables_problem() == accounts.NO_USER_TABLES
    assert accounts.user_access(1) is None
    assert accounts.session_access("anything") is None
    with pytest.raises(accounts.UsersUnavailable):
        accounts.sign_in(ISSUER, identity())
    with pytest.raises(accounts.UsersUnavailable):
        accounts.start_login(hashed("browser"), hashed("state"), "n", "v", None)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    grid = grid_app.app.test_client()
    r = grid.get("/api/admin/users", headers=ADMIN)
    assert r.status_code == 200
    assert (r.get_json()["ready"], r.get_json()["users"], r.get_json()["problem"]) == (False, [],
                                                                                       accounts.NO_USER_TABLES)
    assert [role["id"] for role in grid.get("/api/admin/roles", headers=ADMIN).get_json()["roles"]] == [-1, -2, -3, -4]
    for path, body in [("/api/admin/roles", {"name": "QA", "permissions": []}),
                       ("/api/admin/groups", {"group": "qa", "roleId": permissions.TESTER}),
                       ("/api/admin/users/1/disable", {})]:
        r = grid.post(path, json=body, headers=ADMIN)
        assert (r.status_code, r.get_json()) == (503, {"error": accounts.NO_PEOPLE_TABLES}), path


def people(*names):
    """Users with these names, ids from 1, and nothing else."""
    store.ensure_schema()
    for name in names:
        sql("INSERT INTO users (issuer, subject, name, createdAt) VALUES (%s, %s, %s, UTC_TIMESTAMP())",
            (ISSUER, name.lower(), name))


def test_two_admins_taking_each_others_admin_role_at_once_leave_one(mysql):
    """GET_LOCK lines them up, and the one that runs second finds its actor no longer an admin: worked out again
    under the lock, so it is refused (rule "actor"), and one admin is left."""
    people("Robin Hale", "Dana Lee")
    sql("INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (1, -1, UTC_TIMESTAMP()), (2, -1, UTC_TIMESTAMP())")
    pairs = [(1, 2), (2, 1)]  # (actor, the one they take Admin from)

    def take():
        actor_id, target = pairs.pop()
        actor = store.Actor(actor_id, "user {}".format(actor_id), "oidc", None, permissions.ALL)
        return accounts.take_role(target, permissions.ADMIN, actor)

    got, raised = from_threads(2, take)
    assert len(got) == 1
    assert [(type(e), str(e), e.rule) for e in raised] == [
        (store.NotPermitted, "Your roles no longer include “Manage people and roles”.", "actor")]
    assert sql("SELECT COUNT(*) FROM userRoles WHERE roleId = -1") == [(1,)]
    assert sql("SELECT action FROM auditLog") == [("user.role_removed",)]


def test_two_requests_making_the_same_role_at_once_make_one(mysql):
    store.ensure_schema()
    got, raised = from_threads(2, lambda: accounts.create_role("QA leads", None, ["mfa.add"], ADMIN_ACTOR))
    assert [role["name"] for role in got] == ["QA leads"]
    assert [(type(e), str(e)) for e in raised] == [(accounts.InvalidRole, "There is already a role called 'QA leads'.")]
    assert sql("SELECT name, permissions FROM roles") == [("QA leads", "mfa.add")]


def test_role_names_mysql_takes_for_one_another_are_taken(mysql):
    """MySQL's collation folds accents (Python's casefold doesn't): its unique key refuses the second name, and the
    answer is the one a name found taken gets."""
    store.ensure_schema()
    accounts.create_role("Résumé", None, [], ADMIN_ACTOR)
    cv = accounts.create_role("CV", None, [], ADMIN_ACTOR)["id"]
    with pytest.raises(accounts.InvalidRole) as refused:
        accounts.create_role("Resume", None, [], ADMIN_ACTOR)
    assert str(refused.value) == "There is already a role called 'Resume'."
    with pytest.raises(accounts.InvalidRole) as refused:
        accounts.update_role(cv, {"name": "resume"}, ADMIN_ACTOR)
    assert str(refused.value) == "There is already a role called 'resume'."
    assert sql("SELECT name FROM roles ORDER BY id") == [("Résumé",), ("CV",)]


def test_the_admin_page_says_when_another_change_to_access_is_in_progress(mysql, monkeypatch):
    """GET_LOCK waits 5 s for the change that holds it, then the page is told to try again."""
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    people("Dana Lee")
    with store._session() as held:
        accounts._access_lock(held)
        with pytest.raises(accounts.AccessBusy):
            accounts.give_role(1, permissions.VIEWER, ADMIN_ACTOR)
        r = grid_app.app.test_client().post("/api/admin/users/1/roles/add", json={"role": permissions.VIEWER},
                                            headers=ADMIN)
    assert (r.status_code, r.get_json()) == (409, {"error": "Another change to access is in progress. Try again."})
    assert sql("SELECT COUNT(*) FROM userRoles") == [(0,)]
    assert accounts.give_role(1, permissions.VIEWER, ADMIN_ACTOR)["user"]["roles"] == [
        {"id": permissions.VIEWER, "name": "Viewer", "source": "manual"}]


def test_the_admin_page_in_mysqls_sql(mysql, monkeypatch):
    """Listing people with a search and a role filter, a user's sheet, roles, mappings and the audit log."""
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    people("Robin Hale", "Ann 100% Sure", "Bo_sun", "Bobsun")
    sql("INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (1, -1, UTC_TIMESTAMP())")
    sql("UPDATE users SET idpGroups = '[\"qa\"]' WHERE id = 3")
    grid = grid_app.app.test_client()
    qa = grid.post("/api/admin/roles", json={"name": "QA", "permissions": ["mfa.add"]}, headers=ADMIN).get_json()
    for group in ("qa", "QA"):  # two groups: compared exactly
        r = grid.post("/api/admin/groups", json={"group": group, "roleId": qa["role"]["id"]}, headers=ADMIN)
        assert r.status_code == 201

    def names(query):
        return [u["name"] for u in grid.get("/api/admin/users?sort=name&" + query, headers=ADMIN).get_json()["users"]]

    assert names("q=0%25") == ["Ann 100% Sure"]
    assert names("q=o_s") == ["Bo_sun"]
    assert names("role=-1") == ["Robin Hale"]
    assert names("role={}".format(qa["role"]["id"])) == ["Bo_sun"]
    assert names("role=none") == ["Ann 100% Sure", "Bobsun"]
    assert grid.post("/api/admin/users/2/disable", json={}, headers=ADMIN).get_json()["user"]["disabled"] is True
    sheet = grid.get("/api/admin/users/3", headers=ADMIN).get_json()
    assert (sheet["user"]["groups"], sheet["permissions"]) == (["qa"], [{"id": "mfa.use", "via": [qa["role"]["id"]]},
                                                                        {"id": "mfa.add", "via": [qa["role"]["id"]]}])
    assert [h["action"] for h in sheet["history"]] == []
    roles = grid.get("/api/admin/roles", headers=ADMIN).get_json()["roles"]
    assert (roles[0]["members"], roles[-1]["members"], len(roles[-1]["mappings"])) == (
        {"manual": 1, "groups": 0}, {"manual": 0, "groups": 1}, 2)
    assert grid.post("/api/admin/roles/{}/delete".format(qa["role"]["id"]), json={}, headers=ADMIN).get_json() == {
        "deleted": True, "id": qa["role"]["id"], "removed": {"users": 0, "mappings": 2, "shares": 0}}
    audit = grid.get("/api/admin/audit?limit=2", headers=ADMIN).get_json()
    assert [e["action"] for e in audit["entries"]] == ["role.deleted", "user.disabled"]
    assert [e["action"] for e in grid.get("/api/admin/audit?before={}".format(audit["next"]),
                                          headers=ADMIN).get_json()["entries"]] == ["group.mapped", "group.mapped",
                                                                                     "role.created"]
    assert [e["action"] for e in grid.get("/api/admin/audit?category=group", headers=ADMIN).get_json()["entries"]] == [
        "group.mapped", "group.mapped"]


def test_what_people_see_and_use_of_each_others_items(mysql, monkeypatch):
    """The listings' filters, the owner rows and the checks' rules, in MySQL's SQL."""
    monkeypatch.setenv("GRID_SECRETS_KEY", "a-test-key-for-the-mysql-sharing-test")
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    sql("DROP TABLE IF EXISTS mfaIssues, mfaSecrets, savedSignins, accessTokens")
    bob, dana, carol = (store.Access(user_id, (permissions.TESTER,), TESTER) for user_id in (1, 2, 3))
    admin = store.Access(None, (permissions.ADMIN,), permissions.ALL)
    by_bob = store.Actor(1, "Bob Stone", "oidc", "10.0.0.1", TESTER)
    secret = store.create_mfa("intranet", SEED, owner_id=1, actor=by_bob, access=bob)["id"]
    sql("INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) "
        "VALUES ('mfa', %s, 'user', 2, UTC_TIMESTAMP())", (secret,))
    store.create_mfa("admins", SEED)
    store.issue_mfa_code("client token 'dana-ci' (Dana Lee)", name="intranet", access=dana)
    store.issue_mfa_code("GATEWAY_CLIENT_TOKEN", name="admins", access=admin)
    with pytest.raises(store.NotShared):
        store.issue_mfa_code("client token 'carol-ci' (Carol Diaz)", name="intranet", access=carol)
    assert store.peek_mfa_code(secret, access=carol) is None and store.delete_mfa(secret, carol) is False
    with pytest.raises(store.NotPermitted):
        store.delete_mfa(secret, dana)
    listing = store.list_mfa(dana)
    assert [(s["name"], s["access"], s["ownerId"]) for s in listing["secrets"]] == [("intranet", "shared", 1)]
    assert [i["name"] for i in listing["issues"]] == ["intranet"]
    assert store.list_mfa(carol)["secrets"] == [] and store.list_mfa(carol)["issues"] == []
    assert [s["name"] for s in store.list_mfa(admin)["secrets"]] == ["admins", "intranet"]

    lease = store.checkout_signin("client token 'bob-ci' (Bob Stone)", "prod-admin", access=bob, actor=by_bob)
    assert lease["status"] == "sign-in"
    assert sql("SELECT itemKind, itemId, userId FROM itemOwners ORDER BY itemKind") == [
        ("mfa", secret, 1), ("signin", lease["id"], 1)]
    store.save_signin("client token 'bob-ci' (Bob Stone)", "prod-admin", {"cookies": [], "origins": []},
                      lease_token=lease["leaseToken"], access=bob)
    for who in (carol, admin):  # an admin's check too, until it is shared with them or with the Admin role
        with pytest.raises(store.NotShared):
            store.checkout_signin("someone's check", "prod-admin", access=who)
    # Shared with Admin by someone who may do no more than Bob (not something the API lets through), it still
    # doesn't reach the admins' checks; shared by someone who may see every sign-in, it does, while Bob may still
    # do all he could then.
    sql("INSERT INTO users (issuer, subject, name, createdAt) VALUES (%s, 'bob', 'Bob Stone', UTC_TIMESTAMP())",
        (ISSUER,))
    sql("INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (1, %s, UTC_TIMESTAMP())", (permissions.TESTER,))
    sql("INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) "
        "VALUES ('signin', %s, 'role', %s, UTC_TIMESTAMP())", (lease["id"], permissions.ADMIN))
    with pytest.raises(store.NotShared):
        store.checkout_signin("GATEWAY_CLIENT_TOKEN", "prod-admin", access=admin)
    sql("UPDATE itemShares SET vouchedFor = %s WHERE itemKind = 'signin'", (permissions.unparse(TESTER),))
    assert store.checkout_signin("GATEWAY_CLIENT_TOKEN", "prod-admin", access=admin)["status"] == "saved"
    assert [s["name"] for s in store.list_signins(carol)["signins"]] == []
    (listed,) = store.list_signins(admin)["signins"]
    assert (listed["access"], listed["usable"], listed["shares"]) == (
        "shared", True, [{"kind": "role", "id": permissions.ADMIN, "vouchedFor": TESTER}])
    sql("UPDATE userRoles SET roleId = %s", (permissions.VIEWER,))
    with pytest.raises(store.NotShared):
        store.checkout_signin("GATEWAY_CLIENT_TOKEN", "prod-admin", access=admin)
    # Shared again by the admin token, the share vouches for what Bob may do now.
    operator = store.Access(4, (permissions.OPERATOR,), OPERATOR)
    accounts.share_item("signin", lease["id"], "role", permissions.OPERATOR, admin, ADMIN_ACTOR)
    assert sql("SELECT granteeId, vouchedFor FROM itemShares WHERE itemKind = 'signin' ORDER BY id") == [
        (permissions.ADMIN, permissions.unparse(TESTER)), (permissions.OPERATOR, "grid.view")]
    assert store.checkout_signin("client token 'oscar-ci' (Oscar Park)", "prod-admin", access=operator)["status"] == \
        "saved"
    monkeypatch.setattr(store, "SIGNIN_MAX_PER_OWNER", 1)
    with pytest.raises(store.SigninsFull):
        store.checkout_signin("client token 'bob-ci' (Bob Stone)", "another", access=bob)

    with pytest.raises(store.NotPermitted):
        store.create_token("bob-ci", "client", owner_id=1, access=bob)  # the grid's first client token
    store.create_token("legacy", "client", access=admin)
    made = store.create_token("bob-ci", "client", owner_id=1, access=bob, actor=by_bob)
    listing = store.list_tokens(bob)
    assert [(t["name"], t["ownerId"]) for t in listing["tokens"]] == [("bob-ci", 1)]
    assert listing["kindCounts"]["client"] == {"rows": 2, "active": 2, "revoked": 0}
    assert store.revoke_token(made["id"], carol) is None and store.delete_token(made["id"], carol) == "not_found"
    assert sql("SELECT action FROM auditLog ORDER BY id") == [("mfa.created",), ("signin.created",), ("signin.shared",),
                                                              ("token.created",)]


def test_sharing_with_someone_from_several_requests_at_once_shares_once(mysql, monkeypatch):
    """The access lock lines them up, and uq_item_share would settle it anyway: one row, recorded once."""
    monkeypatch.setenv("GRID_SECRETS_KEY", "a-test-key-for-the-mysql-sharing-test")
    sql("DROP TABLE IF EXISTS mfaIssues, mfaSecrets")
    store.ensure_schema()
    sql("INSERT INTO users (issuer, subject, name, createdAt) VALUES (%s, 'bob', 'Bob Stone', UTC_TIMESTAMP()), "
        "(%s, 'dana', 'Dana Lee', UTC_TIMESTAMP())", (ISSUER, ISSUER))
    bob = store.Access(1, (permissions.TESTER,), TESTER)
    by_bob = store.Actor(1, "Bob Stone", "oidc", "10.0.0.1", TESTER)
    secret = store.create_mfa("intranet", SEED, owner_id=1)["id"]
    got, raised = from_threads(4, lambda: accounts.share_item("mfa", secret, "user", 2, bob, by_bob))
    assert raised == []
    assert [row["shares"] for row in got] == [[{"kind": "user", "id": 2, "vouchedFor": None}]] * 4
    assert sql("SELECT itemKind, itemId, granteeKind, granteeId, sharedBy FROM itemShares") == [
        ("mfa", secret, "user", 2, "Bob Stone")]
    assert sql("SELECT action, details FROM auditLog") == [
        ("mfa.shared", '{"grantee":{"kind":"user","id":2,"name":"Dana Lee"}}')]


def test_giving_things_away_in_mysqls_sql(mysql, monkeypatch):
    """The access lock (GET_LOCK), the item's row lock (FOR UPDATE) and the actor's roles read again inside both."""
    monkeypatch.setenv("GRID_SECRETS_KEY", "a-test-key-for-the-mysql-sharing-test")
    sql("DROP TABLE IF EXISTS mfaIssues, mfaSecrets, savedSignins, accessTokens")
    store.ensure_schema()
    sql("INSERT INTO users (issuer, subject, name, createdAt) VALUES (%s, 'bob', 'Bob Stone', UTC_TIMESTAMP()), "
        "(%s, 'dana', 'Dana Lee', UTC_TIMESTAMP()), (%s, 'robin', 'Robin Hale', UTC_TIMESTAMP())", (ISSUER,) * 3)
    sql("INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (1, -3, UTC_TIMESTAMP()), (3, -1, UTC_TIMESTAMP())")
    robin = store.Access(3, (permissions.ADMIN,), permissions.ALL)
    by_robin = store.Actor(3, "Robin Hale", "oidc", "10.0.0.3", permissions.ALL)
    secret = store.create_mfa("intranet", SEED, owner_id=1)["id"]
    given = accounts.set_item_owner("mfa", secret, 2, robin, by_robin)
    assert (given["ownerId"], given["warning"]) == (2, "Dana Lee's roles don't include “Use MFA secrets”, so they "
                                                       "can't use it until they get one.")
    bob = store.Access(1, (permissions.TESTER,), TESTER)
    lease = store.checkout_signin("client token 'bob-ci' (Bob Stone)", "prod-admin", access=bob)
    sql("INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) "
        "VALUES ('signin', %s, 'user', 2, UTC_TIMESTAMP())", (lease["id"],))
    given = accounts.set_item_owner("signin", lease["id"], None, robin, by_robin)
    assert (given["ownerId"], given["shares"], given["leaseHolder"]) == (None, [], None)
    token = store.create_token("legacy", "client")
    assert accounts.set_token_owner(token["id"], 2, by_robin)["ownerId"] == 2
    with pytest.raises(accounts.NotTransferable):
        accounts.set_token_owner(token["id"], 1, by_robin)
    sql("DELETE FROM userRoles WHERE userId = 3")  # Robin is no admin any more
    with pytest.raises(store.NotPermitted, match="no longer include"):
        accounts.set_item_owner("mfa", secret, 1, robin, by_robin)
    assert sql("SELECT itemKind, userId FROM itemOwners ORDER BY itemKind") == [("mfa", 2), ("token", 2)]
    assert [(action, json.loads(details)) for action, details in sql("SELECT action, details FROM auditLog ORDER BY id")] == [
        ("mfa.owner_changed", {"from": {"id": 1, "name": "Bob Stone"}, "to": {"id": 2, "name": "Dana Lee"}}),
        ("signin.owner_changed", {"from": {"id": 1, "name": "Bob Stone"}, "to": None, "sharesRemoved": 1,
                                  "leaseCleared": True, "stateDropped": False}),
        ("token.owner_set", {"to": {"id": 2, "name": "Dana Lee"}})]


def test_two_people_deleting_the_last_two_client_tokens_at_once_leave_one(mysql, monkeypatch):
    """The grid's last client token row is for whoever has every access token to delete. Each delete locks every
    client row first, in id order, so the second waits for the first and then finds its own row the last. (Each
    stops after its locks, before it counts, until the other gets there too or a moment has passed: without those
    locks, both would count the other's row and delete their own.)"""
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    sql("DROP TABLE IF EXISTS accessTokens")
    store.ensure_schema()
    owners = (1, 2)
    tokens = [store.create_token("ci-{}".format(user_id), "client", owner_id=user_id)["id"] for user_id in owners]
    for token_id in tokens:
        store.revoke_token(token_id)
    arrived, status = threading.Barrier(2), store._token_status

    def counted_together(*args):
        try:
            arrived.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass  # the other is waiting for this one's locks, as it should
        return status(*args)

    monkeypatch.setattr(store, "_token_status", counted_together)
    pending = [(token_id, store.Access(user_id, (permissions.TESTER,), TESTER)) for token_id, user_id in zip(tokens, owners)]
    got, raised = from_threads(2, lambda: store.delete_token(*pending.pop()))
    assert got == ["deleted"]
    assert [(type(e), str(e)) for e in raised] == [(store.NotPermitted, store.LAST_CLIENT_TOKEN)]
    assert sql("SELECT COUNT(*) FROM accessTokens WHERE kind = 'client'") == [(1,)]


def test_the_directory_in_mysqls_sql(mysql):
    """What was typed is a parameter with LIKE's '%' and '_', and the escape '!' itself, escaped."""
    store.ensure_schema()
    for name in ("Ann 100% Sure", "Ann 1000 Times", "Bo_sun", "Bobsun", "Wow!!", "Wowza", "Ann Gone"):
        sql("INSERT INTO users (issuer, subject, name, createdAt, disabledAt) VALUES (%s, %s, %s, UTC_TIMESTAMP(), "
            + ("UTC_TIMESTAMP())" if name == "Ann Gone" else "NULL)"), (ISSUER, name, name))
    sql("INSERT INTO roles (name, permissions, createdAt) VALUES ('QA', 'mfa.use', UTC_TIMESTAMP())")

    def found(q):
        return [user["name"] for user in accounts.directory(q)["users"]]

    assert found("0%") == ["Ann 100% Sure"]
    assert found("o_s") == ["Bo_sun"]
    assert found("w!") == ["Wow!!"]
    assert found("%%") == found("__") == found("!%") == []
    assert sorted(found("ANN")) == ["Ann 100% Sure", "Ann 1000 Times"]
    assert [role["name"] for role in accounts.directory()["roles"]] == ["Admin", "Operator", "Tester", "Viewer", "QA"]


def test_two_owners_checks_starting_the_same_new_name_at_once_make_one_owner(mysql, monkeypatch):
    """One adds the name and owns it; the other is told it is someone else's, as it would be a moment later."""
    monkeypatch.setenv("GRID_SECRETS_KEY", "a-test-key-for-the-mysql-sharing-test")
    sql("DROP TABLE IF EXISTS savedSignins")
    store.ensure_schema()
    owners = [1, 2]

    def checkout():
        user_id = owners.pop()
        return store.checkout_signin("client token 'ci' (user {})".format(user_id), "prod-admin",
                                     access=store.Access(user_id, (permissions.TESTER,), TESTER),
                                     actor=store.Actor(user_id, "user {}".format(user_id), "client-token", None, TESTER))

    got, raised = from_threads(2, checkout)
    assert [answer["status"] for answer in got] == ["sign-in"]
    assert [type(e) for e in raised] == [store.NotShared]
    ((owner,),) = sql("SELECT userId FROM itemOwners WHERE itemKind = 'signin'")
    assert sql("SELECT id, leaseHolder FROM savedSignins") == [(got[0]["id"], "client token 'ci' (user {})".format(owner))]
    assert sql("SELECT actorId, action FROM auditLog") == [(owner, "signin.created")]
