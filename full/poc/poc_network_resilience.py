"""Network resilience: a browser on the grid survives a flaky network the way a real single-page app must.

The check serves a small inventory app from a FakeSite and, round after round, injects different faults
through request routing: the items API fails its first requests with HTTP 500, an aborted connection or a
response too slow for the app's timeout, and the app has to retry with backoff, show its loading, retry
and error states and finally the data, with the fake server seeing exactly the expected requests. Each
round also checks the auth header and JSON body the app sends, a slow endpoint driving a spinner,
offline mode (navigator.onLine and the online/offline events) with a queue flushed on reconnect, an
HTTP Basic auth protected page (401 without credentials, a sign-in with them) and a WebSocket chat
mocked with page.route_web_socket.
"""
import contextlib
import json
import time
from base64 import b64encode
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error, expect

from conftest import FakeSite, html

ORIGIN = "https://net.poc.test"
TIMEOUT_MS = 10 * 1000
WORDS = ("anchor", "beacon", "cobalt", "delta", "ember", "falcon", "garnet", "harbor", "indigo", "juniper",
         "kestrel", "lumen")
# Faults the items API answers with before it succeeds, cycled through by round. A plan longer than the
# app's attempt limit drives it into its error state, from which the check presses Retry.
FAULT_PLANS = (
    ("500",),
    ("abort",),
    ("slow",),
    ("500", "abort"),
    ("500", "abort", "slow"),
    ("abort", "slow", "500"),
    ("slow", "500", "abort", "500", "abort"),
    (),
)
# How the app describes each fault in its status line.
FAULT_TEXT = {"500": "HTTP 500", "abort": "network", "slow": "timeout"}

APP_JS = """
const params = new URLSearchParams(location.search);
const cfg = {token: params.get('token'), round: +params.get('round'), attempts: +params.get('attempts'),
             backoff: +params.get('backoff'), timeout: +params.get('timeout')};
const $ = id => document.getElementById(id);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function api(path, options = {}) {
  const headers = Object.assign({'Authorization': 'Bearer ' + cfg.token, 'Accept': 'application/json'},
                                options.headers || {});
  return fetch(path, Object.assign({}, options, {headers}));
}

// Items: fetch with a timeout, retry with exponential backoff, then an error state with a Retry button.
function setState(kind, text) {
  $('state').dataset.state = kind;
  $('state').textContent = text;
  const li = document.createElement('li');
  li.textContent = kind + ': ' + text;
  $('trail').append(li);
}
function describe(error) {
  if (error.name === 'AbortError') return 'timeout';
  if (error instanceof TypeError) return 'network';
  return error.message;
}
async function fetchItems() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), cfg.timeout);
  try {
    const response = await api('/api/items?round=' + cfg.round, {signal: controller.signal});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}
async function loadItems() {
  $('load').disabled = true;
  $('retry').hidden = true;
  for (let n = 1; ; n++) {
    setState('loading', 'attempt ' + n);
    try {
      const data = await fetchItems();
      $('items').replaceChildren(...data.items.map(item => {
        const li = document.createElement('li');
        li.textContent = item.name + ' x' + item.qty;
        return li;
      }));
      setState('ready', data.items.length + ' items');
      break;
    } catch (error) {
      const why = 'attempt ' + n + ' failed: ' + describe(error);
      if (n >= cfg.attempts) {
        setState('error', why);
        $('retry').hidden = false;
        break;
      }
      const wait = cfg.backoff * 2 ** (n - 1);
      setState('retrying', why + ', retrying in ' + wait + ' ms');
      await sleep(wait);
    }
  }
  $('load').disabled = false;
}
$('load').onclick = loadItems;
$('retry').onclick = loadItems;

// Notes: JSON POSTs, queued while offline and sent when the browser is back online.
const queue = [];
let noteSeq = 0;
function updateNetwork() {
  $('offline-banner').hidden = navigator.onLine;
  $('network').textContent = navigator.onLine ? 'online' : 'offline';
  $('queued').textContent = queue.length + ' queued';
}
async function postNote(note) {
  const response = await api('/api/notes', {
    method: 'POST', body: JSON.stringify(note),
    headers: {'Content-Type': 'application/json', 'X-Request-Id': 'note-' + cfg.round + '-' + (++noteSeq)}});
  if (response.status !== 201) throw new Error('HTTP ' + response.status);
  const saved = await response.json();
  $('note-status').textContent = 'Saved note #' + saved.id + ': ' + saved.text;
  const li = document.createElement('li');
  li.textContent = '#' + saved.id + ' ' + saved.text;
  $('notes').append(li);
}
async function flushQueue() {
  while (queue.length && navigator.onLine) {
    const note = queue.shift();
    updateNetwork();
    await postNote(note);
  }
  updateNetwork();
}
$('note-form').onsubmit = async event => {
  event.preventDefault();
  const note = {text: $('note').value, round: cfg.round, tags: ['r' + cfg.round, 'poc'], queued: false};
  $('note').value = '';
  if (!navigator.onLine) {
    note.queued = true;
    queue.push(note);
    $('note-status').textContent = 'Queued (offline): ' + note.text;
    updateNetwork();
    return;
  }
  await postNote(note);
};
window.addEventListener('online', flushQueue);
window.addEventListener('offline', updateNetwork);
updateNetwork();

// Report: a slow endpoint behind a spinner.
$('report-button').onclick = async () => {
  $('report-button').disabled = true;
  $('spinner').hidden = false;
  try {
    const data = await (await api('/api/report?round=' + cfg.round)).json();
    $('report').textContent = data.title + ': total ' + data.total;
  } finally {
    $('spinner').hidden = true;
    $('report-button').disabled = false;
  }
};

// Chat over a WebSocket.
let socket = null;
function addChat(who, text) {
  const li = document.createElement('li');
  li.textContent = who + ': ' + text;
  $('chat-log').append(li);
}
$('chat-connect').onclick = () => {
  $('chat-state').textContent = 'connecting';
  socket = new WebSocket('wss://' + location.host + '/chat?round=' + cfg.round);
  socket.onopen = () => {
    $('chat-state').textContent = 'connected';
    socket.send(JSON.stringify({type: 'hello', round: cfg.round}));
  };
  socket.onmessage = event => addChat('server', JSON.parse(event.data).text);
  socket.onclose = event => { $('chat-state').textContent = 'closed ' + event.code + ' ' + event.reason; };
};
$('chat-form').onsubmit = event => {
  event.preventDefault();
  const text = $('chat-input').value;
  socket.send(JSON.stringify({type: 'say', text}));
  addChat('me', text);
  $('chat-input').value = '';
};
"""

