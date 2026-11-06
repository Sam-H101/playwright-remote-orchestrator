"""MFA secrets: TOTP codes and seed encryption (mfa.py), store.py and the API against sqlite.
Run from the repo root: python -m pytest ui/tests"""
import base64
import contextlib
import datetime
import logging
import sqlite3
import sys
import threading
import time
import types

import pytest

from ui import app as grid_app, auth, demo, mfa, store

ADMIN = {"X-Grid-Token": "s3cret"}
KEY = "a-test-key-for-the-mfa-tests-only"
# The SHA1 seed of RFC 6238 appendix B, as an authenticator app is given it.
RAW_SEED = b"12345678901234567890"
SEED = base64.b32encode(RAW_SEED).decode()
OTHER_SEED = "JBSWY3DPEHPK3PXP"
BOOTSTRAP = "bootstrap-client-token-for-tests"
CHECK = {"Authorization": "Bearer " + BOOTSTRAP}
STEP = 37037036  # the 30 s step of 1111111109, one of the RFC's times


def at(step, seconds_in=0.0):
    """The time seconds_in seconds into a 30 s step's window."""
    return step * 30 + seconds_in


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


# ---- codes and seeds (mfa.py) ----------------------------------------------------------

RFC_SEEDS = {"SHA1": b"12345678901234567890", "SHA256": b"12345678901234567890123456789012",
             "SHA512": b"1234567890123456789012345678901234567890123456789012345678901234"}


@pytest.mark.parametrize("t, sha1, sha256, sha512", [
    (59, "94287082", "46119246", "90693936"),
    (1111111109, "07081804", "68084774", "25091201"),
    (1111111111, "14050471", "67062674", "99943326"),
    (1234567890, "89005924", "91819424", "93441116"),
    (2000000000, "69279037", "90698825", "38618901"),
    (20000000000, "65353130", "77737706", "47863826"),
])
def test_rfc_6238_appendix_b(t, sha1, sha256, sha512):
    for algorithm, expected in (("SHA1", sha1), ("SHA256", sha256), ("SHA512", sha512)):
        seed = mfa.normalize_seed(base64.b32encode(RFC_SEEDS[algorithm]).decode())
        assert mfa.code(seed, mfa.step_at(t, 30), 8, algorithm) == expected, algorithm
    assert mfa.code(SEED, mfa.step_at(t, 30)) == sha1[-6:]  # 6 digits: the last six of the eight


@pytest.mark.parametrize("given", [
    SEED, SEED.lower(), " ".join(SEED[i:i + 4] for i in range(0, len(SEED), 4)),
    "-".join(SEED[i:i + 4].lower() for i in range(0, len(SEED), 4)), SEED + "====", "\t" + SEED + "\n",
])
def test_seeds_are_taken_as_authenticator_apps_take_them(given):
    assert mfa.normalize_seed(given) == SEED


def test_seed_length_limits():
    assert mfa.normalize_seed("A" * 16) == "A" * 16  # 10 bytes
    assert mfa.normalize_seed("A" * 205) == "A" * 205  # 128 bytes


@pytest.mark.parametrize("given", [
    None, 12345, SEED.encode(), "", "   ", "====",
    SEED[:-1] + "1", SEED[:-1] + "0", SEED[:-1] + "8", SEED[:8] + "_" + SEED[9:], SEED + "é",
    "A" * 15,   # 9 bytes
    "A" * 208,  # 130 bytes
    "AAA", "A" * 9,  # not a whole number of bytes
])
def test_seeds_that_are_refused(given):
    with pytest.raises(mfa.InvalidSecret) as e:
        mfa.normalize_seed(given)
    if isinstance(given, str) and len(given.strip()) > 4:
        assert given.strip() not in str(e.value)


def test_an_enrolment_uri_gives_every_setting():
    parsed = mfa.parse_otpauth(" otpauth://totp/ACME%20Co:jane%40example.com?secret={}&issuer=ACME%20Co"
                               "&algorithm=sha256&digits=8&period=60 ".format(SEED.lower()))
    assert parsed == {"seed": SEED, "issuer": "ACME Co", "account": "jane@example.com", "algorithm": "SHA256",
                      "digits": 8, "period": 60}


@pytest.mark.parametrize("uri, issuer, account", [
    ("otpauth://totp/Intranet:svc-monitor?secret={}", "Intranet", "svc-monitor"),
    ("otpauth://totp/Intranet%3Asvc-monitor?secret={}", "Intranet", "svc-monitor"),
    ("otpauth://totp/Old%20name:svc-monitor?secret={}&issuer=New+name", "New name", "svc-monitor"),
    ("otpauth://totp/svc-monitor?secret={}", None, "svc-monitor"),
    ("otpauth://totp/svc-monitor?secret={}&issuer=Intranet", "Intranet", "svc-monitor"),
    ("OTPAUTH://TOTP/?secret={}", None, None),
])
def test_the_issuer_comes_from_the_parameter_before_the_label(uri, issuer, account):
    parsed = mfa.parse_otpauth(uri.format(SEED))
    assert (parsed["issuer"], parsed["account"]) == (issuer, account)
    assert (parsed["seed"], parsed["algorithm"], parsed["digits"], parsed["period"]) == (SEED, "SHA1", 6, 30)


