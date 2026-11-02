"""Cross-browser proof of concept: one fake web app, the same scenario on Chromium, Firefox and WebKit.

The check connects to the hub once per browser engine, one after another (the hub sends Firefox and WebKit
to the Linux runners), and gives each browser about a third of POC_DURATION_S. Every round drives a fake
shop that the check serves itself (FakeSite), with data derived from the round number:

- an order form walked with the keyboard (Tab order, Space on a checkbox, arrow keys in a radio group) and
  submitted with Enter, using text, email, number, date, select, checkbox and radio inputs;
- HTML5 constraint validation read through element.validity, and an invalid form whose submission the
  browser must block;
- a CSS grid and flex layout whose element boxes must come in the expected order and relative positions;
- drag and drop between two lists (locator.drag_to), saved to the server with fetch();
- hover menus that open, pick an item and close again.

A JavaScript feature probe (structuredClone, Intl.Segmenter, ...) is reported per browser, not asserted.
Everything the app shows is browser-independent, so each round's results must be identical in the three
browsers. The check prints every browser's version and round count.
"""
import contextlib
import json
import time
from datetime import date, timedelta
from html import escape
from urllib.parse import parse_qsl, urlsplit

import pytest
from playwright.sync_api import Error, Playwright, expect

from conftest import Budget, FakeSite, connect, html

ORIGIN = "https://shop.poc.test"
BROWSERS = ("chromium", "firefox", "webkit")
TIMEOUT_MS = 15 * 1000
VIEWPORT = {"width": 1000, "height": 800}
# Box comparisons allow for sub-pixel rounding, never for a different layout.
TOLERANCE_PX = 1.0
MAX_TABS = 25

NAMES = ("Ada Lovelace", "Grace Hopper", "Alan Turing", "Katherine Johnson", "Margaret Hamilton",
         "Dennis Ritchie", "Barbara Liskov")
PLANS = ("basic", "plus", "pro")
# The fields with constraints, in form (DOM) order, which is also the order of their invalid events.
VALIDATED = ("name", "email", "code", "qty", "date", "terms")
TAB_ORDER = ["name", "email", "code", "qty", "date", "plan", "gift", "ship-standard", "terms", "submit"]
# (field, value, the one validity flag it must raise); None for terms means unchecked.
INVALID = (
    ("name", "", "valueMissing"),
    ("name", "R2-D2", "patternMismatch"),
    ("email", "not-an-email", "typeMismatch"),
    ("email", "", "valueMissing"),
    ("code", "abc-12", "patternMismatch"),
    ("qty", "0", "rangeUnderflow"),
    ("qty", "12", "rangeOverflow"),
    ("qty", "2.5", "stepMismatch"),
    ("date", "2025-12-31", "rangeUnderflow"),
    ("date", "2027-01-01", "rangeOverflow"),
    ("terms", None, "valueMissing"),
)
WORDS = ("apples", "bread", "cheese", "dates", "eggs", "figs", "grapes", "honey")
MENUS = {"Products": ("Laptops", "Phones", "Tablets", "Watches"), "Help": ("FAQ", "Contact")}

FORM_PAGE = html("""
<h1>Order</h1>
<form id="order" method="post" action="/submit">
  <p><label for="name">Name</label> <input id="name" name="name" required pattern="[A-Za-z ]+"></p>
  <p><label for="email">Email</label> <input id="email" name="email" type="email" required></p>
  <p><label for="code">Voucher code</label> <input id="code" name="code" required pattern="[A-Z]{3}-[0-9]{2}"></p>
  <p><label for="qty">Quantity</label> <input id="qty" name="qty" type="number" min="1" max="9" step="1" required></p>
  <p><label for="date">Delivery date</label>
     <input id="date" name="date" type="date" min="2026-01-01" max="2026-12-31" required></p>
  <p><label for="plan">Plan</label>
     <select id="plan" name="plan"><option value="basic">Basic</option><option value="plus">Plus</option>
       <option value="pro">Pro</option></select></p>
  <p><label><input id="gift" name="gift" type="checkbox" value="yes"> Gift wrap</label></p>
  <fieldset><legend>Shipping</legend>
    <label><input id="ship-standard" name="ship" type="radio" value="standard" checked> Standard</label>
    <label><input id="ship-express" name="ship" type="radio" value="express"> Express</label>
  </fieldset>
  <p><label><input id="terms" name="terms" type="checkbox" value="accepted" required> I accept the terms</label></p>
  <button id="submit" type="submit">Place order</button>
</form>
<p>Invalid on submit: <output id="invalid-fields"></output></p>
<script>
  // invalid does not bubble, so listen in the capture phase.
  const invalid = [];
  document.getElementById("order").addEventListener("invalid", e => {
    invalid.push(e.target.id);
    document.getElementById("invalid-fields").textContent = invalid.join(",");
  }, true);
</script>
""", title="Order")

