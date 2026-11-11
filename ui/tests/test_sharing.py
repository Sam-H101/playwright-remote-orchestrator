"""Who owns MFA secrets, saved sign-ins and tokens, who they are shared with, and what checks get: store.py,
accounts.py and the API against sqlite.
Run from the repo root: python -m pytest ui/tests"""
import base64
import json
import sqlite3
import threading
import types
import urllib.parse

import pytest

from ui import accounts, app as grid_app, auth, mfa, permissions, store
from ui.tests.test_accounts import (ADMIN, BOOTSTRAP, OPERATOR, TESTER, add_client_token, add_item,  # noqa: F401
                                    add_mapping, add_role, answer, audit_rows, db, make_user, oidc_on, out_of_scope,
                                    run, sign_in_as, ts, unparsed)  # (db is a fixture)

KEY = "a-test-key-for-the-sharing-tests-only"
SEED = base64.b32encode(b"12345678901234567890").decode()
COOKIE = "a-cookie-value-for-the-sharing-tests"
STATE = {"cookies": [{"name": "sid", "value": COOKIE, "domain": "contoso.example", "path": "/"}], "origins": []}
NEWER = {"cookies": [{"name": "sid", "value": COOKIE + "-newer", "domain": "contoso.example", "path": "/"}], "origins": []}
STEP = 37037036  # a 30 s step of the RFC's times
CHECKOUT, SAVE, RELEASE, CODE = "/api/signins/checkout", "/api/signins/save", "/api/signins/release", "/api/mfa/code"
# The keys listing rows gain for who has an item.
OWNERSHIP = ("owner", "access", "canManage", "sharedWith")


@pytest.fixture
def grid(db, monkeypatch):
    """The db fixture's sqlite with single sign-on (people are signed in directly), the admin token and a secrets
    key, and no bootstrap client token unless a test sets one."""
    oidc_on(monkeypatch)
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return db


@pytest.fixture
def people(grid):
    """Bob, a Tester, owns things. Dana, a Tester too, is given them by name, and Erin through the QA role her
    group maps to. Carol is a Tester nobody shares anything with; Robin is an admin."""
    qa = add_role(grid, "QA", "grid.view mfa.add signins.add")
    add_mapping(grid, "qa", qa)
    return types.SimpleNamespace(
        path=grid, qa=qa,
        bob=make_user(grid, "Bob Stone", [permissions.TESTER], email="bob@example.com"),
        dana=make_user(grid, "Dana Lee", [permissions.TESTER], email="dana@example.com"),
        erin=make_user(grid, "Erin Wu", groups=["qa"]),
        carol=make_user(grid, "Carol Diaz", [permissions.TESTER]),
        robin=make_user(grid, "Robin Hale", [permissions.ADMIN]))


def share(path, kind, item_id, grantee_kind, grantee_id, vouched_for=None):
    """A share of the item, as the page's Share dialog records it: by someone who may see every one when vouched_for
    is what its owner could do then (a set of permissions)."""
    run(path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt, vouchedFor) "
              "VALUES (?, ?, ?, ?, ?, ?)", (kind, item_id, grantee_kind, grantee_id, ts(60), unparsed(vouched_for)))


def bobs_secret(people, name="intranet"):
    """Bob's MFA secret, shared with Dana and with the QA role. Returns its id."""
    secret_id = store.create_mfa(name, SEED, owner_id=people.bob)["id"]
    share(people.path, "mfa", secret_id, "user", people.dana)
    share(people.path, "mfa", secret_id, "role", people.qa)
    return secret_id


def bobs_signin(people, name="prod-admin", saved=True):
    """Bob's saved sign-in, shared with Dana and with the QA role, and with STATE saved by his check unless
    saved is False. Returns its id."""
    signin_id = add_item(people.path, "signin", name, owner=people.bob,
                         shares=[("user", people.dana), ("role", people.qa)])
    if saved:
        lease = store.checkout_signin("client token 'bob-ci' (Bob Stone)", name)
        store.save_signin("client token 'bob-ci' (Bob Stone)", name, STATE, lease_token=lease["leaseToken"])
    return signin_id


def caller(who):
    """How a user (by id), or the admin token ("admin token"), asks the API: ask(method, path, body=None)."""
    client = grid_app.app.test_client()
    headers = ADMIN if who == "admin token" else {}
    if who != "admin token":
        sign_in_as(client, who)

    def ask(method, path, body=None):
        if method == "GET":
            return client.get(path, headers=headers)
        return client.post(path, json={} if body is None else body, headers=headers)
    return ask


def check(token, path, **body):
    """A check's request with its client token."""
    return grid_app.app.test_client().post(path, json=body, headers={"Authorization": "Bearer " + token})


def signin_row(path, name="prod-admin"):
    ((version, saved, leased, saved_by, refreshes, rejected),) = run(
        path, "SELECT version, stateCipher IS NOT NULL, leaseHash IS NOT NULL, savedBy, refreshCount, rejectedCount "
              "FROM savedSignins WHERE name = ?", (name,))
    return {"version": version, "saved": bool(saved), "leased": bool(leased), "savedBy": saved_by,
            "refreshCount": refreshes, "rejectedCount": rejected}


def issued(path):
    return [to for (to,) in run(path, "SELECT issuedTo FROM mfaIssues ORDER BY id")]


# ---- the page: who may do what to an item by its id ---------------------------------------------

VIEWERS = ("the owner", "a grantee", "a grantee by role", "a stranger", "an admin", "the admin token")
# What each of them gets from the routes that use an item, and from those that manage it. A grantee may use it,
# never manage it; to a stranger it isn't there.
USES = {"the owner": 200, "a grantee": 200, "a grantee by role": 200, "a stranger": 404, "an admin": 200,
        "the admin token": 200}
MANAGES = dict(USES, **{"a grantee": 403, "a grantee by role": 403})


def viewer(people, who):
    return {"the owner": people.bob, "a grantee": people.dana, "a grantee by role": people.erin,
            "a stranger": people.carol, "an admin": people.robin, "the admin token": "admin token"}[who]


@pytest.mark.parametrize("who", VIEWERS)
def test_what_each_person_may_do_to_a_secret_and_a_sign_in(people, who):
    secret, signin = bobs_secret(people), bobs_signin(people)
    ask = caller(viewer(people, who))
    for method, path, verb, expected in (
            ("GET", "/api/mfa/{}/code".format(secret), None, USES[who]),
            ("POST", "/api/mfa/{}/use".format(secret), None, USES[who]),
            ("POST", "/api/signins/{}/forget".format(signin), "forget", MANAGES[who]),
            ("POST", "/api/mfa/{}/delete".format(secret), "delete", MANAGES[who]),
            ("POST", "/api/signins/{}/delete".format(signin), "delete", MANAGES[who])):
        if verb == "forget":
            # Whoever may use the secret took a code, in their name (deleting it takes the hand-outs too).
            assert issued(people.path) == ([] if USES[who] == 404 else [
                {"the owner": "Bob Stone", "a grantee": "Dana Lee", "a grantee by role": "Erin Wu",
                 "an admin": "Robin Hale", "the admin token": "Admin"}[who]])
        r = ask(method, path)
        assert r.status_code == expected, (who, path, r.get_json())
        assert r.headers["Cache-Control"] == "no-store"
        if expected == 403:
            assert r.get_json() == {"error": "Only its owner or an admin can {} it.".format(verb), "forbidden": True}
    gone = MANAGES[who] == 200
    assert run(people.path, "SELECT COUNT(*) FROM mfaSecrets") == [(0 if gone else 1,)]
    assert run(people.path, "SELECT COUNT(*) FROM savedSignins") == [(0 if gone else 1,)]
    if not gone:  # nor was its state forgotten
        assert signin_row(people.path)["saved"] is True


# Giving an item to someone else needs every MFA secret, every saved sign-in or every access token, which the gate
# asks for before the item is even looked at.
GIVES = {who: 200 if who in ("an admin", "the admin token") else 403 for who in VIEWERS}


@pytest.mark.parametrize("who", VIEWERS)
def test_what_each_person_may_do_to_who_has_an_item(people, who):
    """Sharing is managing: a grantee may use an item, never share it on. Giving it away is for admins."""
    secret, signin = bobs_secret(people), bobs_signin(people)
    token_id, _ = add_client_token(people.path, "legacy")
    ask = caller(viewer(people, who))
    carol = {"kind": "user", "id": people.carol, "name": "Carol Diaz", "canUse": True}
    for kind, item_id, everything in (("mfa", secret, "mfa.all"), ("signins", signin, "signins.all")):
        for action in ("add", "remove"):
            r = ask("POST", "/api/{}/{}/shares/{}".format(kind, item_id, action), {"user": people.carol})
            assert r.status_code == MANAGES[who], (who, kind, action, r.get_json())
            assert r.headers["Cache-Control"] == "no-store"
            if MANAGES[who] == 403:
                assert r.get_json() == {"error": "Only its owner or an admin can share it.", "forbidden": True}
            elif MANAGES[who] == 200:
                assert (carol in r.get_json()["sharedWith"]) == (action == "add")
        r = ask("POST", "/api/{}/{}/owner".format(kind, item_id), {"userId": people.dana})
        assert r.status_code == GIVES[who], (who, kind, r.get_json())
        if GIVES[who] == 403:
            assert (r.get_json()["forbidden"], r.get_json()["permission"]) == (True, everything)
    r = ask("POST", "/api/tokens/{}/owner".format(token_id), {"userId": people.dana})
    assert r.status_code == GIVES[who], r.get_json()
    if GIVES[who] == 403:
        assert r.get_json()["permission"] == "tokens.all"
    given = GIVES[who] == 200
    assert run(people.path, "SELECT itemKind, userId FROM itemOwners ORDER BY itemKind") == (
        [("mfa", people.dana), ("signin", people.dana), ("token", people.dana)] if given
        else [("mfa", people.bob), ("signin", people.bob)])
    # Nobody was left sharing with Carol; a sign-in given away lost its shares, an MFA secret kept them.
    assert run(people.path, "SELECT itemKind, granteeKind, granteeId FROM itemShares ORDER BY itemKind, id") == (
        [("mfa", "user", people.dana), ("mfa", "role", people.qa)] + ([] if given else [
            ("signin", "user", people.dana), ("signin", "role", people.qa)]))


def test_strangers_get_the_404_of_an_item_that_is_not_there(people):
    """Byte for byte: ids can be counted through, so a stranger can't tell someone's item from no item."""
    secret, signin = bobs_secret(people), bobs_signin(people)
    token_id, _ = add_client_token(people.path, "bob-ci", owner=people.bob)
    run(people.path, "UPDATE accessTokens SET revokedAt = ?", (ts(5),))
    carol = caller(people.carol)
    requests = [("GET", "/api/mfa/{}/code".format(secret), None), ("POST", "/api/mfa/{}/use".format(secret), None),
                ("POST", "/api/mfa/{}/delete".format(secret), None),
                ("POST", "/api/signins/{}/forget".format(signin), None),
                ("POST", "/api/signins/{}/delete".format(signin), None),
                ("POST", "/api/tokens/{}/revoke".format(token_id), None),
                ("POST", "/api/tokens/{}/delete".format(token_id), None)]
    for kind, item_id in (("mfa", secret), ("signins", signin)):
        for action, body in (("add", {"user": people.dana}), ("add", {"role": people.qa}),
                             ("remove", {"user": people.dana}), ("remove", {"role": 999})):
            requests.append(("POST", "/api/{}/{}/shares/{}".format(kind, item_id, action), body))
    hidden = [answer(carol(method, path, body)) for method, path, body in requests]
    assert all(status == 404 for status, _, _, _ in hidden)
    assert run(people.path, "SELECT issuedCount FROM mfaSecrets") == [(0,)] and signin_row(people.path)["saved"]
    assert run(people.path, "SELECT COUNT(*) FROM itemShares") == [(4,)]
    for table in ("mfaSecrets", "savedSignins", "accessTokens"):
        run(people.path, "DELETE FROM " + table)
    assert [answer(carol(method, path, body)) for method, path, body in requests] == hidden


