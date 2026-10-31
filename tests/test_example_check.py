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

GATEWAY = "ws://hub:4500/connect/node1/1?gateway_token=t"
STATIC = "ws://runner01:3000"


# Error texts as Playwright 1.64 reports them for each case.
@pytest.mark.parametrize("ws, error, hub", [
    (GATEWAY, "BrowserType.connect: WebSocket error: ws://hub:4500/connect/node1/1 401 Unauthorized\n"
              "missing or bad gateway token", True),
    (GATEWAY, "BrowserType.connect: WebSocket error: ws://hub:4500/connect/node1/1 503 Service Unavailable\n"
              "hub is shutting down", True),
    (GATEWAY, "BrowserType.connect: WebSocket error: connect ECONNREFUSED 127.0.0.1:4500", True),
    (GATEWAY, "BrowserType.connect: WebSocket error: getaddrinfo ENOTFOUND hub", True),
    (GATEWAY, "BrowserType.connect: WebSocket error: ws://hub:4500/connect/node1/1 404 Not Found\n"
              "node node1 is not connected", False),
    (GATEWAY, "BrowserType.connect: WebSocket error: ws://hub:4500/connect/node1/1 502 Bad Gateway\n"
              "node node1 slot 1: run-server is not running", False),
    (GATEWAY, "BrowserType.connect: WebSocket error: ws://hub:4500/connect/node1/1 504 Gateway Timeout", False),
    (GATEWAY, "BrowserType.connect: Timeout 30000ms exceeded.", False),
    (STATIC, "BrowserType.connect: WebSocket error: connect ECONNREFUSED 10.0.0.5:3000", False),
    ("ws://hub:4500/connect/503/1", "WebSocket error: ws://hub:4500/connect/503/1 404 Not Found", False),
])
def test_hub_errors_are_told_apart_from_slot_errors(ws, error, hub):
    assert example.is_hub_error(ws, Exception(error)) is hub


def test_markers_match_main():
    assert example.CONNECT_FAILURE == main.CONNECT_FAILURE_MARKER
    assert example.HUB_FAILURE == main.HUB_FAILURE_MARKER


@pytest.mark.parametrize("endpoint, marker", [
    ("ws://127.0.0.1:9/connect/node1/1", "hub_Failure"),  # nothing listens on the discard port
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
