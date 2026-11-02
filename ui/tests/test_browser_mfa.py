"""The MFA view (authenticator seeds the Grid UI keeps for checks) in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import re
import time

import pytest

from ui import mfa, store

pytest.importorskip("playwright.sync_api")
pytest.importorskip("cryptography")

from ui.tests.test_browser import browser, grid, page, refresh_now, run  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_tokens import _admin_context, admin_grid  # noqa: E402,F401  (fixtures)

SEED = "JBSWY3DPEHPK3PXP"
# Lets a test hold back the answer to its page's next request: window.__slow["GET api/mfa"] = ms, keyed the
# way api.js asks. The server has answered by then; only the page hears it late.
SLOW_FETCH = """
window.__slow = {};
const realFetch = window.fetch;
window.fetch = (url, opts) => {
  const key = ((opts && opts.method) || "GET") + " " + String(url).split("?")[0];
  const ms = window.__slow[key];
  delete window.__slow[key];
  const answer = realFetch(url, opts);
  return ms ? answer.then((r) => new Promise((resolve) => setTimeout(() => resolve(r), ms))) : answer;
};
"""
# Moves the page's monotonic clock on by window.__skew ms, so a test needn't wait a minute.
SKEW_CLOCK = """
window.__skew = 0;
const realNow = performance.now.bind(performance);
performance.now = () => realNow() + window.__skew;
"""


@pytest.fixture
def mfa_grid(admin_grid, monkeypatch):  # noqa: F811
    """The demo grid with an admin token and GRID_SECRETS_KEY set."""
    monkeypatch.setenv("GRID_SECRETS_KEY", "k" * 32)
    return admin_grid


@pytest.fixture
def mfa_page(browser):  # noqa: F811
    """A signed-in tab that keeps its console errors and whose clipboard the test can read back."""
    ctx = _admin_context(browser)
    ctx.grant_permissions(["clipboard-read", "clipboard-write"])
    p = ctx.new_page()
    p.errors = []
    p.on("console", lambda m: p.errors.append(m.text) if m.type == "error" else None)
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    yield p
    ctx.close()


def add_secret(name="intranet", seed=SEED, **kw):
    return store.create_mfa(name, seed, kw.get("issuer", "Intranet"), kw.get("account", "checks@example.com"))


def codes_now(seed=SEED, digits=6):
    """The codes of the previous, current and next time steps: a code read a moment ago is one of them."""
    step = mfa.step_at(time.time(), 30)
    return {mfa.code(seed, s, digits) for s in (step - 1, step, step + 1)}


def fresh_window(min_left=10):
    """Wait for a 30 s window with at least min_left seconds to go, so a test's steps stay in one window."""
    left = 30 - time.time() % 30
    if left < min_left:
        time.sleep(left + 0.3)


def set_hidden(p, hidden):
    """As if the admin switched to another tab (or back)."""
    p.evaluate("""hidden => {
        Object.defineProperty(document, "hidden", {configurable: true, get: () => hidden});
        document.dispatchEvent(new Event("visibilitychange"));
    }""", hidden)


def mfa_requests(p):
    seen = []
    p.on("request", lambda r: seen.append((r.method, r.url.split("/api/")[1])) if "/api/mfa" in r.url or "/api/signins" in r.url else None)
    return seen


def row(p, name):
    return p.locator("#mfa-rows tr", has_text=name)


def test_mfa_without_an_admin_token_explains_the_setting(grid, page):  # noqa: F811
    url, _ = grid
    seen = mfa_requests(page)
    page.goto(url + "#/mfa")
    page.wait_for_selector("#mfa-state .empty")
    assert "Set an admin token" in page.text_content("#mfa-state")
    assert "GRID_ADMIN_TOKEN=" in page.text_content("#mfa-state")
    assert not page.is_visible("#add-mfa-btn") and not page.is_visible("#mfa-panel")
    assert page.title() == "MFA & sign-ins · Playwright Grid"
    refresh_now(page)
    assert seen == []  # nothing to ask without an admin


