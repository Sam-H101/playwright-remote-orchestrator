"""Long session: one grid browser kept open for the whole budget, mostly idle.

Proves that a long-lived session survives the grid's keep-alives and timeouts. The check opens
https://example.com at the start and again at the end (the only real-internet use), and in between keeps
one app page from a FakeSite open. It alternates short bursts of work on that page (a 5,000-row table that
is filtered and sorted, a setInterval clock, large DOM fragments appended and removed) with idle gaps of
15-40 s in which it sends nothing at all. After every gap it asserts that the browser is still connected,
the same page is still there (a value stored in window survived, the page was never reloaded) and it still
answers. Memory growth is bounded by comparing DOM node counts and the JS heap of the first and last burst.
"""
import time

from playwright.sync_api import expect

from conftest import FakeSite, html

APP = "https://app.poc.test"
EXAMPLE_URL = "https://example.com/"
EXAMPLE_TITLE = "Example Domain"
TIMEOUT_MS = 20 * 1000
ROWS = 5000
CATEGORIES = ("alpha", "bravo", "charlie", "delta", "echo")
SORTS = ("amount-asc", "amount-desc", "name-desc", "id")
TICK_MS = 250
# Idle gaps cycle through these; the last one is stretched or cut to fill the budget.
GAPS_S = (25, 35, 20, 40, 15, 30)
# Room kept at the end of the budget for one burst plus the closing example.com visit.
BURST_RESERVE_S = 6
# Generous bounds: the last burst leaves the page in the same state as the first one.
MAX_NODE_GROWTH = 1.5
MAX_HEAP_GROWTH = 3.0

APP_PAGE = html("""
<h1>Long session</h1>
<section id="table-tools">
  <label>Seed <input id="seed" type="number" value="0"></label>
  <button id="render">Render</button>
  <label>Category <input id="filter"></label>
  <label>Min amount <input id="min" type="number" value="0"></label>
  <label>Sort <select id="sort">
    <option value="id">Id</option><option value="amount-asc">Amount, low first</option>
    <option value="amount-desc">Amount, high first</option><option value="name-desc">Name, Z first</option>
  </select></label>
  <p id="status">No data</p>
</section>
<section>
  <p>Clock <span id="clock">0</span> <button id="ping">Ping</button> <span id="pings">0</span></p>
  <label>Fragment size <input id="frag-size" type="number" value="0"></label>
  <button id="append">Append fragment</button> <button id="clear">Clear fragments</button>
  <div id="scratch"></div>
</section>
<table><thead><tr><th>Id</th><th>Name</th><th>Category</th><th>Amount</th></tr></thead><tbody id="rows"></tbody></table>
<script>
const CATS = ["alpha", "bravo", "charlie", "delta", "echo"];
const $ = id => document.getElementById(id);
let data = [];
let ticks = 0;
let pings = 0;

function makeRows(seed) {
  const out = [];
  for (let i = 0; i < 5000; i++) {
    out.push({id: i + 1, cat: CATS[(i * 7 + seed * 3) % 5], cents: (i * 7919 + seed * 104729) % 10000});
  }
  return out;
}
const money = c => Math.floor(c / 100) + "." + String(c % 100).padStart(2, "0");
const itemName = id => "item-" + String(id).padStart(4, "0");
const ORDER = {
  "id": (a, b) => a.id - b.id,
  "amount-asc": (a, b) => (a.cents - b.cents) || (a.id - b.id),
  "amount-desc": (a, b) => (b.cents - a.cents) || (a.id - b.id),
  "name-desc": (a, b) => b.id - a.id,
};
function view() {
  const cat = $("filter").value.trim().toLowerCase();
  const min = Number($("min").value || 0) * 100;
  const shown = data.filter(r => r.cat.includes(cat) && r.cents >= min).sort(ORDER[$("sort").value]);
  $("rows").innerHTML = shown.map(r =>
    `<tr><td>${r.id}</td><td>${itemName(r.id)}</td><td>${r.cat}</td><td>${money(r.cents)}</td></tr>`).join("");
  $("status").textContent = `Showing ${shown.length} of ${data.length}`;
}
$("render").addEventListener("click", () => { data = makeRows(Number($("seed").value)); view(); });
for (const id of ["filter", "min"]) $(id).addEventListener("input", view);
$("sort").addEventListener("change", view);

setInterval(() => { ticks++; $("clock").textContent = String(ticks); }, %TICK_MS%);
window.clockTicks = () => ticks;
$("ping").addEventListener("click", () => { pings++; $("pings").textContent = String(pings); });

$("append").addEventListener("click", () => {
  const scratch = $("scratch");
  const base = scratch.children.length;
  const frag = document.createDocumentFragment();
  for (let k = 0; k < Number($("frag-size").value); k++) {
    const div = document.createElement("div");
    div.className = "frag";
    const label = document.createElement("span");
    label.textContent = "f-" + (base + k);
    const payload = document.createElement("b");
    payload.textContent = "x".repeat(64);
    div.append(label, payload);
    frag.append(div);
  }
  scratch.append(frag);
});
$("clear").addEventListener("click", () => $("scratch").replaceChildren());
</script>
""".replace("%TICK_MS%", str(TICK_MS)), title="Long session")