def test_a_hidden_secret_that_does_not_decrypt_answers_like_any_hidden_one(people, monkeypatch):
    """Its seed is never looked at for someone who may not see it, so nothing tells it from the others."""
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-another")
    secret = store.create_mfa("old-intranet", SEED, owner_id=people.bob)["id"]
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    _, carols = add_client_token(people.path, "carol-ci", owner=people.carol)
    carol = caller(people.carol)
    for r in (carol("GET", "/api/mfa/{}/code".format(secret)), carol("POST", "/api/mfa/{}/use".format(secret))):
        assert (r.status_code, r.get_json()) == (404, {"error": "There is no MFA secret {}.".format(secret)})
    r = check(carols, CODE, name="old-intranet")
    assert (r.status_code, r.get_json()) == (403, {
        "error": "The MFA secret 'old-intranet' belongs to someone else and isn't shared with this client token's "
                 "owner.", "not_shared": True})
    # It really doesn't decrypt: its owner is told so.
    assert caller(people.bob)("GET", "/api/mfa/{}/code".format(secret)).get_json()["cannot_decrypt"] is True
    assert run(people.path, "SELECT issuedCount FROM mfaSecrets") == [(0,)]


# ---- the page: listings ------------------------------------------------------------------------


def test_listings_show_each_person_what_they_may_see(people):
    bobs_secret(people)
    bobs_signin(people)
    store.create_mfa("admins-only", SEED)
    store.issue_mfa_code("GATEWAY_CLIENT_TOKEN", name="admins-only")
    store.issue_mfa_code("client token 'bob-ci' (Bob Stone)", name="intranet")

    def listed(who, what="mfa"):
        r = caller(who)("GET", "/api/" + what)
        assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
        return r.get_json()

    bobs = listed(people.bob)
    assert [s["name"] for s in bobs["secrets"]] == ["intranet"]
    assert [i["name"] for i in bobs["issues"]] == ["intranet"]  # the hand-out log is filtered too
    (row,) = bobs["secrets"]
    assert {k: row[k] for k in OWNERSHIP} == {
        "owner": {"id": people.bob, "name": "Bob Stone", "disabled": False}, "access": "owner", "canManage": True,
        "sharedWith": [{"kind": "user", "id": people.dana, "name": "Dana Lee", "email": "dana@example.com", "canUse": True},
                       {"kind": "role", "id": people.qa, "name": "QA", "canUse": True}]}
    assert "ownerId" not in row and "shares" not in row and "usable" not in row  # MFA secrets have no stewards
    # Who it is shared with is for those who may manage it.
    for who in (people.dana, people.erin):
        (row,) = listed(who)["secrets"]
        assert {k: row[k] for k in OWNERSHIP} == {
            "owner": {"id": people.bob, "name": "Bob Stone", "disabled": False}, "access": "shared",
            "canManage": False, "sharedWith": []}
    assert listed(people.carol)["secrets"] == [] and listed(people.carol)["issues"] == []
    for who in (people.robin, "admin token"):
        everything = listed(who)
        assert [(s["name"], s["access"], s["owner"]) for s in everything["secrets"]] == [
            ("admins-only", "all", None), ("intranet", "all", {"id": people.bob, "name": "Bob Stone", "disabled": False})]
        assert [i["name"] for i in everything["issues"]] == ["intranet", "admins-only"]
        assert everything["secrets"][1]["sharedWith"] == bobs["secrets"][0]["sharedWith"]

    (row,) = listed(people.bob, "signins")["signins"]
    assert (row["access"], row["canManage"], row["usable"], len(row["sharedWith"])) == ("owner", True, True, 2)
    (row,) = listed(people.dana, "signins")["signins"]
    assert (row["access"], row["canManage"], row["usable"], row["sharedWith"]) == ("shared", False, True, [])
    # Admins see it on the page, but their checks don't use a sign-in someone else controls.
    (row,) = listed(people.robin, "signins")["signins"]
    assert (row["access"], row["canManage"], row["usable"]) == ("all", True, False)
    assert listed(people.carol, "signins")["signins"] == []
    assert COOKIE not in json.dumps(listed(people.bob, "signins"))


def test_a_new_secret_is_answered_as_it_is_listed(people):
    made = caller(people.bob)("POST", "/api/mfa", {"name": "bobs-own", "secret": SEED})
    assert made.status_code == 201
    (listed,) = caller(people.bob)("GET", "/api/mfa").get_json()["secrets"]
    assert made.get_json() == listed
    assert {k: listed[k] for k in OWNERSHIP} == {"owner": {"id": people.bob, "name": "Bob Stone", "disabled": False},
                                                 "access": "owner", "canManage": True, "sharedWith": []}
    assert run(people.path, "SELECT itemKind, itemId, userId FROM itemOwners") == [("mfa", listed["id"], people.bob)]
    # Made with the admin token, it is the admins'.
    made = caller("admin token")("POST", "/api/mfa", {"name": "admins", "secret": SEED}).get_json()
    assert {k: made[k] for k in OWNERSHIP} == {"owner": None, "access": "all", "canManage": True, "sharedWith": []}
    assert run(people.path, "SELECT COUNT(*) FROM itemOwners") == [(1,)]


def test_a_share_does_nothing_without_the_permission(people):
    """It is kept, and the answer says it does nothing (canUse), until whoever it is for gets the permission."""
    secret = bobs_secret(people)
    vic = make_user(people.path, "Vic Viewer", [permissions.VIEWER])
    bob = caller(people.bob)
    r = bob("POST", "/api/mfa/{}/shares/add".format(secret), {"user": vic})
    assert r.status_code == 200
    assert r.get_json()["sharedWith"][-1] == {"kind": "user", "id": vic, "name": "Vic Viewer", "canUse": False}
    shared = bob("POST", "/api/mfa/{}/shares/add".format(secret), {"role": permissions.VIEWER}).get_json()
    assert shared["sharedWith"][-1] == {"kind": "role", "id": permissions.VIEWER, "name": "Viewer", "canUse": False}
    (row,) = bob("GET", "/api/mfa").get_json()["secrets"]
    assert row == shared
    assert caller(vic)("GET", "/api/mfa").get_json()["forbidden"] is True
    _, vics = add_client_token(people.path, "vic-ci", owner=vic)
    r = check(vics, CODE, name="intranet")
    assert (r.status_code, r.get_json()) == (403, {"error": "This client token's owner's roles don't include “Use MFA "
                                                            "secrets”.", "forbidden": True, "permission": "mfa.use"})
    assert issued(people.path) == []


def test_a_disabled_owner_keeps_their_items_and_grantees_keep_using_them(people):
    secret = bobs_secret(people)
    run(people.path, "UPDATE users SET disabledAt = ? WHERE id = ?", (ts(5), people.bob))
    (row,) = caller(people.robin)("GET", "/api/mfa").get_json()["secrets"]
    assert row["owner"] == {"id": people.bob, "name": "Bob Stone", "disabled": True}
    assert caller(people.dana)("POST", "/api/mfa/{}/use".format(secret)).status_code == 200


def test_a_taken_name_says_only_that_to_someone_who_cannot_see_it(people):
    secret = bobs_secret(people)
    taken = (400, {"error": "The name 'intranet' is taken. Names are shared by everyone on the grid: choose another."})
    r = caller(people.carol)("POST", "/api/mfa", {"name": "intranet", "secret": SEED})
    assert (r.status_code, r.get_json()) == taken
    # Whoever sees it is told how to replace it (if they may): a grantee by name or by role, and an admin.
    for who in (people.dana, people.erin, people.robin, "admin token"):
        r = caller(who)("POST", "/api/mfa", {"name": "intranet", "secret": SEED})
        assert (r.status_code, r.get_json()) == (400, {
            "error": "There is already a secret called 'intranet'. Delete it first to replace it."}), who
    # Once it isn't shared with her, Dana is only told the name is taken.
    caller(people.bob)("POST", "/api/mfa/{}/shares/remove".format(secret), {"user": people.dana})
    r = caller(people.dana)("POST", "/api/mfa", {"name": "intranet", "secret": SEED})
    assert (r.status_code, r.get_json()) == taken


def test_delete_takes_owner_and_shares_with_it_and_forget_keeps_them(people):
    secret, signin = bobs_secret(people), bobs_signin(people)
    bob = caller(people.bob)
    assert bob("POST", "/api/signins/{}/forget".format(signin)).status_code == 200
    assert run(people.path, "SELECT COUNT(*) FROM itemOwners WHERE itemKind = 'signin'") == [(1,)]
    assert run(people.path, "SELECT COUNT(*) FROM itemShares WHERE itemKind = 'signin'") == [(2,)]
    forgotten = bob("POST", "/api/signins/{}/forget".format(signin)).get_json()["signin"]
    assert (forgotten["access"], forgotten["canManage"], forgotten["usable"]) == ("owner", True, True)
    assert "ownerId" not in forgotten and len(forgotten["sharedWith"]) == 2
    assert bob("POST", "/api/mfa/{}/delete".format(secret)).get_json() == {"deleted": True, "id": secret}
    assert bob("POST", "/api/signins/{}/delete".format(signin)).get_json() == {"deleted": True, "id": signin}
    assert run(people.path, "SELECT COUNT(*) FROM itemOwners") == run(people.path, "SELECT COUNT(*) FROM itemShares") == [(0,)]
    # A namesake added later starts unshared, owned by whoever adds it.
    made = caller(people.dana)("POST", "/api/mfa", {"name": "intranet", "secret": SEED}).get_json()
    assert (made["owner"]["id"], made["sharedWith"]) == (people.dana, [])
    assert caller(people.erin)("GET", "/api/mfa").get_json()["secrets"] == []


def test_what_people_change_on_the_page_is_in_the_audit_log(people):
    bob = caller(people.bob)
    secret = bob("POST", "/api/mfa", {"name": "intranet", "secret": SEED}).get_json()["id"]
    bob("POST", "/api/mfa/{}/use".format(secret))
    bob("GET", "/api/mfa/{}/code".format(secret))
    bob("GET", "/api/mfa/{}/code".format(secret))  # the page asks again every window: written once
    caller(people.robin)("GET", "/api/mfa/{}/code".format(secret))
    signin = bobs_signin(people)
    bob("POST", "/api/signins/{}/forget".format(signin))
    bob("POST", "/api/signins/{}/delete".format(signin))
    bob("POST", "/api/mfa/{}/delete".format(secret))
    rows = [(actor, name, method, action, kind, target_id, target, details)
            for actor, name, method, _, action, kind, target_id, target, details in audit_rows(people.path)]
    assert rows == [
        (people.bob, "Bob Stone", "oidc", "mfa.created", "mfa", secret, "intranet", None),
        (people.bob, "Bob Stone", "oidc", "mfa.code_used", "mfa", secret, "intranet", None),
        (people.bob, "Bob Stone", "oidc", "mfa.code_viewed", "mfa", secret, "intranet", None),
        (people.robin, "Robin Hale", "oidc", "mfa.code_viewed", "mfa", secret, "intranet", None),
        (people.bob, "Bob Stone", "oidc", "signin.forgotten", "signin", signin, "prod-admin", None),
        (people.bob, "Bob Stone", "oidc", "signin.deleted", "signin", signin, "prod-admin", None),
        (people.bob, "Bob Stone", "oidc", "mfa.deleted", "mfa", secret, "intranet", None),
    ]
    assert {address for _, _, _, address, *_ in audit_rows(people.path)} == {"127.0.0.1"}
    assert SEED not in json.dumps(audit_rows(people.path))


def test_a_code_viewed_is_recorded_at_most_every_few_minutes(people, monkeypatch):
    secret = bobs_secret(people)
    clock = [1000.0]
    monkeypatch.setattr(grid_app, "time", types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0.0))
    dana = caller(people.dana)
    for seconds in (0, 299, 1):
        clock[0] += seconds
        assert dana("GET", "/api/mfa/{}/code".format(secret)).status_code == 200
    assert [row[4] for row in audit_rows(people.path)] == ["mfa.code_viewed", "mfa.code_viewed"]
    # Never at the cost of the code: a failed write is only logged.
    def broken(*args):
        raise store.DatabaseUnavailable("the audit log is full")

    monkeypatch.setattr(store, "_audit", broken)
    clock[0] += 300
    assert dana("GET", "/api/mfa/{}/code".format(secret)).status_code == 200


# ---- the page: sharing ---------------------------------------------------------------------------


def details_of(path, action):
    return [(row[0], row[6], row[7], json.loads(row[8])) for row in audit_rows(path) if row[4] == action]


