"""Shared pieces of the proof-of-concept checks in this folder.

Each check is a pytest file that main.py runs like full/example/example.py: it connects to the hub through
PW_WS_ENDPOINT, the hub picks a runner, and the check spends about POC_DURATION_S (180 s) doing real
browser work there. Most checks drive a fake site that the check itself serves through Playwright's
request routing (FakeSite), so they test the grid and the browsers, not someone else's website.
"""
import contextlib
import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import Error, Playwright

# The hub holds a connect while every slot that fits is busy (GATEWAY_QUEUE_TIMEOUT_S, 60 s) and then
# tries up to GATEWAY_CONNECT_ATTEMPTS slots, so wait longer than that for its answer.
CONNECT_TIMEOUT_MS = int(os.environ.get("PW_CONNECT_TIMEOUT_S", "180")) * 1000
# How long each check keeps working. The checks repeat their scenario until this is used up.
DURATION_S = float(os.environ.get("POC_DURATION_S", "180"))
CONNECT_FAILURE = "connect_Failure"
HUB_FAILURE = "hub_Failure"
# Through the hub, an HTTP answer means the hub refused the check or found no slot in time, and a
# network error means it could not be reached (same rule as full/example/example.py).
HUB_ERRORS = re.compile(r"WebSocket error: \S+ \d{3} [A-Z]|\b(ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH)\b")


def log_dir():
    cwd_logs = Path.cwd() / "logs"
    return cwd_logs if cwd_logs.is_dir() else Path(__file__).resolve().parent / "logs"


def connect(playwright: Playwright, browser_name="chromium"):
    """A browser on the grid (a local one when PW_WS_ENDPOINT is unset, for a dev machine)."""
    browser_type = getattr(playwright, browser_name)
    ws = os.environ.get("PW_WS_ENDPOINT")
    if not ws:
        return browser_type.launch()
    try:
        return browser_type.connect(ws, timeout=CONNECT_TIMEOUT_MS)
    except Exception as ex:
        hub = urlsplit(ws).path.rstrip("/") == "/connect" and HUB_ERRORS.search(str(ex)) is not None
        # main.py logs why the check got no browser; the markers are constants (see example.py).
        raise Exception(HUB_FAILURE if hub else CONNECT_FAILURE) from ex


class Budget:
    """Wall-clock budget for a check: `while budget.more(): ...` repeats a scenario until the time is
    used up, keeping room for one more round of `reserve_s` seconds."""

    def __init__(self, seconds):
        self.seconds = seconds
        self.started = time.monotonic()
        self.rounds = 0

    def elapsed(self):
        return time.monotonic() - self.started

    def left(self):
        return self.seconds - self.elapsed()

    def more(self, reserve_s=0.0):
        if self.rounds and self.left() <= reserve_s:
            return False
        self.rounds += 1
        return True


class Capture:
    """Pages to screenshot into the log folder if the test fails."""

    def __init__(self):
        self.pages = []

    def add(self, page):
        self.pages.append(page)
        return page


class FakeSite:
    """A website served by the check itself: every request to `origin` is answered by `handler(route,
    request)` through context.route, so no server and no outside network is involved, and every
    response is deterministic. Handlers usually call `route.fulfill(...)`."""

    def __init__(self, context, origin, handler):
        self.origin = origin.rstrip("/")
        self.requests = []
        self._handler = handler
        context.route(self.origin + "/**", self._serve)

    def _serve(self, route, request):
        self.requests.append((request.method, urlsplit(request.url).path))
        self._handler(route, request)

    def url(self, path="/"):
        return self.origin + path

    def count(self, method, path):
        return sum(1 for m, p in self.requests if m == method and p == path)


def html(body, title="POC", head=""):
    return "<!doctype html><html><head><meta charset='utf-8'><title>{}</title>{}</head><body>{}</body></html>".format(title, head, body)


@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    setattr(item, "rep_" + outcome.get_result().when, outcome.get_result())


@pytest.fixture
def budget():
    return Budget(DURATION_S)


@pytest.fixture
def capture(request):
    c = Capture()
    yield c
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed and c.pages:
        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        for i, page in enumerate(c.pages):
            with contextlib.suppress(Error):
                if not page.is_closed():
                    page.screenshot(path=str(directory / "{}_failure_{}.png".format(request.node.name, i)), full_page=True)


@pytest.fixture
def grid_browser(playwright):
    """A Chromium on the grid, closed after the test."""
    browser = connect(playwright, "chromium")
    yield browser
    with contextlib.suppress(Error):
        browser.close()