def serve(route, request):
    if request.url.rstrip("/") == APP:
        route.fulfill(status=200, content_type="text/html", body=APP_PAGE)
    else:
        route.fulfill(status=404, content_type="text/plain", body="not found")


def table_rows(seed):
    """The rows the app generates for `seed` (mirrors makeRows in the page)."""
    return [{"id": i + 1, "cat": CATEGORIES[(i * 7 + seed * 3) % 5], "cents": (i * 7919 + seed * 104729) % 10000}
            for i in range(ROWS)]


def row_cells(row):
    return [str(row["id"]), "item-{:04d}".format(row["id"]), row["cat"], "{}.{:02d}".format(row["cents"] // 100, row["cents"] % 100)]


def expected_view(rows, category, min_amount, sort):
    shown = [r for r in rows if category in r["cat"] and r["cents"] >= min_amount * 100]
    keys = {
        "id": lambda r: r["id"],
        "amount-asc": lambda r: (r["cents"], r["id"]),
        "amount-desc": lambda r: (-r["cents"], r["id"]),
        "name-desc": lambda r: -r["id"],
    }
    return sorted(shown, key=keys[sort])


def assert_table(page, expected, total, what):
    body = page.locator("#rows tr")
    expect(page.locator("#status"), what + ": status line").to_have_text("Showing {} of {}".format(len(expected), total))
    expect(body, what + ": row count").to_have_count(len(expected))
    expect(body.first.locator("td"), what + ": first row").to_have_text(row_cells(expected[0]))
    expect(body.last.locator("td"), what + ": last row").to_have_text(row_cells(expected[-1]))


def work_table(page, n):
    """Render the table for seed n, filter and sort it, then reset the view. Returns rows checked."""
    rows = table_rows(n)
    page.locator("#seed").fill(str(n))
    page.locator("#render").click()
    assert_table(page, rows, ROWS, "burst {} full table".format(n))

    category = CATEGORIES[n % len(CATEGORIES)]
    min_amount = (n * 37) % 50
    sort = SORTS[n % len(SORTS)]
    expected = expected_view(rows, category, min_amount, sort)
    assert expected, "burst {}: test data leaves no rows for {} >= {}".format(n, category, min_amount)
    page.locator("#filter").fill(category)
    page.locator("#min").fill(str(min_amount))
    page.locator("#sort").select_option(sort)
    assert_table(page, expected, ROWS, "burst {} {} >= {} sorted {}".format(n, category, min_amount, sort))

    # Back to the unfiltered table so every burst ends with the same DOM (for the memory comparison).
    page.locator("#filter").fill("")
    page.locator("#min").fill("0")
    page.locator("#sort").select_option("id")
    assert_table(page, rows, ROWS, "burst {} reset".format(n))
    return ROWS + len(expected)


def work_clock(page, n):
    """Wait for the page's setInterval clock to advance a few ticks and check that took real time."""
    wanted = 6 + n % 5
    start_ticks = page.evaluate("window.clockTicks()")
    started = time.monotonic()
    page.wait_for_function("t => window.clockTicks() >= t", arg=start_ticks + wanted, timeout=TIMEOUT_MS)
    took = time.monotonic() - started
    expected_s = wanted * TICK_MS / 1000
    # One tick may land right after the first read, and each call has a network round trip.
    assert expected_s - TICK_MS / 1000 - 0.1 <= took <= expected_s + 4, (
        "burst {}: clock advanced {} ticks ({:.2f} s) in {:.2f} s of wall time".format(n, wanted, expected_s, took))
    shown = int(page.locator("#clock").inner_text())
    assert shown >= start_ticks + wanted, "burst {}: clock shows {}, expected at least {}".format(n, shown, start_ticks + wanted)


def work_fragments(page, n):
    """Append three large fragments, check them, remove them. Returns elements appended."""
    size = 2000 + 250 * (n % 5)
    fragments = page.locator("#scratch > .frag")
    page.locator("#frag-size").fill(str(size))
    for i in range(1, 4):
        page.locator("#append").click()
        expect(fragments, "burst {}: fragments after append {}".format(n, i)).to_have_count(size * i)
    expect(fragments.last.locator("span"), "burst {}: last fragment".format(n)).to_have_text("f-{}".format(size * 3 - 1))
    page.locator("#clear").click()
    expect(fragments, "burst {}: fragments after clear".format(n)).to_have_count(0)
    return size * 3


def memory(page):
    return page.evaluate("""() => ({
        nodes: document.getElementsByTagName('*').length,
        heap: (performance.memory && performance.memory.usedJSHeapSize) || null,
    })""")


def burst(page, n, totals):
    totals["rows"] += work_table(page, n)
    work_clock(page, n)
    totals["fragments"] += work_fragments(page, n)
    return memory(page)


def clock_reading(page):
    return time.monotonic(), page.evaluate("window.clockTicks()")


def assert_alive(browser, context, page, site, marker, before, pings, gap):
    what = "after {:.0f} s idle".format(gap)
    assert browser.is_connected(), what + ": the browser is no longer connected"
    assert not page.is_closed() and page in context.pages, what + ": the app page is gone"
    assert page.evaluate("window.__pocMarker") == marker, what + ": the value stored in window was lost"
    assert site.count("GET", "/") == 1, what + ": the app page was reloaded ({} loads)".format(site.count("GET", "/"))

    # The page's timers kept running while the check was silent.
    then, ticks_then = before
    now, ticks_now = clock_reading(page)
    wall, advanced = now - then, (ticks_now - ticks_then) * TICK_MS / 1000
    assert 0.8 * wall - 1 <= advanced <= wall + 1, (
        what + ": the page clock advanced {:.1f} s in {:.1f} s of wall time".format(advanced, wall))

    page.locator("#ping").click()
    expect(page.locator("#pings"), what + ": the page does not answer clicks").to_have_text(str(pings))


def assert_example(page, when):
    response = page.goto(EXAMPLE_URL, wait_until="domcontentloaded", timeout=30 * 1000)
    assert response is not None and response.ok, "{}: {} answered HTTP {}".format(
        when, EXAMPLE_URL, response.status if response else "nothing")
    expect(page, "{}: {} title".format(when, EXAMPLE_URL)).to_have_title(EXAMPLE_TITLE)


def test_long_session(grid_browser, budget, capture):
    browser = grid_browser
    context = browser.new_context()
    # example.com first, then the app page, so the app is the front page whose timers run at full speed.
    example = capture.add(context.new_page())
    assert_example(example, "start")
    site = FakeSite(context, APP, serve)
    app = capture.add(context.new_page())
    app.set_default_timeout(TIMEOUT_MS)
    app.goto(site.url("/"))
    app.bring_to_front()
    expect(app.locator("h1")).to_have_text("Long session")
    marker = "poc-long-session-{}".format(int(time.time()))
    app.evaluate("m => { window.__pocMarker = m; }", marker)

    totals = {"rows": 0, "fragments": 0}
    first = last = burst(app, 1, totals)
    bursts, idle, gaps = 1, 0.0, []
    min_gap = min(GAPS_S) if budget.seconds >= 120 else budget.seconds / 4
    while budget.more(reserve_s=min_gap + BURST_RESERVE_S):
        room = budget.left() - BURST_RESERVE_S
        gap = GAPS_S[(budget.rounds - 1) % len(GAPS_S)]
        if room - gap < min_gap + BURST_RESERVE_S:
            gap = min(room, max(GAPS_S))  # No room for another round after this one: use the rest.
        gap = max(1.0, gap)
        before = clock_reading(app)
        app.wait_for_timeout(gap * 1000)  # Idle by design: nothing is sent to the browser.
        idle += gap
        gaps.append(gap)
        assert_alive(browser, context, app, site, marker, before, budget.rounds, gap)
        bursts += 1
        last = burst(app, bursts, totals)

    assert_example(example, "end")
    assert last["nodes"] <= first["nodes"] * MAX_NODE_GROWTH, (
        "DOM grew from {} to {} nodes between the first and last burst".format(first["nodes"], last["nodes"]))
    if first["heap"] and last["heap"]:
        assert last["heap"] <= first["heap"] * MAX_HEAP_GROWTH, (
            "JS heap grew from {} to {} bytes between the first and last burst".format(first["heap"], last["heap"]))
    # A full run must spend at least half its budget idle; short dev runs have less room.
    min_idle = budget.seconds / 2 if budget.seconds >= 120 else budget.seconds / 4
    assert idle >= min_idle, "only {:.0f} s of {:.0f} s were idle (want {:.0f} s)".format(idle, budget.seconds, min_idle)

    heap = "{:.1f}->{:.1f}MB".format(first["heap"] / 1e6, last["heap"] / 1e6) if first["heap"] and last["heap"] else "n/a"
    print("long session: rounds={} bursts={} idle={:.0f}s of {:.0f}s gaps={} rows_checked={} fragments={} "
          "nodes={}->{} heap={} example.com=2".format(
              budget.rounds, bursts, idle, budget.elapsed(), "/".join("{:.0f}".format(g) for g in gaps),
              totals["rows"], totals["fragments"], first["nodes"], last["nodes"], heap))
    # Closed only after a passing run: on failure the pages stay open for capture's screenshots and the
    # grid_browser fixture closes everything.
    context.close()
