"""Single sign-on in a real browser: the redirect through a fake identity provider, the sign-in prompt, signing
out, the account menu, and what the page shows people the grid doesn't let in. The provider is served at
localhost and the grid at 127.0.0.1, so the redirects between them are cross-site, as in production.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import urllib.parse

import pytest

from ui import app as grid_app, auth, oidc, permissions

pytest.importorskip("playwright.sync_api")
pytest.importorskip("cryptography")

from ui.tests import fake_idp  # noqa: E402
from ui.tests.test_accounts import OIDC_SETTINGS, add_mapping, add_session, make_user, ts  # noqa: E402
from ui.tests.test_browser import browser, grid, page, refresh_now, run  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_mfa import set_hidden  # noqa: E402
from ui.tests.test_browser_tokens import admin_grid  # noqa: E402,F401  (fixtures)

DANA = fake_idp.USER["sub"]
DENIED = "The identity provider didn't sign you in."


@pytest.fixture
def sso_grid(admin_grid, monkeypatch):  # noqa: F811
    """The demo grid with single sign-on through a fake identity provider at localhost, and GRID_ADMIN_TOKEN as
    the break-glass sign-in. (grid URL, sqlite path, the provider)."""
    url, path = admin_grid
    idp = fake_idp.serve("localhost")
    idp.redirect_uri = url + "auth/callback"
    for name in OIDC_SETTINGS + ("GRID_OIDC_END_SESSION", "GRID_UI_READS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRID_OIDC_ISSUER", idp.base_url)
    monkeypatch.setenv("GRID_OIDC_CLIENT_ID", fake_idp.CLIENT_ID)
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", fake_idp.CLIENT_SECRET)
    monkeypatch.setenv("GRID_UI_PUBLIC_URL", url)
    yield url, path, idp
    idp.stop()


def watched(p):
    """The page, keeping its console errors as the page fixture does."""
    p.errors = []
    p.on("console", lambda m: p.errors.append(m.text) if m.type == "error" else None)
    p.on("pageerror", lambda e: p.errors.append(str(e)))
    return p


def signed_in_context(browser, url, path, user_id, width=1280, height=900):  # noqa: F811
    """A context signed in as the user, as single sign-on leaves a browser: a session row, and its cookie."""
    ctx = browser.new_context(viewport={"width": width, "height": height})
    ctx.add_cookies([{"name": oidc.cookie_names().session, "value": add_session(path, user_id), "url": url}])
    return ctx


def dana(path, idp, **kw):
    """Dana Lee, as her last sign-in through the provider left her. Returns her id."""
    return make_user(path, "Dana Lee", email="dana@example.com", issuer=idp.base_url, subject=DANA, **kw)


def status_polls(p):
    seen = []
    p.on("request", lambda r: seen.append(r.url) if "/api/status" in r.url else None)
    return seen


def test_signing_in_through_the_provider_comes_back_signed_in_where_it_started(sso_grid, browser):  # noqa: F811
    """The cookie questions, settled in a real browser: the redirect through the provider (another site) signs in,
    the SameSite=Strict session cookie goes with the page's first request after it, and the page comes back to
    the view it started from."""
    url, path, idp = sso_grid
    add_mapping(path, "grid-testers", permissions.TESTER)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.click("#account-btn")
        p.wait_for_selector("#signin-dialog[open] a#signin-sso")
        requests = []
        p.on("request", lambda r: requests.append(r) if r.url.startswith(url + "api/") else None)
        p.click("#signin-sso")
        p.wait_for_selector("#continue")  # the provider's own page
        assert p.url.startswith(idp.origin + "/authorize?")
        p.click("#continue")
        p.wait_for_selector("#account-btn.signed-in")
        assert p.url == url + "#/mfa"
        assert p.text_content("#account-btn .acct-label") == "Dana Lee"
        assert p.get_attribute("#account-btn", "title") == "Dana Lee"
        first = requests[0]
        assert first.url.startswith(url + "api/session")
        assert oidc.cookie_names().session + "=" in (first.all_headers().get("cookie") or "")
        (cookie,) = [c for c in ctx.cookies() if c["name"] == oidc.cookie_names().session]
        assert cookie["sameSite"] == "Strict" and cookie["httpOnly"]
        p.wait_for_selector("#view-mfa:not([hidden]) h1")
        assert p.errors == []
    finally:
        ctx.close()


def test_the_prompt_offers_a_link_to_sign_in_and_the_admin_token_folded_under_it(sso_grid, page):  # noqa: F811
    url, _, _ = sso_grid
    page.goto(url + "#/sessions")
    page.click("#account-btn")
    page.wait_for_selector("#signin-dialog[open]")
    sso = page.locator("#signin-sso")
    assert sso.evaluate("a => a.tagName") == "A" and sso.text_content() == "Sign in with single sign-on"
    assert sso.get_attribute("href") == "auth/login?return=" + urllib.parse.quote("#/sessions", safe="")
    # A link, outside any form: after a submitted form, form-action 'self' would stop the redirect to the provider.
    assert sso.evaluate("a => a.closest('form') === null")
    assert page.evaluate("document.activeElement.id") == "signin-sso"
    assert page.text_content("#signin-dialog .signin-back") == "You'll come back to this page."
    assert page.text_content("#signin-other summary") == "Use the admin token instead"
    assert not page.is_visible("#signin-token")
    page.click("#signin-other summary")
    page.wait_for_selector("#signin-token", state="visible")
    # The field takes focus on the details element's toggle event, which follows the click a moment later.
    page.wait_for_function("() => document.activeElement.id === 'signin-token'")
    assert "For when single sign-on is down" in page.text_content("#signin-hint")

    page.fill("#signin-token", "wrong")
    page.press("#signin-token", "Enter")
    page.wait_for_function("() => document.getElementById('signin-error').textContent === 'That admin token was rejected.'")
    assert page.is_visible("#signin-dialog") and not page.is_visible("#account-btn.signed-in")
    page.fill("#signin-token", "s3cret")
    page.press("#signin-token", "Enter")
    page.wait_for_selector("#account-btn.signed-in")
    assert page.text_content("#account-btn .acct-label") == "Admin"
    page.wait_for_selector("#session-rows tr")  # the token may read the grid
    page.click("#account-btn")
    assert "Signed in with admin token · this tab only" in page.text_content("#account-menu")
    assert page.locator("#account-menu [data-menu=signout-all]").count() == 0  # nothing to end anywhere else
    assert page.errors == []


def test_single_sign_on_that_cant_work_says_so_and_opens_the_admin_token(sso_grid, page, monkeypatch):  # noqa: F811
    url, _, _ = sso_grid
    monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", "")
    page.goto(url)
    page.click("#account-btn")
    page.wait_for_selector("#signin-dialog[open]")
    assert page.evaluate("document.getElementById('signin-sso').tagName") == "BUTTON"
    assert page.is_disabled("#signin-sso")
    # Not the setting that's wrong (it may name a file): that goes to the log and to admins.
    assert page.text_content("#signin-problem") == ("Single sign-on isn't set up correctly. The Grid UI's log says what's wrong; "
                                                    "an admin can sign in with the admin token meanwhile.")
    assert page.get_attribute("#signin-other", "open") is not None
    assert page.evaluate("document.activeElement.id") == "signin-token"
    assert page.errors == []


def test_cancelling_at_the_provider_says_so_and_leaves_nothing_in_the_address(sso_grid, page):  # noqa: F811
    url, _, _ = sso_grid
    page.goto(url + "#/activity")
    page.click("#account-btn")
    page.click("#signin-sso")
    page.click("#cancel")
    page.wait_for_selector("#signin-dialog[open] #signin-error:visible")
    assert page.text_content("#signin-error") == DENIED
    assert "notice" in page.get_attribute("#signin-error", "class")
    assert page.evaluate("location.search") == "" and page.url == url + "#/activity"
    assert page.errors == []


def test_a_disabled_person_is_told_why_they_can_not_sign_in(sso_grid, page):  # noqa: F811
    url, path, idp = sso_grid
    idp.auto = True  # the provider sends the browser straight back
    dana(path, idp, disabled=True)
    page.goto(url)
    page.click("#account-btn")
    page.click("#signin-sso")
    page.wait_for_selector("#signin-dialog[open] #signin-error:visible")
    assert page.text_content("#signin-error") == "Your account on this grid is disabled. Ask an admin to enable it."
    assert page.evaluate("location.search") == "" and not page.is_visible("#account-btn.signed-in")
    assert page.errors == []


def test_roles_follow_changes_without_signing_in_again(sso_grid, browser):  # noqa: F811
    """A mapping takes effect on the next load, and a role given while the page is open shows once the tab is
    shown again: the page asks /api/session again then."""
    url, path, idp = sso_grid
    who = dana(path, idp, groups=["grid-testers"])
    ctx = signed_in_context(browser, url, path, who)
    try:
        p = watched(ctx.new_page())
        p.goto(url)
        p.wait_for_selector("#ov-state [data-your-access]")  # no role yet
        add_mapping(path, "grid-testers", permissions.TESTER)
        p.reload()
        p.wait_for_selector(".mx-row[data-key]")
        assert p.is_hidden(".rail [data-admin-nav]")
        # A Tester sees the grid but doesn't run it: its actions stay in place, unavailable, and say why.
        reset = p.locator("#att-list [data-act=reset]").first
        assert reset.is_disabled() and reset.get_attribute("title").startswith("Your roles don't let you reset failures on ")

        run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (who, permissions.ADMIN, ts(0)))
        set_hidden(p, True)
        set_hidden(p, False)
        p.wait_for_selector(".rail [data-admin-nav]:not([hidden])")
        p.click("#account-btn")
        assert set(p.text_content("#account-menu .menu-roles").split(" · ")) == {"Tester", "Admin"}
        assert p.locator("#account-menu [data-menu=admin]").count() == 1  # the phone's way to the Admin page
        assert p.errors == []
    finally:
        ctx.close()


def test_signing_out_and_signing_out_everywhere(sso_grid, browser):  # noqa: F811
    url, path, idp = sso_grid
    who = dana(path, idp, roles=[permissions.OPERATOR])
    live_sessions = lambda: run(path, "SELECT COUNT(*) FROM userSessions WHERE userId = ? AND revokedAt IS NULL", (who,))[0][0]
    one, two = signed_in_context(browser, url, path, who), signed_in_context(browser, url, path, who)
    try:
        a, b = watched(one.new_page()), watched(two.new_page())
        for p in (a, b):
            p.goto(url)
            p.wait_for_selector("#att-list [data-act=reset]")

        # Signing out ends this browser's session only.
        a.click("#account-btn")
        a.click("#account-menu [data-menu=signout]")
        a.wait_for_selector("#ov-state [data-signin]")
        assert not a.is_visible("#account-btn.signed-in")
        assert [c for c in one.cookies() if c["name"] == oidc.cookie_names().session] == []
        assert live_sessions() == 1

        # Signed in again, signing out everywhere ends the other browser's too: it finds out at its next action.
        one.add_cookies([{"name": oidc.cookie_names().session, "value": add_session(path, who), "url": url}])
        a.reload()
        a.wait_for_selector("#account-btn.signed-in")
        b.click("#pause-btn")  # what b does next is an action, not a poll
        a.click("#account-btn")
        a.click("#account-menu [data-menu=signout-all]")
        a.wait_for_selector("#ov-state [data-signin]")
        assert live_sessions() == 0
        b.click("#att-list [data-act=reset] >> nth=0")
        b.wait_for_selector("#signin-dialog[open]")
        assert b.text_content("#signin-dialog .sheet-sub") == ("Your session was ended, by signing out elsewhere or by an admin. "
                                                               "Sign in again.")
        assert b.text_content("#signin-dialog .signin-back") == "You'll come back to this page. Then try it again."
        assert not b.is_visible("#account-btn.signed-in")
        assert a.errors == []
        assert b.errors and all("403" in e for e in b.errors), b.errors  # only the refused action
    finally:
        one.close()
        two.close()


def test_nothing_of_the_sign_in_is_left_where_scripts_or_people_could_read_it(sso_grid, page):  # noqa: F811
    url, path, idp = sso_grid
    idp.auto = True
    add_mapping(path, "grid-testers", permissions.TESTER)
    callbacks = []
    page.on("request", lambda r: callbacks.append(r.url) if "/auth/callback" in r.url else None)
    page.goto(url + "#/overview")
    page.click("#account-btn")
    page.click("#signin-sso")
    page.wait_for_selector("#account-btn.signed-in")
    page.wait_for_selector(".mx-row[data-key]")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(callbacks[-1]).query))
    (cookie,) = [c for c in page.context.cookies() if c["name"] == oidc.cookie_names().session]
    seen = page.evaluate("""() => [document.cookie, location.href, document.title, JSON.stringify(Object.entries(localStorage)),
        JSON.stringify(Object.entries(sessionStorage)), document.documentElement.outerHTML]""")
    assert oidc.cookie_names().session not in seen[0]  # HttpOnly
    for value in (cookie["value"], query["code"], query["state"]):
        assert len(value) >= 20 and not any(value in text for text in seen)
    assert page.errors == []


def test_the_account_menu_says_who_you_are_and_what_your_roles_allow(sso_grid, browser):  # noqa: F811
    url, path, idp = sso_grid
    who = dana(path, idp, groups=["grid-testers"])
    add_mapping(path, "grid-testers", permissions.TESTER)
    ctx = signed_in_context(browser, url, path, who)
    try:
        p = watched(ctx.new_page())
        p.goto(url)
        p.wait_for_selector("#account-btn.signed-in")
        p.click("#account-btn")
        assert "Signed in with single sign-on as dana@example.com" in p.text_content("#account-menu .menu-note")
        assert p.text_content("#account-menu .menu-roles") == "Tester"
        # In order, and no Admin: a Tester doesn't see people or the audit log.
        assert p.eval_on_selector_all("#account-menu [role^=menuitem]", "els => els.map(e => e.dataset.menu)") == [
            "access", "density", "signout", "signout-all"]
        p.click("#account-menu [data-menu=access]")
        p.wait_for_selector("#access-sheet[open] .perm-group")
        sheet = p.text_content("#access-sheet")
        assert "Signed in with single sign-on" in sheet and "dana@example.com" in sheet and "not verified" not in sheet
        assert "Tester · From the group ‘grid-testers’" in " ".join(sheet.split())
        copy = p.locator("#access-sheet [data-copy]")
        assert copy.get_attribute("data-copy") == DANA and copy.get_attribute("aria-label") == "Copy your subject"
        assert "An admin can list you as sub:" + DANA + " in GRID_OIDC_ADMINS" in sheet
        allowed = p.eval_on_selector_all("#access-sheet .perm-group li:has(.g-ok)", "els => els.map(e => e.textContent.split(':')[0])")
        assert set(allowed) == {"See the grid", "Make client tokens", "Use MFA secrets", "Add MFA secrets", "Use saved sign-ins",
                                "Add saved sign-ins"}
        assert p.locator("#access-sheet .perm-group li:has(.g-off)").count() == len(permissions.PERMISSIONS) - len(allowed)
        assert "Need more? Ask an admin of this grid." in sheet
        p.keyboard.press("Escape")
        p.wait_for_selector("#access-sheet", state="hidden")
        assert p.errors == []
    finally:
        ctx.close()


def test_your_access_fits_a_phone_whatever_the_subject(sso_grid, browser):  # noqa: F811
    """An Entra subject is 43 characters with nowhere to break, and an email address can be long too."""
    url, path, _ = sso_grid
    who = make_user(path, "Dana Lee", [permissions.TESTER], email="dana.lee.of.the.qa.team@example.com",
                    subject="AAAAAAAAAAAAAAAAAAAAAIkzqFVrSaSaFHy782bbtaQ")
    for width in (390, 320, 280):
        ctx = signed_in_context(browser, url, path, who, width=width, height=800)
        try:
            p = watched(ctx.new_page())
            p.goto(url)
            p.click("#account-btn")
            p.click("#account-menu [data-menu=access]")
            p.wait_for_selector("#access-sheet[open] .perm-group")
            assert p.evaluate("""() => { const d = document.getElementById('access-sheet');
                return d.scrollWidth <= d.clientWidth && document.documentElement.scrollWidth <= innerWidth; }"""), width
            assert p.errors == []
        finally:
            ctx.close()


def test_an_admin_token_the_server_no_longer_takes_is_dropped_at_once(sso_grid, browser):  # noqa: F811
    """A tab left with an old admin token beside a working session must stop sending it: each one sent counts as a
    wrong guess, and the polls would soon be refused for too many."""
    url, path, idp = sso_grid
    who = dana(path, idp, roles=[permissions.TESTER])
    ctx = signed_in_context(browser, url, path, who)
    # Once, as a tab that signed in with the admin token before it changed would have it.
    ctx.add_init_script("if (!sessionStorage.getItem('seen')) { sessionStorage.setItem('seen', '1'); "
                        "sessionStorage.setItem('gridToken', 'old-token'); }")
    try:
        p = watched(ctx.new_page())
        sent = []
        p.on("request", lambda r: sent.append(r.headers.get("x-grid-token")) if "/api/" in r.url else None)
        p.goto(url)
        p.wait_for_selector(".mx-row[data-key]")
        refresh_now(p)
        assert p.text_content("#account-btn .acct-label") == "Dana Lee"
        assert p.evaluate("sessionStorage.getItem('gridToken')") is None
        assert sent.count("old-token") == 1 and len(sent) > 2
        assert p.errors == []
    finally:
        ctx.close()


@pytest.mark.parametrize("reads", ["open", "signed-in"])
def test_an_admin_token_that_stops_working_while_the_page_is_open_is_dropped_at_the_next_answer(sso_grid, browser, monkeypatch, reads):  # noqa: F811
    """GRID_ADMIN_TOKEN changed and the Grid UI restarted, with a tab still signed in with the old one. Where reading
    is open, or a session cookie rides along, its polls still work, but each counts as a wrong guess: the first answer
    says so and the page forgets the token, rather than lock everyone at its address out of the new one."""
    url, path, idp = sso_grid
    monkeypatch.setenv("GRID_UI_READS", reads)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_init_script("if (!sessionStorage.getItem('seen')) { sessionStorage.setItem('seen', '1'); "
                        "sessionStorage.setItem('gridToken', 's3cret'); }")
    if reads == "signed-in":
        ctx.add_cookies([{"name": oidc.cookie_names().session, "value": add_session(path, dana(path, idp, roles=[permissions.TESTER])),
                          "url": url}])
    try:
        p = watched(ctx.new_page())
        p.goto(url)
        p.wait_for_selector(".mx-row[data-key]")
        assert p.text_content("#account-btn .acct-label") == "Admin"  # the token wins over the session
        monkeypatch.setattr(grid_app, "ADMIN_TOKEN", "rotated")
        p.wait_for_function("() => sessionStorage.getItem('gridToken') === null")
        p.wait_for_function("label => document.querySelector('#account-btn .acct-label').textContent === label",
                            arg="Dana Lee" if reads == "signed-in" else "Sign in")
        p.wait_for_timeout(3000)  # two more polls, without it
        assert len(auth._failures["127.0.0.1"]) == 1
        p.wait_for_selector(".mx-row[data-key]")  # still reading the grid, as Dana or as anyone

        # Anyone else at the address can sign in with the new one.
        other = watched(browser.new_page(viewport={"width": 1280, "height": 900}))
        try:
            other.goto(url)
            other.click("#account-btn")
            other.click("#signin-other summary")
            other.fill("#signin-token", "rotated")
            other.press("#signin-token", "Enter")
            other.wait_for_selector("#account-btn.signed-in")
            assert other.errors == []
        finally:
            other.close()
        assert p.errors == []
    finally:
        ctx.close()


def test_signing_out_while_a_poll_is_on_its_way_still_takes_the_grid_away(sso_grid, browser, monkeypatch):  # noqa: F811
    """Where reading needs sign-in: the poll was answered while Dana was signed in, so once it's in, the page decides
    again, and shows the sign-in panel rather than the grid."""
    url, path, idp = sso_grid
    monkeypatch.setenv("GRID_UI_READS", "signed-in")
    ctx = signed_in_context(browser, url, path, dana(path, idp, roles=[permissions.OPERATOR]))
    try:
        p = watched(ctx.new_page())
        p.goto(url)
        p.wait_for_selector(".mx-row[data-key]")
        held = []

        def hold(route):
            if held:
                return route.continue_()
            held.append((route, route.fetch()))  # answered now, as Dana, and handed to the page once she signed out

        p.route("**/api/status*", hold)
        for _ in range(50):
            if held:
                break
            p.wait_for_timeout(100)
        assert held
        p.click("#account-btn")
        p.click("#account-menu [data-menu=signout]")
        p.wait_for_selector("#account-btn:not(.signed-in)")
        route, answer = held[0]
        route.fulfill(response=answer)
        p.wait_for_selector("#ov-state [data-signin]")
        assert p.locator(".mx-row[data-key]:visible").count() == 0
        assert p.text_content("#ov-h1") == "Signed out"
        assert p.errors == []
    finally:
        ctx.close()


def test_who_took_an_action_is_forgotten_here_on_signing_out(sso_grid, browser, monkeypatch):  # noqa: F811
    """Where reading is open, the server tells only people signed in who took each action. This browser's own copy of
    the recording forgets the names when its person signs out, so whoever comes to it next doesn't see them."""
    url, path, _ = sso_grid
    monkeypatch.setenv("GRID_UI_READS", "open")
    vic = make_user(path, "Vic Viewer", [permissions.VIEWER], subject="00u-vic")
    robin = make_user(path, "Robin Hale", [permissions.ADMIN], subject="00u-robin")
    viewer, admin = signed_in_context(browser, url, path, vic), signed_in_context(browser, url, path, robin)
    try:
        v, a = watched(viewer.new_page()), watched(admin.new_page())
        v.goto(url + "#/activity")
        v.wait_for_selector("#account-btn.signed-in")
        a.goto(url)
        a.click("#att-list [data-act=reset] >> nth=0")
        a.wait_for_selector("#toast:not(.bad)")
        v.wait_for_function("() => document.getElementById('act-rows').textContent.includes('Robin Hale')", timeout=15000)
        v.click("#account-btn")
        v.click("#account-menu [data-menu=signout]")
        v.wait_for_selector("#account-btn:not(.signed-in)")
        v.wait_for_function("() => !document.getElementById('act-rows').textContent.includes('Robin Hale')")
        assert "reset" in v.text_content("#act-rows")  # the action stays, without who took it
        assert "Robin Hale" not in (v.evaluate("localStorage.getItem('gridRecorder')") or "")
        v.reload()
        v.wait_for_selector("#act-rows tr")
        v.wait_for_timeout(1500)
        assert "Robin Hale" not in v.text_content("#act-rows")
        assert v.errors == [] and a.errors == []
    finally:
        viewer.close()
        admin.close()