def test_signing_in_opens_the_page(mfa_grid, page):  # noqa: F811
    url, _ = mfa_grid
    seen = mfa_requests(page)
    page.goto(url + "#/mfa")
    page.wait_for_selector("#mfa-state [data-signin]")
    assert not page.is_visible("#signin-dialog") and seen == []
    page.click("#mfa-state [data-signin]")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    page.wait_for_selector("#mfa-empty [data-add-mfa]")
    assert "No MFA secrets yet" in page.text_content("#mfa-empty")
    assert seen and set(seen) == {("GET", "mfa"), ("GET", "signins")}  # both lists, in the same polls
    assert page.get_attribute("[data-nav-view=mfa] >> visible=true", "aria-current") == "page"
    page.click("#account-btn")
    page.click("#account-menu [data-menu=signout]")
    page.wait_for_selector("#mfa-state [data-signin]")
    assert "Checks keep getting codes" in page.text_content("#mfa-state")
    assert page.errors == []


def test_an_unset_or_short_secrets_key_explains_the_setting(admin_grid, mfa_page, monkeypatch):  # noqa: F811
    url, _ = admin_grid
    monkeypatch.setenv("GRID_SECRETS_KEY", "k" * 32)
    add_secret()  # stored under a key the UI has since lost
    monkeypatch.delenv("GRID_SECRETS_KEY")
    mfa_page.goto(url + "#/mfa")
    mfa_page.wait_for_selector("#mfa-key .empty")
    assert mfa_page.is_disabled("#mfa-rows [data-show-code]")
    assert "give no codes" in mfa_page.text_content("#mfa-key")
    text = mfa_page.text_content("#mfa-key")
    assert "Set a secrets key to store MFA secrets" in text and 'GRID_SECRETS_KEY=$(openssl rand -hex 32)' in text
    assert "don't decrypt under another" in text
    assert mfa_page.locator("#mfa-key [data-copy]").count() == 2
    assert not mfa_page.is_visible("#add-mfa-btn") and mfa_page.locator("#mfa-empty [data-add-mfa]").count() == 0

    monkeypatch.setenv("GRID_SECRETS_KEY", "short")
    mfa_page.click("[data-nav-view=overview] >> visible=true")
    mfa_page.click("[data-nav-view=mfa] >> visible=true")
    mfa_page.wait_for_function("() => document.getElementById('mfa-key').textContent.includes('at least 16 characters')")


