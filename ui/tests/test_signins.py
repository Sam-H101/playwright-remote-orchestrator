"""Saved sign-ins: their encryption (mfa.py), store.py and the API against sqlite.
Run from the repo root: python -m pytest ui/tests"""
import base64
import datetime
import hashlib
import io
import json
import logging
import os
import sqlite3
import sys
import threading
import time

import pytest

from ui import app as grid_app, auth, demo, mfa, store

ADMIN = {"X-Grid-Token": "s3cret"}
KEY = "a-test-key-for-the-signin-tests-only"
BOOTSTRAP = "bootstrap-client-token-for-tests"
CHECK = {"Authorization": "Bearer " + BOOTSTRAP}
SEED = base64.b32encode(b"12345678901234567890").decode()
COOKIE = "estsauth-cookie-value-for-the-tests"
OTHER_COOKIE = "a-newer-cookie-value-for-the-tests"
LOCAL = "local-storage-value-for-the-tests"


def state(value=COOKIE):
    """A storage state as Playwright saves one after a Microsoft sign-in."""
    return {"cookies": [
        {"name": "ESTSAUTH", "value": value, "domain": ".login.microsoftonline.com", "path": "/", "expires": -1,
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "FedAuth", "value": value + "-app", "domain": "contoso.sharepoint.com", "path": "/",
         "expires": 1893456000.5, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
        "origins": [{"origin": "https://contoso.sharepoint.com", "localStorage": [{"name": "msal.token", "value": LOCAL}]}]}


STATE = state()
NEWER = state(OTHER_COOKIE)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def ts(seconds_ago):
    return (utcnow() - datetime.timedelta(seconds=seconds_ago)).strftime("%Y-%m-%d %H:%M:%S")


def seconds_from_now(value):
    return (store._parse_time(value) - utcnow()).total_seconds()


def aesgcm(info):
    """The AES-GCM of GRID_SECRETS_KEY for an HKDF info string, made here without mfa.py."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(KEY.encode()))


# ---- encryption (mfa.py) ---------------------------------------------------------------


def test_seeds_stored_before_sign_ins_shared_the_code_still_decrypt(monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    seeds = aesgcm(b"playwright-grid-ui mfa seeds v1")
    nonce = os.urandom(12)
    stored = base64.b64encode(nonce + seeds.encrypt(nonce, SEED.encode(), b"mfa:intranet")).decode()
    assert mfa.decrypt(stored, "intranet") == SEED
    # And what is stored now decrypts the way it always did.
    raw = base64.b64decode(mfa.encrypt(SEED, "intranet"))
    assert seeds.decrypt(raw[:12], raw[12:], b"mfa:intranet") == SEED.encode()


def test_sign_ins_are_sealed_under_a_key_of_their_own(monkeypatch):
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    sealed = mfa.seal(b"the state", "contoso", "signin")
    assert sealed != mfa.seal(b"the state", "contoso", "signin")  # a new nonce each time
    assert mfa.unseal(sealed, "contoso", "signin") == b"the state"
    raw = base64.b64decode(sealed)
    assert aesgcm(b"playwright-grid-ui saved sign-ins v1").decrypt(raw[:12], raw[12:], b"signin:contoso") == b"the state"

    for name, kind in (("contoso2", "signin"), ("contoso", "mfa")):  # another row, or read as a seed
        with pytest.raises(mfa.CannotDecrypt):
            mfa.unseal(sealed, name, kind)
    with pytest.raises(mfa.CannotDecrypt):
        mfa.decrypt(sealed, "contoso")
    with pytest.raises(mfa.CannotDecrypt):  # nor is a seed of the same name a sign-in
        mfa.unseal(mfa.encrypt(SEED, "contoso"), "contoso", "signin")
    for bad in ("not base64!", "", base64.b64encode(b"short").decode()):
        with pytest.raises(mfa.CannotDecrypt):
            mfa.unseal(bad, "contoso", "signin")
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    with pytest.raises(mfa.CannotDecrypt) as e:
        mfa.unseal(sealed, "contoso", "signin")
    assert "contoso" in str(e.value) and KEY not in str(e.value)


def test_the_key_problem_says_what_the_key_is_for(monkeypatch):
    monkeypatch.delenv(mfa.KEY_ENV, raising=False)
    assert mfa.key_problem("signin") == "Set GRID_SECRETS_KEY to save sign-ins."
    assert mfa.key_problem() == "Set GRID_SECRETS_KEY to store MFA secrets."
    with pytest.raises(mfa.SecretsKeyMissing, match="save sign-ins"):
        mfa.seal(b"x", "contoso", "signin")
    monkeypatch.setenv(mfa.KEY_ENV, "k" * 15)
    assert mfa.key_problem("signin") == "GRID_SECRETS_KEY must be at least 16 characters long."
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    sealed = mfa.seal(b"x", "contoso", "signin")
    assert mfa.key_problem("signin") is None
    for module in ("cryptography", "cryptography.exceptions"):
        monkeypatch.setitem(sys.modules, module, None)  # as if it weren't installed
    assert mfa.key_problem("signin") == "The Grid UI needs the cryptography package to save sign-ins."
    with pytest.raises(mfa.SecretsKeyMissing):
        mfa.unseal(sealed, "contoso", "signin")


# ---- store.py ----------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "grid.db")
    with sqlite3.connect(path) as con:
        demo.create_schema(con)
    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return path


def run(path, sql, params=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, params).fetchall()


def row(path, name="contoso"):
    with sqlite3.connect(path) as con:
        con.row_factory = sqlite3.Row
        found = con.execute("SELECT * FROM savedSignins WHERE name = ?", (name,)).fetchone()
        return dict(found) if found else None


def row_id(path, name="contoso"):
    """The sign-in's id, which a check sends with the version it saves again."""
    return row(path, name)["id"]


def counts(path, name="contoso"):
    r = row(path, name)
    return {k: r[k] for k in ("version", "signInCount", "refreshCount", "useCount", "rejectedCount")}


def sign_in(path, name="contoso", by="check a", saved=STATE, **kwargs):
    """A checkout that is told to sign in, and the save of its state. Returns the version saved."""
    answer = store.checkout_signin(by, name)
    assert answer["status"] == "sign-in", answer
    return store.save_signin(by, name, saved, lease_token=answer["leaseToken"], **kwargs)["version"]


def expire_lease(path, name="contoso"):
    run(path, "UPDATE savedSignins SET leaseUntil = ? WHERE name = ?", (ts(1), name))


def from_threads(count, call):
    """call() from `count` threads at once: (results, exceptions)."""
    results, errors = [], []
    start = threading.Barrier(count)

    def one():
        start.wait()
        try:
            results.append(call())
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=one) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


def test_the_first_checkout_signs_in_and_the_others_wait(db):
    first = store.checkout_signin("check a", "contoso")
    assert first == {"status": "sign-in", "id": row_id(db), "name": "contoso", "version": 0,
                     "leaseToken": first["leaseToken"], "leaseForS": 180}
    assert len(first["leaseToken"]) >= 40
    r = row(db)
    assert r["leaseHash"] == hashlib.sha256(first["leaseToken"].encode()).hexdigest() and r["leaseHolder"] == "check a"
    assert abs(seconds_from_now(r["leaseUntil"]) - 180) < 10
    assert abs(seconds_from_now(r["createdAt"])) < 10
    assert first["leaseToken"] not in repr(run(db, "SELECT * FROM savedSignins"))

    assert store.checkout_signin("check b", "contoso") == {"status": "wait", "name": "contoso", "retryAfterS": 5}
    run(db, "UPDATE savedSignins SET leaseUntil = ?", (ts(-2),))  # 2 s left
    assert store.checkout_signin("check b", "contoso")["retryAfterS"] in (1, 2)
    assert counts(db) == {"version": 0, "signInCount": 0, "refreshCount": 0, "useCount": 0, "rejectedCount": 0}


def test_a_sign_in_is_saved_with_its_lease_and_reused(db):
    lease = store.checkout_signin("check a", "contoso")["leaseToken"]
    assert store.save_signin("check a", "contoso", STATE, lease_token=lease) == {"saved": True, "version": 1}
    r = row(db)
    assert (r["leaseHash"], r["leaseUntil"], r["leaseHolder"]) == (None, None, None)
    assert (r["domains"], r["cookieCount"], r["originCount"], r["savedBy"]) == (
        "contoso.sharepoint.com,login.microsoftonline.com", 2, 1, "check a")
    assert abs(seconds_from_now(r["savedAt"])) < 10
    assert (store._parse_time(r["expiresAt"]) - store._parse_time(r["savedAt"])).total_seconds() == 30 * 86400
    stored = repr(run(db, "SELECT * FROM savedSignins"))
    for secret in (COOKIE, LOCAL, "ESTSAUTH", "msal.token", lease):
        assert secret not in stored

    got = store.checkout_signin("check b", "contoso")
    assert got == {"status": "saved", "id": r["id"], "name": "contoso", "version": 1, "state": STATE,
                   "savedAt": store._iso(store._parse_time(r["savedAt"])),
                   "expiresAt": store._iso(store._parse_time(r["expiresAt"]))}
    assert store.checkout_signin("check c", "contoso")["state"] == STATE
    r = row(db)
    assert (r["useCount"], r["lastUsedBy"]) == (2, "check c") and abs(seconds_from_now(r["lastUsedAt"])) < 10
    assert counts(db) == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 2, "rejectedCount": 0}

    # The lease ended with the save.
    with pytest.raises(store.LeaseLost):
        store.save_signin("check a", "contoso", NEWER, lease_token=lease)
    assert store.release_signin("contoso", lease) is False
    assert store.checkout_signin("check d", "contoso")["state"] == STATE