BOARD_CSS = """<style>
  body { margin: 0; font: 14px sans-serif; }
  #shell { display: grid; grid-template-columns: 160px 1fr; grid-template-areas: "head head" "side main" "foot foot";
           gap: 8px; width: 900px; margin: 8px; }
  header { grid-area: head; height: 48px; display: flex; gap: 16px; align-items: center; background: #eef; }
  aside { grid-area: side; }
  main { grid-area: main; }
  footer { grid-area: foot; height: 32px; background: #eee; }
  .menu { position: relative; }
  .menu > button { width: 100px; height: 32px; }
  .sub { display: none; position: absolute; top: 32px; left: 0; width: 140px; margin: 0; padding: 0;
         list-style: none; background: #fff; z-index: 10; }
  .menu:hover .sub { display: block; }
  .sub a { display: block; height: 28px; }
  #cards { display: grid; grid-template-columns: repeat(3, 80px); grid-auto-rows: 40px; gap: 8px; }
  .card { overflow: hidden; background: #ddf; }
  #flex { display: flex; gap: 8px; margin: 16px 0; }
  #flex div { flex: none; width: 60px; height: 30px; overflow: hidden; background: #dfd; }
  .lists { display: flex; gap: 24px; }
  .list { width: 200px; min-height: 60px; margin: 0; padding: 4px 4px 48px; list-style: none; background: #f4f4f4; }
  .list li { height: 28px; margin: 2px 0; overflow: hidden; background: #fff; cursor: move; }
</style>"""

BOARD_SCRIPT = """<script>
  document.querySelectorAll(".list li").forEach(li => li.addEventListener("dragstart", e => {
    e.dataTransfer.setData("text/plain", li.id);
    e.dataTransfer.effectAllowed = "move";
  }));
  document.querySelectorAll(".list").forEach(list => {
    list.addEventListener("dragover", e => e.preventDefault());
    list.addEventListener("drop", e => {
      e.preventDefault();
      const item = document.getElementById(e.dataTransfer.getData("text/plain"));
      // Dropped on an item: insert before it; dropped on the list's empty area: append.
      const before = e.target.closest("li");
      if (before && before !== item) list.insertBefore(item, before); else list.appendChild(item);
    });
  });
  document.querySelectorAll(".sub a").forEach(a => a.addEventListener("click", e => {
    e.preventDefault();
    document.getElementById("chosen").textContent = a.closest(".menu").dataset.menu + " / " + a.textContent;
  }));
  document.getElementById("save").addEventListener("click", async () => {
    const words = id => [...document.querySelectorAll("#" + id + " li")].map(li => li.textContent);
    const response = await fetch("/board/save", {method: "POST", headers: {"Content-Type": "application/json"},
                                                 body: JSON.stringify({todo: words("todo"), done: words("done")})});
    document.getElementById("save-status").textContent = "saved " + (await response.json()).saved;
  });
</script>"""

FEATURE_PROBE = """() => {
  const fn = (o, k) => o != null && typeof o[k] === "function";
  const f = {
    "structuredClone": typeof structuredClone === "function",
    "Intl.Segmenter": fn(Intl, "Segmenter"),
    "Array.prototype.at": fn([], "at"),
    "Array.prototype.toSorted": fn([], "toSorted"),
    "Object.groupBy": fn(Object, "groupBy"),
    "Promise.withResolvers": fn(Promise, "withResolvers"),
    "Set.prototype.union": fn(new Set(), "union"),
    "URL.canParse": fn(URL, "canParse"),
    "CSS :has()": CSS.supports("selector(:has(a))"),
    "popover": "popover" in HTMLElement.prototype,
    "dialog": typeof HTMLDialogElement === "function",
  };
  const extra = {};
  if (f["structuredClone"]) {
    const value = {when: new Date(0), map: new Map([[1, [2]]])};
    const copy = structuredClone(value);
    extra.structuredCloneDeep = copy !== value && copy.map.get(1)[0] === 2 && copy.when.getTime() === 0;
  }
  if (f["Intl.Segmenter"]) {
    const words = new Intl.Segmenter("en", {granularity: "word"}).segment("Cross browser proof of concept");
    extra.segmenterWords = [...words].filter(s => s.isWordLike).length;
  }
  return {features: f, extra};
}"""