def test_sharing_a_secret_with_a_person_and_with_a_role(people):
    """Each change is answered with the row as it is listed now. Sharing it again, or stopping twice, changes
    nothing and records nothing."""
    secret = store.create_mfa("intranet", SEED, owner_id=people.bob)["id"]
    bob = caller(people.bob)
    path = "/api/mfa/{}/shares/".format(secret)
    dana = {"kind": "user", "id": people.dana, "name": "Dana Lee", "email": "dana@example.com", "canUse": True}
    qa = {"kind": "role", "id": people.qa, "name": "QA", "canUse": True}
    r = bob("POST", path + "add", {"user": people.dana})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.get_json()["sharedWith"] == [dana]
    assert bob("POST", path + "add", {"role": people.qa}).get_json()["sharedWith"] == [dana, qa]
    again = bob("POST", path + "add", {"user": people.dana}).get_json()
    assert again["sharedWith"] == [dana, qa]
    (listed,) = bob("GET", "/api/mfa").get_json()["secrets"]
    assert again == listed
    # Erin has it through the role her identity provider's group maps to.
    erin = caller(people.erin)
    assert [s["name"] for s in erin("GET", "/api/mfa").get_json()["secrets"]] == ["intranet"]
    assert bob("POST", path + "remove", {"role": people.qa}).get_json()["sharedWith"] == [dana]
    assert bob("POST", path + "remove", {"role": people.qa}).get_json()["sharedWith"] == [dana]
    assert erin("GET", "/api/mfa").get_json()["secrets"] == []
    assert run(people.path, "SELECT granteeKind, granteeId, sharedBy FROM itemShares") == [("user", people.dana, "Bob Stone")]
    shared = [(people.bob, secret, "intranet", {"grantee": {"kind": "user", "id": people.dana, "name": "Dana Lee"}}),
              (people.bob, secret, "intranet", {"grantee": {"kind": "role", "id": people.qa, "name": "QA"}})]
    assert details_of(people.path, "mfa.shared") == shared
    assert details_of(people.path, "mfa.unshared") == shared[1:]


@pytest.mark.parametrize("kind", ["mfa", "signins"])
def test_whom_an_item_cannot_be_shared_with(people, kind):
    item = bobs_secret(people) if kind == "mfa" else bobs_signin(people, saved=False)
    vic = make_user(people.path, "Vic Viewer", [permissions.VIEWER], disabled=True)
    path = "/api/{}/{}/shares/add".format(kind, item)
    bob, robin = caller(people.bob), caller(people.robin)
    send = 'Send {"user": <id>} or {"role": <id>}.'
    for ask, body, error in (
            (bob, {"user": people.bob}, "You can't share it with yourself."),
            (robin, {"user": people.bob}, "It's Bob Stone's already: owners always have it."),
            # By their id: the directory leaves disabled people out, and this mustn't name them either.
            (bob, {"user": vic}, "User {} is disabled.".format(vic)),
            (bob, {"user": 999}, "There is no user 999."),
            (bob, {"user": 2 ** 40}, "There is no user 1099511627776."),
            (bob, {"role": 999}, "There is no role 999."),
            (bob, {"role": 0}, "There is no role 0."),
            (bob, {"role": -5}, "There is no role -5."),
            (bob, {}, send), (bob, {"user": people.dana, "role": people.qa}, send),
            (bob, {"user": str(people.dana)}, 'Send {"user": <id>}.'), (bob, {"user": True}, 'Send {"user": <id>}.'),
            (bob, {"role": None}, 'Send {"role": <id>}.'), (bob, [people.dana], "Send a JSON object.")):
        r = ask("POST", path, body)
        assert (r.status_code, r.get_json()) == (400, {"error": error}), body
        assert r.headers["Cache-Control"] == "no-store"
    r = robin("POST", path, {"role": permissions.ADMIN})
    if kind == "mfa":
        assert (r.status_code, r.get_json()) == (400, {"error": "Admins see every MFA secret already."})
    else:
        # Seeing every sign-in doesn't make admins' checks use one; an admin sharing it with the Admin role does.
        assert r.get_json()["sharedWith"][-1] == {"kind": "role", "id": permissions.ADMIN, "name": "Admin",
                                                  "canUse": True}
    assert run(people.path, "SELECT COUNT(*) FROM itemShares") == [(2 if kind == "mfa" else 3,)]
    assert details_of(people.path, "mfa.shared") == []