def test_a_reused_state_is_saved_again_while_it_is_the_newest(db):
    sign_in(db)
    assert store.save_signin("check b", "contoso", NEWER, base_version=1, signin_id=row_id(db)) == {"saved": True, "version": 2}
    assert (row(db)["savedBy"], counts(db)["refreshCount"], counts(db)["signInCount"]) == ("check b", 1, 1)
    # Another check that reused version 1 too: its cookies are older than what is saved now.
    for version in (1, 7):
        assert store.save_signin("check c", "contoso", STATE, base_version=version, signin_id=row_id(db)) == {
            "saved": False, "reason": "newer", "version": 2}
    assert store.checkout_signin("check d", "contoso")["state"] == NEWER
    assert counts(db) == {"version": 2, "signInCount": 1, "refreshCount": 1, "useCount": 1, "rejectedCount": 0}


def test_a_reused_state_is_not_saved_while_someone_signs_in(db):
    sign_in(db)
    signing_in = store.checkout_signin("check b", "contoso", rejected_version=1)
    assert signing_in["status"] == "sign-in" and signing_in["id"] == row_id(db)
    assert store.save_signin("check a", "contoso", STATE, base_version=1, signin_id=row_id(db)) == {
        "saved": False, "reason": "signing-in", "version": 1}
    assert row(db)["stateCipher"] is None
    assert store.save_signin("check b", "contoso", NEWER, lease_token=signing_in["leaseToken"])["version"] == 2
    # Once the lease ran out, a reused state may be saved again.
    store.checkout_signin("check c", "contoso", rejected_version=2)
    expire_lease(db)
    assert store.save_signin("check a", "contoso", STATE, base_version=2, signin_id=row_id(db)) == {"saved": True, "version": 3}
    assert row(db)["leaseHash"] is None


def test_a_state_the_site_refused_is_dropped_once(db):
    sign_in(db)
    refused = store.checkout_signin("check b", "contoso", rejected_version=1)
    assert refused["status"] == "sign-in" and refused["version"] == 1
    r = row(db)
    assert r["stateCipher"] is None and r["rejectedCount"] == 1 and abs(seconds_from_now(r["lastRejectedAt"])) < 10
    assert r["leaseHolder"] == "check b"
    # Another check the site refused it to: nothing left to drop, and someone is signing in already.
    assert store.checkout_signin("check c", "contoso", rejected_version=1)["status"] == "wait"
    assert counts(db)["rejectedCount"] == 1

    store.save_signin("check b", "contoso", NEWER, lease_token=refused["leaseToken"])
    # Refused before the new one was saved: the new one is answered.
    got = store.checkout_signin("check c", "contoso", rejected_version=1)
    assert (got["status"], got["version"], got["state"]) == ("saved", 2, NEWER)
    assert store.checkout_signin("check c", "contoso", rejected_version=2)["status"] == "sign-in"
    assert counts(db) == {"version": 2, "signInCount": 2, "refreshCount": 0, "useCount": 1, "rejectedCount": 2}


def test_a_lease_that_ran_out_is_lost(db):
    first = store.checkout_signin("check a", "contoso")["leaseToken"]
    expire_lease(db)
    second = store.checkout_signin("check b", "contoso")
    assert second["status"] == "sign-in" and second["leaseToken"] != first and row(db)["leaseHolder"] == "check b"
    with pytest.raises(store.LeaseLost) as e:
        store.save_signin("check a", "contoso", STATE, lease_token=first)
    assert first not in str(e.value) and COOKIE not in str(e.value)
    assert store.release_signin("contoso", first) is False
    assert store.save_signin("check b", "contoso", STATE, lease_token=second["leaseToken"])["version"] == 1

    # Lost even when nobody took it over meanwhile: the check was told how long it had.
    third = store.checkout_signin("check c", "contoso", rejected_version=1)["leaseToken"]
    expire_lease(db)
    with pytest.raises(store.LeaseLost):
        store.save_signin("check c", "contoso", NEWER, lease_token=third)
    for wrong in ("wrong", "not a token!", third + "x", "x" * 200):
        with pytest.raises(store.LeaseLost):
            store.save_signin("check c", "contoso", NEWER, lease_token=wrong)
    assert counts(db) == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 0, "rejectedCount": 1}