def test_where_reading_needs_sign_in_the_grid_views_say_why_without_polling(sso_grid, page, browser, monkeypatch):  # noqa: F811
    url, path, idp = sso_grid
    monkeypatch.setenv("GRID_UI_READS", "signed-in")
    polls = status_polls(page)
    page.goto(url)
    page.wait_for_selector("#ov-state [data-signin]")
    text = page.text_content("#ov-state")
    assert "Sign in to see the grid" in text and "This grid needs sign-in to view it." in text
    assert page.text_content("#ov-h1") == "Signed out"
    assert page.text_content("#fresh-text") == "Signed out"
    for hash_, state in (("#/sessions", "#sessions-state"), ("#/activity", "#act-state")):
        page.goto(url + hash_)
        page.wait_for_selector(state + " [data-signin]")
    page.wait_for_timeout(2000)
    assert polls == []
    page.click("#act-state [data-signin]")
    page.wait_for_selector("#signin-dialog[open] #signin-sso")
    assert page.text_content("#signin-dialog .sheet-sub") == "Sign in to see the grid."
    assert page.errors == []

    # Signed in without a role that sees the grid: why, and the way to see what the roles do allow.
    who = make_user(path, "Alex Moreno", issuer=idp.base_url, subject="00u-alex")
    ctx = signed_in_context(browser, url, path, who)
    try:
        p = watched(ctx.new_page())
        theirs = status_polls(p)
        p.goto(url)
        p.wait_for_selector("#ov-state [data-your-access]")
        text = p.text_content("#ov-state")
        assert "You don't have access to the grid view" in text
        assert "Your roles don't include “See the grid”. Ask an admin for a role that does." in text
        p.goto(url + "#/sessions")
        p.wait_for_selector("#sessions-state [data-your-access]")
        p.click("#sessions-state [data-your-access]")
        p.wait_for_selector("#access-sheet[open] .perm-group")
        assert "No roles yet: an admin can give you some." in p.text_content("#access-sheet")
        p.wait_for_timeout(1500)
        assert theirs == []
        assert p.errors == []
    finally:
        ctx.close()
