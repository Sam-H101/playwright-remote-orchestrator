"""The Grid UI in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed:
    pip install playwright && python -m playwright install chromium
"""
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
    with sqlite3.connect(path) as con:
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
        for (const id of ['mx-body', 'att-list', 'tl-lanes']) {
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
    finally:
        server.shutdown()
    assert h1 == "No nodes yet"
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