def test_a_failed_sign_in_releases_its_lease(db):
    first = store.checkout_signin("check a", "contoso")["leaseToken"]
    for wrong in ("wrong", "not a token!", "\ud800", first[:-1]):
        assert store.release_signin("contoso", wrong) is False
    assert row(db)["leaseHolder"] == "check a"
    assert store.release_signin("contoso", first) is True
    assert (row(db)["leaseHash"], row(db)["leaseUntil"], row(db)["leaseHolder"]) == (None, None, None)
    assert store.checkout_signin("check b", "contoso")["status"] == "sign-in"
    assert store.release_signin("contoso", first) is False
    assert store.release_signin("nope", first) is None


@pytest.mark.parametrize("lease_s, expected", [
    (None, 180), (5, 30), (-1, 30), (30, 30), (45.7, 45), (600, 600), (601, 600),
    (10 ** 400, 600), (float("inf"), 600), (float("nan"), 30),
])
def test_the_lease_is_30_s_to_10_min(db, lease_s, expected):
    got = store.checkout_signin("check a", "contoso", lease_s=lease_s)
    assert got["leaseForS"] == expected
    assert abs(seconds_from_now(row(db)["leaseUntil"]) - expected) < 10


@pytest.mark.parametrize("call", [
    lambda: store.checkout_signin("c", ""),
    lambda: store.checkout_signin("c", "   "),
    lambda: store.checkout_signin("c", "has space"),
    lambda: store.checkout_signin("c", "x" * 65),
    lambda: store.checkout_signin("c", "naïve"),
    lambda: store.checkout_signin("c", None),
    lambda: store.checkout_signin("c", ["contoso"]),
    lambda: store.checkout_signin("c", "contoso", rejected_version=-1),
    lambda: store.checkout_signin("c", "contoso", rejected_version=True),
    lambda: store.checkout_signin("c", "contoso", rejected_version="1"),
    lambda: store.checkout_signin("c", "contoso", rejected_version=1.0),
    lambda: store.checkout_signin("c", "contoso", rejected_version=1, signin_id=0),
    lambda: store.checkout_signin("c", "contoso", rejected_version=1, signin_id="1"),
    lambda: store.checkout_signin("c", "contoso", rejected_version=1, signin_id=True),
    lambda: store.checkout_signin("c", "contoso", lease_s="60"),
    lambda: store.checkout_signin("c", "contoso", lease_s=True),
    lambda: store.checkout_signin("c", "contoso", lease_s=[60]),
    lambda: store.save_signin("c", "contoso", STATE),
    lambda: store.save_signin("c", "contoso", STATE, signin_id=1),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0),  # no id
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=0),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=-1),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id="1"),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1.0),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=False),
    lambda: store.save_signin("c", "bad name", STATE, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", None, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", [], base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", "{}", base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": []}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"origins": []}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": {}, "origins": []}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": [], "origins": None}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": [COOKIE], "origins": []}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": [], "origins": [["x"]]}, base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": [{"value": COOKIE, "expires": float("nan")}], "origins": []},
                              base_version=0, signin_id=1),
    lambda: store.save_signin("c", "contoso", {"cookies": [], "origins": [], "x": float("inf")}, base_version=0,
                              signin_id=1),
    lambda: store.save_signin("c", "contoso", STATE, lease_token=5),
    lambda: store.save_signin("c", "contoso", STATE, lease_token=""),
    lambda: store.save_signin("c", "contoso", STATE, base_version=-1, signin_id=1),
    lambda: store.save_signin("c", "contoso", STATE, base_version=False, signin_id=1),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1, max_age_s=59),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1, max_age_s=90 * 86400 + 1),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1, max_age_s="3600"),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1, max_age_s=True),
    lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1, max_age_s=float("nan")),
    lambda: store.release_signin("contoso", None),
    lambda: store.release_signin("contoso", 5),
    lambda: store.release_signin("contoso", ""),
    lambda: store.release_signin("bad name", "token"),
])
def test_requests_that_are_refused(db, call):
    with pytest.raises(store.InvalidSignin) as e:
        call()
    assert COOKIE not in str(e.value)
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(0,)]


def test_a_state_is_at_most_2_mb(db):
    lease = store.checkout_signin("check a", "contoso")["leaseToken"]
    empty = {"cookies": [], "origins": [{"origin": "https://contoso.example", "localStorage": [{"name": "n", "value": ""}]}]}
    room = store.SIGNIN_MAX_STATE_BYTES - len(json.dumps(empty, separators=(",", ":")))
    big = {"cookies": [], "origins": [{"origin": "https://contoso.example",
                                       "localStorage": [{"name": "n", "value": LOCAL + "a" * (room - len(LOCAL))}]}]}
    too_big = json.loads(json.dumps(big))
    too_big["origins"][0]["localStorage"][0]["value"] += "a"
    with pytest.raises(store.SigninTooLarge) as e:
        store.save_signin("check a", "contoso", too_big, lease_token=lease)
    assert LOCAL not in str(e.value) and str(store.SIGNIN_MAX_STATE_BYTES) in str(e.value)
    assert isinstance(e.value, store.InvalidSignin)
    assert store.save_signin("check a", "contoso", big, lease_token=lease)["version"] == 1  # the lease is still there
    assert store.checkout_signin("check b", "contoso")["state"] == big
    assert len(row(db)["stateCipher"]) < 100000  # compressed


def test_the_domains_shown_are_the_cookies_domains():
    cookies = [{"domain": d} for d in (".Contoso.SharePoint.com", "contoso.sharepoint.com", " login.microsoftonline.com ",
                                       "localhost", "[::1]", "evil,comma.example", "tab\t.example", "", ".", 5, None)]
    cookies += [{}, {"name": "no-domain"}]
    assert store._cookie_domains(cookies) == "[::1],contoso.sharepoint.com,localhost,login.microsoftonline.com"
    assert store._cookie_domains([]) is None
    many = ["host-{:03}.contoso.example".format(i) for i in range(100)]
    text = store._cookie_domains([{"domain": d} for d in many])
    assert len(text) <= 1024 and text.split(",") == many[:len(text.split(","))] and len(text) > 1000


def test_how_long_a_state_is_kept(db):
    sign_in(db, max_age_s=60)
    r = row(db)
    assert (store._parse_time(r["expiresAt"]) - store._parse_time(r["savedAt"])).total_seconds() == 60
    assert store.save_signin("c", "contoso", STATE, base_version=1, max_age_s=90 * 86400, signin_id=row_id(db))["saved"] is True
    assert abs(seconds_from_now(row(db)["expiresAt"]) - 90 * 86400) < 10

    run(db, "UPDATE savedSignins SET expiresAt = ?", (ts(1),))
    got = store.checkout_signin("check b", "contoso")
    assert got["status"] == "sign-in" and got["version"] == 2
    assert row(db)["stateCipher"] is None and counts(db)["rejectedCount"] == 0