def test_a_sign_in_is_shared_only_with_people_who_have_no_more_than_its_sharer(people, monkeypatch):
    """Their checks would run as whatever account its owner's checks saved: a Tester who takes a name the admins'
    checks use, and saves an account of his own under it, can't push it on to them (they are told it isn't
    theirs instead). Someone who sees every sign-in may share it with anyone, the Admin role too. An MFA secret
    has no such rule: a code for the wrong seed signs nobody in."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    lease = check(bobs, CHECKOUT, name="prod-admin").get_json()
    assert check(bobs, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"]).get_json()["saved"]
    oscar = make_user(people.path, "Oscar Park", [permissions.OPERATOR])
    bob, path = caller(people.bob), "/api/signins/{}/shares/add".format(lease["id"])
    for body, who in (({"role": permissions.ADMIN}, "the role “Admin”"), ({"user": people.robin}, "Robin Hale"),
                      ({"role": permissions.OPERATOR}, "the role “Operator”"), ({"user": oscar}, "Oscar Park")):
        r = bob("POST", path, body)
        assert (r.status_code, r.get_json()) == (403, {"error": accounts.SIGNIN_BEYOND.format(who),
                                                       "forbidden": True}), body
        assert r.headers["Cache-Control"] == "no-store"
    assert "Every saved sign-in" in accounts.SIGNIN_BEYOND
    assert run(people.path, "SELECT COUNT(*) FROM itemShares") == [(0,)]
    r = check(BOOTSTRAP, CHECKOUT, name="prod-admin")
    assert (r.status_code, r.get_json()["not_shared"]) == (403, True)
    assert details_of(people.path, "access.denied") == [(people.bob, None, None, {
        "route": "POST " + path, "rule": "grantee", "permission": "grid.operate"})]
    # People and roles with no more than his own: other Testers, and QA.
    for body in ({"user": people.dana}, {"role": people.qa}, {"role": permissions.TESTER}):
        assert bob("POST", path, body).status_code == 200, body
    assert check(BOOTSTRAP, CHECKOUT, name="prod-admin").get_json()["not_shared"] is True
    # An admin may opt the admins' checks in.
    assert caller(people.robin)("POST", path, {"role": permissions.ADMIN}).status_code == 200
    assert check(BOOTSTRAP, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    secret = store.create_mfa("intranet", SEED, owner_id=people.bob)["id"]
    assert bob("POST", "/api/mfa/{}/shares/add".format(secret), {"user": people.robin}).status_code == 200


def test_a_sign_in_shared_with_a_role_an_admin_also_has_never_reaches_the_admins_checks(people, monkeypatch):
    """Bob may share his sign-in with Viewer, which the '*' mapping gives everyone, and with Tester, which an admin
    may have too: neither puts the account he saved on an admin's checks. It reaches those who may do no more than
    he may: Carol, a Tester and a Viewer through '*'."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    add_mapping(people.path, "*", permissions.VIEWER)
    run(people.path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)",
        (people.robin, permissions.TESTER, ts(60)))
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    _, carols = add_client_token(people.path, "carol-ci", owner=people.carol)
    lease = check(bobs, CHECKOUT, name="prod-admin").get_json()
    assert check(bobs, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"]).get_json()["saved"]
    bob, path = caller(people.bob), "/api/signins/{}/shares/add".format(lease["id"])
    r = bob("POST", path, {"role": permissions.VIEWER})
    assert r.status_code == 200
    assert r.get_json()["sharedWith"] == [{"kind": "role", "id": permissions.VIEWER, "name": "Viewer", "canUse": False}]
    assert bob("POST", path, {"role": permissions.TESTER}).status_code == 200
    assert run(people.path, "SELECT vouchedFor FROM itemShares") == [(None,), (None,)]
    for token in (robins, BOOTSTRAP):
        for route, body in ((CHECKOUT, {}), (SAVE, {"state": NEWER, "baseVersion": 1, "id": lease["id"]}),
                            (RELEASE, {"leaseToken": "x"})):
            r = check(token, route, name="prod-admin", **body)
            assert (r.status_code, r.get_json()["not_shared"]) == (403, True), route
    assert check(carols, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    (row,) = caller(people.robin)("GET", "/api/signins").get_json()["signins"]
    assert (row["access"], row["usable"]) == ("shared", False)
    (row,) = caller(people.carol)("GET", "/api/signins").get_json()["signins"]
    assert (row["access"], row["usable"]) == ("shared", True)
    assert signin_row(people.path)["version"] == 1


def test_a_sign_in_stops_reaching_whoever_is_given_more_than_its_owner(people):
    """Dana is made an Operator after Bob shared his sign-in with her, and QA is given more than he has: their
    checks stop running as his account, and the Share dialog says why. A share from someone who may see every
    sign-in reaches them whatever they have, so Robin shares it with them again."""
    signin = bobs_signin(people)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    _, erins = add_client_token(people.path, "erin-ci", owner=people.erin)
    for token in (danas, erins):
        assert check(token, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    robin = caller(people.robin)
    assert robin("POST", "/api/admin/users/{}/roles/add".format(people.dana),
                 {"role": permissions.OPERATOR}).status_code == 200
    assert robin("POST", "/api/admin/roles/{}".format(people.qa),
                 {"permissions": ["grid.operate", "mfa.add", "signins.add"]}).status_code == 200
    for token in (danas, erins):
        r = check(token, CHECKOUT, name="prod-admin")
        assert (r.status_code, r.get_json()["not_shared"]) == (403, True)
    (row,) = caller(people.bob)("GET", "/api/signins").get_json()["signins"]
    assert row["sharedWith"] == [
        {"kind": "user", "id": people.dana, "name": "Dana Lee", "email": "dana@example.com", "canUse": False,
         "outranksOwner": True},
        {"kind": "role", "id": people.qa, "name": "QA", "canUse": False, "outranksOwner": True}]
    (row,) = caller(people.dana)("GET", "/api/signins").get_json()["signins"]
    assert (row["access"], row["usable"]) == ("shared", False)
    path = "/api/signins/{}/shares/".format(signin)
    for body in ({"user": people.dana}, {"role": people.qa}):
        assert robin("POST", path + "remove", body).status_code == 200
        assert robin("POST", path + "add", body).status_code == 200
    assert run(people.path, "SELECT granteeKind, vouchedFor, sharedBy FROM itemShares ORDER BY id") == [
        ("user", permissions.unparse(TESTER), "Robin Hale"), ("role", permissions.unparse(TESTER), "Robin Hale")]
    for token in (danas, erins):
        assert check(token, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    (row,) = caller(people.bob)("GET", "/api/signins").get_json()["signins"]
    assert [(g["name"], g["canUse"], "outranksOwner" in g) for g in row["sharedWith"]] == [
        ("Dana Lee", True, False), ("QA", True, False)]


def make_tester(robin, user_id, role_id):
    """Robin makes an admin or an Operator (role_id) a Tester instead."""
    assert robin("POST", "/api/admin/users/{}/roles/add".format(user_id), {"role": permissions.TESTER}).status_code == 200
    assert robin("POST", "/api/admin/users/{}/roles/remove".format(user_id), {"role": role_id}).status_code == 200


def test_a_vouch_for_an_admins_sign_in_lapses_once_they_are_no_admin(people, monkeypatch):
    """Sam, an admin, shares his own sign-in with the Admin role: he may see every sign-in, so it reaches every
    admin's checks. Made a Tester, he forgets it and his check saves an account of his choosing: that never
    reaches the admins' checks, and the Share dialog says why. An admin who shares it again vouches for what Sam's
    checks save now, and it reaches them again."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    sam = make_user(people.path, "Sam Ames", [permissions.ADMIN], email="sam@example.com")
    _, sams = add_client_token(people.path, "sam-ci", owner=sam)
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    lease = check(sams, CHECKOUT, name="prod-admin").get_json()
    assert check(sams, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"]).get_json()["saved"]
    path, robin = "/api/signins/{}/".format(lease["id"]), caller(people.robin)
    assert caller(sam)("POST", path + "shares/add", {"role": permissions.ADMIN}).status_code == 200
    assert run(people.path, "SELECT vouchedFor FROM itemShares") == [(permissions.unparse(permissions.ALL),)]
    for token in (robins, BOOTSTRAP):
        assert check(token, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    make_tester(robin, sam, permissions.ADMIN)
    assert caller(sam)("POST", path + "forget").status_code == 200
    lease = check(sams, CHECKOUT, name="prod-admin").get_json()
    assert check(sams, SAVE, name="prod-admin", state=NEWER, leaseToken=lease["leaseToken"]).get_json()["saved"]
    for token in (robins, BOOTSTRAP):
        r = check(token, CHECKOUT, name="prod-admin")
        assert (r.status_code, r.get_json()["not_shared"]) == (403, True)
    (row,) = robin("GET", "/api/signins").get_json()["signins"]
    assert (row["usable"], row["sharedWith"]) == (False, [{"kind": "role", "id": permissions.ADMIN, "name": "Admin",
                                                           "canUse": False, "outranksOwner": True, "vouchLapsed": True}])
    assert robin("POST", path + "shares/remove", {"role": permissions.ADMIN}).status_code == 200
    r = robin("POST", path + "shares/add", {"role": permissions.ADMIN})
    assert (r.get_json()["usable"], r.get_json()["sharedWith"]) == (
        True, [{"kind": "role", "id": permissions.ADMIN, "name": "Admin", "canUse": True}])
    assert run(people.path, "SELECT vouchedFor, sharedBy FROM itemShares") == [(permissions.unparse(TESTER), "Robin Hale")]
    for token in (robins, BOOTSTRAP):
        assert check(token, CHECKOUT, name="prod-admin").get_json()["state"] == NEWER


def test_a_vouch_lasts_while_its_owner_may_do_all_they_could_then(people):
    """Robin shares Dana's sign-in with Operator while she is one (it needed no vouch). Oscar, an Operator, gets
    what her checks save, also once she is given more; once she is a Tester again, he doesn't, and the Share
    dialog says why."""
    robin = caller(people.robin)
    assert robin("POST", "/api/admin/users/{}/roles/add".format(people.dana),
                 {"role": permissions.OPERATOR}).status_code == 200
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    _, oscars = add_client_token(people.path, "oscar-ci", owner=make_user(people.path, "Oscar Park", [permissions.OPERATOR]))
    lease = check(danas, CHECKOUT, name="danas-site").get_json()
    assert check(danas, SAVE, name="danas-site", state=STATE, leaseToken=lease["leaseToken"]).get_json()["saved"]
    path = "/api/signins/{}/shares/add".format(lease["id"])
    assert robin("POST", path, {"role": permissions.OPERATOR}).get_json()["sharedWith"] == [
        {"kind": "role", "id": permissions.OPERATOR, "name": "Operator", "canUse": True}]
    assert run(people.path, "SELECT vouchedFor FROM itemShares") == [(permissions.unparse(OPERATOR),)]
    assert check(oscars, CHECKOUT, name="danas-site").get_json()["state"] == STATE
    auditors = add_role(people.path, "Auditors", "audit.view")
    assert robin("POST", "/api/admin/users/{}/roles/add".format(people.dana), {"role": auditors}).status_code == 200
    assert check(oscars, CHECKOUT, name="danas-site").get_json()["state"] == STATE
    assert robin("POST", "/api/admin/users/{}/roles/remove".format(people.dana),
                 {"role": permissions.OPERATOR}).status_code == 200
    assert check(danas, SAVE, name="danas-site", state=NEWER, baseVersion=1, id=lease["id"]).get_json()["saved"]
    r = check(oscars, CHECKOUT, name="danas-site")
    assert (r.status_code, r.get_json()["not_shared"]) == (403, True)
    (row,) = caller(people.dana)("GET", "/api/signins").get_json()["signins"]
    assert row["sharedWith"] == [{"kind": "role", "id": permissions.OPERATOR, "name": "Operator", "canUse": False,
                                  "outranksOwner": True, "vouchLapsed": True}]


def test_a_role_share_that_reaches_someone_counts_for_their_own_share(people):
    """Bob shared his sign-in with Dana, who was then made an Operator: his share no longer reaches her. Robin
    shares it with Operator, which reaches her: the dialog says she may use it, as her checks do."""
    signin = bobs_signin(people)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    robin = caller(people.robin)
    assert robin("POST", "/api/admin/users/{}/roles/add".format(people.dana),
                 {"role": permissions.OPERATOR}).status_code == 200
    assert check(danas, CHECKOUT, name="prod-admin").status_code == 403
    (dana, _) = robin("GET", "/api/signins").get_json()["signins"][0]["sharedWith"]
    assert (dana["canUse"], dana["outranksOwner"]) == (False, True)
    r = robin("POST", "/api/signins/{}/shares/add".format(signin), {"role": permissions.OPERATOR})
    assert r.get_json()["sharedWith"] == [
        {"kind": "user", "id": people.dana, "name": "Dana Lee", "email": "dana@example.com", "canUse": True},
        {"kind": "role", "id": people.qa, "name": "QA", "canUse": True},
        {"kind": "role", "id": permissions.OPERATOR, "name": "Operator", "canUse": True}]
    assert check(danas, CHECKOUT, name="prod-admin").get_json()["state"] == STATE


def test_who_may_not_use_sign_ins_at_all_is_told_that_first(people):
    """Vic and the Runners role may run the grid, which Bob may not, but not use saved sign-ins: whoever shared it,
    what they lack is the permission to, so the dialog says that rather than that Bob may do less."""
    runners = add_role(people.path, "Runners", "grid.operate")
    vic = make_user(people.path, "Vic Ray", [runners])
    signin = bobs_signin(people)
    share(people.path, "signin", signin, "user", vic)
    share(people.path, "signin", signin, "role", runners)
    (row,) = caller(people.bob)("GET", "/api/signins").get_json()["signins"]
    assert row["sharedWith"][2:] == [{"kind": "user", "id": vic, "name": "Vic Ray", "canUse": False},
                                     {"kind": "role", "id": runners, "name": "Runners", "canUse": False}]


def test_a_check_that_stops_being_a_steward_while_it_signs_in_saves_nothing(people):
    """Sid's role lets him see every sign-in, so his check signs in for the admins' one, which is shared with
    Tester, a role he keeps. The role loses that permission before his check saves: nothing is saved, and the
    lease ends, so the next steward's check signs in at once rather than wait for it."""
    managers = add_role(people.path, "Sign-in managers", "grid.view tokens.own signins.all")
    sid = make_user(people.path, "Sid Park", [permissions.TESTER, managers])
    signin = add_item(people.path, "signin", "shared-admin", shares=[("role", permissions.TESTER)])
    _, sids = add_client_token(people.path, "sid-ci", owner=sid)
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    lease = check(sids, CHECKOUT, name="shared-admin").get_json()
    assert (lease["status"], lease["id"]) == ("sign-in", signin)
    assert caller(people.robin)("POST", "/api/admin/roles/{}".format(managers),
                                {"permissions": ["grid.view", "tokens.own"]}).status_code == 200
    r = check(sids, SAVE, name="shared-admin", state=NEWER, leaseToken=lease["leaseToken"])
    assert (r.status_code, r.get_json()) == (409, {
        "error": "lease_lost", "message": "Checks using this client token may no longer sign in fresh for 'shared-admin', "
                                          "so its lease ended. Nothing was saved."})
    assert signin_row(people.path, "shared-admin") == {"version": 0, "saved": False, "leased": False, "savedBy": None,
                                                       "refreshCount": 0, "rejectedCount": 0}
    assert check(robins, CHECKOUT, name="shared-admin").get_json()["status"] == "sign-in"


@pytest.mark.parametrize("ends", ["save", "release"])
def test_a_check_that_may_no_longer_use_a_sign_in_still_ends_its_lease(people, ends):
    """The same with the admins' sign-in shared with nobody: once the role loses that permission, Sid's check may
    not use it at all. Its save is refused as anyone's it isn't shared with, and its release is still taken:
    either way its lease ends, so Robin's check signs in at once."""
    managers = add_role(people.path, "Sign-in managers", "grid.view tokens.own signins.all")
    sid = make_user(people.path, "Sid Park", [permissions.TESTER, managers])
    add_item(people.path, "signin", "plain-admin")
    _, sids = add_client_token(people.path, "sid-ci", owner=sid)
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    lease = check(sids, CHECKOUT, name="plain-admin").get_json()
    assert lease["status"] == "sign-in"
    assert caller(people.robin)("POST", "/api/admin/roles/{}".format(managers),
                                {"permissions": ["grid.view", "tokens.own"]}).status_code == 200
    if ends == "save":
        r = check(sids, SAVE, name="plain-admin", state=NEWER, leaseToken=lease["leaseToken"])
        assert (r.status_code, r.get_json()["not_shared"]) == (403, True)
    else:
        r = check(sids, RELEASE, name="plain-admin", leaseToken=lease["leaseToken"])
        assert (r.status_code, r.get_json()) == (200, {"released": True})
    assert signin_row(people.path, "plain-admin") == {"version": 0, "saved": False, "leased": False, "savedBy": None,
                                                      "refreshCount": 0, "rejectedCount": 0}
    assert check(robins, CHECKOUT, name="plain-admin").get_json()["status"] == "sign-in"
    # A lease it no longer holds is anyone's it isn't shared with.
    r = check(sids, RELEASE, name="plain-admin", leaseToken=lease["leaseToken"])
    assert (r.status_code, r.get_json()["not_shared"]) == (403, True)


def statements(monkeypatch, ask):
    """How many SQL statements ask() runs, on any connection it opens."""
    seen, connect = [], sqlite3.connect

    def traced(*args, **kwargs):
        con = connect(*args, **kwargs)
        con.set_trace_callback(seen.append)
        return con

    with monkeypatch.context() as patched:
        patched.setattr(sqlite3, "connect", traced)
        ask()
    return len(seen)


def test_listing_sign_ins_asks_as_much_however_many_people_own_them(people, monkeypatch):
    """What each owner may do, and each person it is shared with, is worked out for all of them at once: the page
    asks for the listing every few seconds. Dana sees sign-ins shared with her; Robin, who manages them all, who
    has each."""
    dana, robin = caller(people.dana), caller(people.robin)

    def owned_by(*letters):
        for letter in letters:
            owner = make_user(people.path, "Owner " + letter, [permissions.TESTER])
            add_item(people.path, "signin", "site-" + letter, owner=owner,
                     shares=[("user", people.dana), ("user", people.erin), ("role", people.qa)])

    def counted(ask, listed):
        assert len(ask("GET", "/api/signins").get_json()["signins"]) == listed
        return statements(monkeypatch, lambda: ask("GET", "/api/signins"))

    owned_by("a")
    few = counted(dana, 1), counted(robin, 1)
    owned_by("b", "c", "d", "e", "f")
    assert (counted(dana, 6), counted(robin, 6)) == few
    rows = dana("GET", "/api/signins").get_json()["signins"]
    assert {(row["access"], row["usable"]) for row in rows} == {("shared", True)}


def test_a_sign_in_given_to_someone_else_loses_its_shares_and_any_sign_in_under_way(people):
    """Its new owner decides who has it, and nobody waits for the sign-in the old owner's check started: that
    check's save is refused, and the new owner's check signs in at once."""
    signin = bobs_signin(people, saved=False)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    lease = check(bobs, CHECKOUT, name="prod-admin").get_json()
    assert lease["status"] == "sign-in"
    r = caller(people.robin)("POST", "/api/signins/{}/owner".format(signin), {"userId": people.dana})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    given = r.get_json()
    assert {k: given[k] for k in OWNERSHIP} == {"owner": {"id": people.dana, "name": "Dana Lee", "disabled": False},
                                                "access": "all", "canManage": True, "sharedWith": []}
    assert (given["leaseHolder"], given["usable"], "warning" in given) == (None, False, False)
    assert run(people.path, "SELECT COUNT(*) FROM itemShares") == [(0,)]
    r = check(bobs, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"])
    assert (r.status_code, r.get_json()["not_shared"]) == (403, True)  # it isn't his any more
    assert check(danas, CHECKOUT, name="prod-admin").get_json()["status"] == "sign-in"
    # Shared back with him, his check still can't save what it signed in with: its lease went with the sign-in.
    caller(people.dana)("POST", "/api/signins/{}/shares/add".format(signin), {"user": people.bob})
    r = check(bobs, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"])
    assert (r.status_code, r.get_json()["error"]) == (409, "lease_lost")
    assert signin_row(people.path)["saved"] is False
    assert details_of(people.path, "signin.owner_changed") == [(people.robin, signin, "prod-admin", {
        "from": {"id": people.bob, "name": "Bob Stone"}, "to": {"id": people.dana, "name": "Dana Lee"},
        "sharesRemoved": 2, "leaseCleared": True, "stateDropped": False})]


@pytest.mark.parametrize("to", ["Admins", "Dana"])
def test_a_sign_in_given_away_keeps_nothing_its_old_owners_checks_saved(people, monkeypatch, to):
    """What is saved is an account the old owner's checks signed in as. A name squatted to catch the admins'
    checks, given to the admins to set that right, would otherwise catch them: the new owner's checks sign in
    afresh, whether it is given away alone or with everything its owner has."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    signin = bobs_signin(people)
    bobs_signin(people, "staging")
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    token, user_id = (BOOTSTRAP, None) if to == "Admins" else (danas, people.dana)
    # Not the admins' checks' to use at all; Dana's to reuse, as it is shared with her.
    before = check(token, CHECKOUT, name="prod-admin").get_json()
    assert (before.get("not_shared"), before.get("state")) == ((True, None) if to == "Admins" else (None, STATE))
    robin = caller(people.robin)
    given = robin("POST", "/api/signins/{}/owner".format(signin), {"userId": user_id}).get_json()
    assert (given["saved"], given["version"], given["expiresAt"]) == (False, 2, None)
    assert check(token, CHECKOUT, name="prod-admin").get_json()["status"] == "sign-in"
    (details,) = [d for (_, _, _, d) in details_of(people.path, "signin.owner_changed")]
    assert (details["sharesRemoved"], details["stateDropped"]) == (2, True)
    moved = robin("POST", "/api/admin/users/{}/move-items".format(people.bob), {"toUserId": user_id}).get_json()
    assert moved == {"moved": {"mfa": 0, "signins": 1}, "sharesRemoved": 2, "leasesCleared": 0, "statesDropped": 1}
    assert check(token, CHECKOUT, name="staging").get_json()["status"] == "sign-in"
    assert details_of(people.path, "user.items_moved")[0][3]["statesDropped"] == 1


def test_an_mfa_secret_given_to_someone_else_keeps_its_shares(people):
    secret = bobs_secret(people)
    robin = caller(people.robin)
    given = robin("POST", "/api/mfa/{}/owner".format(secret), {"userId": people.carol}).get_json()
    assert given["owner"] == {"id": people.carol, "name": "Carol Diaz", "disabled": False}
    assert [s["name"] for s in given["sharedWith"]] == ["Dana Lee", "QA"]
    assert [s["access"] for s in caller(people.carol)("GET", "/api/mfa").get_json()["secrets"]] == ["owner"]
    assert caller(people.bob)("GET", "/api/mfa").get_json()["secrets"] == []
    assert caller(people.dana)("POST", "/api/mfa/{}/use".format(secret)).status_code == 200
    # Given to the admins, it has no owner row at all, like a secret added with the admin token.
    given = robin("POST", "/api/mfa/{}/owner".format(secret), {"userId": None}).get_json()
    assert (given["owner"], given["access"], len(given["sharedWith"])) == (None, "all", 2)
    assert run(people.path, "SELECT COUNT(*) FROM itemOwners") == [(0,)]
    # Given to whoever has it already, nothing changes and nothing is recorded.
    assert robin("POST", "/api/mfa/{}/owner".format(secret), {"userId": None}).get_json() == given
    assert details_of(people.path, "mfa.owner_changed") == [
        (people.robin, secret, "intranet", {"from": {"id": people.bob, "name": "Bob Stone"},
                                            "to": {"id": people.carol, "name": "Carol Diaz"}}),
        (people.robin, secret, "intranet", {"from": {"id": people.carol, "name": "Carol Diaz"}, "to": None})]


@pytest.mark.parametrize("kind, permission", [("mfa", "Use MFA secrets"), ("signins", "Use saved sign-ins")])
def test_whom_an_item_can_be_given_to(people, kind, permission):
    """Anyone enabled, but the answer warns when their roles don't let them use it."""
    item = bobs_secret(people) if kind == "mfa" else bobs_signin(people, saved=False)
    vic = make_user(people.path, "Vic Viewer", [permissions.VIEWER])
    gus = make_user(people.path, "Gus Gone", [permissions.TESTER], disabled=True)
    robin = caller(people.robin)
    path = "/api/{}/{}/owner".format(kind, item)
    given = robin("POST", path, {"userId": vic}).get_json()
    assert (given["owner"]["id"], given["warning"]) == (
        vic, "Vic Viewer's roles don't include “{}”, so they can't use it until they get one.".format(permission))
    assert "warning" not in robin("POST", path, {"userId": people.dana}).get_json()
    send = 'Send {"userId": <id, or null>}.'
    for body, error in (({"userId": gus}, "User {} is disabled.".format(gus)), ({"userId": 999}, "There is no user 999."),
                        ({"userId": 0}, "There is no user 0."), ({"userId": str(vic)}, send), ({"userId": False}, send),
                        ({}, send), ([vic], "Send a JSON object.")):
        r = robin("POST", path, body)
        assert (r.status_code, r.get_json()) == (400, {"error": error}), body
        assert r.headers["Cache-Control"] == "no-store"
    assert run(people.path, "SELECT userId FROM itemOwners") == [(people.dana,)]


@pytest.mark.parametrize("kind, label, permission", [("mfa", "Every MFA secret", "mfa.all"),
                                                     ("signins", "Every saved sign-in", "signins.all"),
                                                     ("tokens", "Every access token", "tokens.all")])
def test_someone_demoted_while_giving_something_away_is_refused(people, monkeypatch, kind, label, permission):
    """Their permissions are worked out again under the access lock: the gate saw an admin, the lock a Tester.
    Nothing changes, and the refusal is recorded."""
    item = {"mfa": lambda: bobs_secret(people), "signins": lambda: bobs_signin(people, saved=False),
            "tokens": lambda: add_client_token(people.path, "legacy")[0]}[kind]()
    lock = accounts._access_lock

    def demoted_first(cur):
        run(people.path, "UPDATE userRoles SET roleId = ? WHERE userId = ?", (permissions.TESTER, people.robin))
        lock(cur)

    monkeypatch.setattr(accounts, "_access_lock", demoted_first)
    path = "/api/{}/{}/owner".format(kind, item)
    r = caller(people.robin)("POST", path, {"userId": people.carol})
    assert (r.status_code, r.get_json()) == (403, {"error": "Your roles no longer include “{}”.".format(label),
                                                   "forbidden": True})
    assert run(people.path, "SELECT itemKind, userId FROM itemOwners") == ([] if kind == "tokens" else [
        ("mfa" if kind == "mfa" else "signin", people.bob)])
    assert details_of(people.path, "access.denied") == [(people.robin, None, None, {
        "route": "POST " + path, "rule": "actor", "permission": permission})]


# ---- the page: whom things can be shared with ---------------------------------------------------

BUILT_IN_ROLES = [
    {"id": permissions.ADMIN, "name": "Admin", "builtIn": True, "permissions": sorted(permissions.ALL)},
    {"id": permissions.OPERATOR, "name": "Operator", "builtIn": True,
     "permissions": ["grid.operate", "grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use", "tokens.agents",
                     "tokens.own"]},
    {"id": permissions.TESTER, "name": "Tester", "builtIn": True,
     "permissions": ["grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use", "tokens.own"]},
    {"id": permissions.VIEWER, "name": "Viewer", "builtIn": True, "permissions": ["grid.view"]},
]


def found(who, q):
    r = caller(who)("GET", "/api/directory?" + urllib.parse.urlencode({"q": q}))
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    return [user["name"] for user in r.get_json()["users"]]


def test_the_directory_lists_every_role_and_finds_people(people):
    r = caller(people.bob)("GET", "/api/directory?q=da")
    assert r.get_json() == {
        "roles": BUILT_IN_ROLES + [{"id": people.qa, "name": "QA", "builtIn": False,
                                    "permissions": ["grid.view", "mfa.add", "mfa.use", "signins.add", "signins.use"]}],
        "users": [{"id": people.dana, "name": "Dana Lee", "email": "dana@example.com"}]}
    # By name or email, whatever the case; nobody for fewer than two characters, so nobody lists everyone.
    assert found(people.bob, "DANA") == ["Dana Lee"]
    assert found(people.bob, "example.com") == ["Bob Stone", "Dana Lee"]
    assert found(people.bob, " d ") == [] and found(people.bob, "") == []
    assert caller(people.bob)("GET", "/api/directory").get_json()["users"] == []
    # Only people who can sign in, and at most 20 of them.
    make_user(people.path, "Dan Gone", [permissions.TESTER], disabled=True)
    assert found(people.bob, "dan") == ["Dana Lee"]
    for i in range(25):
        make_user(people.path, "Tess {:02d}".format(i))
    assert found(people.bob, "tess") == ["Tess {:02d}".format(i) for i in range(20)]


def test_what_is_typed_into_the_directory_is_only_ever_text(people):
    """%, _ and ! mean nothing to the search: '!' is its escape, so it is escaped too."""
    for name in ("Ann 100% Sure", "Ann 1000 Times", "Bo_sun", "Bobsun", "Wow!!", "Wowza"):
        make_user(people.path, name)
    assert found(people.bob, "0%") == ["Ann 100% Sure"]
    assert found(people.bob, "o_s") == ["Bo_sun"]
    assert found(people.bob, "w!") == ["Wow!!"]
    assert found(people.bob, "%%") == found(people.bob, "__") == found(people.bob, "!%") == []


def test_who_may_look_people_up(people, monkeypatch):
    """Whoever may share something, give a token away or see people; on a grid with no sign-in, nobody."""
    vic = make_user(people.path, "Vic Viewer", [permissions.VIEWER])
    r = caller(vic)("GET", "/api/directory?q=da")
    assert (r.status_code, r.get_json()["permission"]) == (403, "mfa.add")
    for perms in ("tokens.all", "users.view", "signins.add"):
        user = make_user(people.path, "Only " + perms, [add_role(people.path, perms, perms)])
        assert found(user, "da") == ["Dana Lee"], perms
    assert found("admin token", "da") == ["Dana Lee"]
    monkeypatch.delenv("GRID_OIDC_ISSUER")
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    r = grid_app.app.test_client().get("/api/directory?q=da")
    assert (r.status_code, r.get_json()) == (403, {
        "error": "Set GRID_OIDC_ISSUER (single sign-on) or GRID_ADMIN_TOKEN to manage people.", "admin_token_unset": True})


# ---- client tokens ---------------------------------------------------------------------------


def test_a_client_token_acts_as_its_owner(people):
    bobs_secret(people)
    bobs_signin(people)
    _, danas = add_client_token(people.path, "ci", owner=people.dana)
    assert check(danas, CODE, name="intranet").status_code == 200
    assert issued(people.path) == ["client token 'ci' (Dana Lee)"]
    assert check(danas, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    # Demoted, from the next request on.
    run(people.path, "UPDATE userRoles SET roleId = ? WHERE userId = ?", (permissions.VIEWER, people.dana))
    r = check(danas, CODE, name="intranet")
    assert (r.status_code, r.get_json()) == (403, {"error": "This client token's owner's roles don't include “Use MFA "
                                                            "secrets”.", "forbidden": True, "permission": "mfa.use"})
    for path, body in ((CHECKOUT, {"name": "prod-admin"}),
                       (SAVE, {"name": "prod-admin", "state": NEWER, "baseVersion": 1, "id": 1}),
                       (RELEASE, {"name": "prod-admin", "leaseToken": "token"})):
        r = check(danas, path, **body)
        assert (r.status_code, r.get_json()) == (403, {
            "error": "This client token's owner's roles don't include “Use saved sign-ins”.", "forbidden": True,
            "permission": "signins.use"}), path
    assert issued(people.path) == ["client token 'ci' (Dana Lee)"] and signin_row(people.path)["version"] == 1
    assert not auth._failures


def test_a_token_whose_owner_is_disabled_or_gone_gets_no_sign_in_either(people):
    """Never an admin's access, and no guess: the token is right, its owner just can't use it."""
    bobs_signin(people)
    _, gones = add_client_token(people.path, "gone-ci", owner=999)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    run(people.path, "UPDATE users SET disabledAt = ? WHERE id = ?", (ts(5), people.bob))
    for token, says in ((bobs, "belongs to Bob Stone, whose account is disabled"),
                        (gones, "belongs to a person this grid no longer knows")):
        for path, body in ((CHECKOUT, {"name": "prod-admin"}), (CODE, {"name": "intranet"}),
                           (RELEASE, {"name": "prod-admin", "leaseToken": "token"})):
            r = check(token, path, **body)
            assert r.status_code == 403 and r.get_json()["owner_disabled"] is True and says in r.get_json()["error"]
    run(people.path, "DROP TABLE users")
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    assert check(danas, CHECKOUT, name="prod-admin").get_json()["owner_disabled"] is True
    assert not auth._failures and signin_row(people.path)["version"] == 1


def test_tokens_made_before_people_act_as_an_admin_under_the_names_checks_know(people, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    # Three codes go out below: from the start of a window, all three are in reach.
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: STEP * 30 + 1))
    bobs_secret(people)
    _, legacy = add_client_token(people.path, "legacy")
    made = caller("admin token")("POST", "/api/tokens", {"name": "made", "kind": "client"}).get_json()
    assert (made["owner"], made["actsAs"]) == (None, "admin")
    for token in (BOOTSTRAP, legacy, made["token"]):
        assert check(token, CODE, name="intranet").status_code == 200  # every MFA secret, as before
    assert issued(people.path) == ["GATEWAY_CLIENT_TOKEN", "client token 'legacy'", "client token 'made'"]


def test_a_token_nobody_owns_can_be_given_to_a_person_once(people):
    """Its checks then act as that person, not as an admin. A token never goes from one person to another: giving
    one away may only ever take power from its checks."""
    bobs_secret(people)
    legacy_id, legacy = add_client_token(people.path, "legacy")
    bobs_id, _ = add_client_token(people.path, "bob-ci", owner=people.bob)
    farm = store.create_token("farm", "agent")
    gus = make_user(people.path, "Gus Gone", [permissions.TESTER], disabled=True)
    robin = caller(people.robin)
    r = robin("POST", "/api/tokens/{}/owner".format(legacy_id), {"userId": people.dana})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert list(r.get_json()) == ["token"]
    token = r.get_json()["token"]
    assert (token["id"], token["name"], token["status"], token["owner"], token["actsAs"]) == (
        legacy_id, "legacy", "active", {"id": people.dana, "name": "Dana Lee", "disabled": False}, "owner")
    assert "ownerId" not in token and "token" not in token
    assert check(legacy, CODE, name="intranet").status_code == 200
    assert issued(people.path) == ["client token 'legacy' (Dana Lee)"]
    for token_id, body, status, error in (
            (legacy_id, {"userId": people.carol}, 409, "It belongs to Dana Lee already; tokens don't change hands."),
            (bobs_id, {"userId": people.carol}, 409, "It belongs to Bob Stone already; tokens don't change hands."),
            (farm["id"], {"userId": people.carol}, 409, "Only client tokens act as someone."),
            (add_client_token(people.path, "another")[0], {"userId": gus}, 400, "User {} is disabled.".format(gus)),
            (legacy_id, {"userId": None}, 400, 'Send {"userId": <id>}.'),
            (999999, {"userId": people.carol}, 404, "There is no access token 999999.")):
        r = robin("POST", "/api/tokens/{}/owner".format(token_id), body)
        assert (r.status_code, r.get_json()) == (status, {"error": error}), token_id
        assert r.headers["Cache-Control"] == "no-store"
    r = caller(people.bob)("POST", "/api/tokens/{}/owner".format(legacy_id), {"userId": people.bob})
    assert (r.status_code, r.get_json()["permission"]) == (403, "tokens.all")
    assert run(people.path, "SELECT itemId, userId FROM itemOwners WHERE itemKind = 'token' ORDER BY itemId") == [
        (legacy_id, people.dana), (bobs_id, people.bob)]
    assert details_of(people.path, "token.owner_set") == [
        (people.robin, legacy_id, "legacy", {"to": {"id": people.dana, "name": "Dana Lee"}})]


def test_a_tester_sees_only_their_own_tokens_but_counts_every_one(people):
    bobs_id, _ = add_client_token(people.path, "bob-ci", owner=people.bob)
    carols_id, _ = add_client_token(people.path, "carol-ci", owner=people.carol)
    add_client_token(people.path, "legacy")
    farm = store.create_token("farm", "agent")
    store.revoke_token(carols_id)
    bob = caller(people.bob)
    body = bob("GET", "/api/tokens").get_json()
    assert [(t["name"], t["owner"], t["actsAs"]) for t in body["tokens"]] == [
        ("bob-ci", {"id": people.bob, "name": "Bob Stone", "disabled": False}, "owner")]
    assert "ownerId" not in body["tokens"][0]
    assert body["kindCounts"] == {"agent": {"rows": 1, "active": 1, "revoked": 0},
                                  "client": {"rows": 3, "active": 2, "revoked": 1}}
    assert body["bootstrap"] == {"agent": False, "client": False}
    # Adding hosts shows every agent token; every access token shows everything.
    oscar = make_user(people.path, "Oscar Park", [permissions.OPERATOR])
    assert [t["name"] for t in caller(oscar)("GET", "/api/tokens").get_json()["tokens"]] == ["farm"]
    everything = caller("admin token")("GET", "/api/tokens").get_json()
    assert {t["name"]: t["actsAs"] for t in everything["tokens"]} == {
        "farm": None, "legacy": "admin", "carol-ci": "owner", "bob-ci": "owner"}
    assert everything["kindCounts"] == body["kindCounts"]
    # Someone else's token isn't there, as for a stranger's secret.
    for action in ("revoke", "delete"):
        r = bob("POST", "/api/tokens/{}/{}".format(carols_id, action))
        assert (r.status_code, r.get_json()) == (404, {"error": "There is no access token {}.".format(carols_id)})
    assert caller(oscar)("POST", "/api/tokens/{}/revoke".format(farm["id"])).status_code == 200
    assert caller(oscar)("POST", "/api/tokens/{}/revoke".format(bobs_id)).status_code == 404
    revoked = caller(people.robin)("POST", "/api/tokens/{}/revoke".format(bobs_id)).get_json()["token"]
    assert (revoked["status"], revoked["owner"]["id"], revoked["actsAs"]) == ("revoked", people.bob, "owner")


def test_what_kind_of_token_each_person_may_make(people):
    store.create_token("legacy", "client")  # not the grid's first client token, then
    bob = caller(people.bob)
    r = bob("POST", "/api/tokens", {"name": "farm", "kind": "agent"})
    assert (r.status_code, r.get_json()) == (403, {
        "error": "Your roles don't include “Add hosts”, which this needs. Ask an admin for a role that does.",
        "forbidden": True, "permission": "tokens.agents"})
    r = bob("POST", "/api/tokens", {"name": "bob-ci", "kind": "client", "expiresInDays": 30})
    assert r.status_code == 201 and r.headers["Cache-Control"] == "no-store"
    made = r.get_json()
    assert (made["owner"]["id"], made["actsAs"], made["token"][:5]) == (people.bob, "owner", "pwgc_")
    assert run(people.path, "SELECT itemKind, itemId, userId FROM itemOwners") == [("token", made["id"], people.bob)]
    oscar = make_user(people.path, "Oscar Park", [permissions.OPERATOR])
    assert caller(oscar)("POST", "/api/tokens", {"name": "farm", "kind": "agent"}).status_code == 201
    vic = make_user(people.path, "Vic Viewer", [permissions.VIEWER])
    assert caller(vic)("POST", "/api/tokens", {"name": "x", "kind": "client"}).get_json()["permission"] == "tokens.own"
    # Someone who only adds hosts gets past the gate, but makes no client token.
    hal = make_user(people.path, "Hal Hosts", [add_role(people.path, "Hosts", "tokens.agents")])
    r = caller(hal)("POST", "/api/tokens", {"name": "hal-ci", "kind": "client"})
    assert (r.status_code, r.get_json()) == (403, {
        "error": "Your roles don't include “Make client tokens”, which this needs. Ask an admin for a role that does.",
        "forbidden": True, "permission": "tokens.own"})
    assert run(people.path, "SELECT COUNT(*) FROM accessTokens WHERE name = 'hal-ci'") == [(0,)]
    assert caller(hal)("POST", "/api/tokens", {"name": "hal-host", "kind": "agent"}).status_code == 201
    bob("POST", "/api/tokens/{}/revoke".format(made["id"]))
    bob("POST", "/api/tokens/{}/delete".format(made["id"]))
    assert [(row[4], row[6], json.loads(row[8]) if row[8] else None) for row in audit_rows(people.path)] == [
        ("token.created", made["id"], {"kind": "client", "owner": people.bob, "scope": None}),
        ("token.created", made["id"] + 1, {"kind": "agent", "owner": oscar}),
        ("token.created", made["id"] + 2, {"kind": "agent", "owner": hal}),
        ("token.revoked", made["id"], None),
        ("token.deleted", made["id"], None)]
    assert run(people.path, "SELECT COUNT(*) FROM itemOwners WHERE itemId = ?", (made["id"],)) == [(0,)]


def test_the_grids_first_client_token_and_its_last_are_not_a_testers_to_switch(people, monkeypatch):
    """Any client token row makes the hub turn away checks without one: the first row and the last are switches
    for everybody's checks. Revoking one never is, so someone can always stop their own leaked token."""
    bob = caller(people.bob)
    r = bob("POST", "/api/tokens", {"name": "first", "kind": "client"})
    assert (r.status_code, r.get_json()) == (403, {"error": store.FIRST_CLIENT_TOKEN, "forbidden": True})
    assert run(people.path, "SELECT COUNT(*) FROM accessTokens") == [(0,)]
    admin = caller("admin token")
    admins = admin("POST", "/api/tokens", {"name": "admins", "kind": "client"}).get_json()["id"]
    bobs = bob("POST", "/api/tokens", {"name": "bob-ci", "kind": "client"}).get_json()["id"]
    admin("POST", "/api/tokens/{}/revoke".format(admins))
    assert admin("POST", "/api/tokens/{}/delete".format(admins)).status_code == 200
    assert bob("POST", "/api/tokens/{}/revoke".format(bobs)).get_json()["token"]["status"] == "revoked"
    r = bob("POST", "/api/tokens/{}/delete".format(bobs))
    assert (r.status_code, r.get_json()) == (403, {"error": store.LAST_CLIENT_TOKEN, "forbidden": True})
    # Once the hub has GATEWAY_CLIENT_TOKEN, it turns them away anyway.
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    assert bob("POST", "/api/tokens/{}/delete".format(bobs)).status_code == 200
    assert bob("POST", "/api/tokens", {"name": "first", "kind": "client"}).status_code == 201
    assert [row[4] for row in audit_rows(people.path) if row[4] == "access.denied"] == []  # no rule refused these


# ---- checks: MFA secrets and saved sign-ins by name ---------------------------------------------


def test_a_check_asking_for_someone_elses_secret_is_told_so(people):
    bobs_secret(people)
    _, carols = add_client_token(people.path, "carol-ci", owner=people.carol)
    r = check(carols, CODE, name="intranet")
    assert (r.status_code, r.get_json()) == (403, {
        "error": "The MFA secret 'intranet' belongs to someone else and isn't shared with this client token's owner.",
        "not_shared": True})
    assert r.headers["Cache-Control"] == "no-store"
    r = check(carols, CODE, name="nope")
    assert (r.status_code, r.get_json()) == (404, {"error": "There is no MFA secret called 'nope'."})
    assert run(people.path, "SELECT issuedCount FROM mfaSecrets") == [(0,)] and issued(people.path) == []


def test_a_check_asking_for_someone_elses_sign_in_is_told_so_at_once(people):
    """Not sent round the checkout's loop: the row is there, it just isn't this check's."""
    signin = bobs_signin(people, saved=False)
    _, carols = add_client_token(people.path, "carol-ci", owner=people.carol)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(check(carols, CHECKOUT, name="prod-admin")))
    worker.start()
    worker.join(5)
    assert answers, "the checkout of someone else's sign-in didn't return"
    not_shared = {"error": "The saved sign-in 'prod-admin' belongs to someone else and isn't shared with this client "
                           "token's owner.", "not_shared": True}
    assert (answers[0].status_code, answers[0].get_json()) == (403, not_shared)
    for path, body in ((SAVE, {"state": STATE, "baseVersion": 0, "id": signin}), (SAVE, {"state": STATE, "leaseToken": "x"}),
                       (RELEASE, {"leaseToken": "x"})):
        r = check(carols, path, name="prod-admin", **body)
        assert (r.status_code, r.get_json()) == (403, not_shared), path
    assert signin_row(people.path) == {"version": 0, "saved": False, "leased": False, "savedBy": None,
                                       "refreshCount": 0, "rejectedCount": 0}


def test_only_checks_that_may_add_sign_ins_add_names(people):
    users = add_role(people.path, "Users", "signins.use")
    _, umas = add_client_token(people.path, "uma-ci", owner=make_user(people.path, "Uma Ng", [users]))
    r = check(umas, CHECKOUT, name="brand-new")
    assert (r.status_code, r.get_json()) == (404, {"error": "There is no saved sign-in called 'brand-new'."})
    assert run(people.path, "SELECT COUNT(*) FROM savedSignins") == [(0,)]

    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    lease = check(danas, CHECKOUT, name="danas-site").get_json()
    assert lease["status"] == "sign-in"
    assert run(people.path, "SELECT itemId, userId FROM itemOwners WHERE itemKind = 'signin'") == [
        (lease["id"], people.dana)]
    (row,) = audit_rows(people.path)
    assert row[:3] == (people.dana, "client token 'dana-ci' (Dana Lee)", "client-token")
    assert row[4:] == ("signin.created", "signin", lease["id"], "danas-site", json.dumps({"owner": people.dana}).replace(" ", ""))
    (listed,) = caller(people.dana)("GET", "/api/signins").get_json()["signins"]
    assert (listed["name"], listed["access"], listed["usable"]) == ("danas-site", "owner", True)


def test_one_persons_checks_add_at_most_their_share_of_names(people, monkeypatch):
    monkeypatch.setattr(store, "SIGNIN_MAX_PER_OWNER", 2)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    for name in ("a", "b"):
        assert check(danas, CHECKOUT, name=name).get_json()["status"] == "sign-in"
    r = check(danas, CHECKOUT, name="c")
    assert (r.status_code, r.get_json()) == (409, {
        "error": "This client token's owner has 2 saved sign-ins already, the most one person's checks may add. Delete "
                 "some on the Grid UI to use new names.", "signins_full": True})
    # Someone who sees every sign-in isn't counted, and the names others own don't count towards Dana's.
    for name in ("c", "d", "e"):
        assert check(robins, CHECKOUT, name=name).get_json()["status"] == "sign-in"
    run(people.path, "DELETE FROM itemOwners WHERE itemId = (SELECT id FROM savedSignins WHERE name = 'a')")
    assert check(danas, CHECKOUT, name="f").get_json()["status"] == "sign-in"


# ---- checks: stewards and grantees ------------------------------------------------------------


def test_a_grantees_checks_reuse_a_sign_in_but_never_sign_in_for_it(people):
    signin = bobs_signin(people, saved=False)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    owner_signs_in = {"error": "Nothing usable is saved for 'prod-admin', and only its owner's checks may sign in for "
                               "it. It is shared with this client token's owner, who can use it once their own check "
                               "has signed in.", "owner_signs_in": True}
    r = check(danas, CHECKOUT, name="prod-admin")
    assert (r.status_code, r.get_json()) == (409, owner_signs_in)
    assert r.headers["Cache-Control"] == "no-store" and not signin_row(people.path)["leased"]
    lease = check(bobs, CHECKOUT, name="prod-admin").get_json()
    assert lease["status"] == "sign-in"
    assert check(danas, CHECKOUT, name="prod-admin").get_json()["status"] == "wait"
    assert check(bobs, SAVE, name="prod-admin", state=STATE, leaseToken=lease["leaseToken"]).get_json() == {
        "saved": True, "version": 1}
    got = check(danas, CHECKOUT, name="prod-admin").get_json()
    assert (got["status"], got["id"], got["version"], got["state"]) == ("saved", signin, 1, STATE)
    # A grantee's check never saves over the owner's state, and never holds a lease to.
    assert check(danas, SAVE, name="prod-admin", state=NEWER, baseVersion=1, id=signin).get_json() == {
        "saved": False, "reason": "shared", "version": 1}
    assert check(danas, SAVE, name="prod-admin", state=NEWER, leaseToken=lease["leaseToken"]).get_json()["error"] == \
        "lease_lost"
    assert signin_row(people.path) == {"version": 1, "saved": True, "leased": False, "savedBy": "client token 'bob-ci' "
                                       "(Bob Stone)", "refreshCount": 0, "rejectedCount": 0}
    # A state the site refused doesn't work for anybody: dropped when a grantee says so, and it stays dropped.
    r = check(danas, CHECKOUT, name="prod-admin", rejectedVersion=1, id=signin)
    assert (r.status_code, r.get_json()) == (409, owner_signs_in)
    assert check(danas, CHECKOUT, name="prod-admin").status_code == 409
    assert signin_row(people.path)["saved"] is False and signin_row(people.path)["rejectedCount"] == 1
    assert check(bobs, CHECKOUT, name="prod-admin").get_json()["status"] == "sign-in"


@pytest.mark.parametrize("whose", ["an admin's", "a legacy token's", "the bootstrap token's"])
def test_admins_checks_use_a_testers_sign_in_only_once_it_is_shared_with_them(people, monkeypatch, whose):
    """Else a Tester could save a state for an account of their own under a name every admin check uses."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    signin = bobs_signin(people)
    token = {"an admin's": lambda: add_client_token(people.path, "robin-ci", owner=people.robin)[1],
             "a legacy token's": lambda: add_client_token(people.path, "legacy")[1],
             "the bootstrap token's": lambda: BOOTSTRAP}[whose]()
    for path, body in ((CHECKOUT, {}), (SAVE, {"state": NEWER, "baseVersion": 1, "id": signin}),
                       (RELEASE, {"leaseToken": "x"})):
        r = check(token, path, name="prod-admin", **body)
        assert r.status_code == 403 and r.get_json()["not_shared"] is True, path
    share(people.path, "signin", signin, "role", permissions.ADMIN, vouched_for=TESTER)  # as only an admin can
    assert check(token, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    assert check(token, SAVE, name="prod-admin", state=NEWER, baseVersion=1, id=signin).get_json()["reason"] == "shared"
    store.forget_signin(signin)
    assert check(token, CHECKOUT, name="prod-admin").get_json()["owner_signs_in"] is True  # never sign-in
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    assert check(bobs, CHECKOUT, name="prod-admin").get_json()["status"] == "sign-in"
    assert check(token, CHECKOUT, name="prod-admin").get_json()["status"] == "wait"


def test_admins_checks_are_stewards_of_the_admins_sign_ins(people, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    signin = add_item(people.path, "signin", "shared-admin", shares=[("user", people.dana)])
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin)
    _, legacy = add_client_token(people.path, "legacy")
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    lease = check(robins, CHECKOUT, name="shared-admin").get_json()
    assert lease["status"] == "sign-in"
    assert check(robins, SAVE, name="shared-admin", state=STATE, leaseToken=lease["leaseToken"]).get_json()["saved"]
    assert check(legacy, SAVE, name="shared-admin", state=NEWER, baseVersion=1, id=signin).get_json() == {
        "saved": True, "version": 2}
    assert check(BOOTSTRAP, CHECKOUT, name="shared-admin").get_json()["state"] == NEWER
    assert check(danas, CHECKOUT, name="shared-admin").get_json()["state"] == NEWER
    assert check(danas, SAVE, name="shared-admin", state=STATE, baseVersion=2, id=signin).get_json()["reason"] == "shared"


def test_one_hand_out_per_time_step_whoever_asks(people, monkeypatch):
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: STEP * 30 + 1))
    secret = bobs_secret(people)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    steps = [check(bobs, CODE, name="intranet").get_json()["step"], check(danas, CODE, name="intranet").get_json()["step"],
             caller(people.erin)("POST", "/api/mfa/{}/use".format(secret)).get_json()["step"]]
    assert steps == [STEP, STEP + 1, STEP + 2]
    assert issued(people.path) == ["client token 'bob-ci' (Bob Stone)", "client token 'dana-ci' (Dana Lee)", "Erin Wu"]


# ---- databases from before single sign-on --------------------------------------------------------


def test_without_an_owner_table_only_admins_add_things(people, monkeypatch):
    """An item a person added would have no owner row, so it would be the admins' (and their token would act as
    an admin): their adds fail, and say which table is missing."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    store.create_mfa("intranet", SEED)
    store.create_token("legacy", "client")
    run(people.path, "DROP TABLE itemOwners")
    admin = caller("admin token")
    assert admin("POST", "/api/mfa", {"name": "admins", "secret": SEED}).status_code == 201
    assert admin("POST", "/api/tokens", {"name": "admins", "kind": "client"}).status_code == 201
    assert check(BOOTSTRAP, CHECKOUT, name="admins-site").get_json()["status"] == "sign-in"
    dana = caller(people.dana)
    for path, body in (("/api/mfa", {"name": "danas", "secret": SEED}), ("/api/tokens", {"name": "danas", "kind": "client"})):
        r = dana("POST", path, body)
        assert (r.status_code, r.get_json()) == (503, {"error": store._NO_OWNER_TABLE}), path
        assert "itemOwners" in r.get_json()["error"]
    with pytest.raises(store.SigninsUnavailable, match="itemOwners"):
        store.checkout_signin("client token 'dana-ci' (Dana Lee)", "danas-site",
                              access=store.Access(people.dana, (permissions.TESTER,), TESTER))
    assert run(people.path, "SELECT name FROM mfaSecrets ORDER BY name") == [("admins",), ("intranet",)]
    assert run(people.path, "SELECT name FROM savedSignins") == [("admins-site",)]
    # Everything is the admins': nobody else sees any of it.
    assert dana("GET", "/api/mfa").get_json()["secrets"] == []
    assert dana("GET", "/api/signins").get_json()["signins"] == []
    assert dana("GET", "/api/tokens").get_json()["tokens"] == []
    assert len(admin("GET", "/api/mfa").get_json()["secrets"]) == 2


def test_without_an_owner_table_no_persons_token_is_taken_for_an_admins(people, monkeypatch):
    """Whose a token is can't be told then (the table went, or the database user can't see it), so checks using a
    managed client token are told why rather than act as an admin. A database from before people, with neither
    table, still takes every token for an admin's, as it always did."""
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: STEP * 30 + 1))
    bobs_secret(people)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    _, legacy = add_client_token(people.path, "legacy")
    run(people.path, "DROP TABLE itemOwners")
    for token in (danas, legacy):
        for path, body in ((CODE, {"name": "intranet"}), (CHECKOUT, {"name": "prod-admin"})):
            r = check(token, path, **body)
            assert (r.status_code, r.get_json()) == (503, {"error": store._NO_TOKEN_OWNERS}), path
    assert "itemOwners" in store._NO_TOKEN_OWNERS
    assert check(BOOTSTRAP, CODE, name="intranet").status_code == 200
    assert run(people.path, "SELECT COUNT(*) FROM savedSignins") == [(0,)] and not auth._failures
    run(people.path, "DROP TABLE users")
    assert check(legacy, CODE, name="intranet").status_code == 200
    assert issued(people.path) == ["GATEWAY_CLIENT_TOKEN", "client token 'legacy'"]


def test_without_a_share_table_nothing_is_shared_or_given_away(people):
    secret = bobs_secret(people)
    run(people.path, "DROP TABLE itemShares")
    robin = caller(people.robin)
    for path, body in (("/api/mfa/{}/shares/add".format(secret), {"user": people.carol}),
                       ("/api/mfa/{}/shares/remove".format(secret), {"user": people.dana}),
                       ("/api/mfa/{}/owner".format(secret), {"userId": people.carol})):
        r = robin("POST", path, body)
        assert (r.status_code, r.get_json()) == (503, {"error": accounts.NO_SHARING_TABLES}), path
        assert "itemShares" in r.get_json()["error"]
    assert run(people.path, "SELECT userId FROM itemOwners") == [(people.bob,)]
    assert [s["name"] for s in robin("GET", "/api/mfa").get_json()["secrets"]] == ["intranet"]


# ---- client tokens with a scope ---------------------------------------------------------------
#
# A scope decides which routes a token may ask (test_accounts.py). What it reaches there is its owner's, judged
# by all of what its owner may do: a scope is never taken out of that.

NOT_SHARED = {"error": "The saved sign-in 'prod-admin' belongs to someone else and isn't shared with this client "
                       "token's owner.", "not_shared": True}


def saved_signin(path, name, owner=None, shares=(), vouched_for=None):
    """A saved sign-in with STATE saved by its owner's check. Returns its id."""
    signin_id = add_item(path, "signin", name, owner=owner, shares=shares, vouched_for=vouched_for)
    lease = store.checkout_signin("client token 'its-owners'", name)
    store.save_signin("client token 'its-owners'", name, STATE, lease_token=lease["leaseToken"])
    return signin_id


def same_answers(unscoped, scoped, asked):
    """Both tokens get the same answer to each (path, name, what is expected) of asked: 'state' and 'code' for
    what is reached, else the key of the refusal."""
    for path, name, expected in asked:
        answers = [check(token, path, name=name) for token in (unscoped, scoped)]
        first, second = [(r.status_code, r.get_json()) for r in answers]
        if expected == "code":
            assert first[0] == second[0] == 200 and sorted(first[1]) == sorted(second[1]), name
            assert "code" in second[1], name
        else:
            assert first == second, name
            if expected == "state":
                assert second[1].get("state") == STATE, (name, second)
            elif expected == 404:
                assert second[0] == 404, (name, second)
            else:
                assert second[1].get(expected) is True, (name, second)


def test_a_scope_never_makes_a_shared_sign_in_reach_a_token_its_owner_cannot_use(people):
    """Bob shared his sign-in with Dana, who has since been given more than he has. A scope of using sign-ins
    alone is less than Bob may do, and her token still isn't let in: it is Dana who is judged, not the scope."""
    signin = bobs_signin(people)
    _, before = add_client_token(people.path, "dana-before", owner=people.dana, scope="signins.use")
    assert check(before, CHECKOUT, name="prod-admin").get_json()["state"] == STATE
    run(people.path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)",
        (people.dana, permissions.OPERATOR, ts(60)))
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana, scope="signins.use")
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin, scope="signins.use")
    _, legacy = add_client_token(people.path, "legacy", scope="signins.use")
    for token in (before, danas, robins, legacy):
        for path, body in ((CHECKOUT, {}), (SAVE, {"state": NEWER, "baseVersion": 1, "id": signin}),
                           (RELEASE, {"leaseToken": "x"})):
            r = check(token, path, name="prod-admin", **body)
            assert (r.status_code, r.get_json()) == (403, NOT_SHARED), path
    assert signin_row(people.path)["version"] == 1


