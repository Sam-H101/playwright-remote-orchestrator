"""TOTP sign-in: a grid browser gets through an internal site's two-step verification with codes from the Grid UI.

The check serves a fake intranet from a FakeSite that asks for a password and then a 6-digit TOTP code
(RFC 6238: HMAC-SHA1, 30 s steps, one step of clock drift allowed either way, each step accepted once),
the way Entra ID, Okta and most self-hosted apps do for an account enrolled in an authenticator app. The
account's seed lives in the Grid UI, as the MFA secret POC_TOTP_SECRET_NAME (default poc-intranet): the
check asks the UI for every code it types (src/mfa.py, with its client token) while the prompt is on
screen, so neither the check nor the runner holds the seed, and the runner only ever sees 6 digits. The
UI hands each 30 s window out once, so a second sign-in gets the next window's code and waits for it.
A real site keeps its own copy of the enrolment to check codes against; the fake site stands in for it,
which is the only reason this POC gives it the seed too (POC_TOTP_SEED, base32, the same as the UI's
secret) and the password (POC_TOTP_PASSWORD, random when unset). Each round proves:
- a code from 60 s ago and made-up codes are refused, the third failure sends the user back to the
  password, and even the right code is refused after that;
- a sign-in that starts from a protected page (password, then a code from the UI, waiting for its window
  when the UI hands out the next one, never typed before it opens) lands back on that page, whose API
  then answers;
- the code the UI gave that sign-in is refused in another browser context, and the code the UI hands
  that context next (from the next window) works;
- signing out ends one session and leaves the other one working;
- the session expires after SESSION_TTL_S, the app sends the user to sign in again, and a new code takes
  them back to the page they were on.
At the end it checks that the seed never reached the browser, the password only went to the sign-in API,
and the site took exactly the time steps the UI said it handed out, none of them early. Against a real internal site the same steps
apply: store its enrolment in the Grid UI, pin the check to a runner that can reach the site
(GATEWAY_NODE) and keep the Grid UI host's clock in sync (NTP), because the code is computed there.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import struct
import time
from collections import Counter
from http.cookies import SimpleCookie
from unittest import mock
from urllib.parse import urlsplit

from playwright.sync_api import Error, expect

from conftest import FakeSite, html
from src import mfa

ORIGIN = "https://intranet.poc.test"
TIMEOUT_MS = 10 * 1000
STEP_S = 30
DIGITS = 6
# The server also takes the codes of the step before and after the current one (clock drift).
DRIFT_STEPS = 1
# A code is only typed with at least this much of its window left, so it can't run out on the way (the
# minValidS the Grid UI is asked for).
MIN_LEFT_S = 4
MAX_TRIES = 3
PENDING_TTL_S = 120
# Longer than a sign-in plus one wait for the next window, so the first session is still alive when the
# second one signs out.
SESSION_TTL_S = 45
# Rounds take 60-100 s, mostly waiting for windows and the session expiry. Another round starts while
# the budget has room for half of the slowest so far (this much before the first has finished), so a run
# ends within half a round of the budget rather than up to a whole round short of it.
ROUND_RESERVE_S = 40
USER = "svc-monitor"
SECRET_NAME = os.environ.get("POC_TOTP_SECRET_NAME") or "poc-intranet"
# The fake site's copy of the enrolment. No random default: it must be the seed of the UI's secret.
SEED = os.environ.get("POC_TOTP_SEED", "")
PASSWORD = os.environ.get("POC_TOTP_PASSWORD") or secrets.token_urlsafe(18)
# RFC 6238 appendix B, SHA-1: the seed "12345678901234567890" gives these 8-digit codes at these times.
RFC_SEED = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = ((59, "94287082"), (1111111109, "07081804"), (1111111111, "14050471"), (1234567890, "89005924"),
               (2000000000, "69279037"), (20000000000, "65353130"))
PEOPLE = [{"name": "Ada Byron", "team": "Platform"}, {"name": "Grace Hopper", "team": "Compilers"},
          {"name": "Alan Turing", "team": "Research"}, {"name": "Katherine Johnson", "team": "Flight"},
          {"name": "Edsger Dijkstra", "team": "Platform"}]
# The pages that need a session, by path, with their headings. A round starts from /people, so a sign-in that
# lost the page the user asked for would land on /dashboard (the default) and fail.
PROTECTED = {"/dashboard": "Dashboard", "/people": "People"}
START = "/people"
NOTICE = {"required": "Sign in to continue.", "expired": "Your session expired. Sign in again.",
          "signedout": "You are signed out.", "restart": "Too many wrong codes. Sign in again."}
REUSED = "That code was already used. Wait for the next one."

LOGIN_PAGE = html("""
<h1>Intranet</h1>
<p id="notice" role="status"></p>
<form id="login">
  <label>User name <input id="user" autocomplete="username" required></label>
  <label>Password <input id="password" type="password" autocomplete="current-password" required></label>
  <button>Sign in</button>
