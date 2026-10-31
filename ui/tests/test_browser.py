"""grid.js in a real browser, against the sqlite demo data.

Skipped unless the playwright package and its Chromium are installed:
    pip install playwright && python -m playwright install chromium
"""
import sqlite3
import threading

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
    yield p
    p.close()


def run(path, sql, params=()):
    with sqlite3.connect(path) as con:
        return con.execute(sql, params).fetchall()


def release_first_session(page):
    page.click("#session-rows [data-release]")
    page.click("#confirm-ok")


def test_a_refused_action_shows_an_error_and_is_not_retried(grid, page):
    url, _ = grid
    posts = []

    def refuse(route):
        posts.append(route.request.url)
        route.fulfill(status=403, content_type="text/html", body="<h1>Forbidden by the proxy</h1>")

    page.route("**/api/slots/*/release", refuse)
    page.goto(url + "#sessions")
    release_first_session(page)
    page.wait_for_selector("#toast.bad")
    page.wait_for_timeout(500)
    assert len(posts) == 1
    assert "HTTP 403" in page.text_content("#toast")
    assert not page.is_visible("#token-dialog")


def test_a_token_set_after_the_page_loaded_is_asked_for(grid, page, monkeypatch):
    url, path = grid
    posts = []
    page.on("request", lambda r: posts.append(r.headers.get("x-grid-token")) if r.method == "POST" else None)
    page.goto(url + "#sessions")  # rendered without a token
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    busy = run(path, "SELECT COUNT(*) FROM portUsage WHERE inUse = 1")[0][0]

    release_first_session(page)
    page.wait_for_selector("#token-dialog[open]")
    page.fill("#token-input", "wrong")
    page.press("#token-input", "Enter")  # Enter submits Continue, not Cancel
    page.wait_for_function("document.getElementById('token-text').textContent.indexOf('rejected') !== -1")
    page.wait_for_selector("#token-dialog[open]")
    page.click("#token-dialog [data-dismiss]")
    page.wait_for_timeout(300)
    assert posts == ["", "wrong"]
    assert not page.is_visible("#token-dialog")

    release_first_session(page)
    page.wait_for_selector("#token-dialog[open]")  # asks first now: no silent empty-token POST
    page.fill("#token-input", "s3cret")
    page.press("#token-input", "Enter")
    page.wait_for_selector("#toast:not(.bad)")
    assert "released" in page.text_content("#toast")
    assert posts == ["", "wrong", "s3cret"]
    assert run(path, "SELECT COUNT(*) FROM portUsage WHERE inUse = 1")[0][0] == busy - 1


def test_sessions_table_scrolls_instead_of_clipping(grid, browser):
    url, _ = grid
    for width, cards in ((800, False), (900, False), (600, True)):
        p = browser.new_page(viewport={"width": width, "height": 800})
        p.goto(url + "#sessions")
        p.wait_for_selector("#session-rows tr")
        panel = p.evaluate("""() => {
            const el = document.querySelector('.table-panel');
            const btn = el.querySelector('[data-release]');
            el.scrollLeft = el.scrollWidth;
            const r = btn.getBoundingClientRect(), box = el.getBoundingClientRect();
            return { overflow: getComputedStyle(el).overflowX, fits: r.left >= box.left && r.right <= box.right + 0.5,
                     pageScroll: document.documentElement.scrollWidth > innerWidth };
        }""")
        p.close()
        assert panel["fits"], (width, panel)  # Release can be scrolled into view inside the panel
        assert not panel["pageScroll"], (width, panel)
        assert (panel["overflow"] == "hidden") == cards, (width, panel)


def test_heartbeats_update_last_seen_without_rebuilding_the_page(grid, page):
    url, path = grid
    page.goto(url)
    page.wait_for_selector(".node")
    page.evaluate("""() => {
        window.__mutations = 0;
        new MutationObserver(() => window.__mutations++).observe(document.getElementById('nodes'), { childList: true });
        new MutationObserver(() => window.__mutations++).observe(document.getElementById('session-rows'), { childList: true });
    }""")
    run(path, "UPDATE nodes SET lastSeen = '2030-01-01 00:00:00' WHERE name = 'WIN-BUILD-01'")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # refresh now
    page.wait_for_function("""document.querySelector('[data-seen="WIN-BUILD-01"]').dataset.ago === '2030-01-01T00:00:00Z'""")
    assert page.evaluate("window.__mutations") == 0


def test_empty_grid_explains_how_to_add_nodes(tmp_path, monkeypatch, page):
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    server = _serve(monkeypatch, path)
    try:
        page.goto("http://127.0.0.1:{}/".format(server.server_port))
        page.wait_for_selector(".steps")
        text = page.text_content(".steps")
    finally:
        server.shutdown()
    assert "docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2" in text
    assert "--token <GATEWAY_AGENT_TOKEN>" in text and "README" in text


def test_ephemeral_nodes_and_failure_cooldowns_are_shown(grid, page):
    url, path = grid
    run(path, "UPDATE nodes SET online = 0 WHERE name = 'runner-7f3a9c'")
    page.goto(url)
    page.wait_for_selector(".node")
    cards = {c["name"]: c for c in page.eval_on_selector_all(".node", """cards => cards.map(c => ({
        name: c.querySelector('.node-name').textContent,
        tag: !!c.querySelector('.node-head .tag'),
        note: (c.querySelector('.node-note') || {}).textContent || '' }))""")}
    assert cards["runner-7f3a9c"]["tag"] and "removed automatically" in cards["runner-7f3a9c"]["note"]
    assert cards["runner-b81e04"]["tag"] and not cards["runner-b81e04"]["note"]  # online: no note
    assert not cards["WIN-BUILD-01"]["tag"]

    chip = page.locator('.slot[title*="retry in"]').first  # runner-b81e04 slot 3 failed 90 s ago
    assert "retry in 4m" in chip.get_attribute("title")
    chip.click()
    assert "retry in 4m" in page.text_content("#slot-details")


def test_slot_dialog_closes_on_the_backdrop_only(grid, page):
    url, _ = grid
    page.goto(url)
    page.click(".slot.busy")
    dialog = page.locator("#slot-dialog")
    box = dialog.bounding_box()
    page.mouse.click(box["x"] + 6, box["y"] + box["height"] / 2)  # the dialog's own margin
    assert dialog.is_visible()

    code = page.locator("#slot-details code").bounding_box()  # drag-select the endpoint out past its end
    page.mouse.move(code["x"] + 2, code["y"] + code["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 8, code["y"] + code["height"] / 2)
    page.mouse.up()
    assert dialog.is_visible()

    page.mouse.click(5, 5)  # the backdrop
    assert not dialog.is_visible()


def test_static_host_ports_are_escaped(grid, page):
    url, path = grid
    run(path, "UPDATE portStatus SET PortNumber = '\"><img src=x onerror=\"window.__xss=1\">' "
              "WHERE PortServer = 'pw-static.lab.local' AND PortNumber = 3000")
    page.goto(url)
    page.wait_for_selector(".node")
    page.wait_for_timeout(300)
    assert page.evaluate("window.__xss") is None
