"""The saved sign-ins on the MFA & sign-ins page in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import re

import pytest

from ui import store

pytest.importorskip("playwright.sync_api")
pytest.importorskip("cryptography")

from ui.tests.test_browser import browser, grid, page, run  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_tokens import _admin_context, admin_grid  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_mfa import add_secret, mfa_grid, mfa_page, set_hidden  # noqa: E402,F401  (fixtures)

COOKIE = "estsauth-cookie-value-for-the-browser-tests"
LOCAL = "local-storage-value-for-the-browser-tests"
CHECKS = "client token 'nightly'"


def state(value=COOKIE):
    """A storage state as Playwright saves one after a Microsoft sign-in."""
    return {"cookies": [
        {"name": "ESTSAUTH", "value": value, "domain": ".login.microsoftonline.com", "path": "/", "expires": -1,
         "httpOnly": True, "secure": True, "sameSite": "None"},
        {"name": "FedAuth", "value": value + "-app", "domain": "contoso.sharepoint.com", "path": "/",
         "expires": 1893456000.5, "httpOnly": True, "secure": True, "sameSite": "Lax"}],
        "origins": [{"origin": "https://contoso.sharepoint.com", "localStorage": [{"name": "msal.token", "value": LOCAL}]}]}


def sign_in(name, who=CHECKS):
    """A check's fresh sign-in, saved. Returns the version."""
    lease = store.checkout_signin(who, name)
    assert lease["status"] == "sign-in"
    return store.save_signin(who, name, state(), lease_token=lease["leaseToken"])["version"]


def signin_requests(p):
    seen = []
    p.on("request", lambda r: seen.append((r.method, r.url.split("/api/")[1])) if "/api/signins" in r.url else None)
    return seen


def row(p, name):
    return p.locator("#signin-rows tr", has_text=name)


def cell(p, name, which):
    return row(p, name).locator(".cell-" + which).inner_text()


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


def test_nothing_saved_yet_explains_and_offers_the_snippet(mfa_grid, mfa_page):
    url, _ = mfa_grid
    p = mfa_page
    seen = signin_requests(p)
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-state .signin-intro")
    text = squash(p.text_content("#signins"))
    assert "No saved sign-ins yet" in text and "The first run signs in" in text and "When the site asks again" in text
    assert 'signin.reuse(browser, "intranet"' in text  # no secret or sign-in to name it after
    assert not p.is_visible("#signin-panel") and not p.is_visible("#signin-how")
    p.click("#signin-state [data-copy]")
    copied = p.evaluate("navigator.clipboard.readText()")
    assert "from src import mfa, signin" in copied and "sign_in=sign_in, signed_in=signed_in) as s:" in copied
    compile(copied, "check.py", "exec")  # pastes as Python
    assert seen and set(seen) == {("GET", "signins")}
    assert p.title() == "MFA & sign-ins · Playwright Grid"
    assert p.text_content(".rail [data-nav-view=mfa] .rail-label") == "MFA & sign-ins"
    assert p.errors == []


