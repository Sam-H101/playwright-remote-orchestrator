"""src/signin.py, the helper checks reuse saved sign-ins with, against a stub Grid UI served from the test process
and a fake browser, so no Playwright is needed."""
import copy
import http.server
import json
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import _grid_ui, signin  # noqa: E402

# Any warning signin.py gives where a test expects none fails that test.
pytestmark = pytest.mark.filterwarnings("error")

TOKEN = "pwgc_a-client-token-for-the-tests"
NAME = "contoso-monitor"
CHECKOUT, SAVE, RELEASE = "/api/signins/checkout", "/api/signins/save", "/api/signins/release"
APP = "https://contoso.sharepoint.com/sites/ops"
LOGIN = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"
# Every cookie value the fake site sets starts with this, so tests can tell none reached a message.
COOKIE = "cookie-value-"


class StubGridUi:
    """The Grid UI's check API for saved sign-ins, in memory: a row per name with its state, version, lease
    and counts, kept as the contract says. An answer scripted for a path, (status, body), comes first. Keeps
    what it was sent and what it answered. Row ids start at 101, so none is taken for a version."""

    def __init__(self, retry_after_s=2):
        self.rows = {}
        self.last_id = 100
        self.scripted = {}
        self.requests = []
        self.answers = []
        self.tokens = {TOKEN}
        # Names only shared with the token's owner: its checks reuse what is saved, but never sign in for them
        # or save them again.
        self.shared = set()
        self.retry_after_s = retry_after_s
        # Set when a caller is told to wait.
        self.waited = threading.Event()
        self.lock = threading.Lock()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                stub.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status, answer = stub.answer(self.path, self.headers.get("Authorization"), body)
                stub.answers.append((self.path, status, answer))
                data = answer.encode() if isinstance(answer, str) else json.dumps(answer).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}".format(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def answer(self, path, authorization, body):
        if authorization not in {"Bearer " + t for t in self.tokens}:
            return 401, {"error": "The client token is wrong, revoked or expired.", "client_token_required": True}
        if self.scripted.get(path):
            return self.scripted[path].pop(0)
        with self.lock:
            if path == CHECKOUT:
                return self.checkout(body["name"], body.get("rejectedVersion"), body.get("id"), body.get("leaseS"))
            if path == SAVE:
                return self.save(body["name"], body["state"], body.get("leaseToken"), body.get("baseVersion"),
                                 body.get("id"), body.get("maxAgeS"))
            if path == RELEASE:
                return self.release(body["name"], body["leaseToken"])
        return 404, {"error": "not found"}

    def row(self, name):
        if name not in self.rows:
            self.last_id += 1
            self.rows[name] = {"id": self.last_id, "version": 0, "state": None, "expires": 0, "lease": None,
                               "leaseUntil": 0, "signIns": 0, "refreshes": 0, "uses": 0, "rejected": 0}
        return self.rows[name]

    def checkout(self, name, rejected_version, rejected_id, lease_s):
        row = self.row(name)
        now = time.time()
        if rejected_version == row["version"] and rejected_id in (None, row["id"]) and row["state"] is not None:
            row["state"] = None
            row["rejected"] += 1
        if row["state"] is not None and row["expires"] > now:
            row["uses"] += 1
            return 200, {"status": "saved", "id": row["id"], "name": name, "version": row["version"],
                         "state": row["state"], "savedAt": now, "expiresAt": row["expires"]}
        if row["lease"] and row["leaseUntil"] > now:
            self.waited.set()
            return 200, {"status": "wait", "name": name, "retryAfterS": self.retry_after_s}
        if name in self.shared:
            return 409, {"error": "Nothing usable is saved for {!r}, and only its owner's checks may sign in for it. It "
                                  "is shared with this client token's owner, who can use it once their own check has "
                                  "signed in.".format(name), "owner_signs_in": True}
        lease_for = min(max(lease_s or 180, 30), 600)
        row.update(lease=secrets.token_urlsafe(16), leaseUntil=now + lease_for)
        return 200, {"status": "sign-in", "id": row["id"], "name": name, "version": row["version"],
                     "leaseToken": row["lease"], "leaseForS": lease_for}

    def save(self, name, state, lease_token, base_version, signin_id, max_age_s):
        row = self.rows.get(name)
        if row is None:
            return 404, {"error": "There is no saved sign-in called {!r}.".format(name)}
        if not (isinstance(state, dict) and isinstance(state.get("cookies"), list) and isinstance(state.get("origins"), list)):
            return 400, {"error": "Send the state as Playwright's storage_state."}
        now = time.time()
        if lease_token is not None:
            if lease_token != row["lease"] or row["leaseUntil"] <= now:
                return 409, {"error": "lease_lost"}
            row["signIns"] += 1
        elif base_version is not None:
            if signin_id is None:
                return 400, {"error": "Send the id the checkout answered with baseVersion."}
            if name in self.shared:
                return 200, {"saved": False, "reason": "shared", "version": row["version"]}
            if base_version != row["version"] or signin_id != row["id"]:
                return 200, {"saved": False, "reason": "newer", "version": row["version"]}
            row["refreshes"] += 1
        else:
            return 400, {"error": "Send leaseToken or baseVersion."}
        row.update(state=state, version=row["version"] + 1, expires=now + (max_age_s or 30 * 86400), lease=None,
                   leaseUntil=0)
        return 200, {"saved": True, "version": row["version"]}

    def release(self, name, lease_token):
        row = self.rows.get(name)
        if row is None:
            return 404, {"error": "There is no saved sign-in called {!r}.".format(name)}
        released = row["lease"] is not None and row["lease"] == lease_token and row["leaseUntil"] > time.time()
        if released:
            row.update(lease=None, leaseUntil=0)
        return 200, {"released": released}

    def preset(self, name, state, version):
        """A state saved earlier, as `version`."""
        self.row(name).update(state=copy.deepcopy(state), version=version, expires=time.time() + 3600)

    def delete(self, name):
        """An admin deletes the sign-in: the next checkout adds the name again, under a new id."""
        del self.rows[name]

    def hold_lease(self, name):
        """Another check is signing in as `name`: its lease token."""
        row = self.row(name)
        row.update(lease=secrets.token_urlsafe(16), leaseUntil=time.time() + 600)
        return row["lease"]

    def bodies(self, path):
        return [r["body"] for r in self.requests if r["path"] == path]

    def paths(self):
        return [r["path"] for r in self.requests]

    def leases_handed_out(self):
        return [a["leaseToken"] for path, status, a in self.answers if path == CHECKOUT and a.get("status") == "sign-in"]