def test_expired_states_of_every_name_are_dropped_as_checkouts_come(db):
    for name in ("a", "b", "c"):
        sign_in(db, name)
    run(db, "UPDATE savedSignins SET expiresAt = ? WHERE name IN ('a', 'b')", (ts(5),))
    listing = {s["name"]: s for s in store.list_signins()["signins"]}
    assert [(listing[n]["saved"], listing[n]["expired"]) for n in "abc"] == [(True, True), (True, True), (True, False)]

    assert store.checkout_signin("check", "c")["status"] == "saved"
    assert run(db, "SELECT name FROM savedSignins WHERE stateCipher IS NOT NULL") == [("c",)]
    listing = {s["name"]: s for s in store.list_signins()["signins"]}
    assert [(listing[n]["saved"], listing[n]["expired"], listing[n]["version"]) for n in "abc"] == [
        (False, False, 1), (False, False, 1), (True, False, 1)]


def test_checkout_and_save_need_the_secrets_key(db, monkeypatch):
    monkeypatch.delenv(mfa.KEY_ENV)
    with pytest.raises(mfa.SecretsKeyMissing, match="save sign-ins"):
        store.checkout_signin("check a", "contoso")
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(0,)]

    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    lease = store.checkout_signin("check a", "contoso")["leaseToken"]
    monkeypatch.setenv(mfa.KEY_ENV, "too-short")
    with pytest.raises(mfa.SecretsKeyMissing):
        store.save_signin("check a", "contoso", STATE, lease_token=lease)
    assert row(db)["leaseHolder"] == "check a" and row(db)["version"] == 0
    assert store.release_signin("contoso", lease) is True  # needs no key
    assert len(store.list_signins()["signins"]) == 1


def test_a_state_that_no_longer_decrypts_means_signing_in_again(db, monkeypatch, caplog):
    sign_in(db)
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")
    got = store.checkout_signin("check b", "contoso")
    assert got["status"] == "sign-in" and got["version"] == 1
    assert counts(db)["useCount"] == 0
    assert any("contoso" in r.getMessage() and "decrypt" in r.getMessage() for r in caplog.records)
    assert store.save_signin("check b", "contoso", NEWER, lease_token=got["leaseToken"])["version"] == 2
    assert store.checkout_signin("check c", "contoso")["state"] == NEWER

    monkeypatch.setenv(mfa.KEY_ENV, KEY)  # saved under the new key: the old one doesn't open it
    assert store.checkout_signin("check d", "contoso")["status"] == "sign-in"


def test_a_state_copied_to_another_name_does_not_decrypt(db):
    sign_in(db, "a")
    sign_in(db, "b", saved=NEWER)
    run(db, "UPDATE savedSignins SET stateCipher = (SELECT stateCipher FROM savedSignins WHERE name = 'a') WHERE name = 'b'")
    assert store.checkout_signin("check", "b")["status"] == "sign-in"
    assert store.checkout_signin("check", "a")["state"] == STATE


def test_new_names_stop_at_200(db):
    with sqlite3.connect(db) as con:
        con.executemany("INSERT INTO savedSignins (name, createdAt) VALUES (?, ?)",
                        [("n-{}".format(i), ts(0)) for i in range(199)])
    assert store.checkout_signin("check", "new")["status"] == "sign-in"
    with pytest.raises(store.SigninsFull, match="200"):
        store.checkout_signin("check", "another")
    assert store.checkout_signin("check", "n-5")["status"] == "sign-in"  # names it has go on working
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(200,)]
    store.delete_signin(row(db, "n-0")["id"])
    assert store.checkout_signin("check", "another")["status"] == "sign-in"


def test_forget_drops_the_state_and_the_lease(db):
    sign_in(db)
    signin_id = row_id(db)
    store.save_signin("check b", "contoso", NEWER, base_version=1, signin_id=signin_id)
    forgotten = store.forget_signin(signin_id)
    assert (forgotten["saved"], forgotten["version"], forgotten["signInCount"], forgotten["refreshCount"]) == (False, 3, 1, 1)
    assert row(db)["stateCipher"] is None
    # A check still using the old state doesn't bring it back.
    assert store.save_signin("check b", "contoso", NEWER, base_version=2, signin_id=signin_id) == {
        "saved": False, "reason": "newer", "version": 3}
    lease = store.checkout_signin("check c", "contoso")
    assert (lease["status"], lease["version"]) == ("sign-in", 3)

    # Forgetting while a check signs in: its lease goes too, so it saves nothing.
    forgotten = store.forget_signin(signin_id)
    assert (forgotten["version"], forgotten["leaseHolder"], forgotten["leaseForS"]) == (4, None, None)
    assert row(db)["leaseHash"] is None
    with pytest.raises(store.LeaseLost):
        store.save_signin("check c", "contoso", STATE, lease_token=lease["leaseToken"])
    for unknown in (0, -1, 9999, 2 ** 31, 2 ** 63, 10 ** 20):
        assert store.forget_signin(unknown) is None


def test_a_forgotten_state_has_no_expiry(db):
    # Saved with the default 30 days, then forgotten: once those 30 days are past, it still reads as forgotten
    # on the page (which says "Expired" for a state whose expiresAt passed), and not as expired.
    sign_in(db)
    assert row(db)["expiresAt"] is not None
    forgotten = store.forget_signin(row_id(db))
    assert (forgotten["saved"], forgotten["expiresAt"], forgotten["expired"]) == (False, None, False)
    assert row(db)["expiresAt"] is None
    # A state already past its expiry but not dropped yet (no checkout came since) loses it too.
    sign_in(db, "late")
    run(db, "UPDATE savedSignins SET expiresAt = ? WHERE name = 'late'", (ts(5),))
    assert store.forget_signin(row_id(db, "late"))["expiresAt"] is None
    # A real expiry keeps expiresAt, so the page can say so.
    sign_in(db, "old")
    expired = ts(5)
    run(db, "UPDATE savedSignins SET expiresAt = ? WHERE name = 'old'", (expired,))
    store.checkout_signin("check", "contoso")
    (old,) = [s for s in store.list_signins()["signins"] if s["name"] == "old"]
    assert (old["saved"], old["expiresAt"]) == (False, store._iso(store._parse_time(expired)))


