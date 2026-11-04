import pytest

from ui import auth, recorder


@pytest.fixture(autouse=True)
def _fresh_sign_in_limit():
    """Wrong admin tokens in one test must not count towards another test's guessing limit."""
    auth.reset_failures()
    yield
    auth.reset_failures()


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