def test_a_scoped_token_reaches_exactly_what_its_owner_reaches(people, monkeypatch):
    """Her own, what is shared with her and with her role, what an admin vouched for, the admins': each as for a
    token of hers without a scope, the refusals too."""
    path = people.path
    oscar = make_user(path, "Oscar Park", [permissions.OPERATOR])
    saved_signin(path, "danas-own", owner=people.dana)
    bobs_signin(people)  # shared with Dana and with QA
    saved_signin(path, "carols", owner=people.carol)
    saved_signin(path, "vouched", owner=people.bob, shares=[("user", oscar)], vouched_for=TESTER)
    saved_signin(path, "not-vouched", owner=people.bob, shares=[("user", oscar)])
    saved_signin(path, "admins-shared", shares=[("user", people.dana), ("role", people.qa), ("user", oscar)],
                 vouched_for=permissions.ALL)
    saved_signin(path, "admins-own")
    bobs_secret(people)  # shared with Dana and with QA
    store.create_mfa("danas-codes", SEED, owner_id=people.dana)
    store.create_mfa("carols-codes", SEED, owner_id=people.carol)
    store.create_mfa("admins-codes", SEED)
    users = add_role(path, "Users", "mfa.use signins.use")
    run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (people.erin, users, ts(60)))
    asked = {
        people.dana: [(CHECKOUT, "danas-own", "state"), (CHECKOUT, "prod-admin", "state"), (CHECKOUT, "carols", "not_shared"),
                      (CHECKOUT, "admins-shared", "state"), (CHECKOUT, "admins-own", "not_shared"),
                      (CODE, "intranet", "code"), (CODE, "danas-codes", "code"), (CODE, "carols-codes", "not_shared"),
                      (CODE, "admins-codes", "not_shared"), (CODE, "nope", 404)],
        people.erin: [(CHECKOUT, "prod-admin", "state"), (CHECKOUT, "admins-shared", "state"),
                      (CHECKOUT, "carols", "not_shared"), (CODE, "intranet", "code"), (CODE, "carols-codes", "not_shared")],
        oscar: [(CHECKOUT, "vouched", "state"), (CHECKOUT, "not-vouched", "not_shared"),
                (CHECKOUT, "admins-shared", "state"), (CHECKOUT, "prod-admin", "not_shared")],
    }
    now = [STEP * 30 + 1]  # a secret gives a few codes ahead and no more: each pair of tokens asks at its own time
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: now[0]))
    for number, (owner, questions) in enumerate(asked.items()):
        _, unscoped = add_client_token(path, "ci-{}".format(number), owner=owner)
        for scope in ("mfa.use signins.use", "grid.view mfa.use signins.add signins.use"):
            _, scoped = add_client_token(path, "ci-{}-{}".format(number, len(scope)), owner=owner, scope=scope)
            now[0] += 300
            same_answers(unscoped, scoped, questions)