</form>
<p id="error" role="alert"></p>
<script>
const $ = id => document.getElementById(id);
const q = new URLSearchParams(location.search);
const NOTICES = %s;
$('notice').textContent = NOTICES[q.get('reason')] || '';
$('login').onsubmit = async event => {
  event.preventDefault();
  $('error').textContent = '';
  const response = await fetch('/api/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                              body: JSON.stringify({user: $('user').value, password: $('password').value})});
  if (response.ok) location.href = '/2fa?next=' + encodeURIComponent(q.get('next') || '/dashboard');
  else $('error').textContent = (await response.json()).error;
};
</script>""".replace("%s", json.dumps(NOTICE)), "Sign in")

CODE_PAGE = html("""
<h1>Two-step verification</h1>
<p>Enter the 6-digit code from your authenticator app.</p>
<form id="verify">
  <label>Code <input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>
  <button>Verify</button>
</form>
<p id="error" role="alert"></p>
<script>
const $ = id => document.getElementById(id);
const wanted = new URLSearchParams(location.search).get('next') || '';
// Only a path on this site: never send the user to another origin after signing in.
const next = wanted.startsWith('/') && !wanted.startsWith('//') ? wanted : '/dashboard';
$('verify').onsubmit = async event => {
  event.preventDefault();
  $('error').textContent = '';
  const response = await fetch('/api/2fa', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                            body: JSON.stringify({code: $('code').value})});
  if (response.ok) { location.href = next; return; }
  const data = await response.json();
  if (data.restart) { location.href = '/login?reason=restart&next=' + encodeURIComponent(next); return; }
  $('error').textContent = data.error;
  $('code').value = '';
};
</script>""", "Verify")

DASHBOARD_PAGE = html("""
<header><h1>Intranet</h1> <span id="who">Signed in as __USER__</span> <button id="signout">Sign out</button></header>
<h2 id="page">__PAGE__</h2>
<button id="load">Load directory</button>
<p id="status"></p>
<ul id="people"></ul>
<script>
const $ = id => document.getElementById(id);
$('load').onclick = async () => {
  const response = await fetch('/api/directory');
  const data = await response.json();
  if (response.status === 401) {
    location.href = '/login?reason=' + data.error + '&next=' + encodeURIComponent(location.pathname);
    return;
  }
  $('people').replaceChildren(...data.people.map(person => {
    const li = document.createElement('li');
    li.textContent = person.name + ', ' + person.team;
    return li;
  }));
  $('status').textContent = data.people.length + ' people';
};
$('signout').onclick = async () => {
  await fetch('/api/logout', {method: 'POST'});
  location.href = '/login?reason=signedout';
};
</script>""".replace("__USER__", USER), "Dashboard")


def seed_key(seed):
    """The key in a base32 seed, written the way authenticator apps show it (any case, spaces, dashes)."""
    seed = re.sub(r"[\s-]", "", seed).upper()
    return base64.b32decode(seed + "=" * (-len(seed) % 8))


def step_at(t):
    return int(t // STEP_S)


def totp(key, step, digits=DIGITS):
    """The RFC 6238 code of a time step: RFC 4226 HOTP with the step number as the counter."""
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def redirect_page(target):
    return html("<script>location.replace({!r})</script>".format(target), "Redirecting")


class Intranet:
    """The fake site's server: one account with a password and a TOTP enrolment, sign-ins waiting for
    their code (the pending cookie), and sessions (the sid cookie). Every context's FakeSite shares it."""

    def __init__(self, seed):
        self.key = seed_key(seed)
        self.last_step = -1
        self.pending = {}
        self.sessions = {}
        self.seen = []
        self.stats = Counter()
        # The steps of the codes that signed someone in, in order.
        self.taken = []

    def verify(self, code):
        """'ok', 'reused' (a step that already signed someone in) or 'wrong'."""
        now = step_at(time.time())
        for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 1):
            if hmac.compare_digest(totp(self.key, step), code):
                if step <= self.last_step:
                    return "reused"
                # Taken, as the drift allows, but counted: a code from the UI is typed once its window
                # opens, so one from a later window means the check didn't wait for it.
                if step > now:
                    self.stats["codes early"] += 1
                self.last_step = step
                self.taken.append(step)
                return "ok"
        return "wrong"

    def current_code(self):
        """The right code for now, without using it up (for requests the site must refuse before it checks)."""
        return totp(self.key, step_at(time.time()))

    def bad_codes(self):
        """MAX_TRIES codes the site must call wrong: the code of two steps ago (outside the allowed drift),
        then made-up ones, leaving out any that happens to be valid in the next few seconds."""
        now = step_at(time.time())
        valid = {totp(self.key, step) for step in range(now - DRIFT_STEPS, now + DRIFT_STEPS + 2)}
        stale = totp(self.key, now - DRIFT_STEPS - 1)
        made_up = (str((int(stale) + 104729 * i) % 10 ** DIGITS).zfill(DIGITS) for i in range(1, 20))
        return [code for code in (stale, *made_up) if code not in valid][:MAX_TRIES]

    def session(self, cookies):
        """'ok', 'expired' (a session that has just timed out, forgotten from now on) or 'required'."""
        sid = cookies["sid"].value if "sid" in cookies else None
        if sid not in self.sessions:
            return "required"
        if self.sessions[sid] <= time.time():
            del self.sessions[sid]
            return "expired"
        return "ok"

    def expires(self, sid):
        return self.sessions[sid]

    def handle(self, route, request):
        path = urlsplit(request.url).path
        self.seen.append((path, request.url, request.post_data or ""))
        cookies = SimpleCookie(request.all_headers().get("cookie", ""))
        post = request.method == "POST"
        if path == "/login":
            return route.fulfill(content_type="text/html", body=LOGIN_PAGE)
        if path == "/2fa":
            ticket = cookies["pending"].value if "pending" in cookies else None
            if self.pending.get(ticket, {}).get("until", 0) > time.time():
                return route.fulfill(content_type="text/html", body=CODE_PAGE)
            return route.fulfill(content_type="text/html", body=redirect_page("/login?reason=required"))
        if path in PROTECTED:
            state = self.session(cookies)
            if state == "ok":
                return route.fulfill(content_type="text/html", body=DASHBOARD_PAGE.replace("__PAGE__", PROTECTED[path]))
            return route.fulfill(content_type="text/html",
                                 body=redirect_page("/login?reason={}&next={}".format(state, path)))
        if path == "/api/login" and post:
            data = request.post_data_json or {}
            # As bytes: compare_digest refuses str with non-ASCII characters.
            if not (hmac.compare_digest(str(data.get("user", "")).encode(), USER.encode())
                    and hmac.compare_digest(str(data.get("password", "")).encode(), PASSWORD.encode())):
                self.stats["bad passwords"] += 1
                return route.fulfill(status=401, json={"error": "Wrong user name or password."})
            ticket = secrets.token_urlsafe(16)
            self.pending[ticket] = {"tries": 0, "until": time.time() + PENDING_TTL_S}
            self.stats["passwords"] += 1
            return route.fulfill(json={"next": "/2fa"}, headers={
                "set-cookie": "pending={}; Path=/; Secure; HttpOnly; SameSite=Strict".format(ticket)})
        if path == "/api/2fa" and post:
            ticket = cookies["pending"].value if "pending" in cookies else None
            pending = self.pending.get(ticket)
            if pending is None or pending["until"] <= time.time():
                self.stats["codes without a password"] += 1
                return route.fulfill(status=403, json={"error": "Sign in again.", "restart": True})
            code = str((request.post_data_json or {}).get("code", ""))
            if not re.fullmatch(r"\d{%d}" % DIGITS, code):
                return route.fulfill(status=400, json={"error": "Enter the {}-digit code.".format(DIGITS)})
            result = self.verify(code)
            self.stats["codes " + result] += 1
            if result == "reused":
                return route.fulfill(status=409, json={"error": REUSED})
            if result == "wrong":
                pending["tries"] += 1
                left = MAX_TRIES - pending["tries"]
                if left <= 0:
                    del self.pending[ticket]
                    return route.fulfill(status=403, json={"error": "Too many wrong codes.", "restart": True})
                return route.fulfill(status=401, json={"error": "That code is wrong. {} {} left.".format(
                    left, "try" if left == 1 else "tries")})
            del self.pending[ticket]
            sid = secrets.token_urlsafe(16)
            self.sessions[sid] = time.time() + SESSION_TTL_S
            return route.fulfill(json={"user": USER}, headers={
                "set-cookie": "sid={}; Path=/; Secure; HttpOnly; SameSite=Strict".format(sid)})
        if path == "/api/directory":
            state = self.session(cookies)
            if state != "ok":
                return route.fulfill(status=401, json={"error": state})
            self.stats["directory loads"] += 1
            return route.fulfill(json={"people": PEOPLE})
        if path == "/api/logout" and post:
            if "sid" in cookies:
                self.sessions.pop(cookies["sid"].value, None)
            self.stats["sign-outs"] += 1
            return route.fulfill(json={}, headers={
                "set-cookie": "sid=; Path=/; Max-Age=0; Secure; HttpOnly; SameSite=Strict"})
        route.fulfill(status=404, content_type="text/plain", body="not found")


