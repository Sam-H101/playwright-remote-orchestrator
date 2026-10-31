import os
import subprocess
import sys

import pytest

pytest.importorskip("playwright")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "full", "example"))

import example  # noqa: E402
import main  # noqa: E402

HUB = "ws://hub:4500/connect?gateway_token=t"
STATIC = "ws://runner01:3000"


# Error texts as Playwright 1.64 reports them for each case; the URL in them has no query.
@pytest.mark.parametrize("ws, error, hub", [
    (HUB, "BrowserType.connect: WebSocket error: ws://hub:4500/connect 401 Unauthorized\n"
          "missing or bad gateway token", True),
    (HUB, "BrowserType.connect: WebSocket error: ws://hub:4500/connect 503 Service Unavailable\n"
          "no free firefox slot within 60s: 4 matching slots, all busy", True),
    (HUB, "BrowserType.connect: WebSocket error: ws://hub:4500/connect 404 Not Found\n"
          "no node or static host named zz is known to the hub", True),
    (HUB, "BrowserType.connect: WebSocket error: ws://hub:4500/connect 502 Bad Gateway\n"
          "node node1 slot 1: run-server is not running (tried 3 slots)", True),
    (HUB, "BrowserType.connect: WebSocket error: ws://hub:4500/connect 504 Gateway Timeout", True),
    (HUB, "BrowserType.connect: WebSocket error: connect ECONNREFUSED 127.0.0.1:4500", True),
    (HUB, "BrowserType.connect: WebSocket error: getaddrinfo ENOTFOUND hub", True),
    ("ws://hub:4500/connect/?node=n", "BrowserType.connect: WebSocket error: ws://hub:4500/connect/ 401 "
                                      "Unauthorized\nmissing or bad gateway token", True),
    # no answer from the hub in time, or the session broke after it opened
    (HUB, "BrowserType.connect: Timeout 180000ms exceeded.", False),
    (HUB, "Browser.new_page: Target page, context or browser has been closed", False),
    # not the hub: a run-server connected directly
    (STATIC, "BrowserType.connect: WebSocket error: connect ECONNREFUSED 10.0.0.5:3000", False),
])
def test_hub_errors_are_told_apart_from_other_connect_errors(ws, error, hub):
    assert example.is_hub_error(ws, Exception(error)) is hub


@pytest.mark.skipif("PW_CONNECT_TIMEOUT_S" in os.environ, reason="the connect timeout is overridden")
def test_connect_timeout_outlasts_the_hubs_queue_and_failover():
    # GATEWAY_QUEUE_TIMEOUT_S (60 s), then up to GATEWAY_CONNECT_ATTEMPTS (3) x OPEN_TIMEOUT_S (15 s)
    assert example.CONNECT_TIMEOUT_MS > (60 + 3 * 15) * 1000


def test_markers_match_main():
    assert example.CONNECT_FAILURE == main.CONNECT_FAILURE_MARKER
    assert example.HUB_FAILURE == main.HUB_FAILURE_MARKER


@pytest.mark.parametrize("endpoint, marker", [
    ("ws://127.0.0.1:9/connect", "hub_Failure"),  # nothing listens on the discard port
    ("ws://127.0.0.1:9", "connect_Failure"),
])
def test_output_names_only_one_marker(endpoint, marker):
    # pytest prints open_browser's source on failure; only the raised marker may appear in it.
    env = dict(os.environ, PW_WS_ENDPOINT=endpoint)
    proc = subprocess.run([sys.executable, "-m", "pytest", os.path.join(ROOT, "full", "example", "example.py")],
                          env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 1
    other = {"hub_Failure": "connect_Failure", "connect_Failure": "hub_Failure"}[marker]
    assert marker in output and other not in output