def test_a_scoped_ownerless_token_reaches_what_an_unscoped_one_does(people):
    """A token nobody owns is the admins': theirs, and a Tester's only once it is shared with the admins."""
    path = people.path
    saved_signin(path, "admins-own")
    bobs_signin(people)
    saved_signin(path, "for-admins", owner=people.bob, shares=[("role", permissions.ADMIN)], vouched_for=TESTER)
    saved_signin(path, "to-admins-by-bob", owner=people.bob, shares=[("role", permissions.ADMIN)])
    bobs_secret(people)
    store.create_mfa("admins-codes", SEED)
    _, unscoped = add_client_token(path, "legacy")
    _, scoped = add_client_token(path, "legacy-limited", scope="mfa.use signins.use")
    same_answers(unscoped, scoped, [
        (CHECKOUT, "admins-own", "state"), (CHECKOUT, "prod-admin", "not_shared"), (CHECKOUT, "for-admins", "state"),
        (CHECKOUT, "to-admins-by-bob", "not_shared"), (CODE, "intranet", "code"), (CODE, "admins-codes", "code"),
        (CODE, "nope", 404)])
    assert issued(path) == ["client token 'legacy'", "client token 'legacy-limited'"] * 2


def test_without_signins_add_in_scope_a_token_cannot_start_a_new_name(people):
    """A sign-in is added by checking out a name nothing has. Its owner may; the token's scope leaves it out."""
    signin = add_item(people.path, "signin", "danas-site", owner=people.dana)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana, scope="signins.use")
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin, scope="mfa.use signins.use")
    _, legacy = add_client_token(people.path, "legacy", scope="grid.view signins.use")
    for token in (danas, robins, legacy):
        r = check(token, CHECKOUT, name="brand-new")
        assert (r.status_code, r.get_json()) == (403, {
            "error": "There is no saved sign-in called 'brand-new', and this client token's scope doesn't include “Add "
                     "saved sign-ins”, so it can't start one.", "out_of_scope": True, "scope": "signins.add"})
        assert r.headers["Cache-Control"] == "no-store"
    assert run(people.path, "SELECT name FROM savedSignins") == [("danas-site",)]
    assert audit_rows(people.path) == [] and not auth._failures
    # A name that is there is used in full: checked out, signed in for, saved, signed in for afresh, released.
    lease = check(danas, CHECKOUT, name="danas-site").get_json()
    assert (lease["status"], lease["id"]) == ("sign-in", signin)
    assert check(danas, SAVE, name="danas-site", state=STATE, leaseToken=lease["leaseToken"]).get_json() == {
        "saved": True, "version": 1}
    got = check(danas, CHECKOUT, name="danas-site").get_json()
    assert (got["status"], got["version"], got["state"]) == ("saved", 1, STATE)
    again = check(danas, CHECKOUT, name="danas-site", rejectedVersion=1, id=signin).get_json()
    assert again["status"] == "sign-in"
    r = check(danas, RELEASE, name="danas-site", leaseToken=again["leaseToken"])
    assert (r.status_code, r.get_json()) == (200, {"released": True})
    assert signin_row(people.path, "danas-site")["leased"] is False
    # With it in scope the name is started, as by a token without a scope.
    _, adds = add_client_token(people.path, "dana-adds", owner=people.dana, scope="signins.add signins.use")
    assert check(adds, CHECKOUT, name="brand-new").get_json()["status"] == "sign-in"
    assert run(people.path, "SELECT userId FROM itemOwners WHERE itemKind = 'signin' ORDER BY itemId") == [
        (people.dana,), (people.dana,)]
    assert [row[4] for row in audit_rows(people.path)] == ["signin.created"]