class Authenticator:
    """The check's authenticator app: the Grid UI's MFA secret, asked for a code at each prompt. The UI
    hands each step out once, as the site takes each one once. Keeps the last code, which the site must
    now refuse, and the step the UI said each code is from, which the site must have taken."""

    def __init__(self, name):
        self.name = name
        self.last = None
        self.steps = []
        self.waits = 0
        self.waited_s = 0.0

    def code(self, page, label):
        """A code the site will take, with MIN_LEFT_S of its window left. When the UI hands out the next
        window's code, waits on the page (so route handlers keep being served) until that window opens."""
        def wait(seconds):
            self.waits += 1
            self.waited_s += seconds
            page.wait_for_timeout(seconds * 1000)

        # mfa.code() as any check calls it (the waiting is its job), with the UI's answers kept on the way.
        answers = []
        issue = mfa.issue

        def ask(*args, **kwargs):
            answers.append(issue(*args, **kwargs))
            return answers[-1]

        with mock.patch.object(mfa, "issue", ask):
            self.last = mfa.code(self.name, label=label, min_valid_s=MIN_LEFT_S, sleep=wait)
        assert answers and answers[-1]["code"] == self.last, "mfa.code() returned a code the Grid UI didn't hand out"
        self.steps.append(answers[-1]["step"])
        return self.last