def test_delete(db):
    sign_in(db)
    sign_in(db, "other")
    signin_id = row_id(db)
    assert store.delete_signin(signin_id) is True
    assert store.delete_signin(signin_id) is False
    assert [s["name"] for s in store.list_signins()["signins"]] == ["other"]
    assert store.save_signin("check", "contoso", STATE, base_version=1, signin_id=signin_id) is None
    assert store.release_signin("contoso", "token") is None
    got = store.checkout_signin("check", "contoso")  # a check that asks again starts afresh, under a new id
    assert (got["status"], got["version"]) == ("sign-in", 0) and got["id"] > signin_id
    for unknown in (0, -1, 9999, 2 ** 31, 2 ** 63, 10 ** 20):
        assert store.delete_signin(unknown) is False


def test_a_check_still_using_a_deleted_sign_in_cannot_save_it_back(db):
    # A signs in and saves version 1; C reuses it and is still running when an admin deletes the sign-in.
    sign_in(db, by="check a", saved=state("OLD-SESSION"))
    reused = store.checkout_signin("check c", "contoso")
    assert store.delete_signin(reused["id"]) is True
    # B's checkout adds the name again, and B signs in and saves its version 1.
    fresh = store.checkout_signin("check b", "contoso")
    assert (fresh["status"], fresh["version"]) == ("sign-in", 0) and fresh["id"] != reused["id"]
    assert store.save_signin("check b", "contoso", state("NEW-SESSION"), lease_token=fresh["leaseToken"])["version"] == 1
    # C ends and saves its context again, as of its version 1: not B's version 1.
    assert store.save_signin("check c", "contoso", state("OLD-SESSION-rolled"), base_version=reused["version"],
                             signin_id=reused["id"]) == {"saved": False, "reason": "newer", "version": 1}
    got = store.checkout_signin("check d", "contoso")
    assert (got["id"], got["version"], got["state"]) == (fresh["id"], 1, state("NEW-SESSION"))
    assert counts(db)["refreshCount"] == 0
    # Nor does the site refusing C's state drop B's.
    got = store.checkout_signin("check c", "contoso", rejected_version=reused["version"], signin_id=reused["id"])
    assert (got["status"], got["state"]) == ("saved", state("NEW-SESSION")) and counts(db)["rejectedCount"] == 0
    # B's own refresh and refusal work as ever.
    assert store.save_signin("check b", "contoso", NEWER, base_version=1, signin_id=fresh["id"]) == {"saved": True, "version": 2}
    got = store.checkout_signin("check b", "contoso", rejected_version=2, signin_id=fresh["id"])
    assert got["status"] == "sign-in" and counts(db)["rejectedCount"] == 1


def test_a_checkout_adds_the_name_again_when_it_is_deleted_meanwhile(db, monkeypatch):
    real, calls = store._add_signin, []

    def add_then_an_admin_deletes(name):
        real(name)
        calls.append(name)
        if len(calls) == 1:
            run(db, "DELETE FROM savedSignins WHERE name = ?", (name,))

    monkeypatch.setattr(store, "_add_signin", add_then_an_admin_deletes)
    assert store.checkout_signin("check", "contoso")["status"] == "sign-in"
    assert calls == ["contoso", "contoso"] and row(db)["leaseHolder"] == "check"


def test_the_listing_shows_everything_but_the_state(db):
    listing = store.list_signins()
    assert listing == {"ready": True, "generated": listing["generated"], "signins": []}
    assert abs(seconds_from_now(listing["generated"][:-1])) < 10
    sign_in(db, by="client token 'nightly'")
    store.checkout_signin("GATEWAY_CLIENT_TOKEN", "contoso")
    store.checkout_signin("check c", "contoso", rejected_version=1)
    r = row(db)
    (entry,) = store.list_signins()["signins"]
    assert entry == {
        "id": r["id"], "name": "contoso", "version": 1, "saved": False,
        "domains": ["contoso.sharepoint.com", "login.microsoftonline.com"], "cookieCount": 2, "originCount": 1,
        "savedAt": store._iso(store._parse_time(r["savedAt"])), "savedBy": "client token 'nightly'",
        "expiresAt": store._iso(store._parse_time(r["expiresAt"])), "expired": False,
        "signInCount": 1, "refreshCount": 0, "useCount": 1,
        "lastUsedAt": store._iso(store._parse_time(r["lastUsedAt"])), "lastUsedBy": "GATEWAY_CLIENT_TOKEN",
        "rejectedCount": 1, "lastRejectedAt": store._iso(store._parse_time(r["lastRejectedAt"])),
        "leaseHolder": "check c", "leaseForS": entry["leaseForS"]}
    assert 170 <= entry["leaseForS"] <= 180 and entry["savedAt"].endswith("Z")
    expire_lease(db)
    (entry,) = store.list_signins()["signins"]
    assert (entry["leaseHolder"], entry["leaseForS"]) == (None, None)
    store.checkout_signin("check", "a-new-one")
    assert [s["name"] for s in store.list_signins()["signins"]] == ["a-new-one", "contoso"]
    assert store.list_signins()["signins"][0]["savedAt"] is None
    assert COOKIE not in repr(store.list_signins())


def test_concurrent_checkouts_get_one_lease(db):
    answers, errors = from_threads(8, lambda: store.checkout_signin("check", "contoso"))
    assert errors == []
    assert sorted(a["status"] for a in answers) == ["sign-in"] + ["wait"] * 7
    (lease,) = [a["leaseToken"] for a in answers if a["status"] == "sign-in"]
    store.save_signin("check", "contoso", STATE, lease_token=lease)

    answers, errors = from_threads(8, lambda: store.checkout_signin("check", "contoso"))
    assert errors == [] and [a["status"] for a in answers] == ["saved"] * 8
    # All of them were refused that state: one signs in, the others wait for it.
    answers, errors = from_threads(8, lambda: store.checkout_signin("check", "contoso", rejected_version=1))
    assert errors == [] and sorted(a["status"] for a in answers) == ["sign-in"] + ["wait"] * 7
    assert counts(db) == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 8, "rejectedCount": 1}
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(1,)]


def test_concurrent_saves_of_a_reused_state_keep_one(db):
    sign_in(db)
    signin_id = row_id(db)
    answers, errors = from_threads(6, lambda: store.save_signin("check", "contoso", NEWER, base_version=1,
                                                                signin_id=signin_id))
    assert errors == []
    assert sorted(json.dumps(a, sort_keys=True) for a in answers) == sorted(
        [json.dumps({"saved": True, "version": 2}, sort_keys=True)]
        + [json.dumps({"saved": False, "reason": "newer", "version": 2}, sort_keys=True)] * 5)
    assert counts(db) == {"version": 2, "signInCount": 1, "refreshCount": 1, "useCount": 0, "rejectedCount": 0}


