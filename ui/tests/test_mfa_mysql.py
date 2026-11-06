"""MFA secrets: store.py and the check route against a real MySQL. Opt in with ORCHESTRATOR_TEST_DB=<database to
wipe>, plus DB_HOST/DB_PORT/DB_USER/DB_PASSWORD. The MFA tables in that database are dropped."""
import base64
import datetime
import os
import threading

import pytest

from ui import app as grid_app, mfa, store

TEST_DB = os.environ.get("ORCHESTRATOR_TEST_DB")
pytestmark = pytest.mark.skipif(not TEST_DB, reason="set ORCHESTRATOR_TEST_DB to run against MySQL")

KEY = "a-test-key-for-the-mfa-tests-only"
SEED = base64.b32encode(b"12345678901234567890").decode()
OTHER_SEED = "JBSWY3DPEHPK3PXP"
STEP = 37037036


def at(step, seconds_in=0.0):
    return step * 30 + seconds_in


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def sql(statement, args=None):
    with store._session() as cur:
        cur.execute(statement, args)
        return cur.fetchall()


@pytest.fixture
def mysql(monkeypatch):
    monkeypatch.setenv("DB_NAME", TEST_DB or "")
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    monkeypatch.setattr(store, "DB_ENGINE", "mysql")
    monkeypatch.setattr(store, "_schema_attempted", False)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    # The owner and share rows too: ids start over, and old rows would belong to the new secrets.
    sql("DROP TABLE IF EXISTS mfaIssues, mfaSecrets, itemOwners, itemShares")


