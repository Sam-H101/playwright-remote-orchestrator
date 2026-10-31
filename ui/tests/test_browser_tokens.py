"""The Access tab (managed access tokens) in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import hashlib
import re

import pytest

from ui import app as grid_app, store

pytest.importorskip("playwright.sync_api")

from ui.tests.test_browser import browser, grid, page, run  # noqa: E402,F401  (fixtures)


@pytest.fixture
def admin_grid(grid, monkeypatch):  # noqa: F811
    """The demo grid with GRID_ADMIN_TOKEN set and a known hub URL."""
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(store, "GATEWAY_URL", "ws://grid-hub.example:4500")
    monkeypatch.delenv("GATEWAY_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return grid


def _admin_context(browser, width=1280, height=900):  # noqa: F811
    """A context whose tab already holds the admin token, as after entering it once."""
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_init_script("sessionStorage.setItem('gridToken', 's3cret')")
    return ctx


@pytest.fixture
def admin_page(browser):  # noqa: F811
    ctx = _admin_context(browser)
    yield ctx.new_page()
    ctx.close()


def token_requests(p):
    seen = []
    p.on("request", lambda r: seen.append((r.method, r.headers.get("x-grid-token"))) if "/api/tokens" in r.url else None)
    return seen


def refresh_now(p):
    p.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    p.wait_for_timeout(400)


def row(p, name):
    return p.locator("#token-rows tr", has_text=name)


def names(p):
    return p.eval_on_selector_all("#token-rows .tok-name", "els => els.map(e => e.textContent)")


def test_access_tab_without_an_admin_token_explains_the_setting(grid, page):  # noqa: F811
    url, _ = grid
    seen = token_requests(page)
    page.goto(url + "#access")
    page.wait_for_selector("#access-state .empty")
    assert "Set an admin token" in page.text_content("#access-state")
    assert "GRID_ADMIN_TOKEN=" in page.text_content("#access-state")
    assert not page.is_visible("#token-dialog") and not page.is_visible("#new-token-btn")
    page.click("#tab-overview")
    page.click("#tab-access")
    refresh_now(page)
    assert seen == [("GET", "")]  # asked once; that 403 is final, not retried


def test_access_tab_asks_for_the_admin_token_and_a_refusal_does_not_loop(admin_grid, page):  # noqa: F811
    url, _ = admin_grid
    seen = token_requests(page)
    page.goto(url + "#access")
    page.wait_for_selector("#token-dialog[open]")  # no token stored: asked when the tab opens
    assert seen == []
    page.fill("#token-input", "wrong")
    page.press("#token-input", "Enter")
    page.wait_for_function("document.getElementById('token-text').textContent.indexOf('rejected') !== -1")
    page.click("#token-dialog [data-dismiss]")
    page.wait_for_selector("[data-unlock]")
    assert "rejected" in page.text_content("#access-state")
    refresh_now(page)
    refresh_now(page)
    assert seen == [("GET", "wrong")]

    page.click("[data-unlock]")
    page.fill("#token-input", "s3cret")
    page.press("#token-input", "Enter")
    page.wait_for_selector("#token-rows tr")
    assert seen == [("GET", "wrong"), ("GET", "s3cret")]
    assert page.locator("#token-rows tr").count() == 6


def test_create_reveal_revoke_and_delete(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    p.goto(url + "#access")
    p.wait_for_selector("#token-rows tr")
    p.click("#new-token-btn")
    p.click("#nt-submit")  # no name: stays open and says why
    assert "name" in p.text_content("#nt-error")
    p.fill("#nt-name", "Build farm")
    p.fill("#nt-node", "Win Build")
    p.click("#nt-submit")
    assert "node name" in p.text_content("#nt-error")
    p.fill("#nt-node", "Win-Build-09")
    p.select_option("#nt-expiry", "custom")
    p.fill("#nt-days", "45")
    p.click("#nt-submit")

    p.wait_for_selector(".reveal")
    token = p.text_content("#reveal-token")
    assert re.fullmatch(r"pwga_[A-Za-z0-9_-]{43}", token)
    ((stored_hash, node_name, expires, created),) = run(
        path, "SELECT tokenHash, nodeName, expiresAt, createdAt FROM accessTokens WHERE name = 'Build farm'")
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest() and node_name == "win-build-09"
    assert (store._parse_time(expires) - store._parse_time(created)).days == 45
    usage = p.text_content(".usage")
    assert ".\\install-backend.ps1 -HubUrl ws://grid-hub.example:4500 -AgentToken " + token + " -NodeName win-build-09" in usage
    assert "GATEWAY_AGENT_TOKEN=" + token + " node gateway/agent.js --hub ws://grid-hub.example:4500 --name win-build-09" in usage
    assert "won't be shown again" in p.text_content(".reveal")
    p.wait_for_selector("#token-rows tr:has-text('Build farm')")

    p.click("[data-reveal-done]")  # not copied yet: asks first
    p.wait_for_selector("#confirm-dialog[open]")
    p.click("#confirm-dialog [data-dismiss]")
    assert p.is_visible(".reveal")
    p.click(".reveal [data-copy-label='the token']")
    p.click("[data-reveal-done]")
    p.wait_for_selector(".reveal", state="detached")
    assert token not in p.content()

    row(p, "Docker runners").locator("[data-revoke]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert p.eval_on_selector_all("#confirm-list li", "els => els.map(e => e.textContent)") == ["runner-7f3a9c", "runner-b81e04"]
    p.click("#confirm-ok")
    p.wait_for_selector("#token-rows tr[data-status=revoked]:has-text('Docker runners')")
    assert run(path, "SELECT revokedAt IS NOT NULL FROM accessTokens WHERE name = 'Docker runners'") == [(1,)]

    row(p, "Docker runners").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    p.click("#confirm-ok")
    p.wait_for_selector("#token-rows tr:has-text('Docker runners')", state="detached")
    assert run(path, "SELECT COUNT(*) FROM nodes WHERE name LIKE 'runner-%' AND tokenId IS NOT NULL") == [(0,)]


def test_client_token_usage_and_the_first_token_notice(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "UPDATE accessTokens SET revokedAt = '2026-01-01 00:00:00' WHERE kind = 'client'")
    p = admin_page
    p.goto(url + "#access")
    p.wait_for_selector("#token-rows tr")
    p.click("#new-token-btn")
    assert not p.is_visible("#nt-first")  # live agent tokens exist already
    p.check("input[name=kind][value=client]")
    assert not p.is_visible("#nt-node-field")
    assert "first client token" in p.text_content("#nt-first")
    p.fill("#nt-name", "Checks")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    token = p.text_content("#reveal-token")
    assert re.fullmatch(r"pwgc_[A-Za-z0-9_-]{43}", token)
    assert "GATEWAY_CLIENT_TOKEN=" + token in p.text_content(".usage")

    # Revoking the last working client token warns that checks are locked out (without the env token):
    # the hub keeps asking for one.
    row(p, "Checks").locator("[data-revoke]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    note = p.text_content("#confirm-note")
    assert "last working client token" in note and "GATEWAY_CLIENT_TOKEN" in note and "until you create a new token" in note
    assert p.is_hidden("#confirm-list")


def test_deleting_the_last_token_on_record_warns_that_the_hub_opens_up(admin_grid, admin_page):
    """Any row of a kind, revoked or expired, keeps the hub asking; only deleting the last one opens it on restart."""
    url, path = admin_grid
    p = admin_page
    p.goto(url + "#access")
    p.wait_for_selector("#token-rows tr")

    # Another client token is on record (revoked or not): deleting the expired one changes nothing at the hub.
    run(path, "UPDATE accessTokens SET revokedAt = '2026-01-01 00:00:00' WHERE name = 'Example checks'")
    refresh_now(p)
    p.wait_for_selector("#token-rows tr[data-status=revoked]:has-text('Example checks')")
    row(p, "Staging checks 2025").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert "turns it away" in p.text_content("#confirm-text")
    assert p.is_hidden("#confirm-note")
    p.click("#confirm-dialog [data-dismiss]")

    # With only the revoked one left, deleting it means a restarted hub stops asking for client tokens.
    run(path, "DELETE FROM accessTokens WHERE name = 'Staging checks 2025'")
    refresh_now(p)
    p.wait_for_selector("#token-rows tr:not(:has-text('Staging checks 2025'))")
    row(p, "Example checks").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    note = p.text_content("#confirm-note")
    assert "last client token on record" in note and "GATEWAY_CLIENT_TOKEN" in note and "next restart" in note
    p.click("#confirm-dialog [data-dismiss]")
    assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE kind = 'client'") == [(1,)]


def test_token_filters(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    p.goto(url + "#access")
    p.wait_for_selector("#token-rows tr")

    p.click("#token-kind [data-kind=client]")
    assert sorted(names(p)) == ["Example checks", "Staging checks 2025"]
    p.click("#token-status [data-status=active]")
    assert names(p) == ["Example checks"]
    assert p.text_content("#token-kind [data-kind=agent]") == "Agent3"  # counts follow the other filter
    p.click("#token-kind [data-kind=all]")
    p.click("#token-status [data-status=revoked]")
    assert names(p) == ["Old build farm"]
    p.click("#token-status [data-status=all]")
    p.fill("#token-filter", "win-build")
    assert names(p) == ["WIN-BUILD-01 agent"]
    p.fill("#token-filter", "nothing like this")
    p.wait_for_selector("#tokens-empty:not([hidden])")
    p.click("[data-clear-token-filter]")
    assert len(names(p)) == 6
    assert names(p)[-2:] == ["Staging checks 2025", "Old build farm"]  # expired, then revoked, last


def test_an_older_demo_database_shows_an_empty_state(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "DROP TABLE accessTokens")
    admin_page.goto(url + "#access")
    admin_page.wait_for_selector("#access-state .empty")
    assert "aren't set up" in admin_page.text_content("#access-state")
    assert not admin_page.is_visible("#new-token-btn") and not admin_page.is_visible("#token-panel")


def test_token_names_are_escaped(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "UPDATE accessTokens SET name = '<img src=x onerror=\"window.__xss=1\">', nodeName = '\"><b>' WHERE id = 1")
    admin_page.goto(url + "#access")
    admin_page.wait_for_selector("#token-rows tr")
    row(admin_page, "<img").locator("[data-revoke]").click()
    admin_page.wait_for_timeout(300)
    assert admin_page.evaluate("window.__xss") is None
    assert "<img" in admin_page.text_content("#confirm-title")


def test_access_tab_fits_a_phone(admin_grid, browser):  # noqa: F811
    url, _ = admin_grid
    ctx = _admin_context(browser, 375, 800)
    p = ctx.new_page()
    try:
        p.goto(url + "#access")
        p.wait_for_selector("#token-rows tr")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert p.evaluate("getComputedStyle(document.querySelector('.tokens thead')).display") == "none"
        p.click("#new-token-btn")
        p.fill("#nt-name", "phone")
        p.click("#nt-submit")
        p.wait_for_selector(".reveal")
        p.wait_for_timeout(300)
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        top = p.evaluate("document.querySelector('.reveal').getBoundingClientRect().top")
        assert top >= p.evaluate("document.querySelector('.top').getBoundingClientRect().bottom")  # not under the bar
    finally:
        ctx.close()