def test_a_database_without_the_table(db):
    run(db, "DROP TABLE savedSignins")
    assert store.list_signins()["ready"] is False and store.list_signins()["signins"] == []
    for call in (lambda: store.checkout_signin("c", "contoso"),
                 lambda: store.save_signin("c", "contoso", STATE, base_version=0, signin_id=1),
                 lambda: store.release_signin("contoso", "token")):
        with pytest.raises(store.SigninsUnavailable):
            call()
    assert store.forget_signin(1) is None and store.delete_signin(1) is False
    r = private(grid_app.app.test_client().get("/api/signins", headers=ADMIN))
    assert r.status_code == 200 and r.get_json()["ready"] is False


# ---- API ---------------------------------------------------------------------------------


@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", BOOTSTRAP)
    return grid_app.app.test_client()


def private(r):
    assert r.headers.get("Cache-Control") == "no-store", (r.request.method, r.request.path, r.status_code)
    return r


def check(client, route, headers=CHECK, **body):
    return private(client.post("/api/signins/" + route, json=body, headers=headers))


def admin(client, method, path="", headers=ADMIN):
    if method == "GET":
        return private(client.get("/api/signins" + path, headers=headers))
    return private(client.post("/api/signins" + path, json={}, headers=headers))


def check_requests(client, headers):
    return [check(client, "checkout", headers, name="contoso"),
            check(client, "save", headers, name="contoso", state=STATE, baseVersion=1, id=1),
            check(client, "release", headers, name="contoso", leaseToken="token")]


def admin_requests(client, sid, headers):
    return [admin(client, "GET", headers=headers), admin(client, "POST", "/{}/forget".format(sid), headers),
            admin(client, "POST", "/{}/delete".format(sid), headers)]


def test_a_check_signs_in_saves_and_reuses_through_the_api(client, db):
    r = check(client, "checkout", name=" contoso ")
    assert r.status_code == 200
    lease = r.get_json()
    assert lease == {"status": "sign-in", "id": row_id(db), "name": "contoso", "version": 0,
                     "leaseToken": lease["leaseToken"], "leaseForS": 180}
    assert check(client, "checkout", name="contoso").get_json() == {"status": "wait", "name": "contoso", "retryAfterS": 5}
    r = check(client, "save", name="contoso", state=STATE, leaseToken=lease["leaseToken"])
    assert (r.status_code, r.get_json()) == (200, {"saved": True, "version": 1})

    got = check(client, "checkout", name="contoso", leaseS=60).get_json()
    assert (got["status"], got["id"], got["version"], got["state"]) == ("saved", lease["id"], 1, STATE)
    assert got["savedAt"].endswith("Z") and got["expiresAt"].endswith("Z")
    assert check(client, "save", name="contoso", state=NEWER, baseVersion=1, id=got["id"], maxAgeS=3600).get_json() == {
        "saved": True, "version": 2}
    assert check(client, "save", name="contoso", state=STATE, baseVersion=1, id=got["id"]).get_json() == {
        "saved": False, "reason": "newer", "version": 2}
    # The rejection of another sign-in's version 2 (a deleted one's) drops nothing.
    assert check(client, "checkout", name="contoso", rejectedVersion=2, id=got["id"] + 1).get_json()["status"] == "saved"

    body = admin(client, "GET").get_json()
    assert body["ready"] is True and body["keyProblem"] is None and abs(body["serverTime"] - time.time()) < 5
    (entry,) = body["signins"]
    assert [entry[k] for k in ("name", "version", "saved", "savedBy", "signInCount", "refreshCount", "useCount",
                               "lastUsedBy")] == ["contoso", 2, True, "GATEWAY_CLIENT_TOKEN", 1, 1, 2, "GATEWAY_CLIENT_TOKEN"]
    assert abs(seconds_from_now(entry["expiresAt"][:-1]) - 3600) < 10
    assert "stateCipher" not in entry and COOKIE not in json.dumps(body) and OTHER_COOKIE not in json.dumps(body)

    r = admin(client, "POST", "/{}/forget".format(entry["id"]))
    assert r.status_code == 200 and r.get_json()["forgotten"] is True
    assert (r.get_json()["signin"]["saved"], r.get_json()["signin"]["version"]) == (False, 3)
    lease = check(client, "checkout", name="contoso", leaseS=45).get_json()
    assert (lease["status"], lease["version"], lease["leaseForS"]) == ("sign-in", 3, 45)
    assert check(client, "release", name="contoso", leaseToken=lease["leaseToken"]).get_json() == {"released": True}
    assert check(client, "release", name="contoso", leaseToken=lease["leaseToken"]).get_json() == {"released": False}
    assert admin(client, "POST", "/{}/delete".format(entry["id"])).get_json() == {"deleted": True, "id": entry["id"]}
    assert admin(client, "POST", "/{}/delete".format(entry["id"])).status_code == 404
    assert admin(client, "GET").get_json()["signins"] == []


def test_the_check_routes_need_a_client_token(client, db):
    sign_in(db)
    for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer    "},
                    {"Authorization": "Basic " + BOOTSTRAP}, {"Authorization": BOOTSTRAP}, ADMIN):
        for r in check_requests(client, headers):
            assert r.status_code == 401 and r.get_json()["client_token_required"] is True, (headers, r.request.path)
    assert counts(db) == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 0, "rejectedCount": 0}
    for _ in range(auth.FAILURE_LIMIT + 5):  # no token at all isn't a guess
        assert check(client, "checkout", {}, name="contoso").status_code == 401
    assert check(client, "checkout", name="contoso").status_code == 200


def test_wrong_client_tokens_count_towards_the_guessing_limit(client):
    for i in range(auth.FAILURE_LIMIT):
        assert check(client, "checkout", {"Authorization": "Bearer wrong-{}".format(i)}, name="contoso").status_code == 401
    r = check(client, "checkout", name="contoso")  # even the right token now
    assert r.status_code == 429 and int(r.headers["Retry-After"]) == r.get_json()["retry_after_s"]
    assert admin(client, "GET").status_code == 429  # the limit admin sign-in has
    auth.reset_failures()
    assert check(client, "checkout", name="contoso").status_code == 200


def test_managed_client_tokens(client, db, monkeypatch):
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN")
    active = store.create_token("nightly checks", "client")
    revoked = store.create_token("revoked", "client")
    store.revoke_token(revoked["id"])
    agent = store.create_token("agent", "agent")
    for token in (revoked, agent):
        assert check(client, "checkout", {"Authorization": "Bearer " + token["token"]}, name="contoso").status_code == 401
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(0,)]
    managed = {"Authorization": "Bearer " + active["token"]}
    lease = check(client, "checkout", managed, name="contoso").get_json()["leaseToken"]
    assert row(db)["leaseHolder"] == "client token 'nightly checks'"
    assert check(client, "save", managed, name="contoso", state=STATE, leaseToken=lease).status_code == 200
    assert row(db)["savedBy"] == "client token 'nightly checks'"
    assert run(db, "SELECT name FROM accessTokens WHERE lastUsedAt IS NOT NULL") == [("nightly checks",)]