@pytest.mark.parametrize("uri", [
    "otpauth://hotp/svc?secret={}&counter=0",
    "https://example.com/totp/svc?secret={}",
    "otpauth://totp/svc?secret={}&digits=9",
    "otpauth://totp/svc?secret={}&digits=4",
    "otpauth://totp/svc?secret={}&digits=six",
    "otpauth://totp/svc?secret={}&period=5",
    "otpauth://totp/svc?secret={}&period=301",
    "otpauth://totp/svc?secret={}&period=30s",
    "otpauth://totp/svc?secret={}&algorithm=MD5",
    "otpauth://totp/svc?issuer=Intranet",
    "otpauth://totp/svc?secret=not-base32!",
])
def test_enrolment_uris_that_are_refused(uri):
    with pytest.raises(mfa.InvalidSecret) as e:
        mfa.parse_otpauth(uri.format(SEED))
    assert SEED not in str(e.value)


def test_counter_based_codes_are_refused_by_name():
    with pytest.raises(mfa.InvalidSecret, match="hotp"):
        mfa.parse_otpauth("otpauth://hotp/svc?secret={}&counter=0".format(SEED))


def test_seeds_are_encrypted_and_bound_to_their_name(monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    stored = mfa.encrypt(SEED, "intranet")
    assert SEED not in stored and RAW_SEED not in base64.b64decode(stored) and SEED.encode() not in base64.b64decode(stored)
    assert stored != mfa.encrypt(SEED, "intranet")  # a new nonce each time
    assert mfa.decrypt(stored, "intranet") == SEED

    with pytest.raises(mfa.CannotDecrypt) as e:
        mfa.decrypt(stored, "intranet2")  # copied to another secret's row
    assert SEED not in str(e.value) and "intranet2" in str(e.value)
    flipped = bytearray(base64.b64decode(stored))
    flipped[-1] ^= 1
    for bad in (base64.b64encode(bytes(flipped)).decode(), "not base64!", "", base64.b64encode(b"short").decode()):
        with pytest.raises(mfa.CannotDecrypt):
            mfa.decrypt(bad, "intranet")

    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    with pytest.raises(mfa.CannotDecrypt):
        mfa.decrypt(stored, "intranet")


def test_the_secrets_key_must_be_set_and_long_enough(monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    stored = mfa.encrypt(SEED, "intranet")
    monkeypatch.delenv(mfa.KEY_ENV)
    assert mfa.key_problem() == "Set GRID_SECRETS_KEY to store MFA secrets."
    for call in (lambda: mfa.encrypt(SEED, "intranet"), lambda: mfa.decrypt(stored, "intranet")):
        with pytest.raises(mfa.SecretsKeyMissing):
            call()
    monkeypatch.setenv(mfa.KEY_ENV, "k" * 15)
    assert mfa.key_problem() == "GRID_SECRETS_KEY must be at least 16 characters long."
    with pytest.raises(mfa.SecretsKeyMissing):
        mfa.decrypt(stored, "intranet")
    monkeypatch.setenv(mfa.KEY_ENV, "k" * 16)
    assert mfa.key_problem() is None


def test_without_the_cryptography_package_seeds_are_unavailable(monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    stored = mfa.encrypt(SEED, "intranet")
    for module in ("cryptography", "cryptography.exceptions"):
        monkeypatch.setitem(sys.modules, module, None)  # as if it weren't installed
    assert "cryptography" in mfa.key_problem()
    for call in (lambda: mfa.encrypt(SEED, "intranet"), lambda: mfa.decrypt(stored, "intranet")):
        with pytest.raises(mfa.SecretsKeyMissing):
            call()


# ---- store.py ----------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        demo.create_schema(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return path


def run(path, sql, params=()):
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute(sql, params).fetchall()


def used(path):
    """(lastStep, issuedCount) of every secret, and the number of hand-outs logged."""
    return run(path, "SELECT name, lastStep, issuedCount FROM mfaSecrets ORDER BY name"), \
        run(path, "SELECT COUNT(*) FROM mfaIssues")[0][0]


def test_create_stores_only_the_encrypted_seed(db):
    made = store.create_mfa(" intranet ", "  " + SEED.lower() + "  ", "Intranet", "svc-monitor")
    assert made == {"id": made["id"], "name": "intranet", "issuer": "Intranet", "account": "svc-monitor",
                    "algorithm": "SHA1", "digits": 6, "period": 30, "createdAt": made["createdAt"], "lastStep": None,
                    "lastIssuedAt": None, "lastIssuedTo": None, "issuedCount": 0}
    assert abs((store._parse_time(made["createdAt"]) - utcnow()).total_seconds()) < 10

    rows = repr(run(db, "SELECT * FROM mfaSecrets"))
    for form in (SEED, SEED.lower(), RAW_SEED.decode(), RAW_SEED.hex()):
        assert form not in rows
    ((cipher,),) = run(db, "SELECT seedCipher FROM mfaSecrets")
    assert mfa.decrypt(cipher, "intranet") == SEED

    listing = store.list_mfa()
    assert listing == {"ready": True, "generated": listing["generated"], "secrets": [made], "issues": []}
    assert SEED not in repr(listing) and cipher not in repr(listing)


def test_create_from_an_enrolment_uri(db):
    uri = "otpauth://totp/ACME:jane%40example.com?secret={}&issuer=ACME&algorithm=SHA512&digits=8&period=60".format(SEED)
    made = store.create_mfa("acme", uri)
    assert [made[k] for k in ("issuer", "account", "algorithm", "digits", "period")] == [
        "ACME", "jane@example.com", "SHA512", 8, 60]
    made = store.create_mfa("acme-2", uri, issuer="ACME intranet", digits=6)  # given fields win over the URI's
    assert [made[k] for k in ("issuer", "account", "digits", "period")] == ["ACME intranet", "jane@example.com", 6, 60]
    assert SEED not in repr(run(db, "SELECT * FROM mfaSecrets"))
    assert store.peek_mfa_code(made["id"], now=at(STEP))["code"] == mfa.code(SEED, mfa.step_at(at(STEP), 60), 6, "SHA512")


def test_names_are_unique(db, monkeypatch):
    store.create_mfa("intranet", SEED)
    with pytest.raises(mfa.InvalidSecret, match="already"):
        store.create_mfa("intranet", OTHER_SEED)
    assert run(db, "SELECT COUNT(*) FROM mfaSecrets") == [(1,)]

    # A create that raced past the look-up: the unique key refuses it, with the same answer.
    real = store._sql
    monkeypatch.setattr(store, "_sql", lambda q: real(q.replace("WHERE name = ?", "WHERE name = ? AND 0 = 1")))
    with pytest.raises(mfa.InvalidSecret, match="already a secret called 'intranet'"):
        store.create_mfa("intranet", OTHER_SEED)
    assert run(db, "SELECT COUNT(*) FROM mfaSecrets") == [(1,)]


@pytest.mark.parametrize("kwargs", [
    dict(name="", secret=SEED),
    dict(name="has space", secret=SEED),
    dict(name="x" * 65, secret=SEED),
    dict(name=None, secret=SEED),
    dict(name="naïve", secret=SEED),
    dict(name=["intranet"], secret=SEED),
    dict(name="ok", secret=None),
    dict(name="ok", secret={"seed": SEED}),
    dict(name="ok", secret="0189" + SEED),
    dict(name="ok", secret="AAAA"),
    dict(name="ok", secret="otpauth://hotp/x?secret=" + SEED),
    dict(name="ok", secret=SEED, algorithm="MD5"),
    dict(name="ok", secret=SEED, algorithm=["SHA1"]),  # not hashable: must be refused, not a TypeError
    dict(name="ok", secret=SEED, digits=9),
    dict(name="ok", secret=SEED, digits=True),
    dict(name="ok", secret=SEED, digits="6"),
    dict(name="ok", secret=SEED, period=5),
    dict(name="ok", secret=SEED, period="30"),
    dict(name="ok", secret=SEED, period=30.5),
    dict(name="ok", secret=SEED, issuer="x" * 129),
    dict(name="ok", secret=SEED, issuer="tab\there"),
    dict(name="ok", secret=SEED, account=12),
    dict(name="ok", secret=SEED, account="lone \ud800 surrogate"),
])
def test_create_validation(db, kwargs):
    with pytest.raises(mfa.InvalidSecret) as e:
        store.create_mfa(**kwargs)
    assert SEED not in str(e.value)
    assert run(db, "SELECT COUNT(*) FROM mfaSecrets") == [(0,)]


def test_nothing_is_stored_without_the_secrets_key(db, monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, "too-short")
    with pytest.raises(mfa.SecretsKeyMissing):
        store.create_mfa("intranet", SEED)
    assert run(db, "SELECT COUNT(*) FROM mfaSecrets") == [(0,)]


def test_delete_takes_the_log_with_it(db):
    made = store.create_mfa("intranet", SEED)
    other = store.create_mfa("other", OTHER_SEED)
    store.issue_mfa_code("c", secret_id=made["id"], now=at(STEP))
    store.issue_mfa_code("c", secret_id=other["id"], now=at(STEP))
    assert store.delete_mfa(made["id"]) is True
    assert store.delete_mfa(made["id"]) is False
    assert [s["name"] for s in store.list_mfa()["secrets"]] == ["other"]
    assert run(db, "SELECT secretId FROM mfaIssues") == [(other["id"],)]
    for unknown in (0, -1, 9999, 2 ** 31, 2 ** 63, 10 ** 20):
        assert store.delete_mfa(unknown) is False
    assert store.issue_mfa_code("c", name="intranet") is None


def test_each_step_is_handed_out_once(db):
    sid = store.create_mfa("intranet", SEED)["id"]
    now = at(STEP, 10)  # 20 s of the window left
    first = store.issue_mfa_code("check a", name="intranet", now=now)
    assert first == {"id": sid, "name": "intranet", "code": "081804", "step": STEP, "waitS": 0.0, "validForS": 20.0,
                     "period": 30, "digits": 6}
    second = store.issue_mfa_code("check b", name="intranet", now=now)
    assert (second["step"], second["waitS"], second["validForS"]) == (STEP + 1, 20.0, 50.0)
    third = store.issue_mfa_code("check c", secret_id=sid, now=now)
    assert (third["step"], third["waitS"], third["validForS"]) == (STEP + 2, 50.0, 80.0)
    assert [second["code"], third["code"]] == [mfa.code(SEED, STEP + 1), mfa.code(SEED, STEP + 2)]

    with pytest.raises(store.MfaBusy) as busy:
        store.issue_mfa_code("check d", name="intranet", now=now)
    assert busy.value.retry_after_s == 20.0  # STEP + 3 may go out once STEP + 1 is the current step
    assert "20 s" in str(busy.value)
    later = store.issue_mfa_code("check d", name="intranet", now=now + busy.value.retry_after_s)
    assert (later["step"], later["waitS"], later["validForS"]) == (STEP + 3, 60.0, 90.0)

    # A clock that went back never gets a step again; it waits until the steps ahead are in reach.
    with pytest.raises(store.MfaBusy) as busy:
        store.issue_mfa_code("check e", name="intranet", now=at(STEP - 5))
    assert busy.value.retry_after_s == 7 * 30
    # Once the steps handed out are past, the current one is free again.
    fresh = store.issue_mfa_code("check f", name="intranet", now=at(STEP + 10, 1.5))
    assert (fresh["step"], fresh["waitS"], fresh["validForS"]) == (STEP + 10, 0.0, 28.5)
    assert run(db, "SELECT lastStep, issuedCount, lastIssuedTo FROM mfaSecrets") == [(STEP + 10, 5, "check f")]


def test_busy_never_waits_less_than_a_second(db):
    store.create_mfa("intranet", SEED)
    for _ in range(3):
        store.issue_mfa_code("c", name="intranet", now=at(STEP, 29.5), min_valid_s=0)
    with pytest.raises(store.MfaBusy) as busy:
        store.issue_mfa_code("c", name="intranet", now=at(STEP, 29.5), min_valid_s=0)
    assert busy.value.retry_after_s == 1.0  # 0.5 s to the next window, rounded up to a second


@pytest.mark.parametrize("seconds_in, min_valid_s, ahead", [
    (27, None, 1),    # 3 s left: less than the 5 s a code keeps by default
    (25, None, 0),    # exactly 5 s left
    (27, 0, 0),
    (27, 3, 0),
    (27, 3.5, 1),
    (0, 1000, 0),     # more than a window has: as much as one can give (29 s); a fresh one has 30
    (2, 1000, 1),
    pytest.param(27, 10 ** 400, 1, id="27-past-float-1"),  # JSON can send it
])
def test_a_code_about_to_expire_is_skipped(db, seconds_in, min_valid_s, ahead):
    store.create_mfa("intranet", SEED)
    got = store.issue_mfa_code("c", name="intranet", min_valid_s=min_valid_s, now=at(STEP, seconds_in))
    assert got["step"] == STEP + ahead
    assert got["waitS"] == (30 - seconds_in if ahead else 0)
    assert got["validForS"] == 30 * (ahead + 1) - seconds_in


def test_a_seed_that_does_not_decrypt_uses_nothing_up(db, monkeypatch):
    sid = store.create_mfa("intranet", SEED)["id"]
    store.create_mfa("other", OTHER_SEED)
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    with pytest.raises(mfa.CannotDecrypt):
        store.issue_mfa_code("c", name="intranet", now=at(STEP))
    with pytest.raises(mfa.CannotDecrypt):
        store.peek_mfa_code(sid, now=at(STEP))
    monkeypatch.delenv(mfa.KEY_ENV)
    with pytest.raises(mfa.SecretsKeyMissing):
        store.issue_mfa_code("c", secret_id=sid, now=at(STEP))
    assert used(db) == ([("intranet", None, 0), ("other", None, 0)], 0)

    # A ciphertext copied from another secret's row doesn't decrypt either.
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    run(db, "UPDATE mfaSecrets SET seedCipher = (SELECT seedCipher FROM mfaSecrets WHERE name = 'intranet') "
            "WHERE name = 'other'")
    with pytest.raises(mfa.CannotDecrypt):
        store.issue_mfa_code("c", name="other", now=at(STEP))
    assert store.issue_mfa_code("c", name="intranet", now=at(STEP))["code"] == "081804"
    assert used(db) == ([("intranet", STEP, 1), ("other", None, 0)], 1)


def test_unknown_secrets_and_bad_labels(db):
    store.create_mfa("intranet", SEED)
    assert store.issue_mfa_code("c", name="nope", now=at(STEP)) is None
    for unknown in (9999, 0, -1, 2 ** 31, 2 ** 63, 10 ** 20):
        assert store.issue_mfa_code("c", secret_id=unknown, now=at(STEP)) is None
        assert store.peek_mfa_code(unknown, now=at(STEP)) is None
    for label in ("x" * 129, 5, "new\nline", ["x"]):
        with pytest.raises(mfa.InvalidSecret):
            store.issue_mfa_code("c", name="intranet", label=label, now=at(STEP))
    assert used(db) == ([("intranet", None, 0)], 0)


def test_hand_outs_are_logged_for_a_week(db):
    a = store.create_mfa("a", SEED)["id"]
    b = store.create_mfa("b", OTHER_SEED)["id"]
    run(db, "INSERT INTO mfaIssues (secretId, step, issuedAt, issuedTo) VALUES (?, 1, ?, 'old'), (?, 2, ?, 'recent'), "
            "(?, 3, ?, 'old, another secret')", (a, ts(8 * 86400), a, ts(6 * 86400), b, ts(8 * 86400)))
    store.issue_mfa_code("client token 'checks'", name="a", label=" nightly login ", now=at(STEP))
    store.issue_mfa_code("GATEWAY_CLIENT_TOKEN", name="a", now=at(STEP))
    # Every secret's old ones go, not only those of the secret handed out.
    assert run(db, "SELECT secretId, step, issuedTo, label FROM mfaIssues ORDER BY id") == [
        (a, 2, "recent", None), (a, STEP, "client token 'checks'", "nightly login"), (a, STEP + 1, "GATEWAY_CLIENT_TOKEN", None)]

    listing = store.list_mfa()
    newest = listing["issues"][0]
    assert newest == {"id": newest["id"], "secretId": a, "name": "a", "step": STEP + 1, "issuedAt": newest["issuedAt"],
                      "issuedTo": "GATEWAY_CLIENT_TOKEN", "label": None}
    assert [i["label"] for i in listing["issues"]] == [None, "nightly login", None]
    assert abs((store._parse_time(newest["issuedAt"]) - utcnow()).total_seconds()) < 10
    secret = listing["secrets"][0]
    assert (secret["lastStep"], secret["issuedCount"], secret["lastIssuedTo"]) == (STEP + 1, 2, "GATEWAY_CLIENT_TOKEN")
    assert secret["lastIssuedAt"].endswith("Z")


def test_the_log_shows_only_the_last_week(db):
    a = store.create_mfa("a", SEED)["id"]
    b = store.create_mfa("b", OTHER_SEED)["id"]
    # Nothing handed out since, so nothing purged them: still not "in the last 7 days".
    run(db, "INSERT INTO mfaIssues (secretId, step, issuedAt, issuedTo) VALUES (?, 1, ?, 'old'), (?, 2, ?, 'recent')",
        (b, ts(8 * 86400), a, ts(6 * 86400)))
    assert [(i["name"], i["issuedTo"]) for i in store.list_mfa()["issues"]] == [("a", "recent")]


def test_the_log_shows_the_newest_hand_outs(db, monkeypatch):
    monkeypatch.setattr(store, "MFA_LOG_LIMIT", 3)
    store.create_mfa("intranet", SEED)
    for i in range(5):
        store.issue_mfa_code("c", name="intranet", now=at(STEP + i * 10))
    assert [i["step"] for i in store.list_mfa()["issues"]] == [STEP + 40, STEP + 30, STEP + 20]


def test_peek_uses_nothing_up(db):
    sid = store.create_mfa("intranet", SEED, digits=8)["id"]
    seen = store.peek_mfa_code(sid, now=at(STEP, 10))
    assert seen == {"id": sid, "name": "intranet", "code": "07081804", "step": STEP, "secondsLeft": 20.0, "period": 30,
                    "digits": 8, "issued": False}
    assert store.peek_mfa_code(sid, now=at(STEP, 10)) == seen
    assert used(db) == ([("intranet", None, 0)], 0)

    assert store.issue_mfa_code("c", secret_id=sid, now=at(STEP, 10))["step"] == STEP  # the peek took nothing
    assert store.peek_mfa_code(sid, now=at(STEP, 11))["issued"] is True
    assert store.issue_mfa_code("c", secret_id=sid, now=at(STEP, 11))["step"] == STEP + 1
    assert store.peek_mfa_code(sid, now=at(STEP + 1))["issued"] is True
    assert store.peek_mfa_code(sid, now=at(STEP + 2))["issued"] is False
    assert used(db) == ([("intranet", STEP + 1, 2)], 2)


def test_concurrent_requests_never_get_the_same_step(db):
    store.create_mfa("intranet", SEED)
    got, busy = [], []
    start = threading.Barrier(8)

    def ask():
        start.wait()
        try:
            got.append(store.issue_mfa_code("c", name="intranet", now=at(STEP, 10)))
        except store.MfaBusy as e:
            busy.append(e)

    threads = [threading.Thread(target=ask) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(g["step"] for g in got) == [STEP, STEP + 1, STEP + 2] and len(busy) == 5
    assert used(db) == ([("intranet", STEP + 2, 3)], 3)


def test_a_database_without_the_mfa_tables(db):
    run(db, "DROP TABLE mfaIssues")
    run(db, "DROP TABLE mfaSecrets")
    listing = store.list_mfa()
    assert listing["ready"] is False and listing["secrets"] == [] and listing["issues"] == []
    with pytest.raises(store.MfaUnavailable):
        store.create_mfa("intranet", SEED)
    with pytest.raises(store.MfaUnavailable):
        store.issue_mfa_code("c", name="intranet")
    assert store.peek_mfa_code(1) is None and store.delete_mfa(1) is False

    client = grid_app.app.test_client()
    r = client.get("/api/mfa", headers=ADMIN)
    assert r.status_code == 200 and r.get_json()["ready"] is False
    r = client.post("/api/mfa", json={"name": "intranet", "secret": SEED}, headers=ADMIN)
    assert r.status_code == 503 and "mfaSecrets" in r.get_json()["error"]
    assert r.headers["Cache-Control"] == "no-store"


# ---- API ---------------------------------------------------------------------------------


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    return grid_app.app.test_client()


def private(r):
    assert r.headers.get("Cache-Control") == "no-store", (r.request.method, r.request.path, r.status_code)
    return r


def ask(client, name="intranet", headers=CHECK, **body):
    return private(client.post("/api/mfa/code", json=dict(body, name=name), headers=headers))


def admin_requests(client, sid, headers):
    return [client.get("/api/mfa", headers=headers),
            client.post("/api/mfa", json={"name": "new", "secret": OTHER_SEED}, headers=headers),
            client.get("/api/mfa/{}/code".format(sid), headers=headers),
            client.post("/api/mfa/{}/use".format(sid), json={}, headers=headers),
            client.post("/api/mfa/{}/delete".format(sid), json={}, headers=headers)]


def test_every_admin_route_needs_grid_admin_token_to_be_set(client, db, monkeypatch):
    sid = store.create_mfa("intranet", SEED)["id"]
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    for headers in ({}, {"X-Grid-Token": "anything"}, CHECK):
        for r in admin_requests(client, sid, headers):
            assert private(r).status_code == 403
            assert r.get_json() == {"error": "set GRID_ADMIN_TOKEN to manage access tokens", "admin_token_unset": True}
    assert used(db) == ([("intranet", None, 0)], 0)
    # Checks don't sign in as an admin: they get codes whether or not one is configured.
    assert ask(client).status_code == 200


def test_admin_routes_check_the_admin_token(client, db):
    sid = store.create_mfa("intranet", SEED)["id"]
    for headers in ({}, {"X-Grid-Token": "wrong"}, {"X-Grid-Token": "s3cretX"}, CHECK):
        for r in admin_requests(client, sid, headers):
            assert private(r).status_code == 403 and r.get_json()["token_required"] is True
    assert used(db) == ([("intranet", None, 0)], 0)


def test_create_list_peek_use_and_delete_through_the_api(client):
    r = private(client.post("/api/mfa", json={"name": "intranet", "secret": SEED.lower(), "issuer": "Intranet"},
                            headers=ADMIN))
    assert r.status_code == 201
    made = r.get_json()
    assert made["name"] == "intranet" and made["issuer"] == "Intranet" and made["issuedCount"] == 0
    assert "seed" not in made and "secret" not in made and "seedCipher" not in made
    # Made with the admin token, it is the admins', and the token may do anything with it.
    assert {k: made[k] for k in ("owner", "access", "canManage", "sharedWith")} == {
        "owner": None, "access": "all", "canManage": True, "sharedWith": []}

    body = private(client.get("/api/mfa", headers=ADMIN)).get_json()
    assert body["ready"] is True and body["keyProblem"] is None and abs(body["serverTime"] - time.time()) < 5
    assert body["secrets"] == [made] and body["issues"] == []

    path = "/api/mfa/{}/".format(made["id"])
    peek = private(client.get(path + "code", headers=ADMIN)).get_json()
    assert peek["code"] == mfa.code(SEED, peek["step"]) and peek["issued"] is False and 0 < peek["secondsLeft"] <= 30
    taken = private(client.post(path + "use", json={}, headers=ADMIN)).get_json()
    assert taken["code"] == mfa.code(SEED, taken["step"]) and taken["validForS"] >= store.MFA_MIN_VALID_S
    check = ask(client, label="nightly login")
    assert check.status_code == 200
    assert check.get_json()["step"] == taken["step"] + 1  # never the code the admin took

    issues = private(client.get("/api/mfa", headers=ADMIN)).get_json()["issues"]
    assert [(i["issuedTo"], i["label"]) for i in issues] == [("GATEWAY_CLIENT_TOKEN", "nightly login"), ("Admin", "Grid UI")]

    assert private(client.post(path + "delete", json={}, headers=ADMIN)).get_json() == {"deleted": True, "id": made["id"]}
    assert private(client.post(path + "delete", json={}, headers=ADMIN)).status_code == 404


def test_unknown_secrets_are_404s(client):
    store.create_mfa("intranet", SEED)
    for r in (client.get("/api/mfa/9999/code", headers=ADMIN),
              client.get("/api/mfa/{}/code".format(10 ** 20), headers=ADMIN),
              client.post("/api/mfa/9999/use", json={}, headers=ADMIN),
              client.post("/api/mfa/0/use", json={}, headers=ADMIN),
              client.post("/api/mfa/{}/use".format(2 ** 63), json={}, headers=ADMIN),
              client.post("/api/mfa/9999/delete", json={}, headers=ADMIN),
              client.post("/api/mfa/{}/delete".format(2 ** 63), json={}, headers=ADMIN),
              client.post("/api/mfa/code", json={"name": "nope"}, headers=CHECK)):
        assert private(r).status_code == 404 and r.get_json()["error"], r.request.path


def test_bad_requests_are_400s(client, db):
    store.create_mfa("intranet", SEED)
    for body in ('["not", "an", "object"]',
                 '{"name": "x", "secret": "0189"}',
                 '{"name": "bad name", "secret": "%s"}' % OTHER_SEED,
                 '{"name": "x", "secret": "%s", "algorithm": ["SHA1"]}' % OTHER_SEED,
                 '{"name": "x", "secret": "%s", "digits": "8"}' % OTHER_SEED,
                 '{"name": "x", "secret": "%s", "issuer": "\\ud800"}' % OTHER_SEED,
                 '{"name": "intranet", "secret": "%s"}' % OTHER_SEED):
        r = private(client.post("/api/mfa", data=body, content_type="application/json", headers=ADMIN))
        assert r.status_code == 400 and r.get_json()["error"], body
    for body in ("", "[]", "{}", '{"name": ""}', '{"name": "  "}', '{"name": 5}', '{"name": ["intranet"]}',
                 '{"name": "intranet", "minValidS": -1}', '{"name": "intranet", "minValidS": true}',
                 '{"name": "intranet", "minValidS": "5"}', '{"name": "intranet", "label": 5}',
                 '{"name": "intranet", "label": "%s"}' % ("x" * 129)):
        r = private(client.post("/api/mfa/code", data=body, content_type="application/json", headers=CHECK))
        assert r.status_code == 400 and r.get_json()["error"], body
    assert run(db, "SELECT name FROM mfaSecrets") == [("intranet",)]
    assert used(db) == ([("intranet", None, 0)], 0)


def test_min_valid_s_takes_any_number(client):
    store.create_mfa("intranet", SEED)
    for value in (0, 2.5, 10 ** 400):
        r = ask(client, minValidS=value)
        assert r.status_code == 200, value
    r = private(client.post("/api/mfa/code", data='{"name": "intranet", "minValidS": 1e999}',
                            content_type="application/json", headers=CHECK))
    assert r.status_code in (200, 429)


def test_without_the_secrets_key(client, db, monkeypatch):
    sid = store.create_mfa("intranet", SEED)["id"]  # stored while the key was set
    monkeypatch.delenv(mfa.KEY_ENV)
    body = private(client.get("/api/mfa", headers=ADMIN)).get_json()
    assert body["keyProblem"] == "Set GRID_SECRETS_KEY to store MFA secrets."
    assert [s["name"] for s in body["secrets"]] == ["intranet"]  # what is stored is still listed
    for r in (client.post("/api/mfa", json={"name": "new", "secret": OTHER_SEED}, headers=ADMIN),
              client.get("/api/mfa/{}/code".format(sid), headers=ADMIN),
              client.post("/api/mfa/{}/use".format(sid), json={}, headers=ADMIN),
              client.post("/api/mfa/code", json={"name": "intranet"}, headers=CHECK)):
        assert private(r).status_code == 503 and r.get_json()["secrets_key_unset"] is True, r.request.path
    monkeypatch.setenv(mfa.KEY_ENV, "short")
    assert "at least 16" in client.get("/api/mfa", headers=ADMIN).get_json()["keyProblem"]
    assert used(db) == ([("intranet", None, 0)], 0)


def test_a_changed_secrets_key_is_a_409(client, db, monkeypatch):
    sid = store.create_mfa("intranet", SEED)["id"]
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    for r in (client.get("/api/mfa/{}/code".format(sid), headers=ADMIN),
              client.post("/api/mfa/{}/use".format(sid), json={}, headers=ADMIN),
              client.post("/api/mfa/code", json={"name": "intranet"}, headers=CHECK)):
        assert private(r).status_code == 409 and r.get_json()["cannot_decrypt"] is True, r.request.path
        assert SEED not in r.get_data(as_text=True)
    assert used(db) == ([("intranet", None, 0)], 0)
    assert private(client.post("/api/mfa/{}/delete".format(sid), json={}, headers=ADMIN)).status_code == 200


def test_handing_out_faster_than_codes_change_is_a_429(client, monkeypatch):
    clock = [at(STEP, 10)]
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: clock[0]))
    sid = store.create_mfa("intranet", SEED)["id"]
    steps = [ask(client).get_json()["step"], ask(client).get_json()["step"],
             private(client.post("/api/mfa/{}/use".format(sid), json={}, headers=ADMIN)).get_json()["step"]]
    assert steps == [STEP, STEP + 1, STEP + 2]
    for r in (ask(client), client.post("/api/mfa/{}/use".format(sid), json={}, headers=ADMIN)):
        assert private(r).status_code == 429
        assert r.get_json()["retry_after_s"] == 20.0 and r.headers["Retry-After"] == "20"
    clock[0] += 20
    got = ask(client).get_json()
    assert (got["step"], got["waitS"]) == (STEP + 3, 60.0)


def test_the_bootstrap_client_token_needs_its_real_value(client, db, monkeypatch):
    store.create_mfa("intranet", SEED)
    for value in ("set", "x" * 15):  # older compose files gave the UI the word "set"
        monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", value)
        r = ask(client, headers={"Authorization": "Bearer " + value})
        assert r.status_code == 401 and r.get_json()["client_token_required"] is True
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "x" * 16)
    assert ask(client, headers={"Authorization": "Bearer " + "x" * 16}).status_code == 200
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN")
    assert ask(client, headers={"Authorization": "Bearer " + "x" * 16}).status_code == 401
    assert run(db, "SELECT issuedTo FROM mfaIssues") == [("GATEWAY_CLIENT_TOKEN",)]


def test_managed_client_tokens(client, db, monkeypatch):
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN")
    store.create_mfa("intranet", SEED)
    active = store.create_token("nightly checks", "client")
    revoked = store.create_token("revoked", "client")
    store.revoke_token(revoked["id"])
    expired = store.create_token("expired", "client", expires_in_days=1)
    run(db, "UPDATE accessTokens SET expiresAt = ? WHERE id = ?", (ts(60), expired["id"]))
    agent = store.create_token("agent", "agent")
    for token in (revoked, expired, agent):
        r = ask(client, headers={"Authorization": "Bearer " + token["token"]})
        assert r.status_code == 401 and r.get_json()["client_token_required"] is True, token["name"]
    assert run(db, "SELECT COUNT(*) FROM accessTokens WHERE lastUsedAt IS NOT NULL") == [(0,)]

    assert ask(client, headers={"Authorization": "Bearer " + active["token"]}).status_code == 200
    assert run(db, "SELECT name FROM accessTokens WHERE lastUsedAt IS NOT NULL") == [("nightly checks",)]
    (last_used,) = run(db, "SELECT lastUsedAt FROM accessTokens WHERE id = ?", (active["id"],))[0]
    assert abs((store._parse_time(last_used) - utcnow()).total_seconds()) < 10
    assert run(db, "SELECT issuedTo FROM mfaIssues") == [("client token 'nightly checks'",)]


def test_the_check_route_needs_a_bearer_token(client):
    store.create_mfa("intranet", SEED)
    for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer    "},
                    {"Authorization": "Basic " + BOOTSTRAP}, {"Authorization": BOOTSTRAP}, ADMIN):
        r = ask(client, headers=headers)
        assert r.status_code == 401 and r.get_json()["client_token_required"] is True, headers
    for _ in range(auth.FAILURE_LIMIT + 5):  # no token at all isn't a guess
        assert ask(client, headers={}).status_code == 401
    assert ask(client).status_code == 200
    assert ask(client, headers={"Authorization": "bearer  " + BOOTSTRAP}).status_code == 200