VALIDITY = """e => ({
  flags: ["valueMissing", "typeMismatch", "patternMismatch", "tooLong", "tooShort", "rangeUnderflow",
          "rangeOverflow", "stepMismatch", "badInput", "customError"].filter(flag => e.validity[flag]),
  message: e.validationMessage,
})"""


# ---- per-round data, derived from the round number so every browser gets the same rounds ----

def order_for(k):
    name = NAMES[k % len(NAMES)]
    return {
        "name": name,
        "email": "{}.{}@shop.poc.test".format(name.split()[0].lower(), k),
        "code": "{}{}{}-{:02d}".format(*(chr(65 + (k * m) % 26) for m in (1, 7, 11)), k % 100),
        "qty": str(1 + k % 9),
        "date": (date(2026, 1, 1) + timedelta(days=(k * 37) % 365)).isoformat(),
        "plan": PLANS[k % len(PLANS)],
        "gift": k % 2 == 0,
        "ship": "express" if k % 3 == 1 else "standard",
        "terms": "accepted",
    }


def expected_submission(order):
    fields = {key: order[key] for key in ("name", "email", "code", "qty", "date", "plan", "ship", "terms")}
    if order["gift"]:
        fields["gift"] = "yes"
    return fields


def invalid_cases(k):
    """Two invalid values for two different fields."""
    first = INVALID[k % len(INVALID)]
    i = (k * 5 + 3) % len(INVALID)
    while INVALID[i][0] == first[0]:
        i = (i + 1) % len(INVALID)
    return first, INVALID[i]


def card_orders(k):
    # Few distinct values, so ties occur; tied grid items keep their DOM order.
    return [(i * 3 + k) % 4 for i in range(4 + k % 5)]


def board_items(k):
    return [WORDS[(k + i) % len(WORDS)] for i in range(5)], ["milk"]


# ---- the fake shop ----

def result_page(fields):
    return html("<h1>Order received</h1><pre id='echo'>{}</pre>".format(escape(json.dumps(fields, sort_keys=True))),
                title="Order received")


def board_page(k):
    cards = "".join("<div class='card' id='card-{0}' style='order: {1}'>Card {0}</div>".format(i, o)
                    for i, o in enumerate(card_orders(k)))
    flex = "".join("<div id='flex-{0}'>F{0}</div>".format(i) for i in range(3))
    menus = "".join(
        "<div class='menu' id='menu-{0}' data-menu='{1}'><button type='button'>{1}</button><ul class='sub'>{2}</ul></div>"
        .format(menu.lower(), menu, "".join("<li><a href='#'>{}</a></li>".format(item) for item in items))
        for menu, items in MENUS.items())
    todo, done = board_items(k)

    def items(words):
        return "".join("<li draggable='true' id='item-{0}'>{0}</li>".format(w) for w in words)

    direction = "row-reverse" if k % 2 else "row"
    body = ("<div id='shell'><header>{menus}</header>"
            "<aside><h1>Board</h1><p>Chosen: <span id='chosen'>none</span></p></aside>"
            "<main><div id='cards'>{cards}</div><div id='flex' style='flex-direction: {direction}'>{flex}</div>"
            "<div class='lists'><ul class='list' id='todo'>{todo}</ul><ul class='list' id='done'>{done}</ul></div>"
            "<p><button id='save' type='button'>Save board</button> <span id='save-status'></span></p></main>"
            "<footer>Shop POC</footer></div>").format(menus=menus, cards=cards, direction=direction, flex=flex,
                                                     todo=items(todo), done=items(done))
    return html(body + BOARD_SCRIPT, title="Board", head=BOARD_CSS)