def new_page(browser, site, unexpected, capture):
    """A page in a fresh context that only reaches the fake site; any other request fails and is recorded."""
    context = browser.new_context()
    context.set_default_timeout(TIMEOUT_MS)

    def block(route, request):
        unexpected.append(request.url)
        route.abort()

    # Registered first, so FakeSite's route (registered later) takes precedence for its origin.
    context.route("**/*", block)
    FakeSite(context, ORIGIN, site.handle)
    return capture.add(context.new_page())


def expect_login(page, reason):
    expect(page, "should be sent to sign in ({})".format(reason)).to_have_url(re.compile(r"/login\?reason=" + reason))
    expect(page.locator("#notice")).to_have_text(NOTICE[reason])


def enter_password(page):
    page.get_by_label("User name").fill(USER)
    try:
        page.get_by_label("Password").fill(PASSWORD)
    except Error as ex:
        # main.py logs the output of a failed check: keep the password out of it.
        raise AssertionError("filling in the password failed: " + str(ex).replace(PASSWORD, "<password>")) from None
    page.get_by_role("button", name="Sign in").click()
    expect(page, "the password should lead to the code prompt").to_have_url(re.compile(r"/2fa\?next="))
    expect(page.get_by_role("heading", name="Two-step verification")).to_be_visible()


