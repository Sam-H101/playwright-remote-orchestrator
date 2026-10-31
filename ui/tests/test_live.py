"""The flight recorder and the live views in a real browser: the pure diff engine and formatters,
short runs between polls, bursts, the palette, keyboard, inspector snippets and the edge states.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import datetime
import sqlite3
import time

import pytest

from ui import app as grid_app, demo

pytest.importorskip("playwright.sync_api")

from ui.tests.test_browser import _serve, browser, grid, page, refresh_now, run  # noqa: E402,F401  (fixtures)

# Seven free slots a check could claim in the demo data.
FREE = [("WIN-BUILD-01", 2), ("WIN-BUILD-01", 4), ("runner-7f3a9c", 3), ("runner-7f3a9c", 4), ("runner-7f3a9c", 6),
        ("runner-b81e04", 4), ("pw-static.lab.local", 3002)]


def utc(seconds=0):
    t = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) + datetime.timedelta(seconds=seconds)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def free_ids(path):
    return [run(path, "SELECT id FROM portStatus WHERE PortServer = ? AND PortNumber = ?", s)[0][0] for s in FREE]


def module(page, url, name):
    return "import('{}static/js/{}')".format(url, name)


UNIT = r"""async ([liveUrl, formatUrl]) => {
  const L = await import(liveUrl), F = await import(formatUrl);
  const out = {};
  const slot = (id, o) => Object.assign({ id, port: id, busy: false, fails: 0, healthy: true, state: "free", last_run: null, retry_at: null }, o);
  const node = (host, slots, o) => Object.assign({ host, kind: "gateway", online: true, status: "ONLINE", total: slots.length, busy: 0, slots }, o);
  const snap = (gen, nodes) => ({ generated: gen, nodes });
  const T = "2026-10-31T10:00:00Z", T4 = "2026-10-31T10:00:04Z", G0 = "2026-10-31T10:00:01Z", G1 = "2026-10-31T10:00:06Z";

  const claim = L.diff(snap(G0, [node("n", [slot(1)])]), snap(G1, [node("n", [slot(1, { busy: true, state: "busy", last_run: T })])]));
  out.claim = claim.map((e) => [e.type, e.kind, e.subject, e.start === Date.parse(T)]);

  const fin = L.diff(snap(G0, [node("n", [slot(1, { busy: true, state: "busy", last_run: T })])]), snap(G1, [node("n", [slot(1, { last_run: T4 })])]));
  out.finish = fin.map((e) => [e.type, e.dur, !!e.approx, !!e.failed]);

  const between = L.diff(snap(G0, [node("n", [slot(1, { last_run: T })])]), snap(G1, [node("n", [slot(1, { last_run: T4 })])]));
  out.between = between.map((e) => [e.type, e.approx, e.start, e.end === Date.parse(T4), L.describe(e)]);

  const failed = L.diff(snap(G0, [node("n", [slot(1, { busy: true, state: "busy", last_run: T, fails: 2 })])]),
    snap(G1, [node("n", [slot(1, { last_run: T4, fails: 3, state: "failing", retry_at: "2026-10-31T10:05:04Z" })])]));
  out.failed = failed.map((e) => [e.type, !!e.failed, e.fails || null, L.describe(e)]);

  const reset = L.diff(snap(G0, [node("n", [slot(1, { fails: 4, state: "failing" })])]), snap(G1, [node("n", [slot(1)])]),
    { slots: new Map([[1, "reset"]]), nodes: new Map() });
  out.reset = reset.map((e) => [e.type, e.kind, e.was, e.by, L.describe(e)]);

  const off = L.diff(snap(G0, [node("n", [slot(1), slot(2)])]),
    snap(G1, [node("n", [slot(1, { state: "offline" }), slot(2, { state: "offline" })], { online: false, status: "OFFLINE", last_seen_s: 70 })]));
  out.offline = off.map((e) => e.type);

  const health = L.diff(snap(G0, [node("n", [slot(1)])]), snap(G1, [node("n", [slot(1, { healthy: false, state: "unhealthy" })])]));
  out.health = health.map((e) => [e.type, L.describe(e)]);

  const nodes = L.diff(snap(G0, [node("a", [slot(1)]), node("gone", [slot(2)], { online: false })]),
    snap(G1, [node("a", [slot(1)]), node("new", [slot(3)])]), { slots: new Map(), nodes: new Map([["gone", "forget"]]) });
  out.nodes = nodes.map((e) => [e.type, e.node, e.by || null]);

  const released = L.diff(snap(G0, [node("n", [slot(1, { busy: true, state: "busy", last_run: T })])]), snap(G1, [node("n", [slot(1, { last_run: T4 })])]),
    { slots: new Map([[1, "release"]]), nodes: new Map() });
  out.released = released.map((e) => [e.by, L.describe(e)]);

  const beat = L.diff(snap(G0, [node("n", [slot(1)], { last_seen: "a", last_seen_s: 1 })]), snap(G1, [node("n", [slot(1)], { last_seen: "b", last_seen_s: 6 })]));
  out.heartbeat = beat.length;
  out.none = L.diff(null, snap(G1, [])).length;

  const ends = [0, 300, 600, 900, 1200, 1500, 1900].map((d, i) => ({ type: "finished", t: 1e12 + d, dur: 4000 + (i === 0 ? 700 : 0), id: "x" + i }));
  out.bursts = L.bursts(ends.concat([{ type: "finished", t: 1e12 + 60000, dur: 1000 }])).map((b) => [b.count, Math.round(b.avg)]);

  const mem = new Map(), store = { get: (k) => mem.get(k) || null, set: (k, v) => mem.set(k, v) };
  const rec = new L.Recorder(store);
  const now = Date.now();
  rec.add(Array.from({ length: 520 }, (_, i) => ({ type: "claimed", t: now - 1000 + i })), now);
  rec.add([{ type: "claimed", t: now - 20 * 60e3 }], now);
  out.recorder = [rec.events.length, rec.dropped, JSON.parse(mem.get("gridRecorder")).events.length, new L.Recorder(store).events.length];

  out.format = {
    duration: [F.duration(3725), F.duration(42), F.duration(272), F.duration(null)],
    runLength: [F.runLength(4100), F.runLength(95000)],
    ago: [F.ago(3), F.ago(47 * 60)],
    inTime: [F.inTime(180), F.inTime(42), F.inTime(-1)],
    slotQuery: [F.parseSlotQuery("b81e04/3"), F.parseSlotQuery("b81e04 3"), F.parseSlotQuery("pw-static:3001"), F.parseSlotQuery("nothing")],
    hub: [F.publicHub({ gateway_url: "ws://hub:4500" }, "grid.lab"), F.publicHub({ gateway_url: "wss://hub" }, "::1"),
      F.publicHub({ gateway_url: "ws://hub:4500", public_gateway_url: "wss://grid.example.com/" }, "x")],
    repro: F.reproduce("ws://grid.lab:4500", { host: "runner-b81e04", kind: "gateway" }, { port: 3 }),
    reproStatic: F.reproduce("ws://grid.lab:4500", { host: "pw-static", kind: "static" }, { port: 3000 }),
    agent: [F.agentCommand("windows", "ws://h:4500", "pwga_x", "win-02"), F.agentCommand("linux", "ws://h:4500", "pwga_x", "win-02")],
    sentence: F.slotSentence({ host: "runner-b81e04", kind: "gateway" },
      { state: "failing", fails: 4, healthy: true, last_run: "2026-10-31T10:47:00Z", retry_at: "2026-10-31T10:52:00Z", port: 3 },
      { now: Date.parse("2026-10-31T10:49:00Z"), threshold: 3, cooldown: 300, staleAfter: 3600 }),
  };
  return out;
}"""


def test_diff_and_formatters_are_pure_and_right(grid, page):
    url, _ = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    out = page.evaluate(UNIT, [url + "static/js/live.js", url + "static/js/format.js"])

    assert out["claim"] == [["claimed", "claim", "n / 1", True]]
    assert out["finish"] == [["finished", 4000, False, False]]
    assert out["between"] == [["finished", True, None, True, "finished between updates (approx.)"]]
    assert out["failed"] == [["finished", True, None, "finished in 4.0 s · failures went up"],
                             ["failure", False, 3, "connect failure #3 · now failing"]]
    assert out["reset"] == [["reset", "admin", 4, "you", "you reset failures (was 4)"]]
    assert out["offline"] == ["node-offline"]  # the node says it; no line per slot
    assert out["health"] == [["state", "free → unhealthy"]]
    assert out["nodes"] == [["node-joined", "new", None], ["node-left", "gone", "you"]]
    assert out["released"] == [["you", "released by you after 4.0 s"]]
    assert out["heartbeat"] == 0 and out["none"] == 0
    assert out["bursts"] == [[7, 4100]]
    assert out["recorder"] == [500, 20, 500, 500]  # capped at 500, the overflow counted, old events expire

    f = out["format"]
    assert f["duration"] == ["1h 2m", "42s", "4m 32s", "–"]
    assert f["runLength"] == ["4.1 s", "1m 35s"]
    assert f["ago"] == ["just now", "47m ago"]
    assert f["inTime"] == ["in 3m", "in 42s", "now"]
    assert f["slotQuery"] == [{"node": "b81e04", "slot": "3"}, {"node": "b81e04", "slot": "3"}, {"node": "pw-static", "slot": "3001"}, None]
    assert f["hub"] == ["ws://grid.lab:4500", "wss://[::1]", "wss://grid.example.com"]
    r = f["repro"]
    assert "const browser = await chromium.connect(`${HUB}/connect/runner-b81e04/3?gateway_token=${process.env.GATEWAY_CLIENT_TOKEN}`);" in r["js"]
    assert "const HUB = 'ws://grid.lab:4500';" in r["js"]
    assert "p.chromium.connect(f\"{HUB}/connect/runner-b81e04/3?gateway_token={os.environ['GATEWAY_CLIENT_TOKEN']}\")" in r["python"]
    assert r["powershell"] == "$env:PW_WS = 'ws://grid.lab:4500/connect/runner-b81e04/3?gateway_token=' + $env:GATEWAY_CLIENT_TOKEN"
    assert r["bash"] == 'export PW_WS="ws://grid.lab:4500/connect/runner-b81e04/3?gateway_token=$GATEWAY_CLIENT_TOKEN"'
    assert f["reproStatic"]["bash"] == 'export PW_WS="ws://pw-static:3000"' and "token" not in str(f["reproStatic"])
    assert f["agent"] == [".\\install-backend.ps1 -HubUrl 'ws://h:4500' -AgentToken 'pwga_x' -NodeName 'win-02'",
                          "GATEWAY_AGENT_TOKEN=pwga_x node gateway/agent.js --hub ws://h:4500 --name win-02"]
    s = f["sentence"]
    assert s.startswith("Checks failed to connect 4 times in a row (limit 3). Last attempt ")
    assert "(in 3m), then retried automatically. Its run-server passes health checks, so suspect the gateway path or the browser launch." in s


def test_seven_short_runs_between_two_polls_show_up_within_3_seconds(grid, page):
    url, path = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    page.wait_for_timeout(1700)  # a second poll: the recorder has a baseline
    ids = free_ids(path)
    marks = ",".join("?" * len(ids))
    with sqlite3.connect(path) as con:  # claimed and released between two polls, as 4-second checks do
        con.execute("UPDATE portUsage SET inUse = 1, LastRunDate = ? WHERE id IN ({})".format(marks), [utc()] + ids)
        con.execute("UPDATE portUsage SET inUse = 0, LastRunDate = ? WHERE id IN ({})".format(marks), [utc(4)] + ids)
    started = time.time()
    page.wait_for_function("() => document.querySelectorAll('#tl-lanes .bar.approx').length === 7", timeout=3000)
    assert time.time() - started < 3
    page.wait_for_selector(".tl-flag:has-text('7 runs')")
    assert "approx." in page.text_content(".tl-flag")
    assert page.text_content("#runs-counter").startswith("7 runs finished")
    page.locator("#tl-lanes .bar.approx").first.hover()
    assert "approx." in page.text_content("#tip")
    page.goto(url + "#/activity")
    page.wait_for_selector("#act-rows tr")
    assert page.locator("#act-rows tr", has_text="finished between updates (approx.)").count() == 7


def test_a_burst_while_the_page_watches(grid, page):
    url, path = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    page.wait_for_timeout(1700)
    busy_before = page.locator("#matrix .cell.s-busy").count()
    ids = free_ids(path)
    marks = ",".join("?" * len(ids))
    claimed = utc()
    run(path, "UPDATE portUsage SET inUse = 1, LastRunDate = ? WHERE id IN ({})".format(marks), [claimed] + ids)
    page.wait_for_function("(n) => document.querySelectorAll('#matrix .cell.s-busy').length === n", arg=busy_before + 7, timeout=3000)
    assert page.locator("#matrix .cell.changed-neutral").count() == 7  # the claims glow
    finished = (datetime.datetime.strptime(claimed, "%Y-%m-%d %H:%M:%S") + datetime.timedelta(seconds=4)).strftime("%Y-%m-%d %H:%M:%S")
    run(path, "UPDATE portUsage SET inUse = 0, LastRunDate = ? WHERE id IN ({})".format(marks), [finished] + ids)
    page.wait_for_selector(".tl-flag:has-text('7 runs · 4.0 s avg')", timeout=3000)
    assert page.locator("#tl-lanes .bar.done").count() >= 7
    assert page.locator("#matrix .cell.changed-ok").count() == 7  # 10 s afterglow, green: finished cleanly
    page.wait_for_function("() => document.getElementById('live-summary').textContent.indexOf('7 runs finished') !== -1")

    page.reload()  # the recording survives a reload of this tab
    page.wait_for_selector(".tl-flag:has-text('7 runs · 4.0 s avg')")
    page.goto(url + "#/activity?kind=finish")
    page.wait_for_selector("#act-rows tr")
    assert page.locator("#act-rows tr", has_text="finished in 4.0 s").count() == 7
    assert "since" in page.text_content("#act-sub") and "0 dropped" in page.text_content("#act-sub")


def test_freshness_and_pause(grid, page):
    url, _ = grid
    page.goto(url)
    page.wait_for_function("() => document.getElementById('fresh-text').textContent.startsWith('Live')")
    page.keyboard.press(".")
    assert page.get_attribute("#pause-btn", "aria-pressed") == "true"
    page.wait_for_function("() => /^Paused · \\d+s old$/.test(document.getElementById('fresh-text').textContent)")
    page.click("#pause-btn")
    page.wait_for_function("() => document.getElementById('fresh-text').textContent.startsWith('Live')")


def test_palette_jumps_to_a_slot_and_keys_drive_the_views(grid, page):
    url, path = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    page.keyboard.press("/")
    page.wait_for_selector("#palette[open]")
    assert page.get_attribute("#pal-input", "role") == "combobox"
    page.keyboard.type("b81e04/3")
    first = page.locator("#pal-list [role=option]").first
    assert first.locator(".pal-label").text_content() == "runner-b81e04 / 3"
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.text_content("#insp-title") == "runner-b81e04 / slot 3"
    page.keyboard.press("Escape")
    page.wait_for_selector("#inspector", state="hidden")

    page.keyboard.press("Control+k")
    page.wait_for_selector("#palette[open]")
    page.keyboard.type("sessions")
    page.keyboard.press("Enter")
    page.wait_for_selector("#view-sessions:not([hidden])")

    page.keyboard.press("g")
    page.keyboard.press("a")
    page.wait_for_selector("#view-activity:not([hidden])")
    page.keyboard.press("g")
    page.keyboard.press("s")
    page.wait_for_selector("#view-sessions:not([hidden])")
    page.keyboard.press("j")
    assert page.evaluate("document.activeElement.matches('#session-rows tr')")
    page.keyboard.press("j")
    second = page.evaluate("document.activeElement.dataset.id")
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector:not([hidden])")
    assert page.evaluate("location.hash") == "#/session/" + second
    page.keyboard.press("Escape")
    page.wait_for_selector("#inspector", state="hidden")
    assert page.evaluate("document.activeElement.dataset.id") == second
    page.keyboard.press("?")
    page.wait_for_selector("#shortcuts-dialog[open]")
    assert "Pause or resume live updates" in page.text_content("#shortcuts-list")


def test_the_matrix_is_one_tab_stop_with_arrow_keys(grid, page):
    url, _ = grid
    page.goto(url)
    page.wait_for_selector(".mx-row[data-key]")
    assert page.locator("#matrix [tabindex='0']").count() == 1
    assert page.get_attribute("#matrix", "role") == "grid"
    page.focus("#matrix [tabindex='0']")
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.classList.contains('cell')")
    first = page.evaluate("document.activeElement.dataset.slot")
    page.keyboard.press("ArrowDown")
    assert page.evaluate("document.activeElement.closest('.mx-row').dataset.key") != page.evaluate(
        "document.querySelector('#matrix [data-slot=\"%s\"]').closest('.mx-row').dataset.key" % first)
    assert page.locator("#matrix [tabindex='0']").count() == 1
    page.keyboard.press("Enter")
    page.wait_for_selector("#inspector:not([hidden])")


def test_reproduce_snippets_follow_the_hub_address(grid, page):
    url, path = grid
    sid = run(path, "SELECT id FROM portStatus WHERE PortServer = 'runner-b81e04' AND PortNumber = 3")[0][0]
    page.goto(url + "#/slot/{}".format(sid))
    page.wait_for_selector("#repro-code")
    code = page.text_content("#repro-code")
    assert "chromium.connect(`${HUB}/connect/runner-b81e04/3?gateway_token=${process.env.GATEWAY_CLIENT_TOKEN}`)" in code
    assert "const HUB = 'ws://127.0.0.1:4500';" in code
    page.fill("#inspector [data-hub-input]", "ws://grid.lab:4500")
    page.wait_for_function("() => document.getElementById('repro-code').textContent.includes('ws://grid.lab:4500')")
    page.focus("#repro-tab-js")
    page.keyboard.press("ArrowRight")
    page.keyboard.press("ArrowRight")
    assert page.get_attribute("#repro-tab-powershell", "aria-selected") == "true"
    assert page.text_content("#repro-code") == \
        "$env:PW_WS = 'ws://grid.lab:4500/connect/runner-b81e04/3?gateway_token=' + $env:GATEWAY_CLIENT_TOKEN"
    page.keyboard.press("ArrowRight")
    assert page.text_content("#repro-code") == 'export PW_WS="ws://grid.lab:4500/connect/runner-b81e04/3?gateway_token=$GATEWAY_CLIENT_TOKEN"'
    assert "ws://grid.lab:4500/connect/runner-b81e04/3" in page.text_content("#insp-body .facts")


def test_signed_out_admin_controls_sign_in_first_then_confirm(grid, page, monkeypatch):
    url, path = grid
    monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "s3cret")
    page.goto(url)
    page.wait_for_selector("#att-list .att-row")
    release = page.locator("#att-list [data-act=release]")
    assert release.get_attribute("aria-label").startswith("Sign in to release")
    assert release.locator("svg use").get_attribute("href") == "#i-lock"
    assert not page.is_visible("#unprotected-btn")
    release.click()
    page.wait_for_selector("#signin-dialog[open]")
    assert not page.is_visible("#confirm-dialog")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    page.wait_for_selector("#confirm-dialog[open]")
    text = page.text_content("#confirm-dialog")
    assert "Release pw-static.lab.local:3000?" in text and "(stale)" in text and "both will fail" in text
    page.click("#confirm-ok")
    page.wait_for_selector("#toast:not(.bad)")
    page.wait_for_function("() => !document.getElementById('att-list').textContent.includes('pw-static.lab.local:3000')")
    assert page.evaluate("document.activeElement !== document.body")
    assert run(path, "SELECT inUse FROM portUsage WHERE id = (SELECT id FROM portStatus WHERE PortServer = 'pw-static.lab.local' AND PortNumber = 3000)") == [(0,)]
    page.goto(url + "#/activity?kind=admin")
    page.wait_for_selector("#act-rows tr")
    assert "you released pw-static.lab.local:3000" in page.text_content("#act-rows")


def test_a_database_error_is_one_panel_with_the_retry(tmp_path, monkeypatch, page):
    server = _serve(monkeypatch, str(tmp_path))  # a directory: every query fails
    try:
        page.goto("http://127.0.0.1:{}/".format(server.server_port))
        page.wait_for_selector(".db-error[role=alert]")
        assert page.text_content("#ov-h1") == "Can't reach the database"
        text = page.text_content(".db-error")
        assert "Retrying" in text and "No data yet" in text
        assert page.locator(".banner:not([hidden])").count() == 0
        assert page.text_content("#verdict-chip-text") == "No data"
    finally:
        server.shutdown()


def test_no_console_errors_or_csp_violations_across_views(grid, browser):
    url, _ = grid
    for width in (1440, 375):
        p = browser.new_page(viewport={"width": width, "height": 860})
        errors = []
        p.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        p.on("pageerror", lambda e: errors.append(str(e)))
        p.goto(url)
        p.wait_for_selector(".mx-row[data-key]")
        for hash_ in ("#/activity", "#/sessions", "#/access", "#/node/WIN-BUILD-01", "#/overview"):
            p.goto(url + hash_)
            p.wait_for_timeout(250)
            assert p.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, hash_)
        p.close()
        assert errors == [], errors


def test_a_large_grid_stays_usable(tmp_path, monkeypatch, browser):
    path = str(tmp_path / "large.db")
    with sqlite3.connect(path) as con:
        demo.create_schema(con)
        now = utc()
        for n in range(40):
            name = "runner-{:02d}".format(n)
            slots = 12 if n % 5 == 0 else 7
            con.execute("INSERT INTO nodes (name, online, slots, platform, browsers, lastSeen, playwrightVersion) VALUES (?, 1, ?, 'linux', 'chromium', ?, '1.64.1')",
                        (name, slots, now))
            for i in range(1, slots + 1):
                cur = con.execute("INSERT INTO portStatus (PortNumber, PortStatus, PortServer) VALUES (?, 1, ?)", (i, name))
                con.execute("INSERT INTO portUsage (id, portNumber, inUse, LastRunDate, failCount) VALUES (?, ?, ?, ?, ?)",
                            (cur.lastrowid, i, int(i % 3 == 0), now, int(i == 5)))
    total = run(path, "SELECT COUNT(*) FROM portStatus")[0][0]
    assert total >= 300
    server = _serve(monkeypatch, path)
    try:
        for width in (1440, 375):
            p = browser.new_page(viewport={"width": width, "height": 900})
            started = time.time()
            p.goto("http://127.0.0.1:{}/".format(server.server_port))
            p.wait_for_function("() => document.querySelectorAll('.mx-row[data-key]').length === 40")
            assert time.time() - started < 5
            assert p.locator(".tl-lane.agg").count() == 8  # nodes over 8 slots show an occupancy band
            assert p.locator("#matrix [tabindex='0']").count() == 1  # still one Tab stop
            assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")
            p.close()
    finally:
        server.shutdown()