APP_BODY = """
<header><h1>Inventory</h1> <span id="network"></span></header>
<div id="offline-banner" role="alert" hidden>You are offline</div>
<section aria-label="Items">
  <p id="state" data-state="idle">idle</p>
  <button id="load">Load items</button> <button id="retry" hidden>Retry</button>
  <ul id="items"></ul>
  <ol id="trail"></ol>
</section>
<section aria-label="Notes">
  <form id="note-form"><input id="note" aria-label="Note"> <button>Save note</button></form>
  <p id="note-status"></p> <p id="queued"></p>
  <ul id="notes"></ul>
</section>
<section aria-label="Report">
  <button id="report-button">Load report</button>
  <span id="spinner" role="progressbar" aria-label="Loading report" hidden></span>
  <p id="report"></p>
</section>
<section aria-label="Chat">
  <button id="chat-connect">Connect chat</button> <span id="chat-state">not connected</span>
  <form id="chat-form"><input id="chat-input" aria-label="Message"> <button>Send</button></form>
  <ul id="chat-log"></ul>
</section>
<script>{}</script>
""".format(APP_JS)

APP_HEAD = """<style>
[hidden] { display: none !important; }
#spinner { display: inline-block; width: 1em; height: 1em; border: 2px solid #999; border-top-color: transparent;
           border-radius: 50%; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
#offline-banner { background: #b00; color: #fff; padding: 4px; }
</style>"""

# Signs in to the Basic-auth protected /admin by sending the Authorization header itself (see
# check_basic_auth for why the browser's own http_credentials cannot be used against a routed site).
LOGIN_BODY = """
<form id="login"><input id="user" aria-label="User"> <input id="password" type="password" aria-label="Password">
  <button>Sign in</button></form>
<p id="login-status"></p>
<div id="admin"></div>
<script>
document.getElementById('login').onsubmit = async event => {
  event.preventDefault();
  const user = document.getElementById('user').value, password = document.getElementById('password').value;
  const response = await fetch('/admin', {headers: {'Authorization': 'Basic ' + btoa(user + ':' + password)}});
  const page = new DOMParser().parseFromString(await response.text(), 'text/html');
  document.getElementById('login-status').textContent = response.ok ? 'Signed in'
      : 'Sign-in failed: HTTP ' + response.status + ' (' + response.headers.get('WWW-Authenticate') + ')';
  document.getElementById('admin').replaceChildren(...(response.ok ? page.body.childNodes : []));
};
</script>
"""