class ShopApp:
    """Request handler for the FakeSite; keeps what the board's Save button sent."""

    def __init__(self):
        self.saves = []

    def handle(self, route, request):
        parts = urlsplit(request.url)
        key = (request.method, parts.path)
        if key == ("GET", "/form"):
            route.fulfill(content_type="text/html; charset=utf-8", body=FORM_PAGE)
        elif key == ("POST", "/submit"):
            fields = dict(parse_qsl(request.post_data or "", keep_blank_values=True))
            route.fulfill(content_type="text/html; charset=utf-8", body=result_page(fields))
        elif key == ("GET", "/board"):
            k = int(dict(parse_qsl(parts.query)).get("r", "0"))
            route.fulfill(content_type="text/html; charset=utf-8", body=board_page(k))
        elif key == ("POST", "/board/save"):
            self.saves.append(json.loads(request.post_data or "{}"))
            route.fulfill(content_type="application/json", body=json.dumps({"saved": len(self.saves)}))
        else:
            route.fulfill(status=404, content_type="text/plain", body="not found")


# ---- the scenario ----

def active_id(page):
    return page.evaluate("() => document.activeElement ? document.activeElement.id : ''")


def walk_tab_order(page):
    """The focus order from the first field to the submit button. A date input can take several Tab
    presses (one per date part) while staying the active element, so repeats are collapsed."""
    page.locator("#name").focus()
    seen = [active_id(page)]
    for _ in range(MAX_TABS):
        if seen[-1] == TAB_ORDER[-1]:
            break
        page.keyboard.press("Tab")
        current = active_id(page)
        if current != seen[-1]:
            seen.append(current)
    return seen


def set_field(page, field, value):
    if field == "terms":
        page.locator("#terms").set_checked(value is not None)
    else:
        page.locator("#" + field).fill(value)


def validity(page, field):
    return page.locator("#" + field).evaluate(VALIDITY)


def run_form(page, site, k):
    order = order_for(k)
    page.goto(site.url("/form"))
    expect(page.get_by_role("heading", name="Order")).to_be_visible()

    tab_order = walk_tab_order(page)
    assert tab_order == TAB_ORDER, "round {}: Tab order {} != expected {}".format(k, tab_order, TAB_ORDER)

    for field in VALIDATED:
        set_field(page, field, order[field])
    page.locator("#plan").select_option(order["plan"])
    if order["gift"]:
        page.locator("#gift").focus()
        page.keyboard.press("Space")
        expect(page.locator("#gift"), "round {}: Space did not check the gift box".format(k)).to_be_checked()
    if order["ship"] == "express":
        page.locator("#ship-standard").focus()
        page.keyboard.press("ArrowDown")
        expect(page.locator("#ship-express"), "round {}: ArrowDown did not move the radio".format(k)).to_be_checked()

    cases = invalid_cases(k)
    for field, value, _ in cases:
        set_field(page, field, value)
    invalid = []
    for field, value, flag in cases:
        state = validity(page, field)
        assert state["flags"] == [flag], "round {}: {}={!r} has validity {} instead of [{}]".format(
            k, field, value, state["flags"], flag)
        assert state["message"], "round {}: {} is invalid but has no validationMessage".format(k, field)
        invalid.append([field, flag])
    bad_fields = [field for field in VALIDATED if field in {c[0] for c in cases}]
    for field in VALIDATED:
        if field not in bad_fields:
            state = validity(page, field)
            assert not state["flags"], "round {}: valid {} reports {}".format(k, field, state["flags"])

    posts = site.count("POST", "/submit")
    page.locator("#name").press("Enter")
    expect(page.locator("#invalid-fields"), "round {}: invalid form did not fire invalid events".format(k)) \
        .to_have_text(",".join(bad_fields))

    for field, _, _ in cases:
        set_field(page, field, order[field])
    assert page.locator("#order").evaluate("f => f.checkValidity()"), "round {}: fixed form still invalid".format(k)
    page.locator("#name").press("Enter")
    expect(page).to_have_url(site.url("/submit"))
    expect(page.get_by_role("heading", name="Order received")).to_be_visible()
    # One POST for the valid submit and none for the blocked one.
    assert site.count("POST", "/submit") == posts + 1, "round {}: expected exactly one order POST".format(k)
    submitted = json.loads(page.locator("#echo").text_content())
    assert submitted == expected_submission(order), "round {}: server received {} instead of {}".format(
        k, submitted, expected_submission(order))
    return {"tab_order": tab_order, "invalid": invalid, "blocked": bad_fields, "submitted": submitted}


def box(page, selector):
    b = page.locator(selector).bounding_box()
    assert b, "{} has no box".format(selector)
    return b


def right(b):
    return b["x"] + b["width"]


def bottom(b):
    return b["y"] + b["height"]


