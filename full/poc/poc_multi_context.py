"""Many users at once: one grid browser, several isolated contexts used side by side.

Each round 3 or 4 users, each in their own browser context with their own locale, time zone,
geolocation and a mobile or desktop device, sign in to a fake notes app that keeps a cookie session.
The check proves that the contexts never share cookies, localStorage or BroadcastChannel messages,
that a context's storage_state signs a fresh context in as the same user, that popups and
alert/confirm/prompt dialogs are handled per page, and that every emulated setting is what page
JavaScript actually sees. The round repeats with new users and data until POC_DURATION_S is used up.
"""
import json
import time
from collections import namedtuple
from html import escape
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Playwright, expect

from conftest import FakeSite, html

ORIGIN = "https://notes.poc.test"
TIMEOUT_MS = 15 * 1000
THEMES = ("amber", "teal", "plum", "slate", "coral")
# Without this, a mobile viewport lays the page out 980 px wide and innerWidth is not the device width.
MOBILE_HEAD = "<meta name='viewport' content='width=device-width, initial-scale=1'>"

# decimal: the separator Intl.NumberFormat uses for the locale; offsets: Date.getTimezoneOffset() in
# minutes on 15 January and 15 July 2026 at noon UTC (so the zones with summer time differ).
Profile = namedtuple("Profile", "name locale timezone lat lon decimal offsets")
PROFILES = (
    Profile("berlin", "de-DE", "Europe/Berlin", 52.52, 13.405, ",", (-60, -120)),
    Profile("tokyo", "ja-JP", "Asia/Tokyo", 35.6762, 139.6503, ".", (-540, -540)),
    Profile("saopaulo", "pt-BR", "America/Sao_Paulo", -23.5505, -46.6333, ",", (180, 180)),
    Profile("singapore", "en-SG", "Asia/Singapore", 1.3521, 103.8198, ".", (-480, -480)),
    Profile("newyork", "en-US", "America/New_York", 40.7128, -74.006, ".", (300, 240)),
    Profile("sydney", "en-AU", "Australia/Sydney", -33.8688, 151.2093, ".", (-660, -600)),
    Profile("madrid", "es-ES", "Europe/Madrid", 40.4168, -3.7038, ",", (-60, -120)),
    Profile("london", "en-GB", "Europe/London", 51.5074, -0.1278, ".", (0, -60)),
)
MOBILE_DEVICES = ("iPhone 13", "Pixel 7")

LOGIN_PAGE = html("""
<h1>Sign in</h1>
<form id="login">
  <label>User <input name="user" required></label>
  <label>Theme <select name="theme">{}</select></label>
  <button>Sign in</button>
</form>
<script>
document.getElementById('login').addEventListener('submit', async event => {{
  event.preventDefault();
  const form = event.target;
  const response = await fetch('/api/login', {{
    method: 'POST', headers: {{'content-type': 'application/json'}},
    body: JSON.stringify({{user: form.user.value, theme: form.theme.value}})}});
  const data = await response.json();
  localStorage.setItem('profile', JSON.stringify(data));
  document.cookie = 'pref=' + data.theme + '; path=/; secure';
  location.href = '/dashboard';
}});
</script>""".format("".join("<option>{}</option>".format(t) for t in THEMES)), "Sign in", MOBILE_HEAD)

SIGNED_OUT_PAGE = html("<h1>Please sign in</h1><a href='/login'>Go to sign in</a>", "Signed out", MOBILE_HEAD)

DASHBOARD_PAGE = """
<h1>Signed in as {user}</h1>
<p id="lang">Accept-Language: {lang}</p>
<p id="profile"></p>
<p><label>Amount <input id="amount"></label> <button id="pay">Open receipt</button>
  <button id="close-receipt">Close receipt</button></p>
<p id="receipt-status">No receipt</p>
<p><label>Note <input id="note"></label>
  <button id="save">Save</button> <button id="delete">Delete</button> <button id="rename">Rename</button></p>
<ul id="log"></ul>
<script>
const $ = id => document.getElementById(id);
const profile = JSON.parse(localStorage.getItem('profile') || 'null');
$('profile').textContent = profile ? 'Profile: ' + profile.user + ' / ' + profile.theme : 'Profile: none';
const log = text => {{ const li = document.createElement('li'); li.textContent = text; $('log').append(li); }};
let receipt = null;
$('pay').onclick = () => {{
  receipt = window.open('/receipt?amount=' + encodeURIComponent($('amount').value), 'receipt', 'popup,width=420,height=360');
}};
$('close-receipt').onclick = () => receipt && receipt.close();
addEventListener('message', event => {{ if (event.origin === location.origin) $('receipt-status').textContent = event.data; }});
$('save').onclick = () => {{ alert('Saved ' + $('note').value); log('saved ' + $('note').value); }};
$('delete').onclick = () => log(confirm('Delete ' + $('note').value + '?') ? 'deleted' : 'kept');
$('rename').onclick = () => {{
  const name = prompt('Rename ' + $('note').value, $('note').value);
  log(name === null ? 'rename cancelled' : 'renamed to ' + name);
}};
</script>"""