class Scenario:
    """Everything one round varies, derived from the round number."""

    def __init__(self, n):
        self.n = n
        self.token = "tok-{:04d}-{}".format(n, n * 7919 % 10007)
        self.faults = FAULT_PLANS[(n - 1) % len(FAULT_PLANS)]
        # Every other pass through the fault plans allows one attempt more, so the same plan sometimes
        # recovers by itself and sometimes needs the Retry button.
        self.attempts = 3 + (n - 1) // len(FAULT_PLANS) % 2
        self.backoff_ms = 100 + n % 3 * 50
        # The app's own fetch timeout. The check holds every items request for a few grid round trips
        # (it asserts the loading state before answering), so this must comfortably exceed that even on a
        # loaded runner, or a planned 500/abort/success would turn into a timeout. Only "slow" faults wait
        # it out.
        self.timeout_ms = 2000 + n % 2 * 500
        self.items = [{"id": n * 10 + i, "name": "{}-{}".format(WORDS[(n + i * 5) % len(WORDS)], n),
                       "qty": (n * 7 + i * 3) % 20 + 1} for i in range(2 + n % 4)]
        self.notes = ["restock {} by {}".format(WORDS[n % len(WORDS)], n + 1),
                      "offline audit {}".format(n * 3)]
        self.chat = ["hi from round {}".format(n), "order {} {}s".format(n * 3, WORDS[(n + 2) % len(WORDS)])]
        self.close_code = 4000 + n % 100
        self.user = "ops{}".format(n)
        self.password = "pw-{}-{}".format(n, n * 31 % 97)

    def note_id(self, k):
        """The id the fake server gives this round's k-th note (from 1)."""
        return 500 + self.n * 10 + k

    def app_url(self):
        return "{}/?round={}&token={}&attempts={}&backoff={}&timeout={}".format(
            ORIGIN, self.n, self.token, self.attempts, self.backoff_ms, self.timeout_ms)

    def basic_auth(self):
        return "Basic " + b64encode("{}:{}".format(self.user, self.password).encode()).decode()

    def expected_trail(self):
        """The states the app must pass through: its retry loop replayed against the fault plan."""
        trail, attempt = [], 0
        for fault in self.faults:
            attempt += 1
            failed = "attempt {} failed: {}".format(attempt, FAULT_TEXT[fault])
            trail.append("loading: attempt {}".format(attempt))
            if attempt == self.attempts:
                trail.append("error: " + failed)
                attempt = 0
            else:
                trail.append("retrying: {}, retrying in {} ms".format(failed, self.backoff_ms * 2 ** (attempt - 1)))
        return trail + ["loading: attempt {}".format(attempt + 1), "ready: {} items".format(len(self.items))]


class Counts:
    def __init__(self):
        self.items_requests = 0
        self.faults = {fault: 0 for fault in FAULT_TEXT}
        self.manual_retries = 0
        self.notes = 0
        self.queued = 0
        self.reports = 0
        self.chat_messages = 0
        self.auth_checks = 0


class Api:
    """The fake server's side of one round: answers what it can at once and holds the requests whose
    timing the check controls (items and report) until the check answers them."""

    def __init__(self, scenario):
        self.s = scenario
        self.held = {"/api/items": [], "/api/report": []}
        self.notes = []  # (headers, JSON body) of every note POST
        self.admin_auth = []  # Authorization header of every /admin request

    def handle(self, route, request):
        path = urlsplit(request.url).path
        if path == "/":
            route.fulfill(content_type="text/html", body=html(APP_BODY, "Inventory round {}".format(self.s.n), APP_HEAD))
        elif path == "/login":
            route.fulfill(content_type="text/html", body=html(LOGIN_BODY, "Sign in"))
        elif path in self.held:
            self.held[path].append(route)
        elif path == "/api/notes" and request.method == "POST":
            body = request.post_data_json
            self.notes.append((request.headers, body))
            route.fulfill(status=201, json={"id": self.s.note_id(len(self.notes)), "text": body.get("text")})
        elif path == "/admin":
            auth = request.headers.get("authorization")
            self.admin_auth.append(auth)
            if auth == self.s.basic_auth():
                route.fulfill(content_type="text/html",
                              body=html("<h1>Admin console</h1><p id='user'>Signed in as {}</p>".format(self.s.user)))
            else:
                route.fulfill(status=401, content_type="text/html", headers={"WWW-Authenticate": 'Basic realm="poc-admin"'},
                              body=html("<h1>401 Unauthorized</h1>", "Unauthorized"))
        else:
            route.fulfill(status=404, body="not found")