def reading_order(boxes):
    """Keys sorted top to bottom, then left to right."""
    return sorted(boxes, key=lambda key: (round(boxes[key]["y"] / TOLERANCE_PX), boxes[key]["x"]))


def check_layout(page, k):
    head, side, main, foot = (box(page, s) for s in ("header", "aside", "main", "footer"))
    assert bottom(head) <= min(side["y"], main["y"]) + TOLERANCE_PX, "round {}: header not above the columns".format(k)
    assert abs(head["x"] - side["x"]) <= TOLERANCE_PX and abs(right(head) - right(main)) <= TOLERANCE_PX, \
        "round {}: header does not span both columns ({} vs {} / {})".format(k, head, side, main)
    assert right(side) <= main["x"] + TOLERANCE_PX, "round {}: sidebar not left of main".format(k)
    assert abs(side["y"] - main["y"]) <= TOLERANCE_PX, "round {}: sidebar and main tops differ".format(k)
    assert foot["y"] >= max(bottom(side), bottom(main)) - TOLERANCE_PX, "round {}: footer not below".format(k)
    products, help_ = box(page, "#menu-products"), box(page, "#menu-help")
    assert right(products) <= help_["x"] + TOLERANCE_PX and abs(products["y"] - help_["y"]) <= TOLERANCE_PX, \
        "round {}: menus not side by side in order".format(k)

    orders = card_orders(k)
    expected_cards = sorted(range(len(orders)), key=lambda i: (orders[i], i))
    cards = {i: box(page, "#card-{}".format(i)) for i in range(len(orders))}
    card_order = reading_order(cards)
    assert card_order == expected_cards, "round {}: grid shows cards in order {} (CSS order {}), expected {}".format(
        k, card_order, orders, expected_cards)
    # Three columns: every card lines up with the card above it, and rows go down.
    for position, card in enumerate(expected_cards[3:], start=3):
        above = cards[expected_cards[position - 3]]
        assert abs(cards[card]["x"] - above["x"]) <= TOLERANCE_PX and cards[card]["y"] > bottom(above) - TOLERANCE_PX, \
            "round {}: card {} is not below card {}".format(k, card, expected_cards[position - 3])

    flex = {i: box(page, "#flex-{}".format(i)) for i in range(3)}
    flex_order = reading_order(flex)
    expected_flex = [2, 1, 0] if k % 2 else [0, 1, 2]
    assert flex_order == expected_flex, "round {}: flex order {} != {}".format(k, flex_order, expected_flex)
    assert len({round(b["y"]) for b in flex.values()}) == 1, "round {}: flex items not on one line".format(k)
    return {"cards": card_order, "flex": flex_order}


def drag_items(page, app, k):
    todo, done = board_items(k)

    def item(word):
        return page.locator("#item-" + word)

    def check(step):
        expect(page.locator("#todo li"), "round {} after {}: to-do list".format(k, step)).to_have_text(todo)
        expect(page.locator("#done li"), "round {} after {}: done list".format(k, step)).to_have_text(done)

    # Onto the empty bottom of the done list: appended.
    first = todo[k % len(todo)]
    target = page.locator("#done")
    target_box = box(page, "#done")
    item(first).drag_to(target, target_position={"x": target_box["width"] / 2, "y": target_box["height"] - 12})
    todo.remove(first)
    done.append(first)
    check("dragging {} to the list".format(first))

    # Onto the first done item: inserted before it.
    second = todo[k % len(todo)]
    item(second).drag_to(item(done[0]))
    todo.remove(second)
    done.insert(0, second)
    check("dragging {} onto {}".format(second, done[1]))

    saves = len(app.saves)
    page.locator("#save").click()
    expect(page.locator("#save-status"), "round {}: board not saved".format(k)).to_have_text("saved {}".format(saves + 1))
    assert app.saves[-1] == {"todo": todo, "done": done}, "round {}: server saved {} instead of {}".format(
        k, app.saves[-1], {"todo": todo, "done": done})
    return {"todo": todo, "done": done}


