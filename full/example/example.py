import contextlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Error, Playwright, expect

URL = "https://status.docusign.com/"
PRODUCT = "eSignature"
MONITORED_ENVIRONMENTS = ("NA1", "NA2", "NA3", "NA4")
# status.docusign.com redirects to health.docusign.com, a Next.js app that renders one card per
# product. Per-environment ("site") status only exists in this JSON feed, which the page fetches.
COMPONENTS_FEED = re.compile(r"/components\.json(\?|$)")
OPERATIONAL = "available"
TIMEOUT_MS = 30 * 1000
# The hub holds a connect while every slot that fits is busy (GATEWAY_QUEUE_TIMEOUT_S, 60 s by default)
# and then tries up to GATEWAY_CONNECT_ATTEMPTS slots, so wait longer than that for its answer.
CONNECT_TIMEOUT_MS = int(os.environ.get("PW_CONNECT_TIMEOUT_S", "180")) * 1000
CONNECT_FAILURE = "connect_Failure"
HUB_FAILURE = "hub_Failure"
# Through the hub (ws://hub:4500/connect), an HTTP answer means the hub refused the check, found no slot
# for it in time or tried slots that all failed, and a network error means it could not be reached.
# Playwright puts the status right after the URL, without the URL's query.
HUB_ERRORS = re.compile(r"WebSocket error: \S+ \d{3} [A-Z]|\b(ECONNREFUSED|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|ENETUNREACH)\b")


def log_dir():
    cwd_logs = Path.cwd() / "logs"
    return cwd_logs if cwd_logs.is_dir() else Path(__file__).resolve().parent / "logs"


def endpoint():
    # main.py sets it to <GATEWAY_URL>/connect?gateway_token=...; the hub picks the slot.
    return os.environ.get("PW_WS_ENDPOINT")


def is_hub_error(ws, error):
    return urlsplit(ws).path.rstrip("/") == "/connect" and HUB_ERRORS.search(str(error)) is not None


def open_browser(playwright: Playwright):
    ws = endpoint()
    if not ws:
        return playwright.chromium.launch()
    try:
        return playwright.chromium.connect(ws, timeout=CONNECT_TIMEOUT_MS)
    except Exception as ex:
        # main.py logs why the check got no browser. The markers are constants because pytest
        # prints this function's source, which must not hold both.
        raise Exception(HUB_FAILURE if is_hub_error(ws, ex) else CONNECT_FAILURE) from ex


def environment_problems(components):
    products = [c for c in components if c.get("type") == "product" and c.get("name") == PRODUCT]
    assert products, "{} is not listed in the status feed".format(PRODUCT)
    child_ids = set(products[0].get("children") or [])
    sites = {c["name"]: c.get("status") for c in components if c.get("id") in child_ids}
    problems = []
    for env in MONITORED_ENVIRONMENTS:
        status = sites.get(env)
        if status is None:
            problems.append("{} (not listed)".format(env))
        elif status != OPERATIONAL:
            problems.append("{} ({})".format(env, status.replace("_", " ")))
    return problems


def test_docusign(playwright: Playwright):
    browser = open_browser(playwright)
    page = None
    try:
        page = browser.new_page()
        page.set_default_timeout(TIMEOUT_MS)
        with page.expect_response(lambda r: COMPONENTS_FEED.search(r.url) is not None) as feed:
            # The status comes from the feed, so don't wait for every asset's load event.
            page.goto(URL, wait_until="domcontentloaded")
        assert feed.value.ok, "status feed {} returned HTTP {}".format(feed.value.url, feed.value.status)

        # The cards are client-rendered from the same feed; seeing ours proves the page itself works.
        card = page.locator("[data-context='{}-status-card-click']".format(PRODUCT))
        expect(card.get_by_role("heading", name=PRODUCT, exact=True)).to_be_visible()
        card_status = card.inner_text().replace(PRODUCT, "", 1).strip()

        problems = environment_problems(feed.value.json()["components"])
        assert not problems, "{} North America environments not operational: {} (product card shows '{}')".format(
            PRODUCT, ", ".join(problems), card_status)
    except Exception:
        if page:
            directory = log_dir()
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(Error):
                page.screenshot(path=str(directory / "docusign_failure.png"), full_page=True)
        raise
    finally:
        if page:
            with contextlib.suppress(Error):
                page.close()
        browser.close()