def isolated_context(browser, unexpected, **options):
    """A context in which every request outside the fake site fails and is recorded."""
    context = browser.new_context(**options)
    context.set_default_timeout(TIMEOUT_MS)

    def block(route, request):
        unexpected.append(request.url)
        route.abort()

    # Registered first, so FakeSite's route (registered later) takes precedence for its origin.
    context.route("**/*", block)
    return context


def wait_for_held(page, routes, what):
    """The next held request, once the app has sent it. Waiting on the page lets Playwright dispatch the
    route handler that queues it."""
    deadline = time.monotonic() + TIMEOUT_MS / 1000
    while not routes:
        assert time.monotonic() < deadline, "the app never sent {}".format(what)
        page.wait_for_timeout(25)
    return routes.pop(0)


def answer_late(routes):
    """Answer requests the app already gave up on (the 'slow' fault); the browser has cancelled them."""
    while routes:
        with contextlib.suppress(Error):
            routes.pop(0).fulfill(json={"items": [{"name": "STALE", "qty": 0}]})


def check_items(page, s, api, counts):
    state = page.locator("#state")
    held = api.held["/api/items"]
    late = []
    page.get_by_role("button", name="Load items").click()
    attempt = 0
    for fault in s.faults + ("ok",):
        attempt += 1
        route = wait_for_held(page, held, "items request {}".format(attempt))
        answer_late(late)
        headers = route.request.headers
        assert headers.get("authorization") == "Bearer " + s.token, \
            "items request {} carried authorization {!r}".format(attempt, headers.get("authorization"))
        assert headers.get("accept") == "application/json", "items request {} accepts {!r}".format(attempt, headers.get("accept"))
        assert parse_qs(urlsplit(route.request.url).query).get("round") == [str(s.n)], \
            "items request {} went to {}".format(attempt, route.request.url)
        expect(state, "app should be loading while request {} is pending".format(attempt)).to_have_text("attempt {}".format(attempt))
        expect(state).to_have_attribute("data-state", "loading")
        expect(page.get_by_role("button", name="Load items"), "Load items should be disabled while loading").to_be_disabled()
        if fault == "ok":
            route.fulfill(json={"items": s.items})
            break
        counts.faults[fault] += 1
        if fault == "500":
            route.fulfill(status=500, json={"error": "injected failure"})
        elif fault == "abort":
            route.abort()
        else:
            late.append(route)  # never answered in time: the app's own timeout gives up on it
        if attempt == s.attempts:
            expect(state, "app should give up after {} attempts".format(attempt)).to_have_text(
                "attempt {} failed: {}".format(attempt, FAULT_TEXT[fault]))
            expect(state).to_have_attribute("data-state", "error")
            answer_late(late)
            page.get_by_role("button", name="Retry").click()
            counts.manual_retries += 1
            attempt = 0

    expect(state, "app should show the data after the faults").to_have_text("{} items".format(len(s.items)))
    expect(page.get_by_role("button", name="Retry")).to_be_hidden()
    expect(page.locator("#items li"), "items list should show this round's data (and no stale answer)").to_have_text(
        ["{} x{}".format(item["name"], item["qty"]) for item in s.items])
    expect(page.locator("#trail li"), "app should pass through the loading, retry and error states in order").to_have_text(
        s.expected_trail())
    return len(s.faults) + 1


