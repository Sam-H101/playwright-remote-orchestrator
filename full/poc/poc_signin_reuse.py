"""Saved sign-ins: a grid browser signs in once and reuses the browser state on later runs, so most runs
skip the password and the MFA prompt the way a person's "stay signed in" does.

The check serves a fake single-sign-on setup from two FakeSites: an app (https://app.poc.test) behind a
Microsoft-style identity provider (https://login.poc.test). The IdP's screens follow Microsoft's flow:
"Sign in" (email), then "Enter password", then "Enter code" (a TOTP from the Grid UI via mfa.code, the
secret POC_SIGNIN_SECRET_NAME, default poc-idp), then "Stay signed in?" (Yes sets the persistent IdP
cookie). The app's own session is short-lived and renews silently through the IdP (an OIDC-like redirect
to /authorize and back) while the IdP cookie is still good, the way a real app keeps you signed in without
a fresh prompt. Every cookie is a stateless HMAC-signed token keyed from POC_TOTP_SEED (issue time and
expiry inside it), so a state one run saved still verifies in the next run's fresh fake site; the IdP
"asks again" only when its cookie has expired or was issued before a revocation time the check controls.

The signed-in state is kept in the Grid UI as the saved sign-in POC_SIGNIN_NAME (default poc-idp-session),
and the browser gets it back with signin.reuse(...). The check counts on being the only one using that name
while it runs (its rounds refuse, take leases on and save it on purpose), so two runs at once, like the
scheduled poc_signin_reuse and a run_poc.sh run, each need their own: run_poc.sh defaults it to
poc-idp-session-manual. Each round proves, with exact counters:
- (a) whatever a previous run saved is reused when the IdP still accepts it (no MFA, no password), or a
  fresh sign-in happens when nothing usable is saved;
- (b) after a revocation the saved state is reported rejected and exactly one new sign-in, with one MFA
  code, happens and is saved;
- (c) more fresh contexts reuse it with no password and no code, including after the app's own cookie has
  expired, which renews silently through the IdP cookie;
- (d) while another caller holds the sign-in lease, reuse() waits and then uses the state that caller
  saves without signing in itself; a lease that is released instead makes the waiter sign in;
- (e) the refresh-save after a reuse bumps the version, and a save against the version before it is told
  it is newer.
At the end it checks that no password or seed reached the browser except the password in the sign-in call,
and that no request left the two fake sites. Against a real Microsoft tenant the same steps apply: store
the account's authenticator seed in the Grid UI as POC_SIGNIN_SECRET_NAME, drive the real sign-in screens
with role and label locators in sign_in(), and reuse() keeps the cookies so later runs skip the prompts
(see the README's "Saved sign-ins"). POC_SIGNIN_SECRET_NAME must be a Grid UI MFA secret whose seed is
POC_TOTP_SEED; the check fails while the seed is unset.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import struct
import time
from collections import Counter
from http.cookies import SimpleCookie
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

from playwright.sync_api import Error, expect

from conftest import FakeSite, html
from src import mfa, signin

APP_ORIGIN = "https://app.poc.test"
IDP_ORIGIN = "https://login.poc.test"
TIMEOUT_MS = 10 * 1000
# The account the fake IdP knows. The e-mail isn't secret; the password is made up per run unless set, and
# the check and the in-process fake IdP share it (a real tenant keeps its own copy).
USER = os.environ.get("POC_SIGNIN_USER") or "monitor@poc.test"
PASSWORD = os.environ.get("POC_SIGNIN_PASSWORD") or base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("=")
SECRET_NAME = os.environ.get("POC_SIGNIN_SECRET_NAME") or "poc-idp"
NAME = os.environ.get("POC_SIGNIN_NAME") or "poc-idp-session"
# The fake IdP's copy of the enrolment. No random default: it must be the seed of the UI's secret.
SEED = os.environ.get("POC_TOTP_SEED", "")
# The app session is short so a later reuse exercises the silent renewal through the IdP cookie; the IdP
# cookie lasts the whole run and across the next few runs, as "stay signed in" does.
APP_SESSION_TTL_S = 5
IDP_TTL_S = 1800
ASSERTION_TTL_S = 120
PENDING_TTL_S = 300
STEP_S = 30
DIGITS = 6
DRIFT_STEPS = 1
MIN_LEFT_S = 4
# A round signs in a few times and waits out the app cookie twice; it takes about 60-90 s. Another round
# starts while the budget has room for the slowest so far.
ROUND_RESERVE_S = 90


def seed_key(seed):
    seed = re.sub(r"[\s-]", "", seed).upper()
    return base64.b32decode(seed + "=" * (-len(seed) % 8))


def step_at(t):
    return int(t // STEP_S)


def totp(key, step, digits=DIGITS):
    mac = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


# The cookies and the sign-in assertion are HMAC-signed tokens the IdP and the app both verify with this
# key, derived from the shared seed. A token signed by one run verifies in the next: the state outlives the
# fake site's own memory, exactly as a real cookie outlives the server process.
TOTP_KEY = seed_key(SEED) if SEED else b""
TOKEN_KEY = hashlib.sha256(b"poc_signin_reuse token v1\n" + TOTP_KEY).digest()


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64u(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(payload):
    body = _b64u(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64u(hmac.new(TOKEN_KEY, body.encode(), hashlib.sha256).digest())
    return body + "." + mac


def unsign(token):
    """The payload of a signed token, or None when it isn't one of ours."""
    if not isinstance(token, str) or "." not in token:
        return None
    body, mac = token.split(".", 1)
    good = _b64u(hmac.new(TOKEN_KEY, body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, good):
        return None
    try:
        return json.loads(_unb64u(body))
    except ValueError:
        return None


def cookie(name, value, max_age=None):
    """A Set-Cookie line. Without max_age it is a session cookie; with it, persistent ("stay signed in")."""
    line = "{}={}; Path=/; Secure; HttpOnly; SameSite=Lax".format(name, value)
    if max_age is not None:
        line += "; Max-Age={}".format(int(max_age))
    return line


def redirect_page(target):
    return html("<script>location.replace({!r})</script>".format(target), "Redirecting")


HOME_PAGE = html(
    "<header><h1>Operations</h1> <span id='who'>Signed in as {}</span></header>"
    "<a href='/runbooks'>Runbooks</a>".format(USER), "Operations")

EMAIL_PAGE = html("""
<h1>Sign in</h1>
<form id="f">
  <label>Email, phone, or Skype <input id="email" autocomplete="username" required></label>
  <button>Next</button>
</form>
<p id="error" role="alert"></p>
<script>
const $ = id => document.getElementById(id);
$('f').onsubmit = async event => {
  event.preventDefault();
  $('error').textContent = '';
  const response = await fetch('/api/email', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                              body: JSON.stringify({email: $('email').value})});
  if (response.ok) location.href = '/password';
  else $('error').textContent = (await response.json()).error;
};
</script>""", "Sign in")

PASSWORD_PAGE = html("""
<h1>Enter password</h1>
<form id="f">
  <label>Password <input id="password" type="password" autocomplete="current-password" required></label>
  <button>Sign in</button>
</form>
<p id="error" role="alert"></p>
<script>
const $ = id => document.getElementById(id);
$('f').onsubmit = async event => {
  event.preventDefault();
  $('error').textContent = '';
  const response = await fetch('/api/password', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                 body: JSON.stringify({password: $('password').value})});
  if (response.ok) location.href = '/code';
  else $('error').textContent = (await response.json()).error;
};
</script>""", "Enter password")

CODE_PAGE = html("""
<h1>Enter code</h1>
<p>Enter the code shown by your authenticator app.</p>
<form id="f">
  <label>Code <input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required></label>
  <button>Verify</button>
</form>
<p id="error" role="alert"></p>
<script>
const $ = id => document.getElementById(id);
$('f').onsubmit = async event => {
  event.preventDefault();
  $('error').textContent = '';
  const response = await fetch('/api/code', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({code: $('code').value})});
  if (response.ok) location.href = '/stay';
  else $('error').textContent = (await response.json()).error;
};
</script>""", "Enter code")

STAY_PAGE = html("""
<h1>Stay signed in?</h1>
<p>Do this to reduce the number of times you are asked to sign in.</p>
<button id="yes">Yes</button>
<button id="no">No</button>
<script>
const $ = id => document.getElementById(id);
const finish = async persist => {
  const response = await fetch('/api/stay', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                             body: JSON.stringify({persist})});
  if (response.ok) location.replace((await response.json()).redirect);
};
$('yes').onclick = () => finish(true);
$('no').onclick = () => finish(false);
</script>""", "Stay signed in?")


def _assertion(now):
    """A short-lived signed grant the app turns into its own session, like an OIDC id_token."""
    return sign({"kind": "asrt", "sub": USER, "iat": now, "exp": now + ASSERTION_TTL_S})


class Idp:
    """The fake identity provider: it keeps a persistent cookie after "stay signed in", issues a grant to
    the app when that cookie is still good (silent renewal), and otherwise walks the sign-in screens. It is
    made fresh for each scenario so its counters start at zero; the cookies it reads were signed by an
    earlier scenario or run, which is the whole point."""

    def __init__(self, seen):
        self.seen = seen
        self.counts = Counter()
        self.revoked_before = 0.0
        self.last_step = -1

    def revoke(self):
        """Make every cookie issued before now stale, so the next visit has to sign in again."""
        self.revoked_before = time.time()

    def _cookie_ok(self, cookies, now):
        payload = unsign(cookies["idp"].value) if "idp" in cookies else None
        return (payload is not None and payload.get("kind") == "idp" and payload.get("exp", 0) > now
                and payload.get("iat", 0) >= self.revoked_before)

    def verify(self, code, now):
        step = step_at(now)
        for candidate in range(step - DRIFT_STEPS, step + DRIFT_STEPS + 1):
            if hmac.compare_digest(totp(TOTP_KEY, candidate), code):
                if candidate <= self.last_step:
                    return "reused"
                self.last_step = candidate
                return "ok"
        return "wrong"

    def handle(self, route, request):
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        self.seen.append((IDP_ORIGIN, path, request.url, request.post_data or ""))
        cookies = SimpleCookie(request.all_headers().get("cookie", ""))
        now = time.time()
        post = request.method == "POST"
        if path == "/authorize":
            rd = (query.get("rd") or [""])[0]
            st = (query.get("st") or ["/"])[0]
            if self._cookie_ok(cookies, now):
                self.counts["silent"] += 1
                return route.fulfill(content_type="text/html", body=redirect_page(
                    rd + "?assertion=" + _assertion(now) + "&st=" + quote(st, safe="")))
            self.counts["interactive"] += 1
            pending = sign({"kind": "pend", "rd": rd, "st": st, "iat": now, "exp": now + PENDING_TTL_S})
            return route.fulfill(content_type="text/html", body=EMAIL_PAGE,
                                 headers={"set-cookie": cookie("pend", pending, PENDING_TTL_S)})
        if path == "/password":
            return route.fulfill(content_type="text/html", body=PASSWORD_PAGE)
        if path == "/code":
            return route.fulfill(content_type="text/html", body=CODE_PAGE)
        if path == "/stay":
            return route.fulfill(content_type="text/html", body=STAY_PAGE)
        if path == "/api/email" and post:
            email = str((request.post_data_json or {}).get("email", ""))
            if hmac.compare_digest(email.encode(), USER.encode()):
                self.counts["emails"] += 1
                return route.fulfill(json={"ok": True})
            return route.fulfill(status=401, json={"error": "We couldn't find an account with that user name."})
        if path == "/api/password" and post:
            password = str((request.post_data_json or {}).get("password", ""))
            if hmac.compare_digest(password.encode(), PASSWORD.encode()):
                self.counts["passwords"] += 1
                return route.fulfill(json={"ok": True})
            self.counts["bad passwords"] += 1
            return route.fulfill(status=401, json={"error": "Your account or password is incorrect."})
        if path == "/api/code" and post:
            code = str((request.post_data_json or {}).get("code", ""))
            if not re.fullmatch(r"\d{%d}" % DIGITS, code):
                return route.fulfill(status=400, json={"error": "Enter the {}-digit code.".format(DIGITS)})
            result = self.verify(code, now)
            self.counts["codes " + result] += 1
            if result == "ok":
                return route.fulfill(json={"ok": True})
            return route.fulfill(status=401, json={"error": "That code didn't work. Try again."})
        if path == "/api/stay" and post:
            pending = unsign(cookies["pend"].value) if "pend" in cookies else None
            if pending is None or pending.get("exp", 0) <= now or pending.get("kind") != "pend":
                return route.fulfill(status=403, json={"error": "Your sign-in timed out. Start again."})
            persist = bool((request.post_data_json or {}).get("persist"))
            self.counts["stay"] += 1
            idp = sign({"kind": "idp", "sub": USER, "iat": now, "exp": now + IDP_TTL_S})
            redirect = pending["rd"] + "?assertion=" + _assertion(now) + "&st=" + quote(pending["st"], safe="")
            return route.fulfill(json={"redirect": redirect}, headers={
                "set-cookie": cookie("idp", idp, IDP_TTL_S if persist else None)})
        route.fulfill(status=404, content_type="text/plain", body="not found")


class App:
    """The fake application. A protected page needs the app session cookie; without it (or once it has
    expired) the app bounces to the IdP and, on the way back, turns the IdP's grant into a fresh short
    session. Made fresh per scenario, like the IdP."""

    def __init__(self, seen):
        self.seen = seen
        self.counts = Counter()

    def handle(self, route, request):
        path = urlsplit(request.url).path
        query = parse_qs(urlsplit(request.url).query)
        self.seen.append((APP_ORIGIN, path, request.url, request.post_data or ""))
        cookies = SimpleCookie(request.all_headers().get("cookie", ""))
        now = time.time()
        if path == "/callback":
            grant = unsign((query.get("assertion") or [""])[0])
            if grant is None or grant.get("kind") != "asrt" or grant.get("exp", 0) <= now:
                return route.fulfill(content_type="text/html", body=redirect_page(APP_ORIGIN + "/"))
            self.counts["callbacks"] += 1
            st = (query.get("st") or ["/"])[0]
            target = st if st.startswith("/") and not st.startswith("//") else "/"
            session = sign({"kind": "app", "sub": grant["sub"], "iat": now, "exp": now + APP_SESSION_TTL_S})
            return route.fulfill(content_type="text/html", body=redirect_page(APP_ORIGIN + target),
                                 headers={"set-cookie": cookie("app", session, APP_SESSION_TTL_S)})
        session = unsign(cookies["app"].value) if "app" in cookies else None
        if session is not None and session.get("kind") == "app" and session.get("exp", 0) > now:
            self.counts["home"] += 1
            return route.fulfill(content_type="text/html", body=HOME_PAGE)
        # No live app session: start the OIDC-like round trip to the IdP and back.
        rd = APP_ORIGIN + "/callback"
        return route.fulfill(content_type="text/html", body=redirect_page(
            IDP_ORIGIN + "/authorize?rd=" + quote(rd, safe="") + "&st=" + quote(path, safe="")))


class RoutedBrowser:
    """A stand-in for the grid browser that wires the two fake sites (and a blocker for everything else)
    into every context signin.reuse() opens, so the check doesn't have to reach inside reuse() to route."""

    def __init__(self, browser, wire):
        self._browser = browser
        self._wire = wire
        self.contexts = []

    def new_context(self, **kwargs):
        context = self._browser.new_context(**kwargs)
        context.set_default_timeout(TIMEOUT_MS)
        self._wire(context)
        self.contexts.append(context)
        return context


class Recorder:
    """Follows signin.checkout and signin.save so a round can see what happened and know the saved version
    and the sign-in's id without an admin token: they come back in the UI's answers."""

    def __init__(self):
        self.version = 0
        self.signin_id = None
        self.checkouts = []  # (rejected_version argument, answer status)
        self.saves = []      # answer dicts

    def _track(self, answer):
        if isinstance(answer, dict) and isinstance(answer.get("version"), int):
            self.version = answer["version"]
        if isinstance(answer, dict) and isinstance(answer.get("id"), int):
            self.signin_id = answer["id"]

    def on_checkout(self, rejected_version, answer):
        self.checkouts.append((rejected_version, answer.get("status")))
        self._track(answer)

    def on_save(self, answer):
        self.saves.append(answer)
        self._track(answer)


def _boom(page):
    raise AssertionError("reuse() signed in again when it should have reused the saved sign-in")


class Round:
    """One pass through scenarios (a)-(e). Fresh fake sites per scenario (so counters are clean); the saved
    sign-in in the Grid UI is what carries over, within a round and between runs."""

    def __init__(self, grid, label, capture, seen, unexpected, recorder, totals):
        self.label = label
        self.capture = capture
        self.seen = seen
        self.recorder = recorder
        self.totals = totals
        self._holder = {}
        self._good_state = None

        def wire(context):
            def block(route, request):
                unexpected.append(request.url)
                route.abort()
            context.route("**/*", block)  # first, so the FakeSite routes below win for their origins
            FakeSite(context, APP_ORIGIN, lambda route, request: self._holder["app"].handle(route, request))
            FakeSite(context, IDP_ORIGIN, lambda route, request: self._holder["idp"].handle(route, request))

        self.grid = RoutedBrowser(grid, wire)

    def _scene(self):
        app, idp = App(self.seen), Idp(self.seen)
        self._holder["app"], self._holder["idp"] = app, idp
        return app, idp

    def _signed_in(self, page):
        self.capture.add(page)
        page.goto(APP_ORIGIN + "/")
        home = page.get_by_role("heading", name="Operations")
        asked = page.get_by_role("heading", name="Sign in")
        expect(home.or_(asked)).to_be_visible()
        return home.is_visible()

    def _sign_in(self, scene):
        def sign_in(page):
            self.capture.add(page)
            page.goto(APP_ORIGIN + "/")
            expect(page.get_by_role("heading", name="Sign in")).to_be_visible()
            page.get_by_label("Email, phone, or Skype").fill(USER)
            page.get_by_role("button", name="Next").click()
            expect(page.get_by_role("heading", name="Enter password")).to_be_visible()
            try:
                page.get_by_label("Password").fill(PASSWORD)
            except Error as ex:
                # main.py logs a failed check's output: keep the password out of it.
                raise AssertionError("filling the password failed: " + str(ex).replace(PASSWORD, "<password>")) from None
            page.get_by_role("button", name="Sign in").click()
            expect(page.get_by_role("heading", name="Enter code")).to_be_visible()
            page.get_by_label("Code").fill(mfa.code(SECRET_NAME, label=scene, min_valid_s=MIN_LEFT_S,
                                                    sleep=lambda s: page.wait_for_timeout(s * 1000)))
            page.get_by_role("button", name="Verify").click()
            expect(page.get_by_role("heading", name="Stay signed in?")).to_be_visible()
            page.get_by_role("button", name="Yes").click()
            # Wait for the IdP to hand back to the app and the app to set its session, so the page is settled
            # (not mid-redirect) when reuse() calls signed_in() next, which would otherwise abort its goto.
            expect(page.get_by_role("heading", name="Operations")).to_be_visible()
        return sign_in

    def _take_lease(self, what):
        """Plays another caller that the site refused the saved state to: drops it and holds the lease. The
        failure names the answer's status and version only, never the state a "saved" answer carries."""
        drop = signin.checkout(NAME, rejected_version=self.recorder.version, signin_id=self.recorder.signin_id)
        assert drop["status"] == "sign-in", (
            "expected to hold the lease {} after dropping version {}, got status {!r} at version {!r}: is another "
            "run using the saved sign-in {!r}? Give each run its own POC_SIGNIN_NAME".format(
                what, self.recorder.version, drop.get("status"), drop.get("version"), NAME))
        return drop["leaseToken"]

    def _expect_home(self, session):
        expect(session.page.get_by_role("heading", name="Operations"),
               "reuse() should leave the page signed in to the app").to_be_visible()

    def run(self):
        self._scenario_reuse_or_signin()
        self._scenario_revocation()
        self._scenario_silent_renewal()
        self._scenario_lease_contention()
        self._scenario_refresh()

    # (a) reuse what a previous run saved, or sign in when nothing usable is saved.
    def _scenario_reuse_or_signin(self):
        app, idp = self._scene()
        with signin.reuse(self.grid, NAME, sign_in=self._sign_in(self.label + " (a)"),
                          signed_in=self._signed_in, refresh=False) as session:
            self._expect_home(session)
            reused = session.reused
        if reused:
            assert idp.counts["passwords"] == 0 and idp.counts["codes ok"] == 0, \
                "a reuse typed a password or a code: {}".format(dict(idp.counts))
            self.totals["reuses"] += 1
        else:
            assert idp.counts["passwords"] == 1 and idp.counts["codes ok"] == 1, \
                "a fresh sign-in should type one password and one code, not {}".format(dict(idp.counts))
            self.totals["signins"] += 1
            self.totals["saves"] += 1
        self._add_codes_passwords(idp)

    # (b) a revocation: the saved state is rejected once, and exactly one new sign-in happens and is saved.
    def _scenario_revocation(self):
        # Let the saved state's app cookie expire, so the app re-consults the IdP instead of serving its own
        # still-live session; only then does revoking the IdP cookie send the browser back to sign in.
        time.sleep(APP_SESSION_TTL_S + 1)
        app, idp = self._scene()
        idp.revoke()
        before = len(self.recorder.checkouts)
        with signin.reuse(self.grid, NAME, sign_in=self._sign_in(self.label + " (b)"),
                          signed_in=self._signed_in, refresh=False) as session:
            self._expect_home(session)
            assert not session.reused, "the revoked state should have been rejected, not reused"
        rejects = sum(1 for rejected, _ in self.recorder.checkouts[before:] if rejected is not None)
        assert rejects == 1, "the saved state should be reported rejected exactly once, saw {}".format(rejects)
        assert idp.counts["passwords"] == 1 and idp.counts["codes ok"] == 1, \
            "the revocation should cause one sign-in with one code, not {}".format(dict(idp.counts))
        self.totals["signins"] += 1
        self.totals["rejected"] += 1
        self.totals["saves"] += 1
        self._add_codes_passwords(idp)

    # (c) more reuses with no password and no code, including a silent renewal once the app cookie expired.
    def _scenario_silent_renewal(self):
        app, idp = self._scene()
        with signin.reuse(self.grid, NAME, sign_in=_boom, signed_in=self._signed_in, refresh=False) as session:
            self._expect_home(session)
            assert session.reused, "the fresh state should reuse"
        assert idp.counts["passwords"] == 0 and idp.counts["codes ok"] == 0
        self.totals["reuses"] += 1
        self._add_codes_passwords(idp)

        time.sleep(APP_SESSION_TTL_S + 1)
        app, idp = self._scene()
        with signin.reuse(self.grid, NAME, sign_in=_boom, signed_in=self._signed_in, refresh=False) as session:
            self._expect_home(session)
            assert session.reused, "an expired app cookie should renew silently, not sign in"
            self._good_state = session.context.storage_state()
        assert idp.counts["passwords"] == 0 and idp.counts["codes ok"] == 0, \
            "the silent renewal typed a password or code: {}".format(dict(idp.counts))
        assert idp.counts["silent"] >= 1 and idp.counts["interactive"] == 0, \
            "the app cookie expired, so the IdP should renew silently, saw {}".format(dict(idp.counts))
        self.totals["reuses"] += 1
        self.totals["silent"] += 1
        self._add_codes_passwords(idp)

    # (d) lease contention: a held lease makes reuse() wait for the holder's state; a released one makes it
    #     sign in.
    def _scenario_lease_contention(self):
        # A contender takes the lease by dropping the saved state and checking out; while reuse() waits, the
        # contender saves a good state, so the waiter reuses it without ever signing in.
        app, idp = self._scene()
        lease = self._take_lease("to save")
        saved_state = self._good_state
        waited = {"n": 0}

        def contender_saves(seconds):
            waited["n"] += 1
            if waited["n"] == 1:
                signin.save(NAME, saved_state, lease_token=lease)  # the other caller finishes its sign-in
            time.sleep(seconds)

        with signin.reuse(self.grid, NAME, sign_in=_boom, signed_in=self._signed_in, sleep=contender_saves,
                          refresh=False) as session:
            self._expect_home(session)
            assert session.reused, "the waiter should reuse the holder's saved state, not sign in"
        assert waited["n"] >= 1, "the waiter should have waited for the lease at least once"
        assert idp.counts["passwords"] == 0 and idp.counts["codes ok"] == 0
        self.totals["reuses"] += 1
        self.totals["waits"] += 1
        self._add_codes_passwords(idp)

        # Now the holder releases the lease without saving: the waiter signs in.
        app, idp = self._scene()
        lease = self._take_lease("to release")
        released = {"n": 0}

        def contender_releases(seconds):
            released["n"] += 1
            if released["n"] == 1:
                assert signin.release(NAME, lease) is True, "the holder should still own the lease it releases"
            time.sleep(seconds)

        with signin.reuse(self.grid, NAME, sign_in=self._sign_in(self.label + " (d)"),
                          signed_in=self._signed_in, sleep=contender_releases, refresh=False) as session:
            self._expect_home(session)
            assert not session.reused, "a released lease should make the waiter sign in"
        assert released["n"] >= 1, "the waiter should have waited before the lease was released"
        assert idp.counts["passwords"] == 1 and idp.counts["codes ok"] == 1, \
            "the waiter should sign in once with one code, not {}".format(dict(idp.counts))
        self.totals["signins"] += 1
        self.totals["released"] += 1
        self.totals["saves"] += 1
        self._add_codes_passwords(idp)

    # (e) the refresh-save after a reuse bumps the version; a save against the older version is told newer.
    def _scenario_refresh(self):
        app, idp = self._scene()
        with signin.reuse(self.grid, NAME, sign_in=_boom, signed_in=self._signed_in, refresh=True) as session:
            self._expect_home(session)
            assert session.reused, "the fresh state should reuse"
            reused_version, signin_id = session.version, session.signin_id
        stale = signin.save(NAME, self._good_state, base_version=reused_version, signin_id=signin_id)
        assert stale == {"saved": False, "reason": "newer", "version": reused_version + 1}, \
            ("the refresh-save on exit should have bumped the version to {}, so a save against {} is newer; "
             "got {}".format(reused_version + 1, reused_version, stale))
        assert idp.counts["passwords"] == 0 and idp.counts["codes ok"] == 0
        self.totals["reuses"] += 1
        self.totals["refreshes"] += 1
        self.totals["saves"] += 1
        self._add_codes_passwords(idp)

    def _add_codes_passwords(self, idp):
        self.totals["codes"] += idp.counts["codes ok"]
        self.totals["passwords"] += idp.counts["passwords"]
        self.totals["bad passwords"] += idp.counts["bad passwords"]


def test_signin_reuse(grid_browser, budget, capture):
    assert SEED, ("set POC_TOTP_SEED to the seed of the Grid UI's MFA secret {!r}: the fake IdP plays the real "
                  "one, which checks codes against its own copy of the enrolment".format(SECRET_NAME))
    try:
        seed_key(SEED)
    except ValueError:  # binascii.Error (a bad base32 seed) subclasses ValueError
        raise AssertionError("POC_TOTP_SEED is not a base32 seed") from None

    recorder = Recorder()
    orig_checkout, orig_save = signin.checkout, signin.save

    def checkout(name, rejected_version=None, lease_s=None, signin_id=None, url=None, token=None, timeout_s=10):
        answer = orig_checkout(name, rejected_version, lease_s, signin_id, url, token, timeout_s)
        recorder.on_checkout(rejected_version, answer)
        return answer

    def save(name, state, lease_token=None, base_version=None, max_age_s=None, signin_id=None, url=None, token=None,
             timeout_s=30):
        answer = orig_save(name, state, lease_token, base_version, max_age_s, signin_id, url, token, timeout_s)
        recorder.on_save(answer)
        return answer

    seen = []
    unexpected = []
    totals = Counter()
    slowest_s = 0.0
    with mock.patch.object(signin, "checkout", checkout), mock.patch.object(signin, "save", save):
        while budget.more(reserve_s=slowest_s or ROUND_RESERVE_S):
            started = time.monotonic()
            Round(grid_browser, "poc_signin_reuse round {}".format(budget.rounds), capture, seen, unexpected,
                  recorder, totals).run()
            slowest_s = max(slowest_s, time.monotonic() - started)
    rounds = budget.rounds

    assert not unexpected, "requests left the fake sites: {}".format(unexpected)
    # The password only ever went to the IdP's password API; the seed went nowhere. Messages name paths
    # only, never the secrets, and cookie values (in Cookie headers, not URLs or bodies) are never logged.
    secrets_seen = {"seed": (SEED, TOTP_KEY.hex()), "password": (PASSWORD,)}
    leaks = sorted({(name, path) for origin, path, url, body in seen for name, values in secrets_seen.items()
                    if any(v and (v in url or v in body) for v in values)
                    and not (name == "password" and path == "/api/password")})
    assert not leaks, "secrets reached the browser: {}".format(leaks)
    bad_bodies = [(path, body[:40]) for origin, path, url, body in seen
                  if path == "/api/code" and not re.fullmatch(r'\{"code":"\d{%d}"\}' % DIGITS, body)]
    assert not bad_bodies, "code requests carried more than a code: {}".format(bad_bodies)
    assert totals["bad passwords"] == 0, "a password was refused by the fake IdP"

    print("poc_signin_reuse: {} rounds in {:.0f}s; {} reuses and {} sign-ins ({} MFA codes, {} passwords from "
          "the Grid UI secret {!r}, saved sign-in {!r}); {} saves, {} states rejected, {} silent renewals, "
          "{} lease waits, {} leases released, {} refresh-saves".format(
              rounds, budget.elapsed(), totals["reuses"], totals["signins"], totals["codes"], totals["passwords"],
              SECRET_NAME, NAME, totals["saves"], totals["rejected"], totals["silent"], totals["waits"],
              totals["released"], totals["refreshes"]))