def test_signins_add_in_scope_still_needs_the_owner_to_hold_it(people):
    """A scope gives nothing: the answer is the one a token without a scope gets."""
    users = add_role(people.path, "Users", "signins.use")
    uma = make_user(people.path, "Uma Ng", [users])
    for name, scope in (("uma-ci", None), ("uma-adds", "signins.add signins.use")):
        _, umas = add_client_token(people.path, name, owner=uma, scope=scope)
        r = check(umas, CHECKOUT, name="brand-new")
        assert (r.status_code, r.get_json()) == (404, {"error": "There is no saved sign-in called 'brand-new'."}), name
    assert run(people.path, "SELECT COUNT(*) FROM savedSignins") == [(0,)] and audit_rows(people.path) == []


def test_without_mfa_use_in_scope_a_token_gets_no_code_for_a_secret_its_owner_can_use(people):
    bobs_secret(people)
    store.create_mfa("danas-codes", SEED, owner_id=people.dana)
    _, bobs = add_client_token(people.path, "bob-ci", owner=people.bob, scope="grid.view")
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana, scope="signins.add signins.use")
    _, nothing = add_client_token(people.path, "dana-nothing", owner=people.dana, scope="")
    for token in (bobs, danas, nothing):
        for name in ("intranet", "danas-codes", "nope"):  # nor is it told which names there are
            r = check(token, CODE, name=name)
            assert (r.status_code, r.get_json()) == (403, out_of_scope("mfa.use")), name
            assert r.headers["Cache-Control"] == "no-store"
    assert issued(people.path) == [] and run(people.path, "SELECT SUM(issuedCount) FROM mfaSecrets") == [(0,)]
    _, codes = add_client_token(people.path, "dana-codes", owner=people.dana, scope="mfa.use")
    for name in ("intranet", "danas-codes"):
        assert check(codes, CODE, name=name).status_code == 200, name
    assert issued(people.path) == ["client token 'dana-codes' (Dana Lee)"] * 2