def check_notes(page, context, s, api, counts):
    page.get_by_role("textbox", name="Note").fill(s.notes[0])
    page.get_by_role("button", name="Save note").click()
    expect(page.locator("#note-status")).to_have_text("Saved note #{}: {}".format(s.note_id(1), s.notes[0]))
    headers, body = api.notes[-1]
    assert headers.get("content-type", "").startswith("application/json"), "note POST content type {!r}".format(headers.get("content-type"))
    assert headers.get("authorization") == "Bearer " + s.token, "note POST authorization {!r}".format(headers.get("authorization"))
    assert headers.get("x-request-id") == "note-{}-1".format(s.n), "note POST request id {!r}".format(headers.get("x-request-id"))
    assert body == {"text": s.notes[0], "round": s.n, "tags": ["r{}".format(s.n), "poc"], "queued": False}, \
        "note POST body {}".format(body)

    banner = page.get_by_role("alert")
    context.set_offline(True)
    expect(banner, "offline banner should appear when the browser goes offline").to_have_text("You are offline")
    expect(page.locator("#network")).to_have_text("offline")
    page.get_by_role("textbox", name="Note").fill(s.notes[1])
    page.get_by_role("button", name="Save note").click()
    expect(page.locator("#note-status")).to_have_text("Queued (offline): " + s.notes[1])
    expect(page.locator("#queued")).to_have_text("1 queued")
    assert len(api.notes) == 1, "app sent a note while offline ({} POSTs)".format(len(api.notes))

    context.set_offline(False)
    expect(banner, "offline banner should go away back online").to_be_hidden()
    expect(page.locator("#network")).to_have_text("online")
    expect(page.locator("#queued"), "queued note should be sent back online").to_have_text("0 queued")
    expect(page.locator("#note-status")).to_have_text("Saved note #{}: {}".format(s.note_id(2), s.notes[1]))
    assert len(api.notes) == 2, "expected 2 note POSTs, the fake server saw {}".format(len(api.notes))
    headers, body = api.notes[-1]
    assert headers.get("x-request-id") == "note-{}-2".format(s.n), "queued note request id {!r}".format(headers.get("x-request-id"))
    assert body == {"text": s.notes[1], "round": s.n, "tags": ["r{}".format(s.n), "poc"], "queued": True}, \
        "queued note body {}".format(body)
    expect(page.locator("#notes li")).to_have_count(2)
    counts.notes += 2
    counts.queued += 1


def check_report(page, s, api, counts):
    button = page.get_by_role("button", name="Load report")
    spinner = page.get_by_role("progressbar", name="Loading report")
    expect(spinner).to_be_hidden()
    button.click()
    route = wait_for_held(page, api.held["/api/report"], "the report request")
    # The report is held until here, so the spinner must be up however fast the grid is.
    expect(spinner, "spinner should show while the report is slow").to_be_visible()
    expect(button, "report button should be disabled while loading").to_be_disabled()
    total = sum(item["qty"] for item in s.items)
    route.fulfill(json={"title": "Report {}".format(s.n), "total": total})
    expect(spinner, "spinner should disappear once the report arrives").to_be_hidden()
    expect(page.locator("#report")).to_have_text("Report {}: total {}".format(s.n, total))
    expect(button).to_be_enabled()
    counts.reports += 1


def check_chat(page, s, counts):
    received = []

    def on_socket(ws):
        def on_message(message):
            data = json.loads(message)
            received.append(data)
            if data["type"] == "hello":
                ws.send(json.dumps({"text": "welcome to round {}".format(data["round"])}))
            elif data["text"] == "/quit":
                ws.close(code=s.close_code, reason="bye")
            else:
                ws.send(json.dumps({"text": "echo {}: {}".format(len(received) - 1, data["text"].upper())}))

        ws.on_message(on_message)

    page.route_web_socket(ORIGIN.replace("https://", "wss://") + "/chat**", on_socket)
    log = page.locator("#chat-log li")
    page.get_by_role("button", name="Connect chat").click()
    expect(page.locator("#chat-state")).to_have_text("connected")
    expected = ["server: welcome to round {}".format(s.n)]
    expect(log, "mocked server should greet the chat").to_have_text(expected)
    for i, text in enumerate(s.chat, start=1):
        page.get_by_role("textbox", name="Message").fill(text)
        page.get_by_role("button", name="Send").click()
        expected += ["me: " + text, "server: echo {}: {}".format(i, text.upper())]
        expect(log, "chat should render the mocked reply to message {}".format(i)).to_have_text(expected)
    page.get_by_role("textbox", name="Message").fill("/quit")
    page.get_by_role("button", name="Send").click()
    expect(page.locator("#chat-state"), "mocked server should close the chat").to_have_text("closed {} bye".format(s.close_code))
    assert received == [{"type": "hello", "round": s.n}] + [{"type": "say", "text": t} for t in s.chat + ["/quit"]], \
        "mocked server received {}".format(received)
    counts.chat_messages += len(received)


