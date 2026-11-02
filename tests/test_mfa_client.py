"""src/mfa.py, the helper checks get MFA codes with, against a stub Grid UI served from the test process."""
import http.server
import json
import os
import socket
import sys
import threading
import traceback
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import mfa  # noqa: E402

TOKEN = "pwgc_a-client-token-for-the-tests"


class StubGridUi:
    """Answers each request with the next scripted (status, body) and keeps what it was sent."""

    def __init__(self):
        self.answers = []
        self.requests = []
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
                status, answer = stub.answers.pop(0) if stub.answers else (500, {"error": "nothing scripted"})
                data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Clock:
    """time.monotonic for code(), moved on only by the sleep() it is given."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.fixture
def grid_ui(monkeypatch):
    stub = StubGridUi()
    monkeypatch.setenv("GRID_UI_URL", stub.url + "/")
    monkeypatch.setenv("GRID_MFA_TOKEN", TOKEN)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    yield stub
    stub.close()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(mfa, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    return clock


def code_answer(code="123456", step=57000000, wait_s=0.0, valid_for_s=20.0):
    return 200, {"id": 1, "name": "intranet", "code": code, "step": step, "waitS": wait_s, "validForS": valid_for_s,
                 "period": 30, "digits": 6}


def busy(retry_after_s=7.5):
    return 429, {"error": "Codes for this secret are being handed out faster than they change.", "retry_after_s": retry_after_s}


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def assert_no_token(error):
    text = str(error) + repr(error) + "".join(traceback.format_exception(error))
    assert TOKEN not in text
    assert error.__cause__ is None and error.__suppress_context__  # urllib's error holds the request


def test_code_asks_with_the_client_token(grid_ui, clock):
    grid_ui.answers = [code_answer("654321"), code_answer()]
    assert mfa.code("intranet", label="nightly login check", min_valid_s=8, sleep=clock.sleep) == "654321"
    assert mfa.code("intranet", sleep=clock.sleep) == "123456"
    assert clock.slept == []
    first, second = grid_ui.requests
    assert first["path"] == "/api/mfa/code"
    assert first["headers"]["Authorization"] == "Bearer " + TOKEN
    assert first["headers"]["Content-Type"] == "application/json"
    assert first["body"] == {"name": "intranet", "label": "nightly login check", "minValidS": 8}
    assert second["body"] == {"name": "intranet"}


def test_issue_returns_the_whole_answer_without_waiting(grid_ui, clock):
    grid_ui.answers = [code_answer(wait_s=12.5, valid_for_s=42.5)]
    answer = mfa.issue("intranet", min_valid_s=0)
    assert answer["waitS"] == 12.5 and answer["validForS"] == 42.5 and answer["code"] == "123456"
    assert grid_ui.requests[0]["body"] == {"name": "intranet", "minValidS": 0}
    assert clock.slept == []


def test_a_code_from_the_next_window_is_typed_once_it_opens(grid_ui, clock):
    grid_ui.answers = [code_answer(wait_s=12.5, valid_for_s=42.5)]
    assert mfa.code("intranet", sleep=clock.sleep) == "123456"
    assert clock.slept == [12.5]


def test_busy_secrets_are_asked_again(grid_ui, clock):
    grid_ui.answers = [busy(7.5), busy(7.5), busy(None), code_answer(wait_s=3)]
    assert mfa.code("intranet", sleep=clock.sleep) == "123456"
    assert clock.slept == [7.5, 7.5, 1, 3]  # no retry_after_s: a second
    assert len(grid_ui.requests) == 4


def test_busy_gives_up_after_max_wait_s(grid_ui, clock):
    grid_ui.answers = [busy(30)] * 5
    with pytest.raises(mfa.MfaError) as e:
        mfa.code("intranet", sleep=clock.sleep, max_wait_s=70)
    assert clock.slept == [30, 30]  # a third wait would end past the 70 s
    assert e.value.status == 429 and e.value.retry_after_s == 30 and "429" in str(e.value)
    assert len(grid_ui.requests) == 3
    assert_no_token(e.value)

    grid_ui.answers = [busy(200)]
    with pytest.raises(mfa.MfaError):
        mfa.code("intranet", sleep=clock.sleep)  # longer than the default 120 s at once: no wait at all
    assert clock.slept == [30, 30]


@pytest.mark.parametrize("status, answer, says", [
    (400, {"error": 'Send {"name": "<secret name>"} as JSON.'}, "Send"),
    (401, {"error": "The client token is wrong, revoked or expired.", "client_token_required": True}, "client token"),
    (404, {"error": "There is no MFA secret called 'intranet'."}, "no MFA secret"),
    (409, {"error": "The seed of 'intranet' doesn't decrypt", "cannot_decrypt": True}, "decrypt"),
    (503, {"error": "Set GRID_SECRETS_KEY to store MFA secrets.", "secrets_key_unset": True}, "GRID_SECRETS_KEY"),
    (502, "<html>Bad gateway</html>", "Bad Gateway"),  # not the Grid UI's answer: the HTTP reason
])
def test_other_refusals_raise_at_once(grid_ui, clock, status, answer, says):
    grid_ui.answers = [(status, answer)]
    with pytest.raises(mfa.MfaError) as e:
        mfa.code("intranet", sleep=clock.sleep)
    assert e.value.status == status and e.value.retry_after_s is None
    assert "HTTP {}".format(status) in str(e.value) and says in str(e.value) and "'intranet'" in str(e.value)
    assert clock.slept == [] and len(grid_ui.requests) == 1
    assert_no_token(e.value)


def test_an_unreachable_grid_ui(monkeypatch, clock):
    monkeypatch.setenv("GRID_MFA_TOKEN", TOKEN)
    url = "http://127.0.0.1:{}".format(closed_port())
    with pytest.raises(mfa.MfaError) as e:
        mfa.code("intranet", url=url, sleep=clock.sleep)
    assert url in str(e.value) and "could not be reached" in str(e.value)
    assert getattr(e.value, "status", None) is None and clock.slept == []  # not retried
    assert_no_token(e.value)


def test_where_the_token_and_url_come_from(grid_ui, monkeypatch, clock):
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "the-bootstrap-token")
    grid_ui.answers = [code_answer() for _ in range(3)]
    mfa.code("intranet", sleep=clock.sleep)
    monkeypatch.delenv("GRID_MFA_TOKEN")
    mfa.code("intranet", sleep=clock.sleep)
    mfa.code("intranet", token="given", sleep=clock.sleep)
    assert [r["headers"]["Authorization"] for r in grid_ui.requests] == [
        "Bearer " + TOKEN, "Bearer the-bootstrap-token", "Bearer given"]

    grid_ui.answers = [code_answer()]
    monkeypatch.setenv("GRID_UI_URL", "http://127.0.0.1:{}".format(closed_port()))
    assert mfa.code("intranet", url=grid_ui.url, sleep=clock.sleep) == "123456"  # url= wins over GRID_UI_URL
    monkeypatch.delenv("GRID_UI_URL")
    assert mfa._settings(None, "t") == ("http://grid_ui:4444", "t")


def test_no_token_no_request(grid_ui, monkeypatch, clock):
    monkeypatch.delenv("GRID_MFA_TOKEN")
    with pytest.raises(mfa.MfaError, match="GRID_MFA_TOKEN or GATEWAY_CLIENT_TOKEN"):
        mfa.code("intranet", sleep=clock.sleep)
    monkeypatch.setenv("GRID_MFA_TOKEN", "")
    with pytest.raises(mfa.MfaError):
        mfa.issue("intranet")
    assert grid_ui.requests == []
