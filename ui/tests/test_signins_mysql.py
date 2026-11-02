"""Saved sign-ins: store.py and the check routes against a real MySQL. Opt in with ORCHESTRATOR_TEST_DB=<database to
wipe>, plus DB_HOST/DB_PORT/DB_USER/DB_PASSWORD. The savedSignins table in that database is dropped."""
import base64
import datetime
import os
import threading

import pytest

from ui import app as grid_app, mfa, store

TEST_DB = os.environ.get("ORCHESTRATOR_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORCHESTRATOR_TEST_DB to run against MySQL")

KEY = "a-test-key-for-the-signin-tests-only"
COOKIE = "estsauth-cookie-value-for-the-tests"


def state(value=COOKIE):
    return {"cookies": [{"name": "ESTSAUTH", "value": value, "domain": ".login.microsoftonline.com", "path": "/",
                         "expires": -1, "httpOnly": True, "secure": True, "sameSite": "None"}],
            "origins": [{"origin": "https://contoso.sharepoint.com", "localStorage": [{"name": "k", "value": value}]}]}


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sql(statement, args=None):
    with store._session() as cur:
        cur.execute(statement, args)
        return cur.fetchall()


COUNTS = ("version", "signInCount", "refreshCount", "useCount", "rejectedCount")


def counts(name="contoso"):
    (values,) = sql("SELECT {} FROM savedSignins WHERE name = %s".format(", ".join(COUNTS)), (name,))
    return dict(zip(COUNTS, values))


@pytest.fixture
def mysql(monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB or "")
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "_schema_attempted", False)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    sql("DROP TABLE IF EXISTS savedSignins")


def from_threads(calls):
    """Every call at once, each from its own thread: (results, exceptions)."""
    results, errors = [], []
    start = threading.Barrier(len(calls))

    def one(call):
        start.wait()
        try:
            results.append(call())
        except Exception as e:  # DatabaseUnavailable from a deadlock is the bug this guards against
            errors.append(repr(e))

    threads = [threading.Thread(target=one, args=(call,)) for call in calls]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def sign_in(name="contoso", saved=None, by="check a"):
    lease = store.checkout_signin(by, name)
    assert lease["status"] == "sign-in", lease
    return store.save_signin(by, name, saved or state(), lease_token=lease["leaseToken"])["version"]