def check_basic_auth(browser, app_page, s, api, unexpected, capture):
    """/admin is protected by HTTP Basic auth: a fresh context without credentials gets the 401 page, and
    the app's sign-in page gets in with the round's credentials (after a wrong password is refused).

    The browser's own http_credentials cannot be used here: the browser never sees a challenge in a 401
    answered by route.fulfill, and with send="always" Playwright adds the header only when a request goes
    on to the network, which a routed fake site never does. Both were tried on the grid's Chromium."""
    anonymous_api = Api(s)
    context = isolated_context(browser, unexpected)
    FakeSite(context, ORIGIN, anonymous_api.handle)
    page = capture.add(context.new_page())
    response = page.goto(ORIGIN + "/admin")
    assert response.status == 401, "/admin without credentials returned HTTP {}".format(response.status)
    assert response.headers.get("www-authenticate") == 'Basic realm="poc-admin"', \
        "401 challenge was {!r}".format(response.headers.get("www-authenticate"))
    expect(page.get_by_role("heading"), "/admin should show its 401 page without credentials").to_have_text("401 Unauthorized")
    assert anonymous_api.admin_auth == [None], "requests to /admin without credentials carried {}".format(anonymous_api.admin_auth)
    context.close()

    page = app_page
    page.goto(ORIGIN + "/login")
    wrong = s.password[::-1] + "x"
    page.get_by_role("textbox", name="User").fill(s.user)
    page.get_by_label("Password").fill(wrong)
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator("#login-status"), "a wrong password should be refused").to_have_text(
        'Sign-in failed: HTTP 401 (Basic realm="poc-admin")')
    page.get_by_label("Password").fill(s.password)
    page.get_by_role("button", name="Sign in").click()
    expect(page.locator("#login-status"), "the right credentials should be accepted").to_have_text("Signed in")
    expect(page.locator("#admin").get_by_role("heading")).to_have_text("Admin console")
    expect(page.locator("#admin #user")).to_have_text("Signed in as " + s.user)
    wrong_auth = "Basic " + b64encode("{}:{}".format(s.user, wrong).encode()).decode()
    assert api.admin_auth == [wrong_auth, s.basic_auth()], "/admin saw authorization headers {}".format(api.admin_auth)


def run_round(browser, s, counts, capture):
    unexpected = []
    api = Api(s)
    context = isolated_context(browser, unexpected)
    site = FakeSite(context, ORIGIN, api.handle)
    page = capture.add(context.new_page())
    response = page.goto(s.app_url())
    assert response.ok, "app page returned HTTP {}".format(response.status)
    expect(page.locator("#state")).to_have_text("idle")
    expect(page.locator("#network")).to_have_text("online")

    expected_requests = check_items(page, s, api, counts)
    assert site.count("GET", "/api/items") == expected_requests, \
        "fake server saw {} items requests, expected {} for faults {}".format(site.count("GET", "/api/items"), expected_requests, s.faults)
    counts.items_requests += expected_requests
    check_notes(page, context, s, api, counts)
    check_report(page, s, api, counts)
    check_chat(page, s, counts)
    assert site.count("POST", "/api/notes") == 2 and site.count("GET", "/api/report") == 1, \
        "unexpected API traffic: {}".format(site.requests)
    check_basic_auth(browser, page, s, api, unexpected, capture)
    counts.auth_checks += 1
    assert not unexpected, "requests left the fake site: {}".format(unexpected)
    # Closed only after a passing round: on failure the pages stay open for capture's screenshots and the
    # grid_browser fixture closes everything.
    context.close()


def test_network_resilience(grid_browser, budget, capture):
    expect.set_options(timeout=TIMEOUT_MS)
    counts = Counts()
    round_s = 0.0
    while budget.more(reserve_s=round_s):
        started = time.monotonic()
        run_round(grid_browser, Scenario(budget.rounds), counts, capture)
        round_s = time.monotonic() - started
    print("poc_network_resilience: {} rounds in {:.0f}s; {} items requests ({} HTTP 500, {} aborted, {} timed out, "
          "{} manual retries); {} notes ({} queued offline); {} slow reports; {} chat messages; {} basic auth checks".format(
              budget.rounds, budget.elapsed(), counts.items_requests, counts.faults["500"], counts.faults["abort"],
              counts.faults["slow"], counts.manual_retries, counts.notes, counts.queued, counts.reports,
              counts.chat_messages, counts.auth_checks))
