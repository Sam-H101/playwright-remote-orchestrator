"""full/poc/poc_signin_reuse.py's care with what its failures say: main.py writes a failed check's output to the
check's logs, so a failure names what went wrong, never the saved state a checkout answer may carry.

Skipped unless the playwright package is installed (the POC imports it), like test_example_check.py."""
import os
import sys

import pytest

pytest.importorskip("playwright")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COOKIE = "estsauth-cookie-value-for-the-poc-tests"


@pytest.fixture
def poc(monkeypatch):
    """The POC, imported as main.py's pytest run imports it: with full/poc/conftest.py as conftest."""
    monkeypatch.syspath_prepend(ROOT)
    monkeypatch.syspath_prepend(os.path.join(ROOT, "full", "poc"))
    for name in ("conftest", "poc_signin_reuse"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    import poc_signin_reuse
    yield poc_signin_reuse
    for name in ("conftest", "poc_signin_reuse"):
        sys.modules.pop(name, None)


def test_a_lease_it_did_not_get_is_reported_without_the_state(poc, monkeypatch):
    # Another run saved meanwhile, so the UI ignores the stale refusal and answers with the newer state.
    answer = {"status": "saved", "id": 7, "name": poc.NAME, "version": 4, "savedAt": "2026-11-02T06:00:00Z",
              "expiresAt": "2026-12-02T06:00:00Z", "state": {"cookies": [
                  {"name": "ESTSAUTH", "value": COOKIE, "domain": ".login.microsoftonline.com"}], "origins": []}}
    asked = []
    monkeypatch.setattr(poc.signin, "checkout", lambda name, **kwargs: asked.append((name, kwargs)) or answer)
    recorder = poc.Recorder()
    recorder.on_checkout(None, {"status": "saved", "id": 7, "version": 3})
    contender = poc.Round.__new__(poc.Round)  # only its recorder is used
    contender.recorder = recorder
    with pytest.raises(AssertionError) as e:
        contender._take_lease("to save")
    assert COOKIE not in str(e.value) and "ESTSAUTH" not in str(e.value)
    assert "got status 'saved' at version 4" in str(e.value)
    assert repr(poc.NAME) in str(e.value) and "POC_SIGNIN_NAME" in str(e.value)
    assert asked == [(poc.NAME, {"rejected_version": 3, "signin_id": 7})]