def test_sign_ins_round_trip_on_a_new_database(mysql):
    listing = store.list_signins()  # runs the schema: the table doesn't exist yet
    assert listing == {"ready": True, "generated": listing["generated"], "signins": []}

    lease = store.checkout_signin("check a", "contoso", lease_s=60)
    assert (lease["status"], lease["version"], lease["leaseForS"]) == ("sign-in", 0, 60)
    ((until, created),) = sql("SELECT leaseUntil, createdAt FROM savedSignins")
    assert abs((until - utcnow()).total_seconds() - 60) < 10  # UTC, although the server's zone isn't
    assert abs((created - utcnow()).total_seconds()) < 10
    assert store.checkout_signin("check b", "contoso")["status"] == "wait"
    assert store.save_signin("check a", "contoso", state(), lease_token=lease["leaseToken"]) == {"saved": True, "version": 1}
    ((cipher, saved_at, expires, domains),) = sql("SELECT stateCipher, savedAt, expiresAt, domains FROM savedSignins")
    assert COOKIE not in cipher and domains == "login.microsoftonline.com"
    assert abs((saved_at - utcnow()).total_seconds()) < 10 and (expires - saved_at).total_seconds() == 30 * 86400

    got = store.checkout_signin("check b", "contoso")
    assert (got["status"], got["id"], got["version"], got["state"]) == ("saved", lease["id"], 1, state())
    # Names compare as MySQL compares them (without case): the row's own name binds the state.
    got = store.checkout_signin("check c", "CONTOSO")
    assert (got["status"], got["name"], got["state"]) == ("saved", "contoso", state())
    assert store.save_signin("check c", "Contoso", state("newer"), base_version=1, signin_id=got["id"]) == {
        "saved": True, "version": 2}
    assert store.checkout_signin("check d", "contoso")["state"] == state("newer")
    assert counts() == {"version": 2, "signInCount": 1, "refreshCount": 1, "useCount": 3, "rejectedCount": 0}

    # Close to 2 MB of cookies that don't compress: MEDIUMTEXT holds them.
    noise = base64.b64encode(os.urandom(1500000)).decode()
    big = {"cookies": [{"name": "big", "value": noise, "domain": "contoso.example"}], "origins": []}
    assert store.save_signin("check d", "contoso", big, base_version=2, signin_id=got["id"])["version"] == 3
    assert store.checkout_signin("check e", "contoso")["state"] == big

    (entry,) = store.list_signins()["signins"]
    assert (entry["name"], entry["version"], entry["saved"], entry["domains"], entry["useCount"]) == (
        "contoso", 3, True, ["contoso.example"], 4)
    assert store.forget_signin(entry["id"])["version"] == 4
    assert sql("SELECT stateCipher FROM savedSignins") == ((None,),)
    for big_id in (2 ** 31, 10 ** 20):  # the same answer as the sqlite demo gives
        assert store.forget_signin(big_id) is None and store.delete_signin(big_id) is False
    assert store.delete_signin(entry["id"]) is True and store.list_signins()["signins"] == []
    # Added again under a new id, never the deleted one's (InnoDB keeps its AUTO_INCREMENT counter), so a check
    # still holding the deleted state can't save it over the new one's versions.
    again = store.checkout_signin("check f", "contoso")
    assert (again["status"], again["version"]) == ("sign-in", 0) and again["id"] > entry["id"]
    assert store.save_signin("check f", "contoso", state("again"), lease_token=again["leaseToken"])["version"] == 1
    assert store.save_signin("check e", "contoso", big, base_version=1, signin_id=entry["id"]) == {
        "saved": False, "reason": "newer", "version": 1}
    assert store.checkout_signin("check g", "contoso")["state"] == state("again")


def test_concurrent_checkouts_of_a_new_name_get_one_lease(mysql):
    store.ensure_schema()
    for round_ in range(5):
        name = "new-{}".format(round_)
        answers, errors = from_threads([lambda: store.checkout_signin("check", name)] * 12)
        assert errors == []
        assert sorted(a["status"] for a in answers) == ["sign-in"] + ["wait"] * 11
    # Different new names at once: each its own lease, nobody deadlocked.
    answers, errors = from_threads([lambda n=n: store.checkout_signin("check", "name-{}".format(n)) for n in range(12)])
    assert errors == [] and [a["status"] for a in answers] == ["sign-in"] * 12
    assert sql("SELECT COUNT(*), COUNT(leaseHash) FROM savedSignins") == ((17, 17),)


def test_concurrent_checkouts_of_a_saved_state(mysql):
    store.ensure_schema()
    sign_in()
    answers, errors = from_threads([lambda: store.checkout_signin("check", "contoso")] * 12)
    assert errors == [] and [(a["status"], a["version"]) for a in answers] == [("saved", 1)] * 12
    # The site refused it to all of them: one signs in, the others wait for it.
    answers, errors = from_threads([lambda: store.checkout_signin("check", "contoso", rejected_version=1)] * 12)
    assert errors == [] and sorted(a["status"] for a in answers) == ["sign-in"] + ["wait"] * 11
    assert counts() == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 12, "rejectedCount": 1}


