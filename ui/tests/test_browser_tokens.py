"""The Access view (managed access tokens) in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import hashlib
import re

import pytest

from ui import app as grid_app, store

pytest.importorskip("playwright.sync_api")

from ui.tests.test_browser import browser, grid, page, refresh_now, run  # noqa: E402,F401  (fixtures)


@pytest.fixture
def admin_grid(grid, monkeypatch):  # noqa: F811
    """The demo grid with GRID_ADMIN_TOKEN set and a known public hub address."""
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    monkeypatch.setattr(store, "GATEWAY_URL", "ws://hub:4500")
    monkeypatch.setattr(grid_app, "PUBLIC_GATEWAY_URL", "ws://grid-hub.example:4500")
    monkeypatch.delenv("GATEWAY_AGENT_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    return grid


def _admin_context(browser, width=1280, height=900):  # noqa: F811
    """A context whose tab is already signed in, as after signing in once."""
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


def row(p, name):
    return p.locator("#token-rows tr", has_text=name)


def names(p):
    return p.eval_on_selector_all("#token-rows .tok-name", "els => els.map(e => e.textContent)")


def test_access_without_an_admin_token_explains_the_setting(grid, page):  # noqa: F811
    url, _ = grid
    seen = token_requests(page)
    page.goto(url + "#access")
    page.wait_for_selector("#access-state .empty")
    assert "Set an admin token" in page.text_content("#access-state")
    assert "GRID_ADMIN_TOKEN=" in page.text_content("#access-state")
    assert not page.is_visible("#signin-dialog") and not page.is_visible("#new-token-btn")
    assert page.is_visible("#unprotected-btn")  # a calm, persistent note that anyone can manage the grid
    page.click("#unprotected-btn")
    assert "GRID_ADMIN_TOKEN" in page.text_content("#unprotected-pop")
    page.click("[data-nav-view=overview] >> visible=true")
    page.click("[data-nav-view=access] >> visible=true")
    refresh_now(page)
    assert seen == []  # the server says no admin is configured: nothing to ask, nothing to retry


def test_access_shows_a_sign_in_panel_and_a_refusal_does_not_loop(admin_grid, page):  # noqa: F811
    url, _ = admin_grid
    seen = token_requests(page)
    page.goto(url + "#/access")
    page.wait_for_selector("#access-state [data-signin]")
    assert not page.is_visible("#signin-dialog")  # no prompt pops up by itself
    assert seen == []
    page.click("#access-state [data-signin]")
    page.wait_for_selector("#signin-dialog[open]")
    page.fill("#signin-token", "wrong")
    page.press("#signin-token", "Enter")
    page.wait_for_function("() => document.getElementById('signin-error').textContent.indexOf('rejected') !== -1")
    page.click("#signin-dialog [data-dismiss]")
    page.wait_for_selector("#access-state [data-signin]")
    refresh_now(page)
    refresh_now(page)
    assert seen == []  # a rejected token never reaches the token routes

    page.click("#access-state [data-signin]")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    page.wait_for_selector("#token-rows tr")
    assert seen[0] == ("GET", "s3cret") and set(seen) == {("GET", "s3cret")}
    assert page.locator("#token-rows tr").count() == 6
    assert "Admin" in page.text_content("#account-btn")

    page.click("#account-btn")
    assert "Signed in with admin token · this tab only" in page.text_content("#account-menu")
    page.click("#account-menu [data-menu=signout]")
    page.wait_for_selector("#access-state [data-signin]")
    assert page.evaluate("sessionStorage.length") == 0 or page.evaluate("sessionStorage.getItem('gridToken')") is None


def test_create_reveal_revoke_and_delete(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    p.goto(url + "#/access")
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
    assert p.evaluate("document.activeElement.dataset.copyLabel") == "the token"  # Copy is focused
    ((stored_hash, node_name, expires, created),) = run(
        path, "SELECT tokenHash, nodeName, expiresAt, createdAt FROM accessTokens WHERE name = 'Build farm'")
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest() and node_name == "win-build-09"
    assert (store._parse_time(expires) - store._parse_time(created)).days == 45
    usage = p.text_content(".usage")
    assert ".\\install-backend.ps1 -HubUrl 'ws://grid-hub.example:4500' -AgentToken '" + token + "' -NodeName 'win-build-09'" in usage
    assert "GATEWAY_AGENT_TOKEN=" + token + " node gateway/agent.js --hub ws://grid-hub.example:4500 --name win-build-09" in usage
    assert "won't be shown again" in p.text_content(".reveal")
    p.wait_for_selector("#token-rows tr:has-text('Build farm')")

    hub = p.locator(".reveal [data-hub-input]")  # every command follows the hub address
    hub.fill("ws://10.9.8.7:4500")
    p.wait_for_function("() => document.querySelector('.usage').textContent.indexOf('--hub ws://10.9.8.7:4500') !== -1")
    assert p.evaluate("localStorage.getItem('gridHub')") == "ws://10.9.8.7:4500"

    p.click("[data-reveal-done]")  # not copied yet: asks first
    p.wait_for_selector("#confirm-dialog[open]")
    p.click("#confirm-dialog [data-dismiss]")
    assert p.is_visible(".reveal")
    p.click(".reveal [data-copy-label='the token']")
    p.click("[data-reveal-done]")
    p.wait_for_selector(".reveal", state="detached")
    assert token not in p.content()
    assert p.evaluate("document.activeElement.id") == "new-token-btn"

    row(p, "Docker runners").locator("[data-revoke]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert p.eval_on_selector_all("#confirm-list .confirm-name", "els => els.map(e => e.textContent)") == ["runner-7f3a9c", "runner-b81e04"]
    assert "danger-fill" in p.get_attribute("#confirm-ok", "class")  # red only inside its confirm
    p.click("#confirm-ok")
    p.wait_for_selector("#token-rows tr[data-status=revoked]:has-text('Docker runners')")
    assert run(path, "SELECT revokedAt IS NOT NULL FROM accessTokens WHERE name = 'Docker runners'") == [(1,)]
    assert p.evaluate("document.activeElement !== document.body")

    row(p, "Docker runners").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    p.click("#confirm-ok")
    p.wait_for_selector("#token-rows tr:has-text('Docker runners')", state="detached")
    assert run(path, "SELECT COUNT(*) FROM nodes WHERE name LIKE 'runner-%' AND tokenId IS NOT NULL") == [(0,)]
    assert p.evaluate("document.activeElement !== document.body")


def test_client_token_usage_and_the_first_token_notice(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "UPDATE accessTokens SET revokedAt = '2026-01-01 00:00:00' WHERE kind = 'client'")
    p = admin_page
    p.goto(url + "#/access")
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
    usage = p.text_content(".usage")
    assert "GATEWAY_CLIENT_TOKEN=" + token in usage
    assert "ws://grid-hub.example:4500/connect?gateway_token=" + token in usage and "/connect/" not in usage
    assert "<" not in usage and ">" not in usage  # real addresses, no placeholders
    p.click(".reveal [data-copy-label='the token']")
    p.click("[data-reveal-done]")

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
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")

    run(path, "UPDATE accessTokens SET revokedAt = '2026-01-01 00:00:00' WHERE name = 'Example checks'")
    p.click("[data-nav-view=overview] >> visible=true")
    p.click("[data-nav-view=access] >> visible=true")  # opening the view reloads the list
    p.wait_for_selector("#token-rows tr[data-status=revoked]:has-text('Example checks')")
    row(p, "Staging checks 2025").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert "turns it away" in p.text_content("#confirm-text")
    assert p.is_hidden("#confirm-note")
    p.click("#confirm-dialog [data-dismiss]")

    run(path, "DELETE FROM accessTokens WHERE name = 'Staging checks 2025'")
    p.click("[data-nav-view=overview] >> visible=true")
    p.click("[data-nav-view=access] >> visible=true")
    p.wait_for_selector("#token-rows tr:has-text('Example checks')")
    p.wait_for_function("() => !document.querySelector('#token-rows').textContent.includes('Staging checks 2025')")
    row(p, "Example checks").locator("[data-delete-token]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    note = p.text_content("#confirm-note")
    assert "last client token on record" in note and "GATEWAY_CLIENT_TOKEN" in note and "next restart" in note
    p.click("#confirm-dialog [data-dismiss]")
    assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE kind = 'client'") == [(1,)]


def test_token_filters(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    p.goto(url + "#/access")
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


def test_an_action_that_empties_the_filter_goes_back_to_all(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    p.click("#token-status [data-status=expired]")
    assert names(p) == ["Staging checks 2025"]
    row(p, "Staging checks 2025").locator("[data-delete-token]").click()
    p.click("#confirm-ok")
    p.wait_for_function("() => document.getElementById('token-note').textContent.indexOf('showing all') !== -1")
    assert p.get_attribute("#token-status [data-status=all]", "aria-pressed") == "true"
    assert len(names(p)) == 5


def test_an_older_demo_database_shows_an_empty_state(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "DROP TABLE accessTokens")
    admin_page.goto(url + "#/access")
    admin_page.wait_for_selector("#access-state .empty")
    assert "aren't set up" in admin_page.text_content("#access-state")
    assert not admin_page.is_visible("#new-token-btn") and not admin_page.is_visible("#token-panel")


def test_token_names_are_escaped(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "UPDATE accessTokens SET name = '<img src=x onerror=\"window.__xss=1\">', nodeName = '\"><b>' WHERE id = 1")
    admin_page.goto(url + "#/access")
    admin_page.wait_for_selector("#token-rows tr")
    row(admin_page, "<img").locator("[data-revoke]").click()
    admin_page.wait_for_timeout(300)
    assert admin_page.evaluate("window.__xss") is None
    assert admin_page.evaluate("document.querySelectorAll('img, #token-rows b').length") == 0
    assert "<img" in admin_page.text_content("#confirm-title")


def test_add_a_host_makes_a_bound_token_and_waits_for_the_node(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    assert not p.is_visible("#add-host")  # one call to action in the header, not a second form above the list
    p.click("#add-host-btn")
    p.wait_for_selector("#add-host #ah-node")
    assert p.get_attribute("#add-host-btn", "aria-expanded") == "true"
    assert p.get_attribute("#ah-node", "placeholder").startswith("e.g. ")
    p.click("#add-host .choice-card:has-text('Linux')")
    p.fill("#ah-node", "Bad Name")
    p.click("#ah-create")
    assert "node name" in p.text_content("#ah-error")
    p.fill("#ah-node", "win-build-02")
    p.click("#ah-create")
    p.wait_for_selector(".reveal")
    token = p.text_content("#reveal-token")
    commands = p.eval_on_selector_all(".reveal [data-usage]", "els => els.map(e => e.textContent)")
    assert commands == ["GATEWAY_AGENT_TOKEN=" + token + " node gateway/agent.js --hub ws://grid-hub.example:4500 --name win-build-02"]
    assert run(path, "SELECT name, kind, nodeName FROM accessTokens WHERE nodeName = 'win-build-02'") == [("win-build-02 agent", "agent", "win-build-02")]
    assert "Waiting for win-build-02 to register" in p.text_content("#wait-line")

    run(path, "INSERT INTO nodes (name, online, slots, lastSeen) VALUES ('win-build-02', 1, 0, datetime('now'))")
    refresh_now(p)
    p.wait_for_selector("#wait-line.done a[data-inspect=node]")
    assert "win-build-02 registered" in p.text_content("#wait-line")

    # The same host again: a warning first (tokens would pile up), then a second token only on purpose.
    p.click("[data-reveal-done]")
    p.click("#confirm-ok")  # hide it without copying: it was only a test
    p.wait_for_selector(".reveal", state="detached")
    p.wait_for_function("() => document.querySelectorAll('#token-rows tr').length === 7")
    p.fill("#ah-node", "WIN-BUILD-02")
    assert "Registers as win-build-02" in p.text_content("#ah-hint")
    p.click("#ah-create")
    p.wait_for_selector("#ah-error.notice")
    assert "already has an active agent token" in p.text_content("#ah-error")
    assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE nodeName = 'win-build-02'") == [(1,)]
    assert "Create another token" in p.text_content("#ah-create")
    p.click("#ah-create")
    p.wait_for_selector(".reveal")
    assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE nodeName = 'win-build-02'") == [(2,)]


def test_access_fits_a_phone(admin_grid, browser):  # noqa: F811
    url, _ = admin_grid
    ctx = _admin_context(browser, 375, 800)
    p = ctx.new_page()
    try:
        p.goto(url + "#/access")
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
        assert top >= p.evaluate("document.querySelector('.topbar').getBoundingClientRect().bottom")  # not under the bar
    finally:
        ctx.close()
