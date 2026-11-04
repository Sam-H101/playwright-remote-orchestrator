"""The Grid UI in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed:
    pip install playwright && python -m playwright install chromium
"""
import contextlib
import sqlite3
import threading
import urllib.parse

import pytest

from ui import app as grid_app, demo, store

sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # no browser downloaded
            pytest.skip("Chromium for Playwright isn't installed: {}".format(e))
        yield b
        b.close()


def _serve(monkeypatch, path):
    from werkzeug.serving import make_server

    monkeypatch.setattr(store, "DB_ENGINE", "sqlite")
    monkeypatch.setattr(store, "SQLITE_PATH", path)
    monkeypatch.setattr(store, "FAIL_THRESHOLD", 3)
    monkeypatch.setattr(store, "FAIL_COOLDOWN_S", 300)
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "")
    monkeypatch.setattr(grid_app, "PUBLIC_GATEWAY_URL", "")
    server = make_server("127.0.0.1", 0, grid_app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def grid(tmp_path, monkeypatch):
    """(base URL, sqlite path) of a UI serving the demo data."""
    path = str(tmp_path / "demo.db")
    con = sqlite3.connect(path)
    demo.seed(con)
    con.close()
    server = _serve(monkeypatch, path)
    yield "http://127.0.0.1:{}/".format(server.server_port), path
    server.shutdown()


@pytest.fixture
def page(browser):
    p = browser.new_page(viewport={"width": 1280, "height": 900})
    p.errors = []
    p.on("console", lambda m: p.errors.append(m.text) if m.type == "error" else None)
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    yield p
    p.close()


def run(path, sql, params=()):
    with contextlib.closing(sqlite3.connect(path)) as con, con:
        return con.execute(sql, params).fetchall()


def slot_id(path, server, port):
    return run(path, "SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?", (server, port))[0][0]


def refresh_now(p):
    p.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    p.wait_for_timeout(400)


def open_session_release(page, url, path):
    """Open the oldest session's inspector and press Release (the only place sessions are released)."""
    sid = slot_id(path, "pw-static.lab.local", 3000)
    page.goto(url + "#/session/{}".format(sid))
    page.wait_for_selector("#inspector:not([hidden]) [data-act=release]")
    page.click("#inspector [data-act=release]")
    return sid


def test_a_refused_action_shows_an_error_and_is_not_retried(grid, page):
    url, path = grid
    posts = []

    def refuse(route):
        posts.append(route.request.url)
        route.fulfill(status=403, content_type="text/html", body="<h1>Forbidden by the proxy</h1>")

    page.route("**/api/slots/*/release", refuse)
    open_session_release(page, url, path)
    page.wait_for_selector("#confirm-dialog[open]")
    page.click("#confirm-ok")
    page.wait_for_selector("#toast.bad")
    page.wait_for_timeout(500)
    assert len(posts) == 1
    assert "HTTP 403" in page.text_content("#toast")
    assert not page.is_visible("#signin-dialog")


def test_a_token_set_after_the_page_loaded_signs_in_retries_once_and_cancel_leaves_it_undone(grid, page, monkeypatch):
    url, path = grid
    posts, checks = [], []
    page.on("request", lambda r: posts.append(r.headers.get("x-grid-token")) if r.method == "POST" else None)
    page.on("request", lambda r: checks.append(r.headers.get("x-grid-token")) if "api/session" in r.url else None)
    page.goto(url + "#/sessions")  # loaded while the grid had no admin token: actions are open
    page.wait_for_selector("#unprotected-btn:not([hidden])")
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    busy = run(path, "SELECT COUNT(*) FROM portUsage WHERE inUse = 1")[0][0]

    open_session_release(page, url, path)
    page.click("#confirm-ok")  # the grid now says the sign-in is missing: sign in, once
    page.wait_for_selector("#signin-dialog[open]")
    page.fill("#signin-token", "wrong")
    page.press("#signin-token", "Enter")  # Enter submits Sign in, not Cancel
    page.wait_for_function("() => document.getElementById('signin-error').textContent.indexOf('rejected') !== -1")
    assert page.is_visible("#signin-dialog")
    page.click("#signin-dialog [data-dismiss]")
    page.wait_for_timeout(300)
    assert posts == [""]  # the refused attempt, never retried with a rejected token
    assert "wrong" in checks
    assert not page.is_visible("#signin-dialog")
    assert run(path, "SELECT COUNT(*) FROM portUsage WHERE inUse = 1")[0][0] == busy  # dismissing left it undone

    # Signed out now, so the control says so, and signs in first, before its confirm.
    assert page.get_attribute("#inspector [data-act=release]", "aria-label").startswith("Sign in to release")
    page.click("#inspector [data-act=release]")
    page.wait_for_selector("#signin-dialog[open]")
    assert not page.is_visible("#confirm-dialog")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    page.wait_for_selector("#confirm-dialog[open]")
    assert page.evaluate("document.activeElement.hasAttribute('data-dismiss')")  # Cancel is focused first
    page.click("#confirm-ok")
    page.wait_for_selector("#toast:not(.bad)")
    assert "released" in page.text_content("#toast")
    assert posts == ["", "s3cret"]
    assert run(path, "SELECT COUNT(*) FROM portUsage WHERE inUse = 1")[0][0] == busy - 1
    page.wait_for_selector("#account-btn.signed-in")


def press(page, selector):
    """A press at a person's pace (down, a moment, up), not Playwright's back-to-back events."""
    b = page.locator(selector).bounding_box()
    page.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(80)
    page.mouse.up()


def test_sign_in_hangs_under_the_chip_without_blocking_the_page(grid, page, monkeypatch):
    url, path = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    posts = []
    page.on("request", lambda r: posts.append(r.url.split("/api/")[1]) if r.method == "POST" else None)
    page.goto(url)
    page.wait_for_selector("#account-btn:not([hidden]) .acct-label")
    chip, dlg = "#account-btn", "#signin-dialog"
    modal = lambda: page.evaluate("document.getElementById('signin-dialog').matches(':modal')")
    focused = lambda: page.evaluate("document.activeElement.id")
    opened = lambda: page.wait_for_selector(dlg + "[open]")
    closed = lambda: page.wait_for_selector(dlg, state="hidden", timeout=3000)
    assert page.get_attribute(chip, "aria-haspopup") == "dialog"

    page.click(chip)
    opened()
    assert not modal() and page.get_attribute(chip, "aria-expanded") == "true" and focused() == "signin-token"
    assert page.get_by_role("dialog", name="Sign in", exact=True).is_visible()  # not "Saved sign-ins", the MFA view's heading
    # One press elsewhere both closes it and does what it pressed; a press on plain text closes it too.
    page.click(".rail a[href='#/sessions']")
    page.wait_for_selector("#view-sessions:not([hidden])")
    assert not page.is_visible(dlg) and page.get_attribute(chip, "aria-expanded") == "false"
    page.goto(url + "#/overview")
    page.click(chip)
    opened()
    page.click("#verdict-sub")
    closed()

    # Esc closes it and only it: the inspector under it stays, and focus goes back to the chip.
    page.click(".util-host >> text=runner-7f3a9c")
    page.wait_for_selector("#inspector:not([hidden])")
    page.click(chip)
    opened()
    page.click(dlg + " h2")  # a press inside it is not a press elsewhere
    assert page.is_visible(dlg) and page.is_visible("#inspector")
    page.keyboard.press("Escape")
    closed()
    assert page.is_visible("#inspector") and focused() == "account-btn"

    # The chip closes what it opened, pressed at a person's pace or at Playwright's.
    press(page, chip)
    opened()
    press(page, chip)
    closed()
    page.click(chip)
    opened()
    page.click(chip)
    closed()
    # Typing in it is typing, not shortcuts, and Ctrl K doesn't take it away.
    page.click(chip)
    page.type("#signin-token", "g s/?")
    page.keyboard.press("Control+k")
    assert not page.is_visible("#view-sessions") and not page.is_visible("#palette") and not page.is_visible("#shortcuts-dialog")
    assert page.input_value("#signin-token") == "g s/?" and page.is_visible(dlg)
    # Focus arriving anywhere else closes it, and it lets go of Esc: that's the inspector's again.
    page.focus("#jump-btn")
    closed()
    page.keyboard.press("Escape")
    page.wait_for_selector("#inspector", state="hidden")

    # In the tab order it sits where it shows, after the chip: Tab past its end goes on to what follows
    # the chip, Shift+Tab goes back to the chip, and either way it closes.
    page.click(chip)
    opened()
    for _ in range(3):
        page.keyboard.press("Tab")
    closed()
    assert focused() == "theme-btn" and page.get_attribute(chip, "aria-expanded") == "false"
    page.click(chip)
    opened()
    page.keyboard.press("Shift+Tab")
    closed()
    assert focused() == "account-btn"

    # An action that asked for it is cancelled by a press elsewhere.
    resets = page.locator("#att-list [data-act=reset]")
    first = resets.nth(0).get_attribute("data-target")
    resets.nth(0).click()
    opened()
    assert "Sign in to reset failures" in page.text_content(dlg) and not modal()
    asked = page.text_content(dlg + " .sheet-sub")
    target = "document.activeElement.dataset.target"
    page.click("#ov-h1")
    closed()
    assert focused() == "ov-h1"  # where the press put it, not back on the cancelled action
    # Shift+Tab goes back to the action it asks for, not to the chip.
    resets.nth(0).click()
    opened()
    page.keyboard.press("Shift+Tab")
    closed()
    assert page.evaluate(target) == first
    # Scrolled away from the action, a press elsewhere still lands where it was aimed: cancelling it
    # doesn't bring its button back under the pointer.
    resets.nth(0).click()
    opened()
    page.mouse.wheel(0, 2000)
    page.wait_for_function("() => scrollY > 300")
    link = page.locator(".node-link").last
    box = link.bounding_box()
    page.mouse.click(box["x"] + 8, box["y"] + box["height"] / 2)
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.evaluate("location.hash") == "#/node/" + link.get_attribute("data-id") and page.evaluate("scrollY") > 300
    page.keyboard.press("Escape")
    page.evaluate("scrollTo(0, 0)")

    # Replaced by another action (here by script), it asks for that one, and Esc leaves focus on it.
    second = resets.nth(1).get_attribute("data-target")
    resets.nth(0).click()
    opened()
    page.evaluate("document.querySelectorAll('#att-list [data-act=reset]')[1].click()")
    page.wait_for_function("(s) => document.querySelector('#signin-dialog[open] .sheet-sub')?.textContent !== s", arg=asked)
    page.keyboard.press("Escape")
    closed()
    page.wait_for_timeout(200)  # after the browser's own focus fix-up
    assert page.evaluate(target) == second

    # Another action while it's open asks for that action instead, so one sign-in never runs both:
    # pressed (the first prompt's close event comes after the second one opens), or clicked by script.
    resets.nth(0).click()
    opened()
    resets.nth(1).click()
    page.wait_for_timeout(300)
    assert page.is_visible(dlg) and page.text_content(dlg + " .sheet-sub") != asked
    page.evaluate("document.querySelectorAll('#att-list [data-act=reset]')[0].click()")
    page.wait_for_function("(s) => document.querySelector('#signin-dialog[open] .sheet-sub')?.textContent === s", arg=asked)
    page.wait_for_timeout(300)
    assert posts == []
    page.fill("#signin-token", "s3cret")
    page.click("#signin-ok")
    page.wait_for_selector("#toast:not(.bad)")
    page.wait_for_timeout(300)
    assert posts == ["slots/{}/reset-failures".format(first)]
    page.wait_for_selector("#account-btn.signed-in")
    assert page.errors == []


def test_a_second_question_never_shares_the_first_ones_ok(grid, page):
    url, _ = grid
    posts = []
    page.on("request", lambda r: posts.append(r.url.split("/api/")[1]) if r.method == "POST" else None)
    page.goto(url)
    page.click("#att-list [data-act=release]")
    page.wait_for_selector("#confirm-dialog[open]")
    asked = page.text_content("#confirm-title")
    page.evaluate("document.querySelector('#att-list [data-act=forget]').click()")  # another action, by script
    page.wait_for_timeout(200)
    assert page.text_content("#confirm-title") == asked
    page.click("#confirm-ok")
    page.wait_for_selector("#toast:not(.bad)")
    page.wait_for_timeout(300)
    assert len(posts) == 1 and posts[0].endswith("/release"), posts


def test_signing_in_from_the_chip_gives_focus_back_to_it(grid, browser, monkeypatch):
    url, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    for keys in ([], ["Tab", "Tab", "Enter"]):  # the Sign in button pressed, then reached and pressed by keyboard
        p = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            p.goto(url)
            p.wait_for_selector("#account-btn:not([hidden])")
            p.click("#account-btn")
            p.fill("#signin-token", "s3cret")
            if keys:
                for k in keys:
                    p.keyboard.press(k)
            else:
                p.click("#signin-ok")  # disabled while it checks, which drops focus
            p.wait_for_selector("#account-btn.signed-in")
            p.wait_for_function("() => document.activeElement && document.activeElement.id === 'account-btn'")
        finally:
            p.close()


@pytest.mark.parametrize("signed_in", [False, True])
def test_an_action_cancelled_from_a_rows_menu_gives_focus_back_to_the_rows_button(grid, browser, monkeypatch, signed_in):
    """Signed out the action asks to sign in, signed in it asks to confirm: cancelled either way, by
    keyboard or by mouse, focus is back on the row's Actions button, not lost to the page."""
    url, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    p = browser.new_page(viewport={"width": 1280, "height": 900})
    try:
        if signed_in:
            p.add_init_script("sessionStorage.setItem('gridToken', 's3cret')")
        p.goto(url)
        p.wait_for_selector("#account-btn.signed-in" if signed_in else "#account-btn:not([hidden]) .acct-label")
        asked = "#confirm-dialog[open]" if signed_in else "#signin-dialog[open]"
        focus = "document.activeElement.dataset.fk"
        for host, act in (("WIN-BUILD-01", "reset"), ("MAC-QA-02", "forget")):
            button = '.mx-row [data-row-menu="{}"]'.format(host)
            p.focus(button)  # by keyboard, and Esc
            p.keyboard.press("Enter")
            p.focus("#row-menu [data-row-act={}]".format(act))
            p.keyboard.press("Enter")
            p.wait_for_selector(asked)
            p.keyboard.press("Escape")
            p.wait_for_selector(asked, state="detached")
            p.wait_for_timeout(200)  # after the browser's own focus fix-up
            assert p.evaluate(focus) == "menu-" + host, (host, act, "Escape")
            p.click(button)  # by mouse, and Cancel
            p.click("#row-menu [data-row-act={}]".format(act))
            p.click(asked + " [data-dismiss]")
            p.wait_for_selector(asked, state="detached")
            p.wait_for_timeout(200)
            assert p.evaluate(focus) == "menu-" + host, (host, act, "Cancel")
        if not signed_in:  # signed in for it, and then the confirm cancelled
            p.focus('.mx-row [data-row-menu="WIN-BUILD-01"]')
            p.keyboard.press("Enter")
            p.focus("#row-menu [data-row-act=reset]")
            p.keyboard.press("Enter")
            p.fill("#signin-token", "s3cret")
            p.keyboard.press("Enter")
            p.wait_for_selector("#confirm-dialog[open]")
            p.keyboard.press("Escape")
            p.wait_for_selector("#confirm-dialog[open]", state="detached")
            p.wait_for_timeout(200)
            assert p.evaluate(focus) == "menu-WIN-BUILD-01"
    finally:
        p.close()


def test_a_sign_in_closed_while_it_checks_signs_nothing_in(grid, page, monkeypatch):
    """Pressing another action while the first prompt's token is being checked closes that prompt,
    and closing cancels it: its answer, when it comes, neither signs the tab in nor runs anything."""
    url, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    posts, held = [], []
    page.on("request", lambda r: posts.append(r.url.split("/api/")[1]) if r.method == "POST" else None)
    page.goto(url)
    page.wait_for_selector("#account-btn:not([hidden]) .acct-label")
    page.route("**/api/session*", lambda r: held.append(r))
    page.click("#att-list [data-act=reset] >> nth=0")
    page.wait_for_selector("#signin-dialog[open]")
    asked = page.text_content("#signin-dialog .sheet-sub")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    while not held:
        page.wait_for_timeout(50)
    press(page, "#att-list [data-act=reset] >> nth=1")
    page.wait_for_function("(s) => document.querySelector('#signin-dialog[open] .sheet-sub')?.textContent !== s", arg=asked)
    held[0].continue_()  # the first token was right, but its prompt is gone
    page.unroute("**/api/session*")
    page.wait_for_timeout(500)
    assert page.is_visible("#signin-dialog") and not page.is_visible("#account-btn.signed-in")
    assert page.evaluate("sessionStorage.getItem('gridToken')") is None
    page.click("#signin-dialog [data-dismiss]")
    page.wait_for_timeout(300)
    assert posts == []
    assert page.errors == []


def test_a_prompt_replaced_before_it_shows_never_shows(grid, page, monkeypatch):
    """A refused action asks the server how to sign in; another action pressed before that answer
    comes is the one the prompt asks for, as it would be once the prompt shows."""
    url, _ = grid
    posts, held = [], []
    page.on("request", lambda r: posts.append(r.url.split("/api/")[1]) if r.method == "POST" else None)
    page.goto(url)  # loaded while the grid had no admin token: actions are open
    page.wait_for_selector("#unprotected-btn:not([hidden])")
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    resets = page.locator("#att-list [data-act=reset]")
    first, second = resets.nth(0).get_attribute("data-target"), resets.nth(1).get_attribute("data-target")
    page.route("**/api/session*", lambda r: held.append(r))
    resets.nth(0).click()  # refused, so it asks the server for a way to sign in, and the answer is slow
    while not held:
        page.wait_for_timeout(50)
    page.wait_for_selector("#att-list [data-act=reset][aria-label^='Sign in']")
    page.locator("#att-list [data-act=reset]").nth(1).click()
    page.wait_for_timeout(200)
    assert not page.is_visible("#signin-dialog")
    held[0].continue_()
    page.unroute("**/api/session*")
    page.wait_for_selector("#signin-dialog[open]")
    page.wait_for_timeout(300)
    assert "Sign in to reset failures" in page.text_content("#signin-dialog .sheet-sub")
    page.fill("#signin-token", "s3cret")
    page.click("#signin-ok")
    page.wait_for_selector("#toast:not(.bad)")
    page.wait_for_timeout(300)
    assert posts == ["slots/{}/reset-failures".format(first), "slots/{}/reset-failures".format(second)]  # the refused one, then the other
    assert not page.is_visible("#signin-dialog")
    assert all("403" in e for e in page.errors), page.errors  # only the refusal


def test_a_prompt_under_the_chip_stays_on_screen_as_the_window_narrows(grid, browser, monkeypatch):
    url, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    p = browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        p.goto(url)
        p.wait_for_selector("#account-btn:not([hidden])")
        p.click("#account-btn")
        p.wait_for_selector("#signin-dialog[open]")
        for width in (1000, 700, 390, 320):  # a phone turned to portrait
            p.set_viewport_size({"width": width, "height": 800})
            p.wait_for_timeout(100)
            box, chip = p.locator("#signin-dialog").bounding_box(), p.locator("#account-btn").bounding_box()
            assert box["x"] >= 0 and box["x"] + box["width"] <= width and box["y"] >= chip["y"] + chip["height"], (width, box, chip)
            assert p.is_visible("#signin-token")
    finally:
        p.close()


def test_on_a_phone_sign_in_is_a_sheet_and_a_tap_outside_closes_it(grid, browser, monkeypatch):
    url, _ = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    p = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        p.goto(url)
        p.wait_for_selector("#account-btn:not([hidden])")
        assert p.get_by_role("button", name="Sign in", exact=True).count() == 1  # only an icon shows, but it's named
        p.click("#account-btn")
        p.wait_for_selector("#signin-dialog[open]")
        assert p.evaluate("document.getElementById('signin-dialog').matches(':modal')")
        box = p.locator("#signin-dialog").bounding_box()
        p.mouse.click(195, box["y"] + 20)  # on the sheet: stays
        assert p.is_visible("#signin-dialog")
        p.mouse.move(195, box["y"] + 20)  # a drag from the sheet onto the backdrop: stays
        p.mouse.down()
        p.mouse.move(195, 120, steps=4)
        p.mouse.up()
        p.wait_for_timeout(200)
        assert p.is_visible("#signin-dialog")
        p.mouse.click(195, 120)  # above it, on the backdrop
        p.wait_for_selector("#signin-dialog", state="hidden")
    finally:
        p.close()


# Where the search box and the pause button are, what the status says, whether the bar fits, and how
# wide the narrowest of the parts that give way is (the product's name, the grid's, the counts), of
# those shown.
TOP_BAR = """() => {
    const at = (id) => document.getElementById(id).getBoundingClientRect(), bar = document.querySelector('.topbar');
    const parts = [...document.querySelectorAll('.brand-name, .grid-name, .capacity')].filter((e) => e.offsetParent && e.textContent);
    return { jump: [at('jump-btn').left, at('jump-btn').width], pause: at('pause-btn').left,
             fresh: document.getElementById('fresh-text').textContent, cut: Math.min(...parts.map((e) => e.clientWidth)),
             fits: bar.scrollWidth <= bar.clientWidth && at('theme-btn').right <= innerWidth
               && document.querySelector('.mark').getBoundingClientRect().right <= at('verdict-chip').left };
}"""


def test_the_search_box_holds_still_whatever_the_top_bar_says(grid, browser, monkeypatch):
    """At widths with room to spare and without, open access and with sign-in: the status beside it changes
    (the age ticks, every slot fills, pause, the server goes away) and the search box never moves.
    Within each state, nor does the pause button. Short of room, the error is left out (its title has it),
    so nothing that gives way is cut to a letter or two."""
    url, path = grid
    widths = (800, 1000, 1100, 1280, 1440)
    monkeypatch.setattr(grid_app, "GRID_NAME", "playwright-grid-production-eu")  # as long as it shows
    for token in ("", "s3cret"):
        monkeypatch.setattr(grid_app, "ADMIN_TOKEN", token)
        p = browser.new_page(viewport={"width": 1440, "height": 800})
        try:
            p.clock.install()  # so the status can show any age without waiting for it
            p.goto(url)
            p.wait_for_selector("#freshness.f-ok")
            p.wait_for_selector("#account-btn:not([hidden])")  # the account controls beside it are in place
            box, pause, said = {}, {}, set()

            def look(state):
                for w in widths:
                    p.set_viewport_size({"width": w, "height": 800})
                    m = p.evaluate(TOP_BAR)
                    assert m["fits"] and m["cut"] >= 24, (token, state, w, m)
                    box.setdefault(w, set()).add(tuple(m["jump"]))
                    pause.setdefault((state, w), set()).add(m["pause"])
                    said.add(m["fresh"])

            def age(seconds):  # the age the status shows, from the next tick
                before = p.text_content("#fresh-text")
                p.clock.fast_forward(seconds * 1000 + 1000)
                p.wait_for_function("(t) => document.getElementById('fresh-text').textContent !== t", arg=before)

            for _ in range(8):
                look("live")
                p.clock.fast_forward(300)
                p.wait_for_timeout(20)
            run(path, "UPDATE portUsage SET inUse = 1")  # every slot: the chip says "Full", the counts gain digits
            refresh_now(p)
            p.wait_for_selector("#verdict-chip.t-warn >> text=Full")
            look("full")
            p.click("#pause-btn")
            p.wait_for_selector("#freshness.f-paused")
            for step in (0, 9, 1, 50, 3480):  # "1s old" to "59m 8s old"
                age(step)
                look("paused")
            p.route("**/api/status**", lambda r: r.abort())
            p.click("#pause-btn")
            p.wait_for_selector("#freshness[title^='Last error']")
            for step in (31, 600, 2900):  # "31s" to "58m 52s"
                age(step)
                p.wait_for_selector("#freshness.f-bad")
                look("stale")
            run(path, "UPDATE portUsage SET inUse = 0")
        finally:
            p.close()
        assert any(s.startswith("Live · ") for s in said) and any(s.startswith("Paused · 59m") for s in said), said
        assert any("Grid UI server unreachable · " in s for s in said), said
        assert all(len(v) == 1 for v in box.values()), (token, box)
        assert all(len(v) == 1 for v in pause.values()), (token, pause)
        assert {w: next(iter(v))[1] for w, v in box.items()} == {800: 200, 1000: 200, 1100: 260, 1280: 320, 1440: 320}


def test_sessions_table_scrolls_instead_of_clipping(grid, browser):
    url, _ = grid
    for width, cards in ((800, False), (900, False), (600, True)):
        p = browser.new_page(viewport={"width": width, "height": 800})
        p.goto(url + "#/sessions")
        p.wait_for_selector("#session-rows tr")
        panel = p.evaluate("""() => {
            const el = document.querySelector('#sessions-panel');
            const last = el.querySelector('#session-rows tr .cell-state .pill');
            el.scrollLeft = el.scrollWidth;
            const r = last.getBoundingClientRect(), box = el.getBoundingClientRect();
            return { overflow: getComputedStyle(el).overflowX, fits: r.left >= box.left && r.right <= box.right + 0.5,
                     pageScroll: document.documentElement.scrollWidth > innerWidth };
        }""")
        p.close()
        assert panel["fits"], (width, panel)  # the last column can be scrolled into view inside the panel
        assert not panel["pageScroll"], (width, panel)
        assert (panel["overflow"] == "hidden") == cards, (width, panel)


def test_sessions_have_no_per_row_release_and_rows_open_the_inspector(grid, page):
    url, path = grid
    page.goto(url + "#/sessions")
    page.wait_for_selector("#session-rows tr")
    assert page.locator("#session-rows [data-act=release], #session-rows button:has-text('Release')").count() == 0
    rows = page.eval_on_selector_all("#session-rows tr", "rs => rs.map(r => r.querySelector('.cell-subject a').textContent)")
    assert rows[0] == "pw-static.lab.local:3000"  # stale first
    # Who claimed each slot: the hub for /connect sessions; the demo's stale one came from elsewhere.
    claims = page.eval_on_selector_all("#session-rows tr .cell-claim", "cs => cs.map(c => c.textContent.trim())")
    assert claims[0] == "Another client" and set(claims[1:]) == {"Hub"}
    assert page.text_content("#view-sessions thead").count("Claimed by") == 1
    page.click("#session-filters [data-filter=stale]")
    page.wait_for_function("() => location.hash.indexOf('filter=stale') !== -1")
    assert page.locator("#session-rows tr").count() == 1
    page.click("#session-rows tr .cell-time")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "pw-static.lab.local:3000"
    assert "/session/" in page.evaluate("location.hash")
    page.keyboard.press("Escape")
    page.wait_for_selector("#inspector", state="hidden")
    assert page.evaluate("document.activeElement.closest('tr') !== null")  # focus back on the row


def test_heartbeats_update_last_seen_without_rebuilding_the_page(grid, page):
    url, path = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    page.evaluate("""() => {
        window.__mutations = 0;
        for (const id of ['mx-body', 'att-list', 'tl-lanes', 'util-list']) {
            new MutationObserver(() => window.__mutations++).observe(document.getElementById(id), { childList: true });
        }
    }""")
    page.goto(url + "#/sessions")
    page.wait_for_selector("#session-rows tr")
    page.evaluate("""() => new MutationObserver(() => window.__mutations++).observe(document.getElementById('session-rows'), { childList: true })""")
    page.goto(url + "#/overview")
    run(path, "UPDATE nodes SET lastSeen = '2030-01-01 00:00:00' WHERE name = 'WIN-BUILD-01'")
    refresh_now(page)
    page.wait_for_function("""() => document.querySelector('[data-seen="WIN-BUILD-01"]').dataset.ago === '2030-01-01T00:00:00Z'""")
    refresh_now(page)
    assert page.evaluate("window.__mutations") == 0


TILES = r"""els => Object.fromEntries(els.map(e => [e.dataset.stat,
    [e.querySelector('[data-v]').textContent, e.querySelector('[data-s]').textContent, e.className]]))"""
UTIL = r"""rows => rows.map(r => [r.dataset.key, r.querySelector('.util-n').textContent,
    r.querySelector('[data-meter]').style.getPropertyValue('--p')])"""
# Whether each line shown in the middle of the donut stays inside its hole: the ring (r 17, width 5 in
# a 42 box) leaves a hole 29/42 of the width across.
IN_HOLE = """() => {
    const r = document.querySelector('.donut-wrap').getBoundingClientRect(), edge = r.width * 6.5 / 42;
    return [...document.querySelectorAll('.donut-center > *')].filter((e) => e.offsetParent)
      .map((e) => e.getBoundingClientRect()).map((b) => b.left >= r.left + edge && b.right <= r.right - edge);
}"""


def test_the_summary_counts_what_the_verdict_counts_and_follows_the_data(grid, page):
    url, path = grid
    # A running slot whose failures reached the limit is running, not failing: neither the verdict nor the summary counts it.
    run(path, "UPDATE portUsage SET failCount = 3 WHERE id = ?", (slot_id(path, "WIN-BUILD-01", 3),))
    page.goto(url)
    page.wait_for_selector("#summary:not([hidden]) .util-row")
    assert page.evaluate("!!(document.getElementById('summary').compareDocumentPosition(document.getElementById('attention')) & 4)")
    assert "2 slots failing, 1 slot unhealthy, 1 stale session, 1 node offline. 7 of 21 slots free · 8 running" in page.text_content("#verdict-sub")
    t = page.eval_on_selector_all(".stat", TILES)
    assert t["nodes"][:2] == ["5", "1 offline"] and "t-warn" not in t["nodes"][2]  # the line is marked, not the count
    assert t["slots"][:2] == ["21", "across 5 nodes"]
    assert t["sessions"][:2] == ["8", "38% utilization"]
    assert t["free"][:2] == ["7", "33% available"] and "t-" not in t["free"][2]
    assert t["problems"][:2] == ["3", "2 failing · 1 unhealthy"] and "t-bad" in t["problems"][2]  # the slots "Needs you" lists
    assert t["stale"][:2] == ["1", "held too long or hub gone"] and "t-warn" in t["stale"][2]
    assert page.locator("#att-list li").count() == 5  # 3 slots, the stale session and the offline node
    assert [page.text_content(s) for s in ("#cap-pct", "#cap-sub", "#cap-busy", "#cap-free", "#cap-out")] == ["38%", "8 of 21 in use", "8", "7", "6"]
    assert page.evaluate(IN_HOLE) == [True, True]
    page.eval_on_selector("#cap-sub", "el => { el.innerHTML = '<span>1200 of 4000</span> <span>in use</span>'; }")
    assert page.evaluate(IN_HOLE) == [True, True]  # a long count wraps instead of running over the ring
    dash = lambda cls: [float(x) for x in page.get_attribute(".donut ." + cls, "stroke-dasharray").split()] + \
        [float(page.get_attribute(".donut ." + cls, "stroke-dashoffset"))]
    assert dash("d-busy") == pytest.approx([800 / 21, 100 - 800 / 21, 0])
    assert dash("d-out") == pytest.approx([600 / 21, 100 - 600 / 21, -800 / 21])  # starts where "in use" ends
    assert page.eval_on_selector_all(".util-row", UTIL) == [
        ["MAC-QA-02", "offline", "0"], ["runner-7f3a9c", "3/6", "0.5"], ["runner-b81e04", "1/4", "0.25"],
        ["WIN-BUILD-01", "2/4", "0.5"], ["pw-static.lab.local", "2/4", "0.5"]]
    # Cut in the middle or not, a node's name reads, copies and finds as one.
    names = page.eval_on_selector_all(".util-host", "els => els.map((a) => [a.title, a.innerText, a.getAttribute('aria-label')])")
    assert names and all(n[0] == n[1] == n[2] for n in names), names
    assert 'link "WIN-BUILD-01"' in page.locator("#util-list").aria_snapshot()

    # A run finishes and the offline node comes back: every number follows on the next poll.
    run(path, "UPDATE portUsage SET inUse = 0 WHERE id = ?", (slot_id(path, "WIN-BUILD-01", 1),))
    run(path, "UPDATE nodes SET online = 1, lastSeen = '2030-01-01 00:00:00' WHERE name = 'MAC-QA-02'")
    run(path, "UPDATE portStatus SET PortStatus = 1 WHERE PortServer = 'MAC-QA-02'")  # and its slots pass health checks
    refresh_now(page)
    page.wait_for_function("() => document.getElementById('cap-busy').textContent === '7'")
    t = page.eval_on_selector_all(".stat", TILES)
    assert t["nodes"][:2] == ["5", "none offline"] and t["sessions"][:2] == ["7", "33% utilization"]
    assert t["free"][:2] == ["11", "52% available"] and t["problems"][0] == "3"
    assert [page.text_content(s) for s in ("#cap-pct", "#cap-free", "#cap-out")] == ["33%", "11", "3"]
    util = dict((r[0], r[1:]) for r in page.eval_on_selector_all(".util-row", UTIL))
    assert util["MAC-QA-02"] == ["0/3", "0"] and util["WIN-BUILD-01"] == ["1/4", "0.25"]

    # A node's link keeps keyboard focus as its row changes under it, and gets it back from the inspector.
    take = ("UPDATE portUsage SET inUse = ? WHERE id = (SELECT MIN(u.id) FROM portUsage u JOIN portStatus s ON s.id = u.id"
            " WHERE s.PortServer = 'runner-7f3a9c' AND u.inUse = ?)")
    count = "(n) => document.querySelector('.util-row[data-key=\"runner-7f3a9c\"] .util-n').textContent === n"
    focus = "document.activeElement.dataset.fk"
    page.focus('[data-fk="util-runner-7f3a9c"]')
    # Its focus ring (2px, 2px out) has room inside the list, which scrolls and so clips.
    assert page.evaluate("""() => { const a = document.activeElement.getBoundingClientRect(), l = document.getElementById('util-list').getBoundingClientRect();
        return a.left - 4 >= l.left && a.right + 4 <= l.right; }""")
    run(path, take, (1, 0))
    refresh_now(page)
    page.wait_for_function(count, arg="4/6")
    assert page.evaluate(focus) == "util-runner-7f3a9c"
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "runner-7f3a9c"
    run(path, take, (0, 1))
    refresh_now(page)
    page.wait_for_function(count, arg="3/6")
    page.keyboard.press("Escape")
    page.wait_for_selector("#inspector", state="hidden")
    assert page.evaluate(focus) == "util-runner-7f3a9c"

    page.click(".util-host >> text=runner-7f3a9c")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "runner-7f3a9c"
    page.keyboard.press("Escape")
    page.click('[data-stat="stale"]')
    page.wait_for_selector("#view-sessions:not([hidden])")
    assert page.evaluate("location.hash") == "#/sessions?filter=stale"
    page.goto(url + "#/overview")
    page.click('[data-stat="sessions"]')
    page.wait_for_function("() => location.hash === '#/sessions'")
    # Every slot taken: none free is a problem, so that count is coloured.
    page.goto(url + "#/overview")
    run(path, "UPDATE portUsage SET inUse = 1")
    refresh_now(page)
    page.wait_for_function("() => document.querySelector('[data-stat=free] [data-v]').textContent === '0'")
    t = page.eval_on_selector_all(".stat", TILES)
    assert t["free"][:2] == ["0", "0% available"] and "t-warn" in t["free"][2]
    # Just wide enough for the card beside the tiles, the counts' labels still fit their cells.
    page.set_viewport_size({"width": 1100, "height": 900})
    page.wait_for_selector("#summary:not([hidden]) .util-row")
    assert page.eval_on_selector_all(".cap-count dt", "els => els.map((e) => e.scrollWidth <= e.clientWidth)") == [True] * 3
    assert page.errors == []


# Per node in the summary: the end of its name's first part, whether that shows (with what follows it
# cut short inside the box), and whether the name still reads as one.
ENDS = """els => els.map((a) => {
    const tail = a.querySelector('.util-tail'), end = tail.getBoundingClientRect(), box = a.getBoundingClientRect();
    const rest = a.querySelector('.util-rest');
    return [tail.textContent, end.width > 0 && end.right <= box.right + 0.5 && (!rest || rest.getBoundingClientRect().right <= box.right + 0.5),
            a.innerText === a.title];
})"""


def test_nodes_whose_names_share_a_domain_stay_apart_in_the_summary(grid, browser):
    """Kept: the end of each name's first part, whole characters (an emoji family is one), and an IP
    address's end."""
    url, path = grid
    family = "ab\U0001F468‍\U0001F469‍\U0001F467-xyz1"
    for table, column in (("nodes", "name"), ("portStatus", "PortServer")):
        run(path, "UPDATE {0} SET {1} = {1} || '.qa.corp.example.com' WHERE {1} IN ('MAC-QA-02', 'WIN-BUILD-01', 'runner-7f3a9c')".format(table, column))
        run(path, "UPDATE {0} SET {1} = ? WHERE {1} = 'runner-b81e04'".format(table, column), (family,))
        run(path, "UPDATE {0} SET {1} = '10.20.30.40' WHERE {1} = 'pw-static.lab.local'".format(table, column))
    tails = {"-QA-02", "7f3a9c", family[2:], "ILD-01", ".30.40"}
    p = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        p.goto(url)
        p.wait_for_selector("#summary:not([hidden]) .util-row")
        for width in (1440, 1100, 390):
            p.set_viewport_size({"width": width, "height": 900})
            p.wait_for_timeout(100)
            ends = p.eval_on_selector_all(".util-host", ENDS)
            # The domain gives way: what tells the nodes apart shows.
            assert {e[0] for e in ends} == tails and all(e[1] and e[2] for e in ends), (width, ends)
    finally:
        p.close()


def test_the_summary_fits_a_phone(grid, browser):
    url, _ = grid
    p = browser.new_page(viewport={"width": 390, "height": 844})
    try:
        p.goto(url)
        p.wait_for_selector("#summary:not([hidden]) .util-row")
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
        columns = p.evaluate("getComputedStyle(document.querySelector('.summary-grid')).gridTemplateColumns.split(' ').length")
        inside = p.evaluate(IN_HOLE)
        # A node's name that doesn't fit is cut in the middle: its end, where names like it differ, shows.
        ends = p.eval_on_selector_all(".util-host", ENDS)
        named = [p.get_by_role("button", name=n, exact=True).count() for n in ("Unprotected", "Open access")]  # only icons show
        p.set_viewport_size({"width": 320, "height": 640})  # the smallest phones: the top bar still fits ("Degraded · 5")
        top = p.evaluate(TOP_BAR)
        p.set_viewport_size({"width": 280, "height": 640})  # and folded ones: the verdict's words give way
        tiny = p.evaluate(TOP_BAR)
    finally:
        p.close()
    assert top["fits"], top
    assert tiny["fits"], tiny
    assert named == [1, 1]
    assert columns == 2
    assert inside and all(inside)
    assert ends and all(e[1] and e[2] for e in ends), ends


def test_in_high_contrast_the_summary_keeps_the_donuts_colours_for_its_key_and_bars(grid, browser):
    url, _ = grid
    p = browser.new_page(viewport={"width": 1280, "height": 900}, forced_colors="active")
    try:
        p.goto(url)
        p.wait_for_selector("#summary:not([hidden]) .util-row")
        colours = p.evaluate("""() => {
            const css = (sel, prop) => getComputedStyle(document.querySelector(sel))[prop];
            return [css('.donut .d-busy', 'stroke'), css('.sw-busy', 'backgroundColor'), css('.util-fill', 'backgroundColor')];
        }""")
    finally:
        p.close()
    assert colours[0] == colours[1] == colours[2], colours


def test_empty_grid_explains_how_to_add_nodes(tmp_path, monkeypatch, page):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    server = _serve(monkeypatch, path)
    try:
        page.goto("http://127.0.0.1:{}/".format(server.server_port))
        page.wait_for_selector(".steps")
        text = page.text_content(".steps")
        h1 = page.text_content("#ov-h1")
        commands = page.eval_on_selector_all("[data-copy]", "els => els.map(e => e.dataset.copy)")
        panel = page.text_content("#first-run")
        summary_hidden = page.is_hidden("#summary")
    finally:
        server.shutdown()
    assert h1 == "No nodes yet" and summary_hidden
    assert "docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2" in text
    assert "node gateway/agent.js --hub ws://127.0.0.1:4500 --slots 4" in text and "README" in text
    assert "ws://127.0.0.1:4500/connect" in commands and "/connect/" not in panel  # checks: one address, no slot
    assert commands and not any("<" in c or ">" in c.replace(">>", "") for c in commands)  # runnable, no placeholders
    assert "Available 0" not in panel and "Ready for new checks" not in panel


def test_ephemeral_nodes_and_failure_cooldowns_are_shown(grid, page):
    url, path = grid
    run(path, "UPDATE nodes SET online = 0 WHERE name = 'runner-7f3a9c'")
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    rows = {r["name"]: r for r in page.eval_on_selector_all(".mx-row[data-key]", """rows => rows.map(r => ({
        name: r.dataset.key,
        tags: Array.from(r.querySelectorAll('.tag')).map(t => t.textContent + ' ' + (t.title || '')).join(' | ') }))""")}
    assert "ephemeral" in rows["runner-7f3a9c"]["tags"] and "removed automatically" in rows["runner-7f3a9c"]["tags"]
    assert "ephemeral" in rows["runner-b81e04"]["tags"] and "removed automatically" not in rows["runner-b81e04"]["tags"]
    assert rows["WIN-BUILD-01"]["tags"] == ""

    cell = page.locator('#matrix .cell[aria-label*="retry in"]').first  # runner-b81e04 slot 3 failed 90 s ago
    assert "retry in 4m" in cell.get_attribute("aria-label")
    assert "s-cooling" in cell.get_attribute("class")
    cell.click()
    page.wait_for_selector("#inspector:not([hidden])")
    body = page.text_content("#insp-body")
    assert "(in 4m)" in body and "4 times in a row (limit 3)" in body
    assert "passes health checks, so suspect the gateway path" in body


def test_inspector_closes_on_outside_clicks_but_not_on_its_own_margin_or_a_drag(grid, page):
    url, _ = grid
    page.goto(url)
    page.wait_for_selector("#matrix .cell.s-busy")
    page.locator("#matrix .cell.s-busy").first.click()
    insp = page.locator("#inspector")
    page.wait_for_selector("#inspector:not([hidden])")
    box = insp.bounding_box()
    page.mouse.click(box["x"] + 10, box["y"] + box["height"] - 10)  # its own padding
    assert insp.is_visible()

    code = page.locator("#repro-code").bounding_box()  # drag-select the snippet out past its end
    page.mouse.move(code["x"] + 2, code["y"] + code["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] - 200, code["y"] + code["height"] / 2)
    page.mouse.up()
    assert insp.is_visible()

    page.evaluate("getSelection().removeAllRanges()")
    head = page.locator(".ov-head").bounding_box()
    page.mouse.click(head["x"] + head["width"] - 20, head["y"] + 4)  # empty space in the page
    page.wait_for_selector("#inspector", state="hidden")
    assert page.evaluate("document.activeElement.classList.contains('cell')")  # focus returns to the cell


def test_the_inspector_is_addressable_resizable_and_keeps_history(grid, page):
    url, path = grid
    page.goto(url + "#/node/WIN-BUILD-01")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "WIN-BUILD-01"
    assert page.get_attribute("#inspector", "role") is None and page.evaluate(
        "document.getElementById('inspector').tagName") == "ASIDE"  # a complementary landmark, not a modal
    page.locator(".insp-mini .cell").first.click()  # one object to another replaces, it doesn't stack
    page.wait_for_function("() => location.hash.indexOf('#/slot/') === 0")
    assert page.text_content("#insp-title") == "WIN-BUILD-01 / slot 1"

    page.goto(url + "#/overview")
    page.wait_for_selector("#inspector", state="hidden")
    page.locator(".mx-row[data-key='runner-7f3a9c'] .cell").first.click()
    page.wait_for_function("() => location.hash.indexOf('#/slot/') === 0")
    page.go_back()
    page.wait_for_function("() => location.hash === '#/overview'")
    page.wait_for_selector("#inspector", state="hidden")
    page.go_forward()
    page.wait_for_function("() => location.hash.indexOf('#/slot/') === 0")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "runner-7f3a9c / slot 1"

    width = page.evaluate("document.getElementById('inspector').getBoundingClientRect().width")
    page.focus("#insp-handle")
    page.keyboard.press("ArrowLeft")
    page.keyboard.press("ArrowLeft")
    assert page.evaluate("document.getElementById('inspector').getBoundingClientRect().width") == width + 32
    assert page.evaluate("localStorage.getItem('gridInspectorWidth')") == str(int(width + 32))
    page.keyboard.press("End")
    assert page.evaluate("document.getElementById('inspector').getBoundingClientRect().width") == 640


def test_legacy_hashes_still_work(grid, page):
    url, _ = grid
    page.goto(url + "#sessions")
    page.wait_for_selector("#view-sessions:not([hidden])")
    assert page.evaluate("location.hash") == "#/sessions"
    page.goto(url + "#access")
    page.wait_for_selector("#view-access:not([hidden])")


def test_hostile_names_and_ports_are_escaped_everywhere(grid, page):
    url, path = grid
    port = '"><img src=x onerror="window.__xss=1">'
    name = '<img src=x onerror=window.__xss=2>'
    run(path, "UPDATE portStatus SET PortNumber = ? WHERE PortServer = 'pw-static.lab.local' AND PortNumber = 3000", (port,))
    for table, column in (("portStatus", "PortServer"), ("nodes", "name")):
        run(path, "UPDATE {0} SET {1} = ? WHERE {1} = 'runner-b81e04'".format(table, column), (name,))
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    run(path, "UPDATE portUsage SET failCount = failCount + 1 WHERE id IN (SELECT id FROM portStatus WHERE PortServer = ?)", (name,))
    refresh_now(page)
    page.wait_for_timeout(1600)

    attention = page.text_content("#att-list")
    assert port in attention and name in attention  # the stale session and the failing slot
    assert name in page.text_content("#util-list")
    page.goto(url + "#/node/" + urllib.parse.quote(name, safe=""))
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == name
    page.goto(url + "#/activity")
    page.wait_for_selector("#act-rows tr")
    assert name in page.text_content("#act-rows")
    page.goto(url + "#/sessions")
    page.wait_for_selector("#session-rows tr")
    sessions = page.text_content("#session-rows")
    assert port in sessions and name in sessions
    page.locator("#session-rows tr", has_text=port).locator(".cell-time").click()
    page.wait_for_selector("#inspector:not([hidden])")
    assert port in page.text_content("#insp-title")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__xss") is None
    assert page.evaluate("document.querySelectorAll('img').length") == 0