def test_the_sign_in_cap_counts_a_scoped_tokens_owner_as_before(people, monkeypatch):
    """The names one person's checks add are counted by person, whichever of their tokens added them."""
    monkeypatch.setattr(store, "SIGNIN_MAX_PER_OWNER", 2)
    _, danas = add_client_token(people.path, "dana-ci", owner=people.dana)
    _, limited = add_client_token(people.path, "dana-limited", owner=people.dana, scope="signins.add signins.use")
    _, robins = add_client_token(people.path, "robin-ci", owner=people.robin, scope="signins.add signins.use")
    assert check(limited, CHECKOUT, name="a").get_json()["status"] == "sign-in"
    assert check(danas, CHECKOUT, name="b").get_json()["status"] == "sign-in"
    for token in (limited, danas):
        r = check(token, CHECKOUT, name="c")
        assert (r.status_code, r.get_json()) == (409, {
            "error": "This client token's owner has 2 saved sign-ins already, the most one person's checks may add. "
                     "Delete some on the Grid UI to use new names.", "signins_full": True})
    # Someone who sees every sign-in isn't counted, with a scope or without.
    for name in ("c", "d", "e"):
        assert check(robins, CHECKOUT, name=name).get_json()["status"] == "sign-in"
    run(people.path, "DELETE FROM itemOwners WHERE itemId = (SELECT id FROM savedSignins WHERE name = 'a')")
    assert check(limited, CHECKOUT, name="f").get_json()["status"] == "sign-in"