RECEIPT_PAGE = """
<h1>Receipt for {user}</h1>
<p>Amount: {amount}</p>
<button>Confirm payment</button>
<script>
document.querySelector('button').onclick = () => {{
  window.opener.postMessage('Paid ' + {amount_js} + ' by ' + {user_js}, location.origin);
  document.querySelector('h1').textContent = 'Payment sent';
}};
</script>"""

CHAT_PAGE = """
<h1>Chat for {user}</h1>
<label>Message <input id="message"></label> <button id="send">Send</button>
<ul id="inbox"></ul>
<script>
const channel = new BroadcastChannel('poc-chat');
channel.onmessage = event => {{
  const li = document.createElement('li');
  li.textContent = event.data;
  document.getElementById('inbox').append(li);
}};
document.getElementById('send').onclick = () => channel.postMessage(document.getElementById('message').value);
</script>"""

ENVIRONMENT_JS = """async () => {
  const options = Intl.DateTimeFormat().resolvedOptions();
  const position = await new Promise((resolve, reject) => navigator.geolocation.getCurrentPosition(
    p => resolve({lat: p.coords.latitude, lon: p.coords.longitude}),
    e => reject(new Error('geolocation error ' + e.code + ': ' + e.message)), {timeout: 10000}));
  return {
    locale: options.locale, timeZone: options.timeZone, language: navigator.language,
    decimal: new Intl.NumberFormat().formatToParts(1234.5).find(p => p.type === 'decimal').value,
    offsets: [new Date(Date.UTC(2026, 0, 15, 12)).getTimezoneOffset(),
              new Date(Date.UTC(2026, 6, 15, 12)).getTimezoneOffset()],
    permission: (await navigator.permissions.query({name: 'geolocation'})).state,
    position, width: innerWidth, dpr: devicePixelRatio, ua: navigator.userAgent, touch: navigator.maxTouchPoints,
  };
}"""

User = namedtuple("User", "name profile theme options context site page dialogs")


class NotesApp:
    """The fake server: one session store shared by every context, so only the cookie a context
    sends decides who it is."""

    def __init__(self):
        self.sessions = {}
        self.logins = 0

    def user_for(self, request):
        cookie = SimpleCookie(request.all_headers().get("cookie", ""))
        return self.sessions.get(cookie["sid"].value) if "sid" in cookie else None

    def serve(self, route, request):
        parts = urlsplit(request.url)
        path, query = parts.path, parse_qs(parts.query)
        if path in ("/", "/login"):
            return route.fulfill(content_type="text/html", body=LOGIN_PAGE)
        if path == "/api/login" and request.method == "POST":
            data = request.post_data_json
            self.logins += 1
            token = "sid-{}".format(self.logins)
            self.sessions[token] = data["user"]
            return route.fulfill(json={"user": data["user"], "theme": data["theme"]},
                                 headers={"set-cookie": "sid={}; Path=/; Secure; HttpOnly; SameSite=Lax".format(token)})
        user = self.user_for(request)
        if path == "/api/me":
            return route.fulfill(status=200 if user else 401, json={"user": user})
        if path not in ("/dashboard", "/receipt", "/chat"):
            return route.fulfill(status=404, content_type="text/plain", body="not found")
        if user is None:
            return route.fulfill(status=401, content_type="text/html", body=SIGNED_OUT_PAGE)
        if path == "/dashboard":
            body = DASHBOARD_PAGE.format(user=escape(user), lang=escape(request.headers.get("accept-language", "")))
        elif path == "/receipt":
            amount = query.get("amount", [""])[0]
            body = RECEIPT_PAGE.format(user=escape(user), amount=escape(amount),
                                       user_js=json.dumps(user), amount_js=json.dumps(amount))
        else:
            body = CHAT_PAGE.format(user=escape(user))
        route.fulfill(content_type="text/html", body=html(body, path.strip("/").title(), MOBILE_HEAD))