def test_the_snippet_names_a_secret_when_there_is_one(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret("contoso-monitor")
    mfa_page.goto(url + "#/mfa")
    mfa_page.wait_for_selector("#signin-state .signin-intro")
    assert 'mfa.code("contoso-monitor")' in mfa_page.text_content("#signin-state .snippet")


def test_the_snippet_gets_codes_by_a_secret_not_by_the_sign_ins_name(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret("poc-idp")
    sign_in("poc-idp-session")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    p.click("#signin-how summary")
    text = p.text_content("#signin-how .snippet")
    assert 'signin.reuse(browser, "poc-idp-session"' in text and 'mfa.code("poc-idp")' in text, text
    # A secret named like the sign-in is the one, though another sorts first.
    add_secret("poc-idp-session")
    p.wait_for_function("""() => document.querySelector("#signin-how .snippet").textContent.includes('mfa.code("poc-idp-session")')""",
                        timeout=8000)
    assert 'signin.reuse(browser, "poc-idp-session"' in p.text_content("#signin-how .snippet")
    assert p.errors == []


def test_saved_sign_ins_show_what_and_how_but_never_a_cookie(mfa_grid, mfa_page):
    url, _ = mfa_grid
    v = sign_in("contoso-monitor")
    got = store.checkout_signin("GATEWAY_CLIENT_TOKEN", "contoso-monitor")
    assert got["status"] == "saved"
    v = store.save_signin("GATEWAY_CLIENT_TOKEN", "contoso-monitor", state(), base_version=v,  # cookies rolled on
                          signin_id=got["id"])["version"]
    lease = store.checkout_signin(CHECKS, "contoso-monitor", rejected_version=v)  # the site asked again
    store.save_signin(CHECKS, "contoso-monitor", state(), lease_token=lease["leaseToken"])
    p = mfa_page
    bodies = []
    p.on("response", lambda r: bodies.append(r.text()) if "/api/signins" in r.url else None)
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    assert p.text_content("#signin-rows .signin-name") == "contoso-monitor"
    assert squash(row(p, "contoso-monitor").locator(".signin-domains").text_content()) == "contoso.sharepoint.com, login.microsoftonline.com"
    saved = squash(cell(p, "contoso-monitor", "saved"))
    assert re.fullmatch(r"just now by client token 'nightly' 2 cookies · 1 origin", saved), saved
    assert re.fullmatch(r"in (29|30)d", cell(p, "contoso-monitor", "expires").strip())
    runs = squash(cell(p, "contoso-monitor", "runs"))
    assert runs == "Reused 1 time Last just now by GATEWAY_CLIENT_TOKEN Signed in 2 times · refreshed 1", runs
    assert squash(cell(p, "contoso-monitor", "asked")) == "1 time Last just now"
    assert p.text_content("#signin-sub") == "1 sign-in · 1 saved"
    assert not p.is_visible("#signin-state .signin-intro")
    p.click("#signin-how summary")
    assert 'signin.reuse(browser, "contoso-monitor"' in p.text_content("#signin-how .snippet")
    assert bodies and not any(COOKIE in b or LOCAL in b for b in bodies)
    assert COOKIE not in p.content() and LOCAL not in p.content()
    assert p.errors == []


def test_a_check_signing_in_shows_and_counts_down(mfa_grid, mfa_page):
    url, _ = mfa_grid
    lease = store.checkout_signin(CHECKS, "erp-nightly", lease_s=120)
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows .tag.live")
    assert "Signing in now" in cell(p, "erp-nightly", "saved") and CHECKS in cell(p, "erp-nightly", "saved")
    assert p.text_content("#signin-sub") == "1 sign-in · 0 saved · 1 signing in now"

    def left():
        m = re.fullmatch(r"(?:(\d+)m)? ?(?:(\d+)s)? left", p.text_content("[data-lease-left]"))  # "1m 58s", "2m", "42s"
        assert m and any(m.groups()), p.text_content("[data-lease-left]")
        return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    first = left()
    assert 110 <= first <= 120
    p.wait_for_timeout(2200)
    assert left() < first  # this tab's clock, between polls
    assert lease["leaseToken"] not in p.content()

    # The check saves what it signed in with: the next poll shows the state.
    store.save_signin(CHECKS, "erp-nightly", state(), lease_token=lease["leaseToken"])
    p.wait_for_selector("#signin-rows .tag.live", state="detached", timeout=8000)
    assert re.match(r"just now by client token 'nightly'", squash(cell(p, "erp-nightly", "saved")))
    assert p.text_content("#signin-sub") == "1 sign-in · 1 saved"
    assert p.errors == []


def test_why_nothing_is_saved(mfa_grid, mfa_page):
    url, path = mfa_grid
    v = sign_in("asked-site")
    lease = store.checkout_signin(CHECKS, "asked-site", rejected_version=v)
    store.release_signin("asked-site", lease["leaseToken"])  # its sign-in failed
    sign_in("lapsed-site")
    run(path, "UPDATE savedSignins SET expiresAt = datetime('now', '-3 days') WHERE name = 'lapsed-site'")
    lease = store.checkout_signin(CHECKS, "never-site")  # a checkout drops every expired state
    store.release_signin("never-site", lease["leaseToken"])
    sign_in("expired-site")
    run(path, "UPDATE savedSignins SET expiresAt = datetime('now', '-2 days') WHERE name = 'expired-site'")  # not dropped yet
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr >> nth=3")
    assert squash(cell(p, "asked-site", "saved")) == "Not saved The site asked to sign in again"
    assert squash(cell(p, "asked-site", "asked")) == "1 time Last just now"
    assert cell(p, "asked-site", "expires").strip() == "–"
    assert squash(cell(p, "lapsed-site", "saved")) == "Not saved Expired"
    assert squash(cell(p, "expired-site", "saved")) == "Expired Saved just now by client token 'nightly'"
    assert cell(p, "expired-site", "expires").strip() == "2d ago"
    assert squash(cell(p, "never-site", "saved")) == "Not saved yet The next run signs in"
    assert row(p, "never-site").locator(".signin-domains").count() == 0 and "No domains yet" in cell(p, "never-site", "signin")
    for name in ("asked-site", "lapsed-site", "never-site"):
        assert row(p, name).locator("[data-forget-signin]").is_disabled()
    assert not row(p, "expired-site").locator("[data-forget-signin]").is_disabled()
    assert p.text_content("#signin-sub") == "4 sign-ins · 0 saved"


def test_forget_makes_the_next_run_sign_in_again(mfa_grid, mfa_page):
    url, path = mfa_grid
    sign_in("contoso-monitor")
    sign_in("intranet-sso", "GATEWAY_CLIENT_TOKEN")
    p = mfa_page
    p.goto(url + "#/mfa")
    forget = row(p, "contoso-monitor").locator("[data-forget-signin]")
    forget.click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert "contoso-monitor" in p.text_content("#confirm-title")
    assert "next run signs in again" in p.text_content("#confirm-text")
    assert p.text_content("#confirm-dialog .confirm-facts dd") == "contoso.sharepoint.com, login.microsoftonline.com"
    assert "danger-fill" not in p.get_attribute("#confirm-ok", "class")  # the next run just signs in
    p.click("#confirm-dialog [data-dismiss]")
    assert p.evaluate("document.activeElement.dataset.forgetSignin") is not None
    assert run(path, "SELECT stateCipher IS NOT NULL, version FROM savedSignins WHERE name = 'contoso-monitor'") == [(1, 1)]

    forget.click()
    p.click("#confirm-ok")
    p.wait_for_function("() => document.getElementById('toast').textContent.includes('The next run signs in again')")
    p.wait_for_function("() => document.querySelector('#signin-rows [data-forget-signin]').disabled")
    assert squash(cell(p, "contoso-monitor", "saved")) == "Not saved Forgotten"
    assert run(path, "SELECT stateCipher IS NOT NULL, version FROM savedSignins WHERE name = 'contoso-monitor'") == [(0, 2)]
    assert p.evaluate("document.activeElement.dataset.signinRow") is not None  # its Forget can't take focus now
    assert "just now by GATEWAY_CLIENT_TOKEN" in squash(cell(p, "intranet-sso", "saved"))  # the other one stays

    # The next run is told to sign in; forgetting while it does takes its turn away.
    lease = store.checkout_signin(CHECKS, "contoso-monitor")
    assert lease["status"] == "sign-in"
    p.wait_for_selector("#signin-rows .tag.live", timeout=8000)
    forget.click()
    assert "A check is signing in to it now" in p.text_content("#confirm-text")
    assert CHECKS in p.text_content("#confirm-dialog .confirm-facts")
    p.click("#confirm-ok")
    p.wait_for_selector("#signin-rows .tag.live", state="detached")
    with pytest.raises(store.LeaseLost):
        store.save_signin(CHECKS, "contoso-monitor", state(), lease_token=lease["leaseToken"])
    assert p.errors == []


def test_forgetting_an_expired_state_says_it_was_forgotten(mfa_grid, mfa_page):
    url, path = mfa_grid
    sign_in("lapsing-site")
    run(path, "UPDATE savedSignins SET expiresAt = datetime('now', '-1 day')")  # past it, not dropped yet
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    assert squash(cell(p, "lapsing-site", "saved")).startswith("Expired")
    row(p, "lapsing-site").locator("[data-forget-signin]").click()
    p.click("#confirm-ok")
    p.wait_for_function("() => document.querySelector('#signin-rows [data-forget-signin]').disabled")
    assert squash(cell(p, "lapsing-site", "saved")) == "Not saved Forgotten"
    assert run(path, "SELECT expiresAt FROM savedSignins") == [(None,)]
    assert p.errors == []


def test_delete_a_sign_in(mfa_grid, mfa_page):
    url, path = mfa_grid
    sign_in("a-site")
    sign_in("b-site")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr >> nth=1")
    row(p, "a-site").locator("[data-delete-signin]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert "a-site" in p.text_content("#confirm-title") and "adds it back" in p.text_content("#confirm-text")
    assert "danger-fill" in p.get_attribute("#confirm-ok", "class")
    p.click("#confirm-ok")
    p.wait_for_selector("#signin-rows tr:has-text('a-site')", state="detached")
    assert run(path, "SELECT name FROM savedSignins") == [("b-site",)]
    assert p.evaluate("document.activeElement.dataset.forgetSignin") is not None  # the row that took its place
    assert p.evaluate("!!document.activeElement.closest('#signin-rows tr')")

    row(p, "b-site").locator("[data-delete-signin]").click()
    p.click("#confirm-ok")
    p.wait_for_selector("#signin-state .signin-intro")
    assert not p.is_visible("#signin-panel")
    p.wait_for_function("() => document.activeElement.id === 'signin-title'")
    assert p.errors == []


def test_a_list_that_fails_says_so_without_hiding_the_secrets(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret()
    sign_in("contoso-monitor")
    p = mfa_page
    p.route("**/api/signins", lambda r: r.fulfill(status=503, json={"error": "The signins test says no."}))
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-state .empty")
    assert "Can't load the saved sign-ins" in p.text_content("#signin-state") and "The signins test says no." in p.text_content("#signin-state")
    assert p.is_visible("#mfa-rows tr") and not p.is_visible("#mfa-banner")
    p.unroute("**/api/signins")
    p.click("#signin-state [data-reload-mfa]")
    p.wait_for_selector("#signin-rows tr")

    # A list on screen stays, under a banner, while the next ones fail.
    p.route("**/api/signins", lambda r: r.fulfill(status=503, json={"error": "Down for the test."}))
    p.wait_for_selector("#mfa-banner:not([hidden])", timeout=8000)
    assert "Can't refresh the saved sign-ins" in p.text_content("#mfa-banner") and "Down for the test." in p.text_content("#mfa-banner")
    assert p.is_visible("#signin-rows tr")


def test_the_list_polls_only_while_it_is_looked_at(mfa_grid, mfa_page):
    url, _ = mfa_grid
    sign_in("contoso-monitor")
    p = mfa_page
    seen = signin_requests(p)
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    p.wait_for_timeout(500)
    before = len(seen)
    p.click("#pause-btn")
    p.wait_for_timeout(5600)
    assert len(seen) == before  # paused
    p.click("#pause-btn")
    p.wait_for_timeout(5600)
    assert len(seen) > before
    set_hidden(p, True)
    before = len(seen)
    p.wait_for_timeout(5600)
    assert len(seen) == before  # another tab is in front
    set_hidden(p, False)


def test_a_lease_that_runs_out_while_paused_waits_for_the_resume(mfa_grid, mfa_page):
    url, path = mfa_grid
    store.checkout_signin(CHECKS, "erp-nightly")
    run(path, "UPDATE savedSignins SET leaseUntil = datetime('now', '+4 seconds')")
    p = mfa_page
    seen = signin_requests(p)
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows .tag.live")
    p.click("#pause-btn")
    before = len(seen)
    p.wait_for_function("() => document.querySelector('[data-lease-left]').textContent === 'running out'", timeout=8000)
    p.wait_for_timeout(3500)
    assert len(seen) == before and p.is_visible("#signin-rows .tag.live")  # paused: the list stays as it was
    p.click("#pause-btn")
    p.wait_for_selector("#signin-rows .tag.live", state="detached", timeout=8000)
    assert len(seen) > before
    assert p.errors == []


def test_lists_on_their_way_at_sign_out_stay_out(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret("contoso-monitor")
    sign_in("contoso-monitor")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    held = []
    p.route("**/api/signins", lambda route: held.append(route))  # the next poll's sign-ins, held on their way
    for _ in range(80):
        if held:
            break
        p.wait_for_timeout(100)
    assert held, "no poll in 8 s"
    p.click("#account-btn")
    p.click("#account-menu [data-menu=signout]")
    p.wait_for_selector("#mfa-state [data-signin]")
    held[0].continue_()  # answered as asked, with the token it was sent with
    p.wait_for_timeout(500)
    assert p.is_visible("#mfa-state [data-signin]")
    assert not p.is_visible("#signin-panel") and not p.is_visible("#mfa-panel")
    p.unroute("**/api/signins")
    p.click("[data-nav-view=overview] >> visible=true")
    p.keyboard.press("Control+K")
    p.keyboard.type("contoso")
    p.wait_for_timeout(300)
    assert p.locator(".pal-opt:has-text('contoso-monitor')").count() == 0
    p.keyboard.press("Escape")


def test_the_palette_finds_them_until_sign_out(mfa_grid, mfa_page):
    url, _ = mfa_grid
    sign_in("contoso-monitor")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    sid = row(p, "contoso-monitor").get_attribute("data-signin-row")
    p.click("[data-nav-view=overview] >> visible=true")
    p.wait_for_selector("#view-overview h1")
    p.keyboard.press("Control+K")
    p.keyboard.type("contoso")
    p.wait_for_selector(".pal-opt:has-text('contoso-monitor')")
    assert "saved" in p.text_content(".pal-opt:has-text('contoso-monitor')")
    p.keyboard.press("Enter")
    p.wait_for_function("() => location.hash === '#/mfa'")
    p.wait_for_function("id => document.activeElement && document.activeElement.dataset.signinRow === id", arg=sid)

    p.click("[data-nav-view=overview] >> visible=true")
    p.keyboard.press("Control+K")
    p.keyboard.type("Saved sign-ins")
    p.keyboard.press("Enter")
    p.wait_for_function("() => document.activeElement && document.activeElement.id === 'signin-title'")

    p.click("#account-btn")
    p.click("#account-menu [data-menu=signout]")
    p.keyboard.press("Control+K")
    p.keyboard.type("contoso")
    p.wait_for_timeout(300)
    assert p.locator(".pal-opt:has-text('contoso-monitor')").count() == 0
    p.keyboard.press("Escape")


def test_an_unset_secrets_key_keeps_the_list_and_says_so(admin_grid, mfa_page, monkeypatch):  # noqa: F811
    url, path = admin_grid
    monkeypatch.setenv("GRID_SECRETS_KEY", "k" * 32)
    sign_in("contoso-monitor")
    monkeypatch.delenv("GRID_SECRETS_KEY")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#mfa-key .empty")
    p.wait_for_selector("#signin-rows tr")
    text = squash(p.text_content("#mfa-key"))
    assert "Set a secrets key to store MFA secrets and saved sign-ins" in text
    assert "checks can't use the saved sign-ins below" in text
    # Forgetting needs no key.
    row(p, "contoso-monitor").locator("[data-forget-signin]").click()
    p.click("#confirm-ok")
    p.wait_for_function("() => document.querySelector('#signin-rows [data-forget-signin]').disabled")
    assert run(path, "SELECT stateCipher IS NULL FROM savedSignins") == [(1,)]


def test_a_database_without_the_table_says_so(mfa_grid, mfa_page):
    url, path = mfa_grid
    run(path, "DROP TABLE savedSignins")
    mfa_page.goto(url + "#/mfa")
    mfa_page.wait_for_selector("#signin-state .empty")
    assert "aren't set up in this database yet" in mfa_page.text_content("#signin-state")
    assert mfa_page.is_visible("#mfa-empty [data-add-mfa]")  # the secrets are fine
    assert mfa_page.errors == []


def test_server_strings_are_escaped(mfa_grid, mfa_page):
    url, path = mfa_grid
    run(path, """INSERT INTO savedSignins (name, version, stateCipher, domains, cookieCount, originCount, savedAt, savedBy, expiresAt,
                 useCount, lastUsedAt, lastUsedBy, rejectedCount, lastRejectedAt, leaseHash, leaseUntil, leaseHolder, createdAt)
                 VALUES (?, 1, 'not a state', ?, 1, 0, datetime('now'), ?, datetime('now', '+1 day'), 1, datetime('now'), ?, 0, NULL,
                         'hash', datetime('now', '+2 minutes'), ?, datetime('now'))""",
        ('<img src=x onerror="window.__xss=1">', '"><b>bold</b>,<svg onload="window.__xss=2">',
         "<script>window.__xss=3</script>", '<img src=y onerror="window.__xss=4">', '"><svg onload="window.__xss=5">'))
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#signin-rows tr")
    assert "<img src=x" in p.text_content("#signin-rows .signin-name")
    assert '"><b>bold</b>' in p.text_content("#signin-rows .signin-domains")
    assert '"><svg onload' in p.text_content("#signin-rows .cell-saved")  # the lease holder
    p.locator("#signin-rows [data-delete-signin]").click()
    p.wait_for_timeout(300)
    assert "<img" in p.text_content("#confirm-title")
    p.click("#confirm-dialog [data-dismiss]")
    p.keyboard.press("Control+K")
    p.keyboard.type("img")
    p.wait_for_timeout(200)
    p.keyboard.press("Escape")
    assert p.evaluate("window.__xss") is None
    assert p.evaluate("document.querySelectorAll('#view-mfa img, #view-mfa b, #view-mfa script, #view-mfa svg:not(.i), #palette img').length") == 0


def test_sign_ins_fit_a_phone(mfa_grid, browser):  # noqa: F811
    url, path = mfa_grid
    sign_in("contoso-monitor")
    store.checkout_signin(CHECKS, "erp-nightly")
    ctx = _admin_context(browser, 375, 800)
    p = ctx.new_page()
    try:
        p.goto(url + "#/mfa")
        p.wait_for_selector("#signin-rows .tag.live")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert p.locator(".tabbar a").count() == 5
        for name in ("contoso-monitor", "erp-nightly"):
            box = row(p, name).locator("[data-delete-signin]").bounding_box()
            assert box["x"] + box["width"] <= 375
        p.click("#signin-how summary")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        run(path, "DELETE FROM savedSignins")
        p.reload()
        p.wait_for_selector("#signin-state .signin-intro")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        ctx.close()
