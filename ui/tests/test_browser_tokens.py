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
    assert page.locator("#token-rows tr").count() == 7
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
    run(path, "DELETE FROM accessTokens WHERE name = 'Grid monitor'")
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
    assert sorted(names(p)) == ["Example checks", "Grid monitor", "Staging checks 2025"]
    p.click("#token-status [data-status=active]")
    assert sorted(names(p)) == ["Example checks", "Grid monitor"]
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
    assert len(names(p)) == 7
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
    assert len(names(p)) == 6


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
    p.wait_for_function("() => document.querySelectorAll('#token-rows tr').length === 8")
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


# ---- token scopes -------------------------------------------------------------------------------

NO_SCOPED_ROUTE = "The Grid UI that answered is from before token scopes, so it made no token. Try again when the update has finished."
SCOPE_GONE = "May do nothing: its scope was lost. Revoke it and make another."
USAGES = "els => els.map(e => e.textContent)"


def token_posts(p):
    """Every request that could make a token, as the page sends it: (path below /api/, body)."""
    seen = []
    p.on("request", lambda r: seen.append((r.url.split("/api/", 1)[1], r.post_data_json))
         if r.method == "POST" and "/api/tokens" in r.url else None)
    return seen


def open_dialog(p, url):
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    p.click("#new-token-btn")
    p.wait_for_selector("#new-token-dialog[open]")


def limited_form(p, name, *scope):
    """Fill the open dialog for a client token called name, limited to these scope ids."""
    p.check("input[name=kind][value=client]")
    p.fill("#nt-name", name)
    p.check("input[name=limit][value=scope]")
    for scope_id in scope:
        p.check("#nt-scope-list input[value='{}']".format(scope_id))


def ticked(p):
    return p.eval_on_selector_all("#nt-scope-list input[name=scope]:checked", "els => els.map(e => e.value)")