class Dialogs:
    """Answers a page's dialogs from a plan of ("accept", text) / ("dismiss", None) and records what
    each one showed. Assertions happen in the test, not in the handler."""

    def __init__(self, page):
        self.plan = []
        self.seen = []
        page.on("dialog", self._answer)

    def _answer(self, dialog):
        self.seen.append((dialog.type, dialog.message, dialog.default_value))
        action, text = self.plan.pop(0) if self.plan else ("dismiss", None)
        if action == "accept":
            dialog.accept(text) if text is not None else dialog.accept()
        else:
            dialog.dismiss()


def context_options(playwright: Playwright, profile, round_no, index):
    """Locale, time zone and geolocation from the profile; every other user is on a phone."""
    options = {
        "locale": profile.locale,
        "timezone_id": profile.timezone,
        "geolocation": {"latitude": round(profile.lat + round_no * 0.001, 6),
                        "longitude": round(profile.lon - index * 0.002, 6)},
        "permissions": ["geolocation"],
    }
    if (round_no + index) % 2 == 0:
        device = dict(playwright.devices[MOBILE_DEVICES[(round_no // 2 + index) % len(MOBILE_DEVICES)]])
        device.pop("default_browser_type", None)
        options.update(device)
    else:
        options.update(
            viewport={"width": 1200 + 40 * (round_no % 5) + 16 * index, "height": 760},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/140.0.0.0 Safari/537.36 PocDesktop/{}".format(round_no))
    return options


def open_user(browser, app, capture, playwright, round_no, index):
    profile = PROFILES[(round_no * 3 + index) % len(PROFILES)]
    options = context_options(playwright, profile, round_no, index)
    context = browser.new_context(**options)
    context.set_default_timeout(TIMEOUT_MS)
    site = FakeSite(context, ORIGIN, app.serve)
    page = capture.add(context.new_page())
    return User("{}-r{}".format(profile.name, round_no), profile, THEMES[(round_no + index) % len(THEMES)],
                options, context, site, page, Dialogs(page))


def sign_in(user):
    page = user.page
    # A brand-new context must not be signed in, whatever other contexts did before it.
    response = page.goto(user.site.url("/dashboard"))
    assert response.status == 401, "{}: a fresh context got HTTP {} for the dashboard".format(user.name, response.status)
    page.get_by_role("link", name="Go to sign in").click()
    page.get_by_label("User").fill(user.name)
    page.get_by_label("Theme").select_option(user.theme)
    page.get_by_role("button", name="Sign in").click()
    expect(page, "{} did not reach the dashboard".format(user.name)).to_have_url(user.site.url("/dashboard"))
    expect(page.get_by_role("heading", level=1)).to_have_text("Signed in as " + user.name)
    expect(page.locator("#profile")).to_have_text("Profile: {} / {}".format(user.name, user.theme))


def session_token(user):
    sids = [c for c in user.context.cookies(ORIGIN) if c["name"] == "sid"]
    assert len(sids) == 1, "{}: expected one sid cookie, got {}".format(user.name, sids)
    assert sids[0]["httpOnly"] and sids[0]["secure"], "{}: sid cookie is not HttpOnly and Secure: {}".format(user.name, sids[0])
    return sids[0]["value"]


def check_isolation(users):
    tokens = {user.name: session_token(user) for user in users}
    assert len(set(tokens.values())) == len(users), "contexts share a session cookie: {}".format(tokens)
    for user in users:
        cookies = {c["name"]: c["value"] for c in user.context.cookies()}
        assert cookies == {"sid": tokens[user.name], "pref": user.theme}, \
            "{} sees cookies {}, expected only its own".format(user.name, cookies)
        page_view = user.page.evaluate("""async () => ({
            cookie: document.cookie, profile: JSON.parse(localStorage.getItem('profile')), keys: localStorage.length,
            me: await (await fetch('/api/me')).json()})""")
        # sid is HttpOnly, so the page itself only sees the cookie it set.
        assert page_view["cookie"] == "pref=" + user.theme, "{}: document.cookie is '{}'".format(user.name, page_view["cookie"])
        assert page_view["profile"] == {"user": user.name, "theme": user.theme} and page_view["keys"] == 1, \
            "{}: localStorage holds {} ({} keys)".format(user.name, page_view["profile"], page_view["keys"])
        assert page_view["me"] == {"user": user.name}, "{}: the server sees {}".format(user.name, page_view["me"])
    return tokens


def check_emulation(user):
    page, profile, options = user.page, user.profile, user.options
    expect(page.locator("#lang")).to_contain_text("Accept-Language: " + profile.locale)
    env = page.evaluate(ENVIRONMENT_JS)
    expected = {"locale": profile.locale, "timeZone": profile.timezone, "language": profile.locale,
                "decimal": profile.decimal, "offsets": list(profile.offsets), "permission": "granted",
                "width": options["viewport"]["width"], "ua": options["user_agent"]}
    actual = {key: env[key] for key in expected}
    assert actual == expected, "{}: page JavaScript sees {}, expected {}".format(user.name, actual, expected)
    want = options["geolocation"]
    assert abs(env["position"]["lat"] - want["latitude"]) < 1e-6 and abs(env["position"]["lon"] - want["longitude"]) < 1e-6, \
        "{}: geolocation is {}, expected {}".format(user.name, env["position"], want)
    if options.get("is_mobile"):
        assert env["touch"] > 0 and env["dpr"] == options["device_scale_factor"], \
            "{}: mobile context has {} touch points at DPR {}".format(user.name, env["touch"], env["dpr"])
    else:
        assert "Mobile" not in env["ua"], "{}: desktop context has a mobile user agent".format(user.name)

    # Moving one user must move only that user.
    moved = {"latitude": round(want["latitude"] + 0.5, 6), "longitude": round(want["longitude"] - 0.25, 6)}
    user.context.set_geolocation(moved)
    position = page.evaluate(ENVIRONMENT_JS)["position"]
    assert abs(position["lat"] - moved["latitude"]) < 1e-6 and abs(position["lon"] - moved["longitude"]) < 1e-6, \
        "{}: after set_geolocation the page sees {}, expected {}".format(user.name, position, moved)
    user.context.set_geolocation(want)


def check_broadcast(users, round_no):
    """Every context opens chat tabs; the first tab broadcasts, the last replies. Messages reach the
    other tabs of the same context only (a BroadcastChannel never echoes to its sender)."""
    tab_count = 2 + round_no % 2
    tabs = {}
    for user in users:
        tabs[user.name] = [user.context.new_page() for _ in range(tab_count)]
        for tab in tabs[user.name]:
            tab.goto(user.site.url("/chat"))
            expect(tab.get_by_role("heading", level=1)).to_have_text("Chat for " + user.name)
    # Everybody sends before anybody is checked, so a leak would have time to arrive.
    messages = {}
    for user in users:
        hello, reply = "r{} hello from {}".format(round_no, user.name), "r{} reply in {}".format(round_no, user.name)
        messages[user.name] = (hello, reply)
        first, last = tabs[user.name][0], tabs[user.name][-1]
        first.get_by_label("Message").fill(hello)
        first.get_by_role("button", name="Send").click()
        expect(last.locator("#inbox li")).to_have_text([hello])
        last.get_by_label("Message").fill(reply)
        last.get_by_role("button", name="Send").click()
    delivered = 0
    for user in users:
        hello, reply = messages[user.name]
        for i, tab in enumerate(tabs[user.name]):
            want = [reply] if i == 0 else [hello] if i == tab_count - 1 else [hello, reply]
            expect(tab.locator("#inbox li"), "{} tab {} inbox".format(user.name, i)).to_have_text(want)
            delivered += len(want)
    for user in users:
        for tab in tabs[user.name]:
            tab.close()
    return delivered


def check_popup(user, round_no, index):
    page = user.page
    amount = "{}.{}0".format(10 + (round_no * 7 + index * 3) % 90, index)
    page.get_by_label("Amount").fill(amount)
    with page.expect_popup() as popup_info:
        page.get_by_role("button", name="Open receipt").click()
    popup = popup_info.value
    assert popup.context == user.context, "{}: the popup opened in another context".format(user.name)
    # The popup is rendered from the session cookie, so this also proves it shares the opener's session.
    expect(popup.get_by_role("heading", level=1)).to_have_text("Receipt for " + user.name)
    expect(popup.get_by_text("Amount: " + amount)).to_be_visible()
    popup.get_by_role("button", name="Confirm payment").click()
    expect(popup.get_by_role("heading", level=1)).to_have_text("Payment sent")
    expect(page.locator("#receipt-status")).to_have_text("Paid {} by {}".format(amount, user.name))
    # The opener closes the popup: a popup that closed itself from a click would race Playwright's click.
    with popup.expect_event("close"):
        page.get_by_role("button", name="Close receipt").click()
    assert popup.is_closed(), "{}: the receipt popup is still open".format(user.name)


def check_dialogs(user, round_no, index):
    page, dialogs = user.page, user.dialogs
    note = "note-{}-{}".format(round_no, index)
    new_name = "{} v{}".format(note, round_no + 1)
    accept_delete = (round_no + index) % 2 == 0
    page.get_by_label("Note").fill(note)
    dialogs.plan[:] = [("accept", None), ("accept" if accept_delete else "dismiss", None),
                       ("accept", new_name), ("dismiss", None)]
    del dialogs.seen[:]
    for button in ("Save", "Delete", "Rename", "Rename"):
        page.get_by_role("button", name=button).click()
    expect(page.locator("#log li")).to_have_text(
        ["saved " + note, "deleted" if accept_delete else "kept", "renamed to " + new_name, "rename cancelled"])
    assert dialogs.seen == [("alert", "Saved " + note, ""), ("confirm", "Delete {}?".format(note), ""),
                            ("prompt", "Rename " + note, note), ("prompt", "Rename " + note, note)], \
        "{}: dialogs shown were {}".format(user.name, dialogs.seen)
    assert not dialogs.plan, "{}: dialogs left unanswered in the plan: {}".format(user.name, dialogs.plan)
    return len(dialogs.seen)


def check_storage_state(browser, app, user, token, tmp_path, round_no):
    """Export the user's storage_state (as a dict or through a file, by round), import it into a fresh
    context and expect the same user; clearing the copy's cookies must not sign the original out."""
    if round_no % 2:
        path = tmp_path / "state-r{}.json".format(round_no)
        user.context.storage_state(path=str(path))
        state = str(path)
        exported = json.loads(path.read_text())
    else:
        state = exported = user.context.storage_state()
    assert any(c["name"] == "sid" and c["value"] == token for c in exported["cookies"]), \
        "{}: storage_state lacks the session cookie".format(user.name)
    origins = {o["origin"]: {i["name"]: i["value"] for i in o["localStorage"]} for o in exported["origins"]}
    assert json.loads(origins.get(ORIGIN, {}).get("profile", "null")) == {"user": user.name, "theme": user.theme}, \
        "{}: storage_state localStorage is {}".format(user.name, origins)

    clone = browser.new_context(storage_state=state)
    try:
        clone.set_default_timeout(TIMEOUT_MS)
        FakeSite(clone, ORIGIN, app.serve)
        page = clone.new_page()
        response = page.goto(ORIGIN + "/dashboard")
        assert response.status == 200, "{}: imported state got HTTP {}".format(user.name, response.status)
        expect(page.get_by_role("heading", level=1)).to_have_text("Signed in as " + user.name)
        expect(page.locator("#profile")).to_have_text("Profile: {} / {}".format(user.name, user.theme))
        clone.clear_cookies()
        response = page.reload()
        assert response.status == 401, "{}: the copy is still signed in after clear_cookies".format(user.name)
    finally:
        clone.close()
    response = user.page.reload()
    assert response.status == 200, "{}: clearing the copy's cookies signed the original out".format(user.name)
    expect(user.page.get_by_role("heading", level=1)).to_have_text("Signed in as " + user.name)


def test_multi_context(playwright: Playwright, grid_browser, budget, capture, tmp_path):
    expect.set_options(timeout=TIMEOUT_MS)
    app = NotesApp()
    totals = {"contexts": 0, "broadcasts": 0, "popups": 0, "dialogs": 0, "imports": 0}
    slowest = 0.0
    # Start a new round while at least half of the slowest one still fits, so the test ends near the budget.
    while budget.more(reserve_s=slowest / 2):
        round_no, started = budget.rounds, time.monotonic()
        del capture.pages[:]  # only this round's pages are worth a screenshot
        users = [open_user(grid_browser, app, capture, playwright, round_no, index)
                 for index in range(3 + round_no % 2)]
        totals["contexts"] += len(users)
        for user in users:
            sign_in(user)
        tokens = check_isolation(users)
        for user in users:
            check_emulation(user)
        totals["broadcasts"] += check_broadcast(users, round_no)
        for index, user in enumerate(users):
            check_popup(user, round_no, index)
            totals["popups"] += 1
            totals["dialogs"] += check_dialogs(user, round_no, index)
        exported = users[round_no % len(users)]
        check_storage_state(grid_browser, app, exported, tokens[exported.name], tmp_path, round_no)
        totals["imports"] += 1
        check_isolation(users)
        # Closed only after a passing round: on failure the pages stay open for capture's screenshots and
        # the grid_browser fixture closes everything.
        for user in users:
            user.context.close()
        slowest = max(slowest, time.monotonic() - started)
    print("multi-context: {} rounds in {:.0f}s, {} contexts, {} logins, {} broadcasts delivered, {} popups, "
          "{} dialogs, {} storage_state imports".format(
              budget.rounds, budget.elapsed(), totals["contexts"], app.logins, totals["broadcasts"],
              totals["popups"], totals["dialogs"], totals["imports"]))