def use_menus(page, k):
    subs = {menu: page.locator("#menu-{} .sub".format(menu.lower())) for menu in MENUS}
    chosen = []
    for menu in MENUS:
        expect(subs[menu], "round {}: {} menu open before hover".format(k, menu)).to_be_hidden()
    for menu, items in MENUS.items():
        page.locator("#menu-{} > button".format(menu.lower())).hover()
        for other in MENUS:
            sub = expect(subs[other], "round {}: {} submenu while hovering {}".format(k, other, menu))
            if other == menu:
                sub.to_be_visible()
            else:
                sub.to_be_hidden()
        item = items[k % len(items)]
        subs[menu].get_by_role("link", name=item, exact=True).click()
        expect(page.locator("#chosen")).to_have_text("{} / {}".format(menu, item))
        chosen.append(item)
    page.locator("footer").hover()
    for menu in MENUS:
        expect(subs[menu], "round {}: {} menu still open after leaving it".format(k, menu)).to_be_hidden()
    return chosen


def run_board(page, site, app, k):
    page.goto(site.url("/board?r={}".format(k)))
    expect(page.get_by_role("heading", name="Board")).to_be_visible()
    result = check_layout(page, k)
    result["lists"] = drag_items(page, app, k)
    result["menus"] = use_menus(page, k)
    return result


def probe_features(context, site):
    page = context.new_page()
    page.goto(site.url("/form"))
    report = page.evaluate(FEATURE_PROBE)
    page.close()
    return report


class Run:
    """One browser's share of the check."""

    def __init__(self, name, version):
        self.name = name
        self.version = version
        self.results = []
        self.report = {}
        self.orders = 0
        self.connect_s = 0.0
        self.slowest_s = 0.0


def run_browser(browser, name, share, capture):
    run = Run(name, browser.version)
    context = browser.new_context(viewport=VIEWPORT, locale="en-US")
    app = ShopApp()
    site = FakeSite(context, ORIGIN, app.handle)
    run.report = probe_features(context, site)
    slowest = 0.0
    while share.more(reserve_s=slowest):
        k = share.rounds - 1
        started = time.monotonic()
        page = capture.add(context.new_page())
        page.set_default_timeout(TIMEOUT_MS)
        run.results.append({"form": run_form(page, site, k), "board": run_board(page, site, app, k)})
        # Left open on failure, so `capture` can screenshot it.
        page.close()
        slowest = max(slowest, time.monotonic() - started)
    run.slowest_s = slowest
    run.orders = site.count("POST", "/submit")
    context.close()
    return run


def assert_same_results(runs):
    common = min(len(run.results) for run in runs)
    reference = runs[0]
    for run in runs[1:]:
        for k in range(common):
            mine, theirs = run.results[k], reference.results[k]
            if mine != theirs:
                diff = {part: (theirs[part], mine[part]) for part in mine if mine[part] != theirs[part]}
                pytest.fail("round {}: {} and {} differ: {}".format(k, reference.name, run.name, diff))
    return common


def describe(run):
    features = run.report["features"]
    missing = [f for f, ok in features.items() if not ok]
    return "{} {}: connected in {:.1f}s, {} rounds (slowest {:.1f}s); {} of {} JS features (missing: {}); extra {}".format(
        run.name, run.version, run.connect_s, len(run.results), run.slowest_s, len(features) - len(missing), len(features),
        ", ".join(missing) or "none", run.report["extra"])


@pytest.fixture
def grid(playwright: Playwright):
    """Opens grid browsers by engine name. Set up before `capture`, so it closes what is still open only
    after capture took its failure screenshots."""
    opened = []

    def open_browser(name):
        browser = connect(playwright, name)
        opened.append(browser)
        return browser

    yield open_browser
    for browser in opened:
        with contextlib.suppress(Error):
            browser.close()


def test_cross_browser(grid, budget, capture):
    expect.set_options(timeout=TIMEOUT_MS)
    runs = []
    for i, name in enumerate(BROWSERS):
        # Each browser gets an equal part of what is left, connect time included.
        share = Budget(max(budget.left(), 0.0) / (len(BROWSERS) - i))
        browser = grid(name)
        connect_s = share.elapsed()
        runs.append(run_browser(browser, name, share, capture))
        runs[-1].connect_s = connect_s
        browser.close()
        print(describe(runs[-1]))

    common = assert_same_results(runs)
    rounds = sum(len(run.results) for run in runs)
    print("cross-browser: {} rounds in {:.0f}s ({}); {} orders submitted, {} invalid fields blocked, {} drags, "
          "{} menu picks; first {} rounds identical in all {} browsers".format(
              rounds, budget.elapsed(),
              ", ".join("{} {} x{}".format(run.name, run.version, len(run.results)) for run in runs),
              sum(run.orders for run in runs), 2 * rounds, 2 * rounds, len(MENUS) * rounds, common, len(runs)))