class FakeSite:
    """The app the checks sign in to: a session per cookie value. sign_in() starts one in the page's
    context; signed_in() says whether the context holds a live one and, when it does, rolls its cookie on
    to a new value, as sites do while they are used (the old one keeps working)."""

    def __init__(self):
        self.sessions = set()
        self.sign_ins = 0

    def new_session(self):
        sid = COOKIE + secrets.token_hex(8)
        self.sessions.add(sid)
        return {"name": "sid", "value": sid, "domain": "contoso.sharepoint.com", "path": "/", "expires": -1,
                "httpOnly": True, "secure": True, "sameSite": "None"}

    def state(self):
        """A storage_state holding a live session, as a check that signed in earlier saved it."""
        return {"cookies": [self.new_session()],
                "origins": [{"origin": APP, "localStorage": [{"name": "theme", "value": "dark"}]}]}

    def sign_in(self, page):
        self.sign_ins += 1
        page.context.cookies = [self.new_session()]
        page.url = APP

    def signed_in(self, page):
        sids = [c["value"] for c in page.context.cookies if c["name"] == "sid"]
        if not sids or sids[0] not in self.sessions:
            page.url = LOGIN
            return False
        page.context.cookies = [self.new_session()]
        page.url = APP
        return True


class FakePage:
    def __init__(self, context):
        self.context = context
        self.url = "about:blank"


class FakeContext:
    def __init__(self, browser, number, storage_state, kwargs):
        self.browser = browser
        self.number = number
        self.given = storage_state
        self.kwargs = kwargs
        state = copy.deepcopy(storage_state) or {"cookies": [], "origins": []}
        self.cookies = state["cookies"]
        self.origins = state["origins"]
        self.closed = False

    def new_page(self):
        assert not self.closed, "new_page() on a closed context"
        return FakePage(self)

    def storage_state(self):
        if self.closed or self.browser.gone:
            raise RuntimeError("Target page, context or browser has been closed")
        return {"cookies": copy.deepcopy(self.cookies), "origins": copy.deepcopy(self.origins)}

    def close(self):
        self.closed = True
        self.browser.events.append(("close", self.number))