def test_the_admin_routes_need_the_admin_token(client, db, monkeypatch):
    sign_in(db)
    sid = row(db)["id"]
    for headers in ({}, {"X-Grid-Token": "wrong"}, CHECK):
        for r in admin_requests(client, sid, headers):
            assert r.status_code == 403 and r.get_json()["token_required"] is True, (headers, r.request.path)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    for headers in ({}, {"X-Grid-Token": "anything"}, CHECK):
        for r in admin_requests(client, sid, headers):
            assert r.status_code == 403
            assert r.get_json() == {"error": "set GRID_ADMIN_TOKEN to manage access tokens", "admin_token_unset": True}
    assert row(db)["version"] == 1 and row(db)["stateCipher"] is not None
    # Checks don't sign in as an admin: they get their sign-ins whether or not one is configured.
    assert check(client, "checkout", name="contoso").get_json()["state"] == STATE


def test_only_the_check_routes_skip_the_same_origin_guard(client, db, monkeypatch):
    monkeypatch.setattr(grid_app, "ALLOWED_HOSTS", set())
    sign_in(db)
    sid = row(db)["id"]
    cross = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
    assert check(client, "checkout", dict(CHECK, **cross), name="contoso").status_code == 200
    r = private(client.post("/api/signins/save", base_url="http://grid_ui:4444",
                            json={"name": "contoso", "state": NEWER, "baseVersion": 1, "id": sid}, headers=CHECK))
    assert r.get_json() == {"saved": True, "version": 2}
    r = private(client.post("/api/signins/release", base_url="http://grid_ui:4444",
                            json={"name": "contoso", "leaseToken": "token"}, headers=CHECK))
    assert r.get_json() == {"released": False}

    for url in ("/api/signins/{}/forget".format(sid), "/api/signins/{}/delete".format(sid), "/api/signins/checkout/"):
        for kwargs in (dict(headers=dict(ADMIN, **cross)), dict(headers=dict(ADMIN, **CHECK), base_url="http://grid_ui:4444")):
            r = private(client.post(url, json={"name": "contoso"}, **kwargs))
            assert r.status_code == 403, (url, kwargs)
            assert "token_required" not in r.get_json() and "admin_token_unset" not in r.get_json()
    assert row(db)["version"] == 2 and row(db)["stateCipher"] is not None


def test_bad_requests_are_400s(client, db):
    sign_in(db)
    for route, body in (("checkout", ""), ("checkout", "[]"), ("checkout", "{}"), ("checkout", '{"name": ""}'),
                        ("checkout", '{"name": "bad name"}'), ("checkout", '{"name": 5}'),
                        ("checkout", '{"name": "contoso", "rejectedVersion": -1}'),
                        ("checkout", '{"name": "contoso", "rejectedVersion": "1"}'),
                        ("checkout", '{"name": "contoso", "rejectedVersion": true}'),
                        ("checkout", '{"name": "contoso", "leaseS": "60"}'),
                        ("checkout", '{"name": "contoso", "rejectedVersion": 1, "id": "1"}'),
                        ("checkout", '{"name": "contoso", "rejectedVersion": 1, "id": 0}'),
                        ("save", '{"name": "contoso", "baseVersion": 1, "id": 1}'),
                        ("save", '{"name": "contoso", "state": [], "baseVersion": 1, "id": 1}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [{"value": "%s"}]}, "baseVersion": 1, "id": 1}'
                                 % COOKIE),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": [], "x": NaN}, "baseVersion": 1, '
                                 '"id": 1}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": []}}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": []}, "baseVersion": 1}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": []}, "baseVersion": 1, "id": true}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": []}, "leaseToken": 5}'),
                        ("save", '{"name": "contoso", "state": {"cookies": [], "origins": []}, "baseVersion": 1, '
                                 '"id": 1, "maxAgeS": 10}'),
                        ("release", '{"name": "contoso"}'), ("release", '{"name": "contoso", "leaseToken": ""}'),
                        ("release", '{"leaseToken": "x"}')):
        r = private(client.post("/api/signins/" + route, data=body, content_type="application/json", headers=CHECK))
        assert r.status_code == 400 and r.get_json()["error"], (route, body)
        assert COOKIE not in r.get_data(as_text=True)
    assert counts(db) == {"version": 1, "signInCount": 1, "refreshCount": 0, "useCount": 0, "rejectedCount": 0}
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(1,)]


def test_a_state_over_2_mb_is_a_413(client, db, monkeypatch):
    lease = check(client, "checkout", name="contoso").get_json()["leaseToken"]
    big = {"cookies": [{"name": "big", "value": COOKIE + "a" * store.SIGNIN_MAX_STATE_BYTES, "domain": "contoso.example"}],
           "origins": []}
    r = check(client, "save", name="contoso", state=big, leaseToken=lease)
    assert r.status_code == 413 and r.get_json()["error"] == "too_large" and COOKIE not in r.get_data(as_text=True)
    # A body past the limit isn't even read.
    limit = store.SIGNIN_MAX_BODY_BYTES
    monkeypatch.setattr(store, "SIGNIN_MAX_BODY_BYTES", 1000)
    r = private(client.post("/api/signins/save", data='{"name": "contoso", "state": "' + "a" * 1000 + '"',
                            content_type="application/json", headers=CHECK))
    assert r.status_code == 413 and r.get_json()["error"] == "too_large"
    monkeypatch.setattr(store, "SIGNIN_MAX_BODY_BYTES", limit)
    assert check(client, "save", name="contoso", state=STATE, leaseToken=lease).get_json() == {"saved": True, "version": 1}


def chunked(client, path, body):
    """A request as gunicorn hands over a chunked one: no Content-Length, and the stream ends where the body does."""
    return private(client.post(path, input_stream=io.BytesIO(body), content_type="application/json",
                               headers=dict(CHECK, **{"Transfer-Encoding": "chunked"}),
                               environ_overrides={"wsgi.input_terminated": True}))


def test_a_chunked_body_is_cut_off_at_the_limit_too(client, db):
    def body(size):
        """A checkout of exactly `size` bytes."""
        empty = json.dumps({"name": "contoso", "padding": ""}).encode()
        return json.dumps({"name": "contoso", "padding": "a" * (size - len(empty))}).encode()

    big = body(store.SIGNIN_MAX_BODY_BYTES + 1)
    for route in ("checkout", "save", "release"):
        r = chunked(client, "/api/signins/" + route, big)
        assert r.status_code == 413 and r.get_json()["error"] == "too_large", route
    assert run(db, "SELECT COUNT(*) FROM savedSignins") == [(0,)]
    # Nor is a code request read past it: what is read isn't JSON (whole, it would ask for a secret "contoso").
    r = chunked(client, "/api/mfa/code", body(3 * store.SIGNIN_MAX_BODY_BYTES))
    assert r.status_code == 400 and r.get_json()["error"] == 'Send {"name": "<secret name>"} as JSON.'
    # With a Content-Length past it, any route refuses it unread.
    r = private(client.post("/api/mfa/code", data=body(store.SIGNIN_MAX_BODY_BYTES + 2), content_type="application/json",
                            headers=CHECK))
    assert r.status_code == 413 and r.get_json()["error"] == "too_large"
    # Up to the limit, a chunked body is read like any other.
    assert len(body(store.SIGNIN_MAX_BODY_BYTES)) == store.SIGNIN_MAX_BODY_BYTES
    assert chunked(client, "/api/signins/checkout", body(store.SIGNIN_MAX_BODY_BYTES)).get_json()["status"] == "sign-in"