def test_wrong_client_tokens_count_towards_the_guessing_limit(client):
    store.create_mfa("intranet", SEED)
    for i in range(auth.FAILURE_LIMIT):
        assert ask(client, headers={"Authorization": "Bearer wrong-{}".format(i)}).status_code == 401
    r = ask(client)  # even the right token now
    assert r.status_code == 429 and r.get_json()["retry_after_s"] > 0
    assert int(r.headers["Retry-After"]) == r.get_json()["retry_after_s"]
    # It is the limit admin sign-in has.
    assert client.get("/api/mfa", headers=ADMIN).status_code == 429
    auth.reset_failures()
    assert ask(client).status_code == 200

    for i in range(auth.FAILURE_LIMIT):
        client.get("/api/mfa", headers={"X-Grid-Token": "wrong-{}".format(i)})
    assert ask(client).status_code == 429


def test_only_the_check_route_skips_the_same_origin_guard(client, db, monkeypatch):
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())
    sid = store.create_mfa("intranet", SEED)["id"]
    cross = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    # Checks reach the UI by its compose name, which no browser page of the grid uses.
    r = ask(client, headers=dict(CHECK, **cross))
    assert r.status_code == 200
    r = private(client.post("/api/mfa/code", base_url="http://grid_ui:4444", json={"name": "intranet"}, headers=CHECK))
    assert r.status_code == 200

    for url, body in (("/api/mfa", {"name": "new", "secret": OTHER_SEED}), ("/api/mfa/{}/use".format(sid), {}),
                      ("/api/mfa/{}/delete".format(sid), {}), ("/api/mfa/code/", {"name": "intranet"})):
        for kwargs in (dict(headers=dict(ADMIN, **cross)), dict(headers=dict(ADMIN, **CHECK), base_url="http://grid_ui:4444")):
            r = client.post(url, json=body, **kwargs)
            assert r.status_code == 403, (url, kwargs)
            assert "token_required" not in r.get_json() and "admin_token_unset" not in r.get_json()
    assert run(db, "SELECT name, issuedCount FROM mfaSecrets") == [("intranet", 2)]