def test_add_a_secret_and_the_seed_never_comes_back(mfa_grid, mfa_page):
    url, path = mfa_grid
    p = mfa_page
    responses = []
    p.on("response", lambda r: responses.append(r) if "/api/mfa" in r.url else None)
    p.goto(url + "#/mfa")
    p.click("#mfa-empty [data-add-mfa]")
    p.wait_for_selector("#add-mfa-dialog[open]")
    assert p.evaluate("document.activeElement.id") == "mfa-name"
    secret = p.locator("#mfa-secret")
    # Dotted, but not a password field: password managers would save the seed as this site's password.
    assert secret.get_attribute("type") == "text" and secret.get_attribute("autocomplete") == "off"
    assert p.locator("#add-mfa-dialog input[type=password]").count() == 0
    assert secret.evaluate("e => getComputedStyle(e).webkitTextSecurity") == "disc"
    for ignore in ("data-1p-ignore", "data-lpignore", "data-bwignore"):
        assert secret.get_attribute(ignore) is not None
    assert secret.get_attribute("spellcheck") == "false"
    p.click("#mfa-submit")
    assert "1 to 64 characters" in p.text_content("#mfa-error")
    p.fill("#mfa-name", "bad name")
    p.click("#mfa-submit")
    assert p.get_attribute("#mfa-name", "aria-invalid") == "true"
    p.fill("#mfa-name", "intranet")
    p.click("#mfa-submit")
    assert "setup key" in p.text_content("#mfa-error")
    p.fill("#mfa-secret", "not base32 at all!")
    p.click("#mfa-submit")
    p.wait_for_function("() => document.getElementById('mfa-error').textContent.includes('base32')")  # the server's word
    assert p.get_attribute("#mfa-secret", "aria-invalid") == "true"

    p.fill("#mfa-secret", "jbsw y3dp ehpk 3pxp")
    p.click("#mfa-secret-toggle")
    assert secret.evaluate("e => getComputedStyle(e).webkitTextSecurity") == "none"
    assert p.get_attribute("#mfa-secret-toggle", "aria-pressed") == "true"
    p.fill("#mfa-issuer", "Intranet")
    p.click("#mfa-submit")
    p.wait_for_selector("#add-mfa-dialog", state="hidden")
    p.wait_for_selector("#mfa-rows tr:has-text('intranet')")
    assert p.evaluate("document.activeElement.dataset.showCode") is not None
    assert "6 digits · 30 s · SHA1" in row(p, "intranet").text_content()
    assert "Intranet" in row(p, "intranet").text_content()
    ((cipher,),) = run(path, "SELECT seedCipher FROM mfaSecrets WHERE name = 'intranet'")
    assert SEED not in cipher
    assert secret.input_value() == "" and "masked" in secret.get_attribute("class")  # cleared on close

    # A name that's taken: the Name field is the one marked.
    p.click("#add-mfa-btn")
    p.fill("#mfa-name", "intranet")
    p.fill("#mfa-secret", SEED)
    p.click("#mfa-submit")
    p.wait_for_function("() => document.getElementById('mfa-error').textContent.includes('already a secret called')")
    assert p.get_attribute("#mfa-name", "aria-invalid") == "true" and p.get_attribute("#mfa-secret", "aria-invalid") is None
    assert p.evaluate("document.activeElement.id") == "mfa-name"
    p.keyboard.press("Escape")
    p.wait_for_selector("#add-mfa-dialog", state="hidden")

    # An otpauth:// address brings its own settings: the advanced fields step aside.
    p.click("#add-mfa-btn")
    assert secret.input_value() == ""
    p.fill("#mfa-name", "vpn")
    p.fill("#mfa-secret", "otpauth://totp/Corp%20VPN:ops%40example.com?secret=" + SEED + "&digits=8&period=60")
    p.click("#mfa-advanced summary")
    assert p.is_disabled("#mfa-digits") and "otpauth" in p.text_content("#mfa-settings-hint")
    p.click("#mfa-submit")
    p.wait_for_selector("#mfa-rows tr:has-text('vpn')")
    assert "8 digits · 60 s · SHA1" in row(p, "vpn").text_content()
    assert "Corp VPN · ops@example.com" in row(p, "vpn").text_content()

    # Pressing Escape mid-entry drops what was typed.
    p.click("#add-mfa-btn")
    p.fill("#mfa-secret", SEED)
    p.keyboard.press("Escape")
    p.wait_for_selector("#add-mfa-dialog", state="hidden")
    # The close event comes a task after the dialog hides.
    p.wait_for_function("() => document.getElementById('mfa-secret').value === ''", timeout=2000)
    bodies = [r.text() for r in responses]
    assert len(bodies) >= 4 and not any(SEED in b or SEED.lower() in b for b in bodies)
    assert SEED not in p.content()
    # The one refusal (the bad seed) is the browser's network note, not a script error.
    assert [e for e in p.errors if "status of 400" not in e] == []