def test_a_lost_lease_is_a_409(client, db):
    lease = check(client, "checkout", name="contoso").get_json()["leaseToken"]
    r = check(client, "save", name="contoso", state=STATE, leaseToken="wrong")
    assert r.status_code == 409 and r.get_json()["error"] == "lease_lost" and r.get_json()["message"]
    expire_lease(db)
    r = check(client, "save", name="contoso", state=STATE, leaseToken=lease)
    assert r.status_code == 409 and r.get_json()["error"] == "lease_lost"
    assert row(db)["version"] == 0 and row(db)["stateCipher"] is None


def test_unknown_names_and_ids_are_404s(client):
    for r in (check(client, "save", name="nope", state=STATE, baseVersion=0, id=1),
              check(client, "save", name="nope", state=STATE, leaseToken="token"),
              check(client, "release", name="nope", leaseToken="token"),
              admin(client, "POST", "/9999/forget"), admin(client, "POST", "/0/forget"),
              admin(client, "POST", "/{}/forget".format(2 ** 63)), admin(client, "POST", "/9999/delete"),
              admin(client, "POST", "/{}/delete".format(10 ** 20))):
        assert r.status_code == 404 and r.get_json()["error"], r.request.path


def test_without_the_secrets_key(client, db, monkeypatch):
    sign_in(db)
    lease = check(client, "checkout", name="other").get_json()["leaseToken"]
    sid = row(db)["id"]
    monkeypatch.delenv(mfa.KEY_ENV)
    body = admin(client, "GET").get_json()
    assert body["keyProblem"] == "Set GRID_SECRETS_KEY to save sign-ins."
    assert [s["name"] for s in body["signins"]] == ["contoso", "other"]  # what is saved is still listed
    for r in (check(client, "checkout", name="contoso"), check(client, "checkout", name="new"),
              check(client, "save", name="contoso", state=NEWER, baseVersion=1, id=sid),
              check(client, "save", name="other", state=NEWER, leaseToken=lease)):
        assert r.status_code == 503 and r.get_json()["secrets_key_unset"] is True, r.request.path
    assert check(client, "release", name="other", leaseToken=lease).get_json() == {"released": True}
    assert admin(client, "POST", "/{}/forget".format(sid)).status_code == 200
    assert admin(client, "POST", "/{}/delete".format(sid)).status_code == 200
    monkeypatch.setenv(mfa.KEY_ENV, "short")
    assert "at least 16" in admin(client, "GET").get_json()["keyProblem"]
    assert run(db, "SELECT name FROM savedSignins") == [("other",)]


def test_a_full_table_and_a_missing_one(client, db, monkeypatch):
    monkeypatch.setattr(store, "SIGNIN_MAX_COUNT", 1)
    assert check(client, "checkout", name="contoso").status_code == 200
    r = check(client, "checkout", name="another")
    assert r.status_code == 409 and r.get_json()["signins_full"] is True and "1 saved sign-ins" in r.get_json()["error"]
    run(db, "DROP TABLE savedSignins")
    for r in check_requests(client, CHECK):
        assert r.status_code == 503 and "savedSignins" in r.get_json()["error"], r.request.path


def test_no_answer_and_no_log_record_holds_a_state_or_a_token(client, db, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    managed = store.create_token("checks", "client")["token"]
    answers, leases = [], []

    def keep(r, holds_state=False):
        answers.append((r, holds_state))
        return r

    lease = keep(check(client, "checkout", name="contoso")).get_json()["leaseToken"]
    leases.append(lease)
    keep(check(client, "checkout", {"Authorization": "Bearer " + managed}, name="contoso"))
    keep(check(client, "save", name="contoso", state=STATE, leaseToken="wrong-" + lease))
    keep(check(client, "save", name="contoso", state=STATE, leaseToken=lease))
    signin_id = keep(check(client, "checkout", name="contoso"), holds_state=True).get_json()["id"]
    keep(check(client, "save", name="contoso", state=NEWER, baseVersion=1, id=signin_id))
    keep(check(client, "save", name="contoso", state=STATE, baseVersion=1, id=signin_id))
    keep(check(client, "save", name="contoso", state={"cookies": [{"value": COOKIE}]}, baseVersion=2, id=signin_id))
    keep(check(client, "save", name="nope", state=STATE, baseVersion=0, id=signin_id))
    monkeypatch.setenv(mfa.KEY_ENV, KEY + "-changed")  # version 2 no longer decrypts
    leases.append(keep(check(client, "checkout", name="contoso")).get_json()["leaseToken"])
    keep(check(client, "release", name="contoso", leaseToken=leases[-1]))
    monkeypatch.setenv(mfa.KEY_ENV, KEY)
    leases.append(keep(check(client, "checkout", name="contoso", rejectedVersion=2)).get_json()["leaseToken"])
    keep(check(client, "release", name="contoso", leaseToken=leases[-1]))
    keep(check(client, "checkout", {"Authorization": "Bearer wrong-" + BOOTSTRAP}, name="contoso"))
    sid = keep(admin(client, "GET")).get_json()["signins"][0]["id"]
    monkeypatch.delenv(mfa.KEY_ENV)
    keep(check(client, "checkout", name="contoso"))
    keep(admin(client, "POST", "/{}/forget".format(sid)))
    keep(admin(client, "POST", "/{}/delete".format(sid)))

    assert [r.status_code for r, _ in answers] == [200, 200, 409, 200, 200, 200, 200, 400, 404, 200, 200, 200, 200, 401,
                                                   200, 503, 200, 200]
    assert any("decrypt" in record.getMessage() for record in caplog.records)
    assert any("contoso" in record.getMessage() for record in caplog.records)  # the log is captured
    logged = caplog.text + "".join(repr(record.args) + record.getMessage() for record in caplog.records)
    for secret in (COOKIE, OTHER_COOKIE, LOCAL, KEY, BOOTSTRAP, managed, *leases):
        assert secret not in logged
    for r, holds_state in answers:
        text = r.get_data(as_text=True)
        for secret in (KEY, BOOTSTRAP, managed) + (() if holds_state else (COOKIE, OTHER_COOKIE, LOCAL)):
            assert secret not in text, (r.request.path, r.status_code)
        assert sum(lease in text for lease in leases) <= ("leaseToken" in text)