def test_no_answer_and_no_log_record_holds_a_seed_or_a_token(client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    # Three codes go out below: from the start of a window, all three are in reach.
    monkeypatch.setattr(store, "time", types.SimpleNamespace(time=lambda: at(STEP, 1)))
    managed = store.create_token("checks", "client")["token"]
    answers = []

    def keep(r):
        answers.append(r)
        return r

    spaced = " ".join(SEED[i:i + 4].lower() for i in range(0, len(SEED), 4))
    sid = keep(client.post("/api/mfa", json={"name": "intranet", "secret": spaced}, headers=ADMIN)).get_json()["id"]
    keep(client.post("/api/mfa", json={"name": "acme", "secret": "otpauth://totp/ACME:jane?secret={}".format(OTHER_SEED)},
                     headers=ADMIN))
    keep(client.post("/api/mfa", json={"name": "intranet", "secret": SEED}, headers=ADMIN))
    keep(client.post("/api/mfa", json={"name": "bad", "secret": SEED + "!"}, headers=ADMIN))
    keep(client.post("/api/mfa", json={"name": "bad", "secret": "otpauth://totp/x?secret={}&digits=9".format(SEED)},
                     headers=ADMIN))
    keep(client.get("/api/mfa", headers=ADMIN))
    keep(client.get("/api/mfa/{}/code".format(sid), headers=ADMIN))
    keep(client.post("/api/mfa/{}/use".format(sid), json={}, headers=ADMIN))
    keep(ask(client, label="nightly"))
    keep(ask(client, headers={"Authorization": "Bearer " + managed}))
    keep(ask(client, name="nope"))
    keep(ask(client, headers={"Authorization": "Bearer wrong-" + BOOTSTRAP}))
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    keep(client.get("/api/mfa/{}/code".format(sid), headers=ADMIN))
    keep(ask(client))
    monkeypatch.delenv(mfa.KEY_ENV)
    keep(client.get("/api/mfa", headers=ADMIN))
    keep(ask(client))
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    keep(client.post("/api/mfa/{}/delete".format(sid), json={}, headers=ADMIN))

    assert [r.status_code for r in answers] == [201, 201, 400, 400, 400, 200, 200, 200, 200, 200, 404, 401, 409, 409,
                                                200, 503, 200]
    assert any("intranet" in record.getMessage() for record in caplog.records)  # the log is captured
    text = "\n".join(r.get_data(as_text=True) for r in answers) + caplog.text + "".join(
        repr(record.args) + record.getMessage() for record in caplog.records)
    for secret in (SEED, SEED.lower(), spaced, OTHER_SEED, RAW_SEED.decode(), KEY, BOOTSTRAP, managed):
        assert secret not in text