def enter_code(page, code):
    page.get_by_label("Code").fill(code)
    page.get_by_role("button", name="Verify").click()


def load_directory(page, why):
    """Clicks Load directory and asserts the site answered it with the directory, not with a 401."""
    with page.expect_response(ORIGIN + "/api/directory") as response:
        page.get_by_role("button", name="Load directory").click()
    assert response.value.status == 200, "{}: the directory API answered HTTP {}".format(why, response.value.status)
    expect(page.locator("#status"), why).to_have_text("{} people".format(len(PEOPLE)))


def expect_signed_in(page, path):
    """Signed in on the page at `path`, whose API answers. Returns the session cookie's value."""
    expect(page, "a right code should lead to " + path).to_have_url(ORIGIN + path)
    expect(page.locator("#page")).to_have_text(PROTECTED[path])
    expect(page.locator("#who")).to_have_text("Signed in as " + USER)
    load_directory(page, "a new session should reach the API")
    expect(page.locator("#people li")).to_have_text(["{}, {}".format(p["name"], p["team"]) for p in PEOPLE])
    sids = [c for c in page.context.cookies(ORIGIN) if c["name"] == "sid"]
    assert len(sids) == 1 and sids[0]["httpOnly"] and sids[0]["secure"], \
        "expected one HttpOnly, Secure session cookie, got {}".format([(c["name"], c["httpOnly"], c["secure"]) for c in sids])
    return sids[0]["value"]


def post_code(page, code):
    """The status the site answers a code with when the page posts it directly."""
    return page.evaluate("""async code => (await fetch('/api/2fa', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})})).status""", code)


def run_round(browser, site, auth, unexpected, capture, label):
    # Wrong codes first, since they use up no step and no session: an old code and made-up ones, then
    # back to the password; the right code no longer helps. These come from the site's side, so they take
    # nothing from the UI.
    c = new_page(browser, site, unexpected, capture)
    c.goto(ORIGIN + "/login")
    enter_password(c)
    codes = site.bad_codes()
    for tries, code in enumerate(codes[:-1], 1):
        enter_code(c, code)
        left = MAX_TRIES - tries
        expect(c.locator("#error"), "wrong code {} should be refused".format(tries)).to_have_text(
            "That code is wrong. {} {} left.".format(left, "try" if left == 1 else "tries"))
    enter_code(c, codes[-1])
    expect_login(c, "restart")
    status = post_code(c, site.current_code())
    assert status == 403, "after too many wrong codes the right one got HTTP {}, expected 403".format(status)
    c.context.close()

    # A fresh sign-in, starting from the page the user wants, which it comes back to.
    a = new_page(browser, site, unexpected, capture)
    a.goto(ORIGIN + START)
    expect_login(a, "required")
    enter_password(a)
    enter_code(a, auth.code(a, label))
    sid_a = expect_signed_in(a, START)

    # The code the UI just gave is refused in another context; the UI hands that one the next window's
    # code by itself, and the site takes it.
    b = new_page(browser, site, unexpected, capture)
    b.goto(ORIGIN + "/login")
    enter_password(b)
    enter_code(b, auth.last)
    expect(b.locator("#error"), "a code that was already used should be refused").to_have_text(REUSED)
    expect(b).to_have_url(re.compile(r"/2fa\?"))
    enter_code(b, auth.code(b, label))
    sid_b = expect_signed_in(b, "/dashboard")
    assert sid_a != sid_b, "the two contexts got the same session"

    # Signing out ends that session only.
    b.get_by_role("button", name="Sign out").click()
    expect_login(b, "signedout")
    b.goto(ORIGIN + "/dashboard")
    expect_login(b, "required")
    # Only fails when the UI made the second sign-in wait more than one window, as it does while another
    # check takes codes for the same secret.
    assert site.expires(sid_a) > time.time() + 1, \
        "the first session ran out before the second one signed out (is another check using {!r}?)".format(auth.name)
    load_directory(a, "the other session should still work")
    b.context.close()

    # The first session expires; the page sends the user to sign in again, and back afterwards.
    a.wait_for_timeout(max(site.expires(sid_a) - time.time() + 0.5, 0) * 1000)
    a.get_by_role("button", name="Load directory").click()
    expect_login(a, "expired")
    enter_password(a)
    enter_code(a, auth.code(a, label))
    expect_signed_in(a, START)
    a.get_by_role("button", name="Sign out").click()
    expect_login(a, "signedout")
    # Closed only after a passing round: on failure the pages stay open for capture's screenshots and the
    # grid_browser fixture closes everything.
    a.context.close()