def test_save_and_refresh_races(mysql):
    store.ensure_schema()
    sign_in()
    ((signin_id,),) = sql("SELECT id FROM savedSignins")
    answers, errors = from_threads([lambda: store.save_signin("check", "contoso", state("newer"), base_version=1,
                                                              signin_id=signin_id)] * 10)
    assert errors == [] and sorted(a["saved"] for a in answers) == [False] * 9 + [True]
    assert {a["reason"] for a in answers if not a["saved"]} == {"newer"}
    assert counts()["version"] == 2

    # A check signs in again while others save the state they reused: the sign-in wins.
    lease = store.checkout_signin("check", "contoso", rejected_version=2)["leaseToken"]
    calls = [lambda: store.save_signin("signer", "contoso", state("fresh"), lease_token=lease)]
    calls += [lambda: store.save_signin("check", "contoso", state("stale"), base_version=2, signin_id=signin_id)] * 8
    answers, errors = from_threads(calls)
    assert errors == [] and answers.count({"saved": True, "version": 3}) == 1
    # Each stale save came before the sign-in's (someone was signing in) or after it (a newer state).
    refused = [{"saved": False, "reason": "signing-in", "version": 2}, {"saved": False, "reason": "newer", "version": 3}]
    assert all(a in refused for a in answers if a != {"saved": True, "version": 3})
    assert store.checkout_signin("check", "contoso")["state"] == state("fresh")
    assert counts() == {"version": 3, "signInCount": 2, "refreshCount": 1, "useCount": 1, "rejectedCount": 1}
    assert sql("SELECT savedBy, leaseHash FROM savedSignins") == (("signer", None),)


def test_expired_states_are_purged_as_checkouts_come(mysql):
    store.ensure_schema()
    names = ["n{}".format(i) for i in range(12)]
    for name in names + ["kept"]:
        sign_in(name)
    sql("UPDATE savedSignins SET expiresAt = UTC_TIMESTAMP() - INTERVAL 5 SECOND WHERE name IN ('n0', 'n1')")
    assert store.checkout_signin("check", "kept")["status"] == "saved"
    assert sql("SELECT name FROM savedSignins WHERE stateCipher IS NULL ORDER BY name") == (("n0",), ("n1",))

    # Every expired name checked out at once: each checkout purges the lot, and none deadlocks.
    sql("UPDATE savedSignins SET expiresAt = UTC_TIMESTAMP() - INTERVAL 5 SECOND WHERE name LIKE 'n%'")
    answers, errors = from_threads([lambda n=n: store.checkout_signin("check", n) for n in names])
    assert errors == [] and sorted((a["name"], a["status"], a["version"]) for a in answers) == sorted(
        (n, "sign-in", 1) for n in names)
    assert sql("SELECT COUNT(*) FROM savedSignins WHERE stateCipher IS NOT NULL") == ((1,),)
    assert store.checkout_signin("check", "kept")["state"] == state()


def test_a_changed_key_means_signing_in_again(mysql, monkeypatch):
    store.ensure_schema()
    sign_in()
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    got = store.checkout_signin("check b", "contoso")
    assert (got["status"], got["version"]) == ("sign-in", 1)
    assert counts()["useCount"] == 0
    assert store.save_signin("check b", "contoso", state("re-keyed"), lease_token=got["leaseToken"])["version"] == 2
    assert store.checkout_signin("check c", "contoso")["state"] == state("re-keyed")


def test_checks_use_their_sign_ins_with_a_managed_client_token(mysql):
    store.ensure_schema()
    token = store.create_token("nightly checks", "client")
    headers = {"Authorization": "Bearer " + token["token"]}
    client = grid_app.app.test_client()
    r = client.post("/api/signins/checkout", json={"name": "contoso"}, headers=headers)
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store" and r.get_json()["status"] == "sign-in"
    r = client.post("/api/signins/save", json={"name": "contoso", "state": state(), "leaseToken": r.get_json()["leaseToken"]},
                    headers=headers)
    assert r.get_json() == {"saved": True, "version": 1}
    r = client.post("/api/signins/checkout", json={"name": "contoso"}, headers=headers)
    assert r.get_json()["state"] == state()
    assert sql("SELECT savedBy, lastUsedBy FROM savedSignins") == (("client token 'nightly checks'",) * 2,)
    r = client.get("/api/signins", headers={"X-Grid-Token": "s3cret"})
    assert r.status_code == 200 and COOKIE not in r.get_data(as_text=True)
    store.revoke_token(token["id"])
    assert client.post("/api/signins/checkout", json={"name": "contoso"}, headers=headers).status_code == 401