def test_making_a_limited_client_token_from_the_dialog(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    posts = token_posts(p)
    open_dialog(p, url)
    limited_form(p, "Uptime probe", "grid.view")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert posts == [("tokens/scoped", {"name": "Uptime probe", "expiresInDays": 365, "scope": ["grid.view"]})]

    token = p.text_content("#reveal-token")
    assert re.fullmatch(r"pwgc_[A-Za-z0-9_-]{43}", token)
    assert run(path, "SELECT tokenHash, kind, scope FROM accessTokens WHERE name = 'Uptime probe'") == [
        (store.scoped_token_hash(token), "client", "grid.view")]
    p.wait_for_selector("#token-rows tr:has-text('Uptime probe')")
    assert row(p, "Uptime probe").locator(".tok-scope").text_content() == "May only see the grid"
    # The hub turns this token away, so the page gives the bearer line and no hub address.
    assert p.text_content(".reveal-title p").startswith("Client token · Uptime probe · may only see the grid · expires ")
    assert p.eval_on_selector_all(".reveal [data-usage]", USAGES) == ["Authorization: Bearer " + token]
    usage = p.text_content(".usage")
    assert "its scope leaves out opening browser sessions" in usage
    assert "/connect" not in usage and "gateway_token" not in usage and "GATEWAY_CLIENT_TOKEN" not in usage
    assert p.get_attribute(".reveal [data-fk=reveal-use-0]", "data-copy") == "Authorization: Bearer " + token
    assert "ws://" not in usage and "grid-hub.example" not in p.inner_text(".reveal")
    assert p.locator(".reveal [data-hub-input]").count() == 0  # nor the field for one: no command here follows it


def test_a_limited_token_that_may_open_sessions_gets_the_hub_address(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    posts = token_posts(p)
    open_dialog(p, url)
    limited_form(p, "Nightly checks", "mfa.use", "sessions.open")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert posts == [("tokens/scoped", {"name": "Nightly checks", "expiresInDays": 365, "scope": ["sessions.open", "mfa.use"]})]
    token = p.text_content("#reveal-token")
    usage = p.text_content(".usage")
    assert "GATEWAY_CLIENT_TOKEN=" + token in usage
    assert "ws://grid-hub.example:4500/connect?gateway_token=" + token in usage
    assert p.input_value(".reveal [data-hub-input]") == "ws://grid-hub.example:4500"
    p.wait_for_selector("#token-rows tr:has-text('Nightly checks')")
    assert row(p, "Nightly checks").locator(".tok-scope").text_content() == "May only open browser sessions and get MFA codes"


def test_the_scope_choice_is_for_client_tokens_and_starts_at_everything(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    posts = token_posts(p)
    open_dialog(p, url)
    assert p.is_checked("input[name=kind][value=agent]") and p.is_hidden("#nt-scope-field")  # an agent token has no scope
    p.check("input[name=kind][value=client]")
    assert p.is_visible("#nt-scope-field") and p.is_checked("input[name=limit][value='']") and p.is_hidden("#nt-scope-list")
    assert p.text_content("#nt-scope-field legend") == "What it may do"
    assert p.get_attribute("#nt-scope-field", "aria-describedby") == "nt-scope-hint"
    assert p.text_content("#nt-scope-hint") == "It can't be changed later: to change what a token may do, make another and revoke this one."
    p.check("input[name=limit][value=scope]")
    assert p.is_visible("#nt-scope-list") and ticked(p) == []
    assert p.eval_on_selector_all("#nt-scope-list label", "els => els.map(e => e.textContent)") == [
        "Open browser sessions", "See the grid", "Use MFA secrets", "Use saved sign-ins", "Add saved sign-ins"]
    p.check("#nt-scope-list input[value='mfa.use']")
    p.check("#nt-scope-list input[value='signins.add']")
    p.check("input[name=kind][value=agent]")
    assert p.is_hidden("#nt-scope-field") and p.is_hidden("#nt-scope-list")
    p.click("#new-token-dialog [data-dismiss]")

    # Opened again, it is as it was the first time: Everything, nothing ticked, nothing locked.
    p.click("#new-token-btn")
    p.wait_for_selector("#new-token-dialog[open]")
    assert p.is_checked("input[name=kind][value=agent]") and p.is_hidden("#nt-scope-field")
    p.check("input[name=kind][value=client]")
    assert p.is_checked("input[name=limit][value='']") and p.is_hidden("#nt-scope-list") and ticked(p) == []
    assert not p.evaluate("document.querySelector('#nt-scope-list input[value=\"signins.use\"]').disabled")
    p.fill("#nt-name", "Everything checks")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert posts == [("tokens", {"name": "Everything checks", "kind": "client", "nodeName": None, "expiresInDays": 365})]
    p.wait_for_selector("#token-rows tr:has-text('Everything checks')")
    assert row(p, "Everything checks").locator(".tok-scope").count() == 0
    assert p.text_content(".reveal-title p").startswith("Client token · Everything checks · expires ")
    p.click(".reveal [data-copy-label='the token']")
    p.click("[data-reveal-done]")
    p.wait_for_selector(".reveal", state="detached")

    # An agent token is asked for as before, whatever was picked while Client was chosen.
    del posts[:]
    p.click("#new-token-btn")
    limited_form(p, "Spare hosts", "grid.view")
    p.check("input[name=kind][value=agent]")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert posts == [("tokens", {"name": "Spare hosts", "kind": "agent", "nodeName": None, "expiresInDays": 365})]
    assert re.fullmatch(r"pwga_[A-Za-z0-9_-]{43}", p.text_content("#reveal-token"))


def test_ticking_add_saved_sign_ins_ticks_and_locks_use_saved_sign_ins(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    posts = token_posts(p)
    open_dialog(p, url)
    limited_form(p, "Sign-in maker")
    use, add = "#nt-scope-list input[value='signins.use']", "#nt-scope-list input[value='signins.add']"
    p.focus(add)
    p.keyboard.press("Space")  # by keyboard, as by mouse
    assert p.is_checked(add) and p.is_checked(use) and p.is_disabled(use)
    p.keyboard.press("Space")
    assert not p.is_checked(add) and p.is_checked(use) and p.is_enabled(use)  # unlocked, and left ticked
    p.uncheck(use)
    assert ticked(p) == []
    p.check(add)
    assert p.is_checked(use) and p.is_disabled(use)
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert posts == [("tokens/scoped", {"name": "Sign-in maker", "expiresInDays": 365, "scope": ["signins.use", "signins.add"]})]
    assert run(path, "SELECT scope FROM accessTokens WHERE name = 'Sign-in maker'") == [("signins.add signins.use",)]
    p.wait_for_selector("#token-rows tr:has-text('Sign-in maker')")
    assert row(p, "Sign-in maker").locator(".tok-scope").text_content() == "May only reuse saved sign-ins and add saved sign-ins"


def test_only_what_you_pick_with_nothing_ticked_sends_nothing(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    posts = token_posts(p)
    open_dialog(p, url)
    limited_form(p, "Nothing at all")
    p.click("#nt-submit")
    assert p.text_content("#nt-error") == "Tick at least one thing the token may do, or choose Everything."
    assert p.is_visible("#nt-error") and p.is_visible("#new-token-dialog")
    assert p.evaluate("document.activeElement.id") == "nt-scope-sessions"
    assert p.get_attribute("#nt-scope-sessions", "aria-invalid") == "true"
    p.wait_for_timeout(300)
    assert posts == [] and p.locator(".reveal").count() == 0
    assert run(path, "SELECT COUNT(*) FROM accessTokens") == [(7,)]

    # Ticking one thing is enough: the token is made, and the box is no longer marked.
    p.keyboard.press("Space")
    p.click("#nt-submit")
    p.wait_for_selector(".reveal")
    assert p.get_attribute("#nt-scope-sessions", "aria-invalid") is None
    assert posts == [("tokens/scoped", {"name": "Nothing at all", "expiresInDays": 365, "scope": ["sessions.open"]})]


def test_a_token_without_a_scope_shows_no_limit_line(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    assert len(names(p)) == 7
    for name in ("Example checks", "Staging checks 2025", "Docker runners", "WIN-BUILD-01 agent"):
        assert row(p, name).locator(".tok-scope").count() == 0, name
        assert row(p, name).locator(".cell-token > *").count() == 2, name  # its name and its first characters, as before
    assert p.locator("#token-rows .tok-scope").count() == 1  # the demo's one limited token


def test_the_demo_grid_monitor_shows_its_limit(admin_grid, admin_page):
    url, _ = admin_grid
    p = admin_page
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    cell = row(p, "Grid monitor").locator(".cell-token")
    assert cell.locator(".tok-scope").text_content() == "May only see the grid"
    assert cell.locator(".tok-scope").evaluate("el => getComputedStyle(el).display") == "block"  # on a line of its own
    assert cell.evaluate("el => [...el.children].map((c) => c.className.split(' ')[0])") == ["tok-name", "tok-prefix", "tok-scope"]
    assert row(p, "Grid monitor").locator(".cell-kind").text_content() == "Client"


def test_a_grid_ui_from_before_scopes_makes_no_token_and_the_dialog_says_so(admin_grid, admin_page):
    url, path = admin_grid
    p = admin_page
    posts = token_posts(p)
    # What a Grid UI from before scopes answers (404), what some proxies answer for it (405), and a 404 with JSON:
    # there is no such route, so nothing was made.
    answers = [dict(status=404, content_type="text/html; charset=utf-8", body="<!doctype html><title>404 Not Found</title>"),
               dict(status=405, content_type="text/html", body="<html><body>405 Not Allowed</body></html>"),
               dict(status=404, json={"error": "Not found, says a proxy."})]
    p.route("**/api/tokens/scoped", lambda r: r.fulfill(**answers.pop(0)))
    open_dialog(p, url)
    limited_form(p, "Uptime probe", "grid.view")
    body = ("tokens/scoped", {"name": "Uptime probe", "expiresInDays": 365, "scope": ["grid.view"]})
    for n in (1, 2, 3):
        p.click("#nt-submit")  # the error line is emptied and the button disabled until the answer is in
        p.wait_for_function("text => document.getElementById('nt-error').textContent === text && "
                            "!document.getElementById('nt-submit').disabled", arg=NO_SCOPED_ROUTE)
        assert posts == [body] * n  # nothing is asked of api/tokens, which would make a token with no limit
        assert p.is_visible("#new-token-dialog") and p.is_visible("#nt-error") and p.locator(".reveal").count() == 0
        assert p.input_value("#nt-name") == "Uptime probe" and ticked(p) == ["grid.view"]  # as it was filled in
        assert p.is_checked("input[name=limit][value=scope]")
    assert answers == []
    p.click("#new-token-dialog [data-dismiss]")
    refresh_now(p)
    p.wait_for_timeout(300)
    assert len(names(p)) == 7 and "Uptime probe" not in names(p) and p.locator(".reveal").count() == 0
    assert run(path, "SELECT COUNT(*) FROM accessTokens") == [(7,)]
    assert posts == [body] * 3

    # Any other refusal of that route is said as the server says it.
    p.unroute("**/api/tokens/scoped")
    p.route("**/api/tokens/scoped", lambda r: r.fulfill(status=503, json={"error": "The scope test says no."}))
    p.click("#new-token-btn")
    limited_form(p, "Uptime probe", "grid.view")
    p.click("#nt-submit")
    p.wait_for_function("() => document.getElementById('nt-error').textContent === 'The scope test says no.'")
    assert run(path, "SELECT COUNT(*) FROM accessTokens") == [(7,)]


def test_a_token_whose_scope_was_lost_says_so_in_the_list(admin_grid, admin_page):
    url, path = admin_grid
    run(path, "UPDATE accessTokens SET scope = NULL WHERE name = 'Grid monitor'")
    p = admin_page
    p.goto(url + "#/access")
    p.wait_for_selector("#token-rows tr")
    assert row(p, "Grid monitor").locator(".tok-scope").text_content() == SCOPE_GONE
    assert p.locator("#token-rows .tok-scope").count() == 1  # never listed as a token with no limit, and no other as lost

    # A stored scope that is empty, or names nothing this version knows, allows nothing: that is not "lost".
    for stored in ("tomorrow.only", ""):
        run(path, "UPDATE accessTokens SET scope = ? WHERE name = 'Grid monitor'", (stored,))
        p.reload()
        p.wait_for_selector("#token-rows tr")
        assert row(p, "Grid monitor").locator(".tok-scope").text_content() == "May do nothing", stored