def test_totp_login(grid_browser, budget, capture):
    rfc_key = seed_key(RFC_SEED)
    for t, want in RFC_VECTORS:
        got = totp(rfc_key, step_at(t), 8)
        assert got == want, "TOTP is wrong for the RFC 6238 vector at T={}: {} instead of {}".format(t, got, want)
    assert SEED, ("set POC_TOTP_SEED to the seed of the Grid UI's MFA secret {!r}: the fake site plays the real "
                  "one, which checks codes against its own copy of the enrolment".format(SECRET_NAME))
    try:
        seed_key(SEED)
    except ValueError:
        raise AssertionError("POC_TOTP_SEED is not a base32 seed") from None

    site = Intranet(SEED)
    auth = Authenticator(SECRET_NAME)
    unexpected = []
    slowest_s = 0.0
    while budget.more(reserve_s=slowest_s / 2 or ROUND_RESERVE_S):
        started = time.monotonic()
        run_round(grid_browser, site, auth, unexpected, capture, "poc_totp_login round {}".format(budget.rounds))
        slowest_s = max(slowest_s, time.monotonic() - started)
    rounds = budget.rounds

    assert not unexpected, "requests left the fake site: {}".format(unexpected)
    # What the browser on the runner was given: the password only in the sign-in call, codes only in the
    # code call, the seed nowhere. Messages name paths only, never the secrets.
    secrets_seen = {"seed": (SEED, seed_key(SEED).hex()), "password": (PASSWORD,)}
    leaks = sorted({(name, path) for path, url, body in site.seen for name, values in secrets_seen.items()
                    if any(v in url or v in body for v in values) and not (name == "password" and path == "/api/login")})
    assert not leaks, "secrets reached the browser: {}".format(leaks)
    bad_bodies = [body[:40] for path, url, body in site.seen
                  if path == "/api/2fa" and not re.fullmatch(r'\{"code":"\d{%d}"\}' % DIGITS, body)]
    assert not bad_bodies, "code requests carried more than a code: {}".format(bad_bodies)

    stats = site.stats
    want = {"passwords": 4 * rounds, "codes ok": 3 * rounds, "codes reused": rounds, "codes wrong": MAX_TRIES * rounds,
            "codes without a password": rounds, "sign-outs": 2 * rounds, "bad passwords": 0, "codes early": 0}
    got = {name: stats[name] for name in want}
    assert got == want, "the site counted {}, expected {}".format(got, want)
    # The UI's word for each code against the step the site matched it to: a code that isn't its step's,
    # or a step the site took that the UI didn't say it handed out, shows here.
    assert auth.steps == site.taken, "the Grid UI handed out steps {}, the site took {}".format(auth.steps, site.taken)
    print("poc_totp_login: {} rounds in {:.0f}s; {} sign-ins with {} codes from the Grid UI, {} codes refused "
          "as reused and {} as wrong, {} directory loads, {} waits for the next window ({:.0f}s)".format(
              rounds, budget.elapsed(), stats["codes ok"], len(auth.steps), stats["codes reused"],
              stats["codes wrong"], stats["directory loads"], auth.waits, auth.waited_s))