def ask_from_threads(count, **kwargs):
    """issue_mfa_code from `count` threads at once: (answers, MfaBusy refusals)."""
    got, busy = [], []
    start = threading.Barrier(count)

    def ask():
        start.wait()
        try:
            got.append(store.issue_mfa_code("c", **kwargs))
        except store.MfaBusy as e:
            busy.append(e)

    threads = [threading.Thread(target=ask) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return got, busy


def test_secrets_round_trip_on_a_new_database(mysql):
    listing = store.list_mfa()  # runs the schema: the tables don't exist yet
    assert listing == {"ready": True, "generated": listing["generated"], "secrets": [], "issues": []}

    made = store.create_mfa("intranet", SEED.lower(), "Intranet", "svc-monitor")
    ((cipher, created),) = sql("SELECT seedCipher, createdAt FROM mfaSecrets")
    assert SEED not in cipher and mfa.decrypt(cipher, "intranet") == SEED
    assert abs((created - utcnow()).total_seconds()) < 10  # UTC, although the server's zone isn't
    assert SEED not in repr(sql("SELECT * FROM mfaSecrets"))
    with pytest.raises(mfa.InvalidSecret, match="already"):
        store.create_mfa("intranet", OTHER_SEED)

    first = store.issue_mfa_code("client token 'checks'", name="intranet", label="nightly", now=at(STEP, 10))
    assert (first["code"], first["step"], first["waitS"], first["validForS"]) == ("081804", STEP, 0.0, 20.0)
    peek = store.peek_mfa_code(made["id"], now=at(STEP, 10))
    assert peek["code"] == "081804" and peek["issued"] is True and peek["secondsLeft"] == 20.0
    assert store.issue_mfa_code("c", secret_id=made["id"], now=at(STEP, 10))["step"] == STEP + 1
    assert store.issue_mfa_code("c", name="intranet", now=at(STEP, 10))["step"] == STEP + 2
    with pytest.raises(store.MfaBusy) as busy:
        store.issue_mfa_code("c", name="intranet", now=at(STEP, 10))
    assert busy.value.retry_after_s == 20.0
    ((last_step, count, last_to, last_at),) = sql("SELECT lastStep, issuedCount, lastIssuedTo, lastIssuedAt FROM mfaSecrets")
    assert (last_step, count, last_to) == (STEP + 2, 3, "c")
    assert abs((last_at - utcnow()).total_seconds()) < 10

    issues = store.list_mfa()["issues"]
    assert [(i["step"], i["issuedTo"], i["label"]) for i in issues] == [
        (STEP + 2, "c", None), (STEP + 1, "c", None), (STEP, "client token 'checks'", "nightly")]

    # Hand-outs older than a week go as new ones come, any secret's; until then the list leaves them out.
    other = store.create_mfa("idle", OTHER_SEED)
    sql("INSERT INTO mfaIssues (secretId, step, issuedAt, issuedTo) VALUES (%s, 1, UTC_TIMESTAMP() - INTERVAL 8 DAY, 'old'), "
        "(%s, 2, UTC_TIMESTAMP() - INTERVAL 6 DAY, 'recent'), (%s, 3, UTC_TIMESTAMP() - INTERVAL 8 DAY, 'old, idle')",
        (made["id"], made["id"], other["id"]))
    assert [i["step"] for i in store.list_mfa()["issues"]] == [2, STEP + 2, STEP + 1, STEP]
    store.issue_mfa_code("c", name="intranet", now=at(STEP + 10))
    assert [step for (step,) in sql("SELECT step FROM mfaIssues ORDER BY step")] == [2, STEP, STEP + 1, STEP + 2, STEP + 10]
    assert store.delete_mfa(other["id"]) is True

    for big in (2 ** 31, 10 ** 20):  # the same answer as the sqlite demo gives
        assert store.issue_mfa_code("c", secret_id=big) is None and store.peek_mfa_code(big) is None
        assert store.delete_mfa(big) is False
    assert store.delete_mfa(made["id"]) is True
    assert sql("SELECT COUNT(*) FROM mfaIssues") == ((0,),) and store.list_mfa()["secrets"] == []


def test_a_changed_key_uses_nothing_up(mysql, monkeypatch):
    store.ensure_schema()
    made = store.create_mfa("intranet", SEED)
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    with pytest.raises(mfa.CannotDecrypt):
        store.issue_mfa_code("c", name="intranet", now=at(STEP))
    with pytest.raises(mfa.CannotDecrypt):
        store.peek_mfa_code(made["id"], now=at(STEP))
    assert sql("SELECT lastStep, issuedCount FROM mfaSecrets") == ((None, 0),)
    assert sql("SELECT COUNT(*) FROM mfaIssues") == ((0,),)


def test_concurrent_requests_never_get_the_same_step(mysql):
    store.ensure_schema()
    store.create_mfa("intranet", SEED)
    got, busy = ask_from_threads(12, name="intranet", now=at(STEP, 10))
    assert sorted(g["step"] for g in got) == [STEP, STEP + 1, STEP + 2] and len(busy) == 9
    assert len({g["code"] for g in got}) == 3

    # The next window: one more step is in reach.
    got, busy = ask_from_threads(6, name="intranet", now=at(STEP + 1))
    assert [g["step"] for g in got] == [STEP + 3] and len(busy) == 5

    # Real time, every step in reach, many threads: still no step twice.
    store.create_mfa("busy", OTHER_SEED)
    got, busy = ask_from_threads(16, name="busy", min_valid_s=0)
    # Three steps are in reach; a fourth only if a window opened meanwhile.
    assert len(got) in (3, 4) and len({g["step"] for g in got}) == len(got) and len(got) + len(busy) == 16
    assert sql("SELECT name, lastStep, issuedCount FROM mfaSecrets ORDER BY name") == (
        ("busy", max(g["step"] for g in got), len(got)), ("intranet", STEP + 3, 4))
    assert sql("SELECT COUNT(*), COUNT(DISTINCT secretId, step) FROM mfaIssues") == ((4 + len(got), 4 + len(got)),)


def test_concurrent_creates_neither_deadlock_nor_take_a_name_twice(mysql):
    store.ensure_schema()

    def create_from_threads(names):
        made, taken, failed = [], [], []
        start = threading.Barrier(len(names))

        def create(name):
            start.wait()
            try:
                made.append(store.create_mfa(name, SEED)["name"])
            except mfa.InvalidSecret as e:
                taken.append(str(e))
            except Exception as e:  # DatabaseUnavailable from a deadlock is the bug this guards against
                failed.append(repr(e))

        threads = [threading.Thread(target=create, args=(name,)) for name in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return made, taken, failed

    for round_ in range(5):  # different names: all of them stored
        names = ["n{}-{}".format(round_, i) for i in range(6)]
        made, taken, failed = create_from_threads(names)
        assert (failed, taken, sorted(made)) == ([], [], names)

    # One name from several threads: one is stored, the others are told it's taken.
    made, taken, failed = create_from_threads(["same"] * 4)
    assert failed == [] and made == ["same"] and len(taken) == 3
    assert all("already a secret called 'same'" in t for t in taken)
    assert sql("SELECT COUNT(*) FROM mfaSecrets") == ((31,),)


def test_checks_get_codes_with_a_managed_client_token(mysql):
    store.ensure_schema()
    store.create_mfa("intranet", SEED)
    token = store.create_token("nightly checks", "client")
    client = grid_app.app.test_client()
    r = client.post("/api/mfa/code", json={"name": "intranet", "label": "login"},
                    headers={"Authorization": "Bearer " + token["token"]})
    assert r.status_code == 200 and r.headers["Cache-Control"] == "no-store"
    assert r.get_json()["code"] == mfa.code(SEED, r.get_json()["step"])
    ((last_used,),) = sql("SELECT lastUsedAt FROM accessTokens WHERE id = %s", (token["id"],))
    assert abs((last_used - utcnow()).total_seconds()) < 10
    assert sql("SELECT issuedTo, label FROM mfaIssues") == (("client token 'nightly checks'", "login"),)
    store.revoke_token(token["id"])
    r = client.post("/api/mfa/code", json={"name": "intranet"}, headers={"Authorization": "Bearer " + token["token"]})
    assert r.status_code == 401