class FakeBrowser:
    """Playwright's Browser as reuse() uses it. events: ("open", n, with a storage_state) and ("close", n)."""

    def __init__(self):
        self.contexts = []
        self.events = []
        self.gone = False

    def new_context(self, storage_state=None, **kwargs):
        context = FakeContext(self, len(self.contexts), storage_state, kwargs)
        self.contexts.append(context)
        self.events.append(("open", context.number, storage_state is not None))
        return context


class Clock:
    """time.monotonic for reuse(), moved on only by the sleep() it is given. on_sleep() runs after each
    sleep, for what other checks do meanwhile."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
        self.on_sleep()


@pytest.fixture
def grid_ui(monkeypatch):
    stub = StubGridUi()
    monkeypatch.setenv("GRID_UI_URL", stub.url + "/")
    monkeypatch.setenv("GRID_MFA_TOKEN", TOKEN)
    monkeypatch.delenv("GATEWAY_CLIENT_TOKEN", raising=False)
    yield stub
    stub.close()


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(signin, "time", types.SimpleNamespace(monotonic=clock.monotonic))
    return clock


@pytest.fixture
def site():
    return FakeSite()


@pytest.fixture
def browser():
    return FakeBrowser()


def reuse(browser, site, clock, **kwargs):
    return signin.reuse(browser, NAME, sign_in=site.sign_in, signed_in=site.signed_in, sleep=clock.sleep, **kwargs)


def closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def assert_no_secrets(error, *also):
    """Neither the client token, nor a lease token in `also`, nor a cookie value is in the error."""
    text = str(error) + repr(error) + "".join(traceback.format_exception(error))
    for secret in (TOKEN, *also):
        assert secret not in text
    assert COOKIE not in text


def test_the_first_run_signs_in_and_saves(grid_ui, clock, site, browser):
    with reuse(browser, site, clock) as s:
        assert not s.reused and s.version == 1 and s.name == NAME and s.signin_id == grid_ui.rows[NAME]["id"]
        assert s.page.context is s.context and s.page.url == APP and not s.context.closed
        signed_in_as = grid_ui.rows[NAME]["state"]["cookies"][0]["value"]
        assert site.signed_in(s.page)  # the check uses the app, which rolls the cookie on
    assert s.context.closed and browser.events == [("open", 0, False), ("close", 0)]
    assert site.sign_ins == 1 and clock.slept == []

    assert grid_ui.paths() == [CHECKOUT, SAVE, SAVE]
    for r in grid_ui.requests:
        assert r["headers"]["Authorization"] == "Bearer " + TOKEN
        assert r["headers"]["Content-Type"] == r["headers"]["Accept"] == "application/json"
    (lease,) = grid_ui.leases_handed_out()
    checkout, fresh, refresh = (r["body"] for r in grid_ui.requests)
    assert checkout == {"name": NAME}
    assert set(fresh) == {"name", "state", "leaseToken"} and fresh["leaseToken"] == lease
    assert [c["value"] for c in fresh["state"]["cookies"]] == [signed_in_as] and fresh["state"]["origins"] == []
    # Saved again on the way out, with the cookie as the app left it.
    assert set(refresh) == {"name", "state", "baseVersion", "id"} and refresh["baseVersion"] == 1
    assert refresh["id"] == grid_ui.rows[NAME]["id"]
    assert refresh["state"]["cookies"] == s.context.cookies and s.context.cookies[0]["value"] != signed_in_as
    row = grid_ui.rows[NAME]
    assert (row["version"], row["signIns"], row["refreshes"], row["lease"]) == (2, 1, 1, None)
    assert s.version == 2


def test_a_saved_sign_in_is_reused_and_saved_again(grid_ui, clock, site, browser):
    state = site.state()
    grid_ui.preset(NAME, state, version=4)
    with reuse(browser, site, clock) as s:
        assert s.reused and s.version == 4 and s.signin_id == grid_ui.rows[NAME]["id"] and s.page.url == APP
        assert browser.contexts[0].given == state  # the context was opened with the saved state
        rolled = s.context.cookies[0]["value"]
        assert rolled != state["cookies"][0]["value"]
    assert site.sign_ins == 0
    assert browser.events == [("open", 0, True), ("close", 0)]
    assert grid_ui.paths() == [CHECKOUT, SAVE]
    refresh = grid_ui.bodies(SAVE)[0]
    assert refresh["baseVersion"] == 4 and refresh["id"] == s.signin_id
    assert "leaseToken" not in refresh and "maxAgeS" not in refresh
    assert refresh["state"]["cookies"][0]["value"] == rolled and refresh["state"]["origins"] == state["origins"]
    row = grid_ui.rows[NAME]
    assert (row["version"], row["uses"], row["refreshes"], row["signIns"]) == (5, 1, 1, 0)
    assert s.version == 5


def test_a_newer_save_is_kept(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)
    newer = site.state()
    with reuse(browser, site, clock) as s:
        # Another check saves its cookies first.
        assert signin.save(NAME, newer, base_version=4, signin_id=s.signin_id) == {"saved": True, "version": 5}
    # No warning (the module's filter would fail the test): the newer state stays.
    assert grid_ui.rows[NAME]["state"] == newer and grid_ui.rows[NAME]["version"] == 5
    assert grid_ui.answers[-1][2] == {"saved": False, "reason": "newer", "version": 5}
    assert s.version == 4 and s.context.closed


def test_a_refused_state_is_reported_and_signed_in_again(grid_ui, clock, site, browser):
    stale = site.state()
    site.sessions.clear()  # the site asks to sign in again
    grid_ui.preset(NAME, stale, version=4)
    with reuse(browser, site, clock, refresh=False, max_age_s=3600) as s:
        assert not s.reused and s.version == 5
        assert browser.contexts[0].closed and not s.context.closed
    assert browser.events == [("open", 0, True), ("close", 0), ("open", 1, False), ("close", 1)]
    assert browser.contexts[0].given == stale and browser.contexts[1].given is None
    assert grid_ui.bodies(CHECKOUT) == [{"name": NAME}, {"name": NAME, "rejectedVersion": 4, "id": 101}]
    (fresh,) = grid_ui.bodies(SAVE)
    assert fresh["leaseToken"] == grid_ui.leases_handed_out()[0] and fresh["maxAgeS"] == 3600
    row = grid_ui.rows[NAME]
    assert (row["rejected"], row["signIns"], row["version"]) == (1, 1, 5)
    assert site.sign_ins == 1


def test_the_refusal_is_reported_once_while_another_check_signs_in(grid_ui, clock, site, browser):
    stale = site.state()
    site.sessions.clear()
    grid_ui.preset(NAME, stale, version=4)
    other = grid_ui.hold_lease(NAME)
    fresh = site.state()
    clock.on_sleep = lambda: len(clock.slept) == 2 and signin.save(NAME, fresh, lease_token=other)
    with reuse(browser, site, clock, refresh=False) as s:
        assert s.reused and s.version == 5 and browser.contexts[1].given == fresh
    assert grid_ui.bodies(CHECKOUT) == [{"name": NAME}, {"name": NAME, "rejectedVersion": 4, "id": 101}, {"name": NAME},
                                        {"name": NAME}]
    assert clock.slept == [2, 2]
    assert grid_ui.rows[NAME]["rejected"] == 1 and site.sign_ins == 0


@pytest.mark.parametrize("failure", ["sign_in raises", "not signed in afterwards", "too large", "lease lost"])
def test_a_failed_sign_in_gives_its_lease_back(grid_ui, clock, site, browser, failure):
    sign_in = site.sign_in
    if failure == "sign_in raises":
        def sign_in(page):
            raise RuntimeError("the password was refused")
    elif failure == "not signed in afterwards":
        def sign_in(page):
            page.url = LOGIN
    elif failure == "too large":
        grid_ui.scripted[SAVE] = [(413, {"error": "too_large"})]
    else:
        grid_ui.scripted[SAVE] = [(409, {"error": "lease_lost"})]

    with pytest.raises((RuntimeError, signin.SigninError)) as e:
        with signin.reuse(browser, NAME, sign_in=sign_in, signed_in=site.signed_in, sleep=clock.sleep):
            pytest.fail("the block ran without a sign-in")
    (lease,) = grid_ui.leases_handed_out()
    assert grid_ui.bodies(RELEASE) == [{"name": NAME, "leaseToken": lease}]
    assert grid_ui.answers[-1] == (RELEASE, 200, {"released": True})
    assert browser.events == [("open", 0, False), ("close", 0)]
    row = grid_ui.rows[NAME]
    assert row["lease"] is None and row["version"] == 0 and row["state"] is None
    assert_no_secrets(e.value, lease)
    if failure == "sign_in raises":
        assert type(e.value) is RuntimeError and str(e.value) == "the password was refused"
    elif failure == "not signed in afterwards":
        assert "isn't signed in as 'contoso-monitor'" in str(e.value) and e.value.status is None
        assert SAVE not in grid_ui.paths()
    else:
        status, error = (413, "too_large") if failure == "too large" else (409, "lease_lost")
        assert e.value.status == status and e.value.answer == {"error": error}
        assert "HTTP {} {}".format(status, error) in str(e.value) and "'contoso-monitor'" in str(e.value)


def test_a_failed_release_only_warns(grid_ui, clock, site, browser):
    grid_ui.scripted[RELEASE] = [(503, {"error": "The database is unavailable."})]

    def sign_in(page):
        raise RuntimeError("the password was refused")

    with pytest.warns(UserWarning, match="HTTP 503 The database is unavailable.*runs out by itself") as warned:
        with pytest.raises(RuntimeError, match="the password was refused"):
            with signin.reuse(browser, NAME, sign_in=sign_in, signed_in=site.signed_in, sleep=clock.sleep):
                pass
    assert TOKEN not in str(warned[0].message) and grid_ui.leases_handed_out()[0] not in str(warned[0].message)
    assert browser.events == [("open", 0, False), ("close", 0)]


def test_waits_for_another_check_signing_in(grid_ui, clock, site, browser):
    other = grid_ui.hold_lease(NAME)
    fresh = site.state()
    clock.on_sleep = lambda: len(clock.slept) == 3 and signin.save(NAME, fresh, lease_token=other)
    with reuse(browser, site, clock, refresh=False) as s:
        assert s.reused and s.version == 1
    assert clock.slept == [2, 2, 2] and len(grid_ui.bodies(CHECKOUT)) == 4
    assert browser.events == [("open", 0, True), ("close", 0)] and site.sign_ins == 0


def test_gives_up_waiting_after_max_wait_s(grid_ui, clock, site, browser):
    other = grid_ui.hold_lease(NAME)
    with pytest.raises(signin.SigninError) as e:
        with reuse(browser, site, clock, max_wait_s=7):
            pass
    assert clock.slept == [2, 2, 2]  # a fourth wait would end past the 7 s
    assert len(grid_ui.bodies(CHECKOUT)) == 4
    assert "'contoso-monitor'" in str(e.value) and "max_wait_s (7 s)" in str(e.value) and e.value.status is None
    assert browser.events == [] and site.sign_ins == 0
    assert_no_secrets(e.value, other)

    grid_ui.retry_after_s = 5
    with pytest.raises(signin.SigninError):
        with reuse(browser, site, clock, max_wait_s=4):
            pass
    assert clock.slept == [2, 2, 2]  # longer than max_wait_s at once: no wait at all


@pytest.mark.parametrize("problem, says", [
    ((503, {"error": "Set GRID_SECRETS_KEY to save sign-ins.", "secrets_key_unset": True}), "HTTP 503 Set GRID_SECRETS_KEY"),
    ((409, {"error": "lease_lost"}), "HTTP 409 lease_lost"),
    ((200, {"saved": False, "reason": "signing-in", "version": 4}), "signing-in"),
    ("browser gone", "has been closed"),
])
def test_a_refresh_that_fails_only_warns(grid_ui, clock, site, browser, problem, says):
    grid_ui.preset(NAME, site.state(), version=4)
    if problem != "browser gone":
        grid_ui.scripted[SAVE] = [problem]
    with pytest.warns(UserWarning, match="'contoso-monitor'") as warned:
        with reuse(browser, site, clock) as s:
            browser.gone = problem == "browser gone"
    assert len(warned) == 1 and says in str(warned[0].message) and TOKEN not in str(warned[0].message)
    assert COOKIE not in str(warned[0].message)
    assert s.version == 4 and s.context.closed and grid_ui.rows[NAME]["version"] == 4


def test_a_refresh_after_an_admin_deleted_the_sign_in_saves_nothing(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)
    newer = site.state()
    with reuse(browser, site, clock) as s:
        old_id = s.signin_id
        # An admin deletes the sign-in; checks that run meanwhile sign in again, up to the same version.
        grid_ui.delete(NAME)
        grid_ui.preset(NAME, newer, version=4)
    # Quietly, like any newer state: the deleted sign-in's cookies don't come back.
    assert grid_ui.bodies(SAVE)[0]["id"] == old_id != grid_ui.rows[NAME]["id"]
    assert grid_ui.answers[-1][2] == {"saved": False, "reason": "newer", "version": 4}
    assert grid_ui.rows[NAME]["state"] == newer and grid_ui.rows[NAME]["refreshes"] == 0


def test_a_shared_sign_in_is_reused_and_not_saved_again(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)
    grid_ui.shared.add(NAME)
    with reuse(browser, site, clock) as s:
        assert s.reused and s.version == 4
    # Quietly (the module's filter would fail the test on a warning): only the owner's checks save it.
    assert grid_ui.paths() == [CHECKOUT, SAVE]
    assert grid_ui.answers[-1][2] == {"saved": False, "reason": "shared", "version": 4}
    assert s.version == 4 and s.context.closed and grid_ui.rows[NAME]["refreshes"] == 0


def test_only_the_owners_checks_sign_in_for_a_shared_sign_in(grid_ui, clock, site, browser):
    stale = site.state()
    site.sessions.clear()  # the site asks to sign in again
    grid_ui.preset(NAME, stale, version=4)
    grid_ui.shared.add(NAME)
    with pytest.raises(signin.SigninError) as e:
        with reuse(browser, site, clock):
            pytest.fail("the block ran without a sign-in")
    assert e.value.status == 409 and e.value.answer["owner_signs_in"] is True
    assert "HTTP 409 Nothing usable is saved for 'contoso-monitor'" in str(e.value)  # the Grid UI's own words
    # The refusal was still reported, so the owner's next check signs in.
    assert grid_ui.bodies(CHECKOUT) == [{"name": NAME}, {"name": NAME, "rejectedVersion": 4, "id": 101}]
    assert grid_ui.rows[NAME]["rejected"] == 1 and grid_ui.rows[NAME]["state"] is None
    assert browser.events == [("open", 0, True), ("close", 0)] and site.sign_ins == 0
    assert grid_ui.leases_handed_out() == []
    assert_no_secrets(e.value)


def test_an_error_in_the_block_saves_nothing(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)
    with pytest.raises(ValueError, match="the page broke"):
        with reuse(browser, site, clock) as s:
            raise ValueError("the page broke")
    assert grid_ui.paths() == [CHECKOUT] and s.context.closed
    assert grid_ui.rows[NAME]["version"] == 4


def test_refresh_false_saves_nothing(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)
    with reuse(browser, site, clock, refresh=False) as s:
        pass
    assert grid_ui.paths() == [CHECKOUT] and s.context.closed and s.version == 4


def test_signed_in_raising_is_not_a_refusal(grid_ui, clock, site, browser):
    grid_ui.preset(NAME, site.state(), version=4)

    def signed_in(page):
        raise TimeoutError("the app didn't load")

    with pytest.raises(TimeoutError):
        with signin.reuse(browser, NAME, sign_in=site.sign_in, signed_in=signed_in, sleep=clock.sleep):
            pass
    assert grid_ui.paths() == [CHECKOUT] and browser.events == [("open", 0, True), ("close", 0)]
    assert grid_ui.rows[NAME]["state"] is not None and grid_ui.rows[NAME]["lease"] is None


def test_context_options_go_to_new_context(grid_ui, clock, site, browser):
    stale = site.state()
    site.sessions.clear()
    grid_ui.preset(NAME, stale, version=4)
    options = {"viewport": {"width": 1280, "height": 720}, "locale": "en-GB"}
    with reuse(browser, site, clock, refresh=False, **options):
        pass
    assert [c.kwargs for c in browser.contexts] == [options, options]
    assert browser.contexts[0].given == stale and browser.contexts[1].given is None

    requests = len(grid_ui.requests)
    with pytest.raises(TypeError, match="storage_state"):
        with reuse(browser, site, clock, storage_state="state.json"):
            pass
    assert len(grid_ui.requests) == requests and len(browser.contexts) == 2


def test_a_refused_version_handed_out_again(grid_ui, clock, site, browser):
    stale = site.state()
    site.sessions.clear()
    saved = {"status": "saved", "id": 7, "name": NAME, "version": 4, "state": stale, "savedAt": 0, "expiresAt": 0}
    grid_ui.scripted[CHECKOUT] = [(200, saved), (200, saved)]
    with pytest.raises(signin.SigninError, match="version 4 again"):
        with reuse(browser, site, clock):
            pass
    assert browser.events == [("open", 0, True), ("close", 0)]


def test_an_unknown_checkout_answer(grid_ui, clock, site, browser):
    grid_ui.scripted[CHECKOUT] = [(200, {"status": "maybe"})]
    with pytest.raises(signin.SigninError, match="unknown status 'maybe'"):
        with reuse(browser, site, clock):
            pass
    assert browser.events == []


def test_the_requests_one_by_one(grid_ui, clock):
    state = {"cookies": [{"name": "sid", "value": COOKIE + "1"}], "origins": []}
    answer = signin.checkout(NAME, lease_s=60)
    assert answer["status"] == "sign-in" and answer["leaseForS"] == 60 and answer["version"] == 0
    assert signin.save(NAME, state, lease_token=answer["leaseToken"], max_age_s=3600) == {"saved": True, "version": 1}
    answer = signin.checkout(NAME)
    assert answer["status"] == "saved" and answer["state"] == state and answer["version"] == 1 and answer["id"] == 101
    assert signin.save(NAME, state, base_version=1, signin_id=101) == {"saved": True, "version": 2}
    assert signin.save(NAME, state, base_version=1, signin_id=101) == {"saved": False, "reason": "newer", "version": 2}
    assert signin.checkout(NAME, rejected_version=2, signin_id=99)["status"] == "saved"  # another sign-in's version 2
    lease = signin.checkout(NAME, rejected_version=2, signin_id=101)["leaseToken"]
    assert signin.checkout(NAME) == {"status": "wait", "name": NAME, "retryAfterS": 2}
    assert signin.release(NAME, lease) is True
    assert signin.release(NAME, lease) is False

    first = grid_ui.leases_handed_out()[0]
    assert [r["body"] for r in grid_ui.requests] == [
        {"name": NAME, "leaseS": 60},
        {"name": NAME, "state": state, "leaseToken": first, "maxAgeS": 3600},
        {"name": NAME},
        {"name": NAME, "state": state, "baseVersion": 1, "id": 101},
        {"name": NAME, "state": state, "baseVersion": 1, "id": 101},
        {"name": NAME, "rejectedVersion": 2, "id": 99},
        {"name": NAME, "rejectedVersion": 2, "id": 101},
        {"name": NAME},
        {"name": NAME, "leaseToken": lease},
        {"name": NAME, "leaseToken": lease},
    ]
    assert grid_ui.paths() == [CHECKOUT, SAVE, CHECKOUT, SAVE, SAVE, CHECKOUT, CHECKOUT, CHECKOUT, RELEASE, RELEASE]
    assert {r["headers"]["Authorization"] for r in grid_ui.requests} == {"Bearer " + TOKEN}
    assert clock.slept == []


@pytest.mark.parametrize("call, status, answer, says", [
    ("checkout", 503, {"error": "Set GRID_SECRETS_KEY to save sign-ins.", "secrets_key_unset": True}, "GRID_SECRETS_KEY"),
    ("checkout", 503, {"error": "The database is unavailable."}, "database"),
    ("checkout", 401, {"error": "The client token is wrong, revoked or expired.", "client_token_required": True}, "client token"),
    ("checkout", 400, {"error": "A sign-in name is 1-64 letters, digits, '.', '_' and '-'."}, "1-64"),
    ("save", 409, {"error": "lease_lost"}, "lease_lost"),
    ("save", 413, {"error": "too_large"}, "too_large"),
    ("save", 404, {"error": "There is no saved sign-in called 'contoso-monitor'."}, "no saved sign-in"),
    ("release", 503, {"error": "The database is unavailable."}, "database"),
    ("checkout", 502, "<html>Bad gateway</html>", "Bad Gateway"),  # not the Grid UI's answer: the HTTP reason
])
def test_refusals_raise_signin_error(grid_ui, clock, call, status, answer, says):
    path = {"checkout": CHECKOUT, "save": SAVE, "release": RELEASE}[call]
    grid_ui.scripted[path] = [(status, answer)]
    lease = "a-lease-token-for-the-tests"
    with pytest.raises(signin.SigninError) as e:
        if call == "checkout":
            signin.checkout(NAME, rejected_version=3)
        elif call == "save":
            signin.save(NAME, {"cookies": [{"name": "sid", "value": COOKIE + "1"}], "origins": []}, lease_token=lease)
        else:
            signin.release(NAME, lease)
    assert e.value.status == status and e.value.answer == (answer if isinstance(answer, dict) else {})
    assert "HTTP {}".format(status) in str(e.value) and says in str(e.value) and "'contoso-monitor'" in str(e.value)
    assert len(grid_ui.requests) == 1 and clock.slept == []
    assert_no_secrets(e.value, lease)
    assert e.value.__cause__ is None and e.value.__suppress_context__  # urllib's error holds the request


def test_an_unreachable_grid_ui(monkeypatch, clock, site, browser):
    monkeypatch.setenv("GRID_MFA_TOKEN", TOKEN)
    url = "http://127.0.0.1:{}".format(closed_port())
    with pytest.raises(signin.SigninError) as e:
        with signin.reuse(browser, NAME, sign_in=site.sign_in, signed_in=site.signed_in, sleep=clock.sleep, url=url):
            pass
    assert url in str(e.value) and "could not be reached to check out the saved sign-in 'contoso-monitor'" in str(e.value)
    assert e.value.status is None and e.value.answer == {} and clock.slept == [] and browser.events == []
    assert_no_secrets(e.value)
    assert e.value.__cause__ is None and e.value.__suppress_context__


def test_where_the_token_and_url_come_from(grid_ui, monkeypatch, clock, site, browser):
    grid_ui.tokens |= {"the-bootstrap-token", "given"}
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "the-bootstrap-token")
    signin.checkout("a")
    monkeypatch.delenv("GRID_MFA_TOKEN")
    signin.checkout("b")
    signin.checkout("c", token="given")
    with signin.reuse(browser, "d", sign_in=site.sign_in, signed_in=site.signed_in, token="given"):
        pass
    assert [r["headers"]["Authorization"] for r in grid_ui.requests] == [
        "Bearer " + TOKEN, "Bearer the-bootstrap-token", "Bearer given"] + ["Bearer given"] * 3

    monkeypatch.setenv("GRID_UI_URL", "http://127.0.0.1:{}".format(closed_port()))
    assert signin.checkout("e", url=grid_ui.url)["status"] == "sign-in"  # url= wins over GRID_UI_URL
    monkeypatch.delenv("GRID_UI_URL")
    assert _grid_ui.settings(None, "t", signin.SigninError) == ("http://grid_ui:4444", "t")


def test_no_token_no_request(grid_ui, monkeypatch, clock, site, browser):
    monkeypatch.delenv("GRID_MFA_TOKEN")
    with pytest.raises(signin.SigninError, match="GRID_MFA_TOKEN or GATEWAY_CLIENT_TOKEN"):
        with reuse(browser, site, clock):
            pass
    monkeypatch.setenv("GRID_MFA_TOKEN", "")
    for call in (lambda: signin.checkout(NAME),
                 lambda: signin.save(NAME, {"cookies": [], "origins": []}, base_version=1, signin_id=101),
                 lambda: signin.release(NAME, "lease")):
        with pytest.raises(signin.SigninError):
            call()
    assert grid_ui.requests == [] and browser.events == []


def test_two_checks_at_once_sign_in_once(grid_ui, site):
    grid_ui.retry_after_s = 0.05
    results, errors = {}, []

    def sign_in(page):
        # Signs in only once the other check has been told to wait for it.
        assert grid_ui.waited.wait(10), "the other check wasn't told to wait"
        site.sign_in(page)

    def check(n):
        try:
            with signin.reuse(FakeBrowser(), NAME, sign_in=sign_in, signed_in=site.signed_in, max_wait_s=10,
                              refresh=False) as s:
                results[n] = s.reused
        except BaseException as e:  # failed asserts too: the test reports them
            errors.append(e)

    threads = [threading.Thread(target=check, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors and sorted(results.values()) == [False, True]
    assert site.sign_ins == 1
    row = grid_ui.rows[NAME]
    # One fresh sign-in, saved under the lease; the other check used what it saved.
    assert (row["signIns"], row["uses"], row["version"], row["lease"]) == (1, 1, 1, None)