def test_a_refused_form_asks_for_sign_in_on_top_of_it(mfa_grid, mfa_page):
    """The prompt opens over the open form, modal like it, not out of reach behind it."""
    url, _ = mfa_grid
    p = mfa_page
    p.goto(url + "#/mfa")
    p.click("#mfa-empty [data-add-mfa]")
    p.wait_for_selector("#add-mfa-dialog[open]")
    p.fill("#mfa-name", "intranet")
    p.fill("#mfa-secret", SEED)
    p.evaluate("sessionStorage.setItem('gridToken', 'rotated')")  # the admin token changed since this tab signed in
    p.click("#mfa-submit")
    p.wait_for_selector("#signin-dialog[open]")
    assert p.evaluate("document.getElementById('signin-dialog').matches(':modal')")
    p.fill("#signin-token", "s3cret")
    p.press("#signin-token", "Enter")
    p.wait_for_selector("#add-mfa-dialog", state="hidden")
    p.wait_for_selector("#mfa-rows tr")
    assert [s["name"] for s in store.list_mfa()["secrets"]] == ["intranet"]


def test_show_code_counts_down_and_hides_itself(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret()
    # Room for a poll to notice the check's hand-out below before the window ends.
    fresh_window(min_left=20)
    p = mfa_page
    p.goto(url + "#/mfa")
    row(p, "intranet").locator("[data-show-code]").click()
    p.wait_for_selector("#mfa-code")
    code = p.text_content("#mfa-code")
    assert re.fullmatch(r"\d{6}", code) and code in codes_now()
    assert p.eval_on_selector_all("#mfa-code .half", "els => els.map(e => e.textContent.length)") == [3, 3]
    assert row(p, "intranet").locator("[data-show-code]").get_attribute("aria-pressed") == "true"
    left = int(re.fullmatch(r"(\d+)s left", p.text_content("#mfa-left")).group(1))
    assert 1 <= left <= 30
    assert abs(left - (30 - time.time() % 30)) <= 2  # the server's window, not a guess
    p.wait_for_timeout(1300)
    later = int(re.fullmatch(r"(\d+)s left", p.text_content("#mfa-left")).group(1))
    assert later < left
    assert code not in p.title() and code not in p.url
    assert code not in p.evaluate("JSON.stringify(Object.assign({}, localStorage)) + JSON.stringify(Object.assign({}, sessionStorage))")
    assert "Looking doesn't use it up" in p.text_content("#mfa-note")
    assert run(mfa_grid[1], "SELECT issuedCount FROM mfaSecrets") == [(0,)]  # looking used nothing up
    assert "mfa.code(\"intranet\"" in p.text_content("#mfa-snippet")

    # A check takes this window's code: the page says a site would refuse it now.
    store.issue_mfa_code("client token 'nightly'", name="intranet", label="nightly sign-in", min_valid_s=0)
    p.wait_for_selector(".mfa-live .tag.note", timeout=8000)
    assert "already handed out" in p.text_content("#mfa-note")
    p.wait_for_selector("#mfa-issue-rows tr:has-text('nightly sign-in')")

    # Leaving the view takes the code off the page.
    p.click("[data-nav-view=overview] >> visible=true")
    p.wait_for_selector("#mfa-code", state="detached")
    p.click("[data-nav-view=mfa] >> visible=true")
    p.wait_for_selector("#mfa-rows tr")
    assert p.locator("#mfa-code").count() == 0
    assert row(p, "intranet").locator("[data-show-code]").get_attribute("aria-pressed") == "false"
    row(p, "intranet").locator("[data-show-code]").click()
    p.wait_for_selector("#mfa-code")
    p.click("[data-hide-code]")
    assert p.locator("#mfa-code").count() == 0
    assert p.evaluate("document.activeElement.dataset.showCode") is not None  # focus back on its row

    # So does switching to another tab.
    row(p, "intranet").locator("[data-show-code]").click()
    p.wait_for_selector("#mfa-code")
    set_hidden(p, True)
    assert p.locator("#mfa-code").count() == 0
    set_hidden(p, False)
    assert p.errors == []


def test_use_code_hands_out_the_next_free_window_and_copies_it(mfa_grid, mfa_page):
    url, path = mfa_grid
    add_secret()
    fresh_window()
    store.issue_mfa_code("client token 'nightly'", name="intranet", min_valid_s=0)  # this window is taken
    p = mfa_page
    p.goto(url + "#/mfa")
    row(p, "intranet").locator("[data-show-code]").click()
    p.wait_for_selector("[data-use-code]")
    p.click("[data-use-code]")
    p.wait_for_selector(".mfa-live.used")
    code = p.text_content("#mfa-code")
    ((step, count),) = run(path, "SELECT lastStep, issuedCount FROM mfaSecrets")
    assert count == 2 and step == mfa.step_at(time.time(), 30) + 1
    assert code == mfa.code(SEED, step)
    assert p.evaluate("navigator.clipboard.readText()") == code
    assert "Copied the code. It's the next window's" in p.text_content("#toast")
    assert re.fullmatch(r"Works in \d+s", p.text_content("#mfa-left"))
    assert "next window's" in p.text_content("#mfa-note")
    assert p.evaluate("document.activeElement.dataset.copyCode") is not None
    assert run(path, "SELECT issuedTo, label, step FROM mfaIssues ORDER BY id DESC LIMIT 1") == [("Admin", "Grid UI", step)]
    p.wait_for_selector("#mfa-issue-rows tr:has-text('Grid UI')")
    assert "to Admin" in row(p, "intranet").locator(".cell-last").text_content()
    assert row(p, "intranet").locator(".cell-count").text_content() == "2"
    assert p.locator("#mfa-issue-rows tr").first.locator(".cell-to").text_content() == "Admin"
    assert code not in p.url and code not in p.title()


def test_a_used_code_stays_while_the_admin_types_it(mfa_grid, mfa_page):
    url, path = mfa_grid
    add_secret()
    fresh_window(min_left=15)
    for _ in range(2):  # checks took this window and the next: Use code gives the one after
        store.issue_mfa_code("client token 'nightly'", name="intranet", min_valid_s=0)
    p = mfa_page
    p.add_init_script(SKEW_CLOCK)
    p.goto(url + "#/mfa")
    row(p, "intranet").locator("[data-show-code]").click()
    p.click("[data-use-code]")
    p.wait_for_selector(".mfa-live.used")
    ((step,),) = run(path, "SELECT lastStep FROM mfaSecrets")
    assert step == mfa.step_at(time.time(), 30) + 2 and p.text_content("#mfa-code") == mfa.code(SEED, step)
    assert re.fullmatch(r"Works in \d+s", p.text_content("#mfa-left"))

    # Off to the site's tab to type it: the code and its countdown are still here on the way back.
    set_hidden(p, True)
    p.wait_for_timeout(600)
    set_hidden(p, False)
    assert p.text_content("#mfa-code") == mfa.code(SEED, step)
    assert re.fullmatch(r"Works in \d+s", p.text_content("#mfa-left"))

    # A minute on, a code looked at would be gone; this one has just opened and works until its window closes.
    p.evaluate("window.__skew = 62000")
    p.wait_for_function("() => /^\\d+s left$/.test(document.getElementById('mfa-left').textContent)")
    assert p.locator(".mfa-live.used").count() == 1 and p.text_content("#mfa-code") == mfa.code(SEED, step)
    assert "type it before the countdown runs out" in p.text_content("#mfa-note")
    assert p.errors == []


def test_answers_on_their_way_land_where_they_belong(mfa_grid, mfa_page):
    url, path = mfa_grid
    secret = add_secret()
    fresh_window()
    p = mfa_page
    p.add_init_script(SLOW_FETCH)
    p.goto(url + "#/mfa")
    show = row(p, "intranet").locator("[data-show-code]")

    # A look still on its way when the view is left never reaches the page.
    p.evaluate("id => { window.__slow['GET api/mfa/' + id + '/code'] = 1000; }", secret["id"])
    show.click()
    p.click("[data-nav-view=overview] >> visible=true")
    p.wait_for_timeout(1500)
    p.click("[data-nav-view=mfa] >> visible=true")
    p.wait_for_selector("#mfa-rows tr")
    assert p.locator("#mfa-code").count() == 0 and show.get_attribute("aria-pressed") == "false"

    # Use code still on its way when the code is hidden: the server has handed it out, so the page shows it.
    show.click()
    p.wait_for_selector("[data-use-code]")
    p.evaluate("id => { window.__slow['POST api/mfa/' + id + '/use'] = 1000; }", secret["id"])
    p.click("[data-use-code]")
    p.click("[data-hide-code]")
    p.wait_for_selector(".mfa-live.used")
    ((step,),) = run(path, "SELECT lastStep FROM mfaSecrets")
    assert p.text_content("#mfa-code") == mfa.code(SEED, step)
    p.wait_for_selector("#mfa-issue-rows tr:has-text('Grid UI')")
    assert p.errors == []


def test_a_delete_is_not_undone_by_a_poll_on_its_way(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret("intranet")
    add_secret("vpn", issuer="", account="")
    p = mfa_page
    p.add_init_script(SLOW_FETCH)
    p.goto(url + "#/mfa")
    p.wait_for_selector("#mfa-rows tr:has-text('vpn')")
    row(p, "intranet").locator("[data-delete-mfa]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    # The 5 s poll asks while the dialog is open; its answer, from before the delete, comes in after it.
    p.evaluate("window.__slow['GET api/mfa'] = 1500")
    with p.expect_request(lambda r: r.method == "GET" and r.url.endswith("/api/mfa"), timeout=7000):
        pass
    p.click("#confirm-ok")
    p.wait_for_selector("#mfa-rows tr:has-text('intranet')", state="detached", timeout=3500)
    assert p.locator("#mfa-rows tr").count() == 1 and row(p, "vpn").count() == 1
    assert p.evaluate("!!document.activeElement.closest('#mfa-rows tr')")  # focus moved to the row left
    p.wait_for_timeout(500)
    assert p.locator("#mfa-rows tr:has-text('intranet')").count() == 0


def test_signing_out_takes_the_secrets_out_of_the_palette(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret()
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#mfa-rows tr")
    p.click("[data-nav-view=overview] >> visible=true")
    p.wait_for_selector("#view-overview h1")
    p.click("#account-btn")
    p.click("#account-menu [data-menu=signout]")
    p.keyboard.press("Control+K")
    p.keyboard.type("intranet")
    p.wait_for_timeout(300)
    assert p.locator(".pal-opt:has-text('intranet')").count() == 0
    assert "checks@example.com" not in p.text_content("#palette")
    p.keyboard.press("Escape")


def test_delete_a_secret_and_its_log(mfa_grid, mfa_page):
    url, path = mfa_grid
    add_secret("intranet")
    add_secret("vpn", issuer="", account="")
    store.issue_mfa_code("Admin", name="intranet", label="Grid UI")
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#mfa-rows tr:has-text('vpn')")
    assert "No issuer or account" in row(p, "vpn").text_content()
    row(p, "intranet").locator("[data-show-code]").click()
    p.wait_for_selector("#mfa-code")
    row(p, "intranet").locator("[data-delete-mfa]").click()
    p.wait_for_selector("#confirm-dialog[open]")
    assert "intranet" in p.text_content("#confirm-title")
    assert "hand-out log" in p.text_content("#confirm-text")
    assert "danger-fill" in p.get_attribute("#confirm-ok", "class")
    p.click("#confirm-ok")
    p.wait_for_selector("#mfa-rows tr:has-text('intranet')", state="detached")
    assert p.locator("#mfa-code").count() == 0  # its code went with it
    assert run(path, "SELECT name FROM mfaSecrets") == [("vpn",)]
    assert run(path, "SELECT COUNT(*) FROM mfaIssues") == [(0,)]
    assert p.evaluate("document.activeElement !== document.body")
    assert "mfa.code(\"vpn\"" in p.text_content("#mfa-snippet")

    row(p, "vpn").locator("[data-delete-mfa]").click()
    p.click("#confirm-ok")
    p.wait_for_selector("#mfa-empty [data-add-mfa]")
    assert not p.is_visible("#mfa-panel")
    p.wait_for_function("() => document.activeElement.matches('#mfa-empty [data-add-mfa]')")  # not a row that's gone


def test_server_strings_are_escaped(mfa_grid, mfa_page):
    url, path = mfa_grid
    secret = add_secret()
    run(path, "UPDATE mfaSecrets SET name = ?, issuer = ?, account = ? WHERE id = ?",
        ('<img src=x onerror="window.__xss=1">', '"><b>bold</b>', "<script>window.__xss=2</script>", secret["id"]))
    run(path, "INSERT INTO mfaIssues (secretId, step, issuedAt, issuedTo, label) VALUES (?, 1, datetime('now'), ?, ?)",
        (secret["id"], "<img src=y onerror=\"window.__xss=3\">", '"><svg onload="window.__xss=4">'))
    p = mfa_page
    p.goto(url + "#/mfa")
    p.wait_for_selector("#mfa-rows tr")
    p.wait_for_selector("#mfa-issue-rows tr")
    assert "<img src=x" in p.text_content("#mfa-rows .mfa-name")
    assert "\"><svg" in p.text_content("#mfa-issue-rows .cell-label")
    p.locator("#mfa-rows [data-delete-mfa]").click()
    p.wait_for_timeout(300)
    assert "<img" in p.text_content("#confirm-title")
    p.click("#confirm-dialog [data-dismiss]")
    p.keyboard.press("Control+K")
    p.keyboard.type("img")
    p.wait_for_timeout(200)
    p.keyboard.press("Escape")
    assert p.evaluate("window.__xss") is None
    assert p.evaluate("document.querySelectorAll('#view-mfa img, #view-mfa b, #view-mfa script, #view-mfa svg:not(.i), #palette img').length") == 0


def test_shortcut_and_palette_reach_the_page(mfa_grid, mfa_page):
    url, _ = mfa_grid
    add_secret()
    p = mfa_page
    p.goto(url + "#/overview")
    p.wait_for_selector("#view-overview h1")
    p.keyboard.press("g")
    p.keyboard.press("m")
    p.wait_for_function("() => location.hash === '#/mfa'")
    p.wait_for_selector("#mfa-rows tr")
    p.click("[data-nav-view=overview] >> visible=true")
    p.keyboard.press("Control+K")
    p.keyboard.type("intranet")
    p.wait_for_selector(".pal-opt:has-text('intranet')")
    p.keyboard.press("Enter")
    p.wait_for_function("() => location.hash === '#/mfa'")


def test_mfa_fits_a_phone(mfa_grid, browser):  # noqa: F811
    url, _ = mfa_grid
    add_secret()
    store.issue_mfa_code("client token 'nightly'", name="intranet", label="nightly sign-in")
    ctx = _admin_context(browser, 375, 800)
    p = ctx.new_page()
    try:
        p.goto(url + "#/mfa")
        p.wait_for_selector("#mfa-rows tr")
        assert p.locator(".tabbar a").count() == 5 and p.is_visible(".tabbar [data-nav-view=mfa]")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        row(p, "intranet").locator("[data-show-code]").click()
        p.wait_for_selector("#mfa-code")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        p.click("#add-mfa-btn")
        p.wait_for_selector("#add-mfa-dialog[open]")
        p.click("#mfa-advanced summary")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        box = p.locator("#add-mfa-dialog").bounding_box()
        assert box["x"] >= 0 and box["x"] + box["width"] <= 375
    finally:
        ctx.close()
