import pytest

from ui import accounts, app as grid_app, auth, oidc, recorder, store


@pytest.fixture(autouse=True)
def _fresh_sign_in_limit():
    """Wrong admin tokens in one test must not count towards another test's guessing limit, its sign-in starts
    towards another's start limit, nor its throttled audit rows keep another's from being written."""
    auth.reset_failures()
    grid_app.reset_login_starts()
    grid_app.reset_audit_throttles()
    yield
    auth.reset_failures()
    grid_app.reset_login_starts()
    grid_app.reset_audit_throttles()


@pytest.fixture(autouse=True)
def _fresh_oidc_caches():
    """One test's discovery document, signing keys and table check must not answer for another test's identity
    provider or database, nor the warnings it logged keep another's from being logged."""
    oidc.reset_caches()
    accounts.reset_caches()
    store.reset_warnings()
    yield
    oidc.reset_caches()
    accounts.reset_caches()
    store.reset_warnings()


@pytest.fixture(autouse=True)
def _recorders_stop_with_their_test(monkeypatch):
    """Every status poll starts the app's background recorder. Left running, it reads whatever database the
    next tests point store at, and races their setup (it applies the schema to a MySQL database they have
    just emptied, for one). Stop each one a test started when the test ends."""
    started = set()
    ensure_running = recorder.Recorder.ensure_running

    def tracked(rec):
        started.add(rec)
        ensure_running(rec)

    monkeypatch.setattr(recorder.Recorder, "ensure_running", tracked)
    yield
    for rec in started:
        rec.stop()
