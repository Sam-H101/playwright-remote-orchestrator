"""What people's roles show and hide, in a real browser: sharing MFA secrets and saved sign-ins, owners on the
Access page, the panels for pages someone's roles leave out, a grid with only the admin token, as before, and the
Admin page: its tabs, the person sheet, the role editor, group mappings, the audit log and single sign-on.

Skipped unless the playwright package and its Chromium are installed, like test_browser.py.
"""
import json
import os
import re
import types

import pytest

from ui import permissions, store

pytest.importorskip("playwright.sync_api")
pytest.importorskip("cryptography")

from ui.tests.test_accounts import LEADS, OPERATOR, add_client_token, add_item, add_mapping, add_role, make_user, ts  # noqa: E402
from ui.tests.test_browser_mfa import set_hidden  # noqa: E402
from ui.tests.test_browser import browser, grid, page, run  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_tokens import _admin_context, admin_grid  # noqa: E402,F401  (fixtures)
from ui.tests.test_browser_oidc import signed_in_context, sso_grid, watched  # noqa: E402,F401  (fixtures)
from ui.tests.test_client_auth import AUDIENCE, W1, non_loopback_address  # noqa: E402

SEED = "JBSWY3DPEHPK3PXP"
HOSTILE = '<img src=x onerror="window.__xss=1">'
SHARE_FIELD = "#share-add-input"
# How many lines an element's text takes: the tops of its boxes, more than 8 px apart.
LINES = """el => { const r = document.createRange(); r.selectNodeContents(el);
    const tops = Array.from(r.getClientRects()).filter((b) => b.height).map((b) => b.top).sort((a, b) => a - b);
    return tops.filter((t, i) => !i || t - tops[i - 1] > 8).length; }"""


@pytest.fixture
def people_grid(sso_grid, monkeypatch):  # noqa: F811
    """The single sign-on grid with GRID_SECRETS_KEY set, and people: Robin, an admin; Bob and Dana, Testers; Vic, a
    Viewer; and a custom role, QA leads. (grid URL, sqlite path, the people and the role's id)."""
    url, path, _ = sso_grid
    monkeypatch.setenv("GRID_SECRETS_KEY", "k" * 32)
    # The demo's people, roles, group mappings and audit log make way for these.
    for table in ("users", "userRoles", "roles", "groupRoles", "auditLog"):
        run(path, "DELETE FROM " + table)
    who = types.SimpleNamespace(
        qa=add_role(path, "QA leads", "grid.view mfa.use signins.use audit.view"),
        robin=make_user(path, "Robin Hale", [permissions.ADMIN], email="robin@example.com"),
        bob=make_user(path, "Bob Stone", [permissions.TESTER], email="bob@example.com"),
        dana=make_user(path, "Dana Lee", [permissions.TESTER], email="dana@example.com"),
        vic=make_user(path, "Vic Viewer", [permissions.VIEWER], email="vic@example.com"))
    return url, path, who


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


def share_rows(p):
    """Who has access, as the share dialog lists them."""
    return [squash(t) for t in p.eval_on_selector_all("#share-list > li", "els => els.map(e => e.innerText)")]


def options(p):
    return [squash(t) for t in p.eval_on_selector_all("#share-add-list [role=option] .picker-name", "els => els.map(e => e.textContent)")]


def token_row(p, name):
    return p.locator("#token-rows tr", has_text=name)


def lines(locator):
    return locator.evaluate(LINES)


def reopen(p, view):
    """Leave the view and come back: opening it loads it again."""
    p.click("[data-nav-view=overview] >> visible=true")
    p.click("[data-nav-view={}] >> visible=true".format(view))


# ---- sharing ------------------------------------------------------------------------------------


def test_sharing_a_secret_from_its_row(people_grid, browser):  # noqa: F811
    """The picker's keys, adding and removing people and roles, the warning for whoever the share does nothing for,
    and focus back on the row's Share. Then the grantee sees it shared with them, to use and not to delete."""
    url, path, who = people_grid
    secret = store.create_mfa("intranet", SEED, "Intranet", "checks@example.com", owner_id=who.bob)["id"]
    ctx = signed_in_context(browser, url, path, who.bob)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#mfa-rows tr")
        r = p.locator("#mfa-rows tr", has_text="intranet")
        assert r.locator(".owner-line").text_content() == "Yours"
        # All is for whoever may see every secret or sign-in.
        assert p.eval_on_selector_all("#mfa-scope button:not([hidden])", "els => els.map(e => e.dataset.label)") == ["Mine", "Shared with me"]
        assert p.get_attribute("#mfa-scope [data-scope=mine]", "aria-pressed") == "true"
        r.locator("[data-share-mfa]").click()
        p.wait_for_selector("#share-dialog[open]")
        assert p.text_content("#share-title") == "Share “intranet”"
        assert p.evaluate("document.activeElement.id") == "share-add-input"
        assert squash(p.text_content("#share-owner")) == "Owner Bob Stone (you)"  # a Tester can't give it away
        assert share_rows(p) == ["Bob Stone Owner", "Admins see everything"]

        # Arrows move through what matches, Enter chooses: it's shared at once.
        field = p.locator(SHARE_FIELD)
        field.fill("da")
        p.wait_for_selector("#share-add-list [role=option]:has-text('Dana Lee')")
        assert field.get_attribute("aria-expanded") == "true" and field.get_attribute("aria-activedescendant") is None
        p.keyboard.press("ArrowDown")
        active = field.get_attribute("aria-activedescendant")
        assert p.get_attribute("#" + active, "aria-selected") == "true" and "Dana Lee" in p.text_content("#" + active)
        p.keyboard.press("Enter")
        p.wait_for_selector("#share-list [data-unshare='user:{}']".format(who.dana))
        assert run(path, "SELECT granteeKind, granteeId FROM itemShares WHERE itemId = ?", (secret,)) == [("user", who.dana)]
        assert field.input_value() == "" and field.get_attribute("aria-expanded") == "false"

        # Esc closes the list, then empties the field; the dialog stays.
        field.fill("vi")
        p.wait_for_selector("#share-add-list [role=option]:has-text('Vic Viewer')")
        assert options(p) == ["Viewer", "Vic Viewer"]  # roles, then people
        assert p.eval_on_selector_all("#share-add-list .picker-group", "els => els.map(e => e.textContent)") == ["Roles", "People"]
        p.keyboard.press("Escape")
        assert p.is_hidden("#share-add-list") and p.is_visible("#share-dialog")
        p.keyboard.press("Escape")
        assert field.input_value() == "" and p.is_visible("#share-dialog")

        # A share does nothing for whoever's roles don't include using it: the dialog says so.
        field.fill("vic")
        p.click("#share-add-list [role=option]:has-text('Vic Viewer')")
        p.wait_for_selector("#share-list [data-unshare='user:{}']".format(who.vic))
        field.fill("view")
        p.click("#share-add-list [role=option]:has-text('Viewer')")
        p.wait_for_selector("#share-list [data-unshare='role:-4']")
        assert p.eval_on_selector_all("#share-list .hint.notice", "els => els.map(e => e.textContent)") == [
            "Vic Viewer can't use it: their roles don't include “Use MFA secrets”.",
            "Viewer can't use it: the role doesn't include “Use MFA secrets”."]
        # Admins see every MFA secret already: the Admin role isn't offered.
        field.fill("adm")
        p.wait_for_selector("#share-add-list .picker-empty")
        assert p.text_content("#share-add-list") == "No one matches “adm”."
        p.keyboard.press("Escape")
        p.keyboard.press("Escape")

        p.click("#share-list [data-unshare='role:-4']")
        p.wait_for_selector("#share-list [data-unshare='role:-4']", state="detached")
        assert p.evaluate("document.activeElement.dataset.unshare") == "user:{}".format(who.vic)
        assert p.get_attribute("#share-list [data-unshare='user:{}']".format(who.vic), "aria-label") == "Stop sharing with Vic Viewer"
        assert sorted(run(path, "SELECT granteeKind, granteeId FROM itemShares")) == sorted([("user", who.dana), ("user", who.vic)])

        p.click("#share-dialog [data-dismiss]")
        p.wait_for_selector("#mfa-rows .owner-line:has-text('shared with 2')")  # the list, loaded again
        assert p.is_hidden("#share-dialog")
        assert p.evaluate("document.activeElement.dataset.shareMfa") == str(secret)
        line = r.locator(".owner-line")
        assert line.text_content() == "Yours · shared with 2"
        assert line.locator("span[title]").get_attribute("title") == "Dana Lee, Vic Viewer"
        assert p.errors == []
    finally:
        ctx.close()

    ctx = signed_in_context(browser, url, path, who.dana)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#mfa-empty [data-scope-to=shared]")  # nothing of her own
        assert "None of the MFA secrets are yours." in p.text_content("#mfa-empty")
        assert p.text_content("#mfa-scope [data-scope=shared]") == "Shared with me1"
        p.click("#mfa-scope [data-scope=shared]")
        r = p.locator("#mfa-rows tr", has_text="intranet")
        r.wait_for()
        assert r.locator(".owner-line").text_content() == "Bob Stone's · shared with you"
        assert r.locator("[data-show-code]").count() == 1
        assert r.locator("[data-delete-mfa], [data-share-mfa]").count() == 0
        assert p.errors == []
    finally:
        ctx.close()


def test_a_sign_in_is_shared_within_what_its_sharer_may_do(people_grid, browser):  # noqa: F811
    """Roles with permissions the sharer lacks aren't offered; a person with more is refused by the server, which
    the dialog says. An admin may give it to someone else, which takes its shares with it."""
    url, path, who = people_grid
    signin = add_item(path, "signin", "prod-admin", owner=who.bob)
    ctx = signed_in_context(browser, url, path, who.bob)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#signin-rows [data-share-signin]")
        assert p.text_content("#signin-rows .owner-line") == "Yours"
        p.click("#signin-rows [data-share-signin]")
        p.wait_for_selector("#share-dialog[open]")
        assert "have their checks reuse this sign-in" in p.text_content("#share-dialog .sheet-sub")
        assert share_rows(p) == ["Bob Stone Owner", "Admins see it here"]
        p.click(SHARE_FIELD)  # every role he may share it with, before anything is typed
        p.wait_for_selector("#share-add-list [role=option]")
        assert options(p) == ["Tester", "Viewer"]  # not Admin, Operator, nor QA leads (it reads the audit log)
        p.fill(SHARE_FIELD, "robin")
        p.click("#share-add-list [role=option]:has-text('Robin Hale')")
        p.wait_for_selector("#share-error:not([hidden])")
        assert p.text_content("#share-error").startswith("You can't share a saved sign-in with Robin Hale: they have permissions you don't have")
        assert p.input_value(SHARE_FIELD) == "robin"  # what was typed stays, to try again
        p.fill(SHARE_FIELD, "dana")
        p.click("#share-add-list [role=option]:has-text('Dana Lee')")
        p.wait_for_selector("#share-list [data-unshare='user:{}']".format(who.dana))
        assert p.is_hidden("#share-error")
        p.click("#share-dialog [data-dismiss]")
        assert all("403" in e for e in p.errors)  # the refusal provoked on purpose
    finally:
        ctx.close()

    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#signin-rows [data-share-signin]")
        assert p.get_attribute("#mfa-scope [data-scope=all]", "aria-pressed") == "true"  # where an admin starts
        assert p.text_content("#signin-rows .owner-line") == "Bob Stone's · shared with 1 · your checks can't use it"
        assert p.get_attribute("#signin-rows .owner-line span[title^='It isn']", "title") == (
            "It isn't shared with you or with Admin. Share it with Admin, or give it to Admins, to let your checks use it.")
        p.click("#signin-rows [data-share-signin]")
        p.wait_for_selector("#share-dialog[open]")
        p.fill(SHARE_FIELD, "adm")
        p.wait_for_selector("#share-add-list [role=option]:has-text('Admin')")
        assert p.text_content("#share-add-list .picker-note") == "Admins' checks can then reuse it"
        p.keyboard.press("Escape")

        p.click("#share-dialog [data-change-owner]")
        p.wait_for_selector("#share-owner-input")
        assert p.evaluate("document.activeElement.id") == "share-owner-input"
        assert "what's saved for it is dropped" in p.text_content("#share-owner .hint.notice")
        p.click("#share-owner-input")
        p.wait_for_selector("#share-owner-list [role=option]:has-text('Admins')")
        p.fill("#share-owner-input", "dana")
        p.wait_for_selector("#share-owner-list [role=option]:has-text('Dana Lee')")
        p.keyboard.press("ArrowDown")
        p.keyboard.press("Enter")
        p.wait_for_selector("#share-owner [data-change-owner]")
        assert squash(p.text_content("#share-owner dd")) == "Dana Lee Change owner…"
        assert share_rows(p) == ["Dana Lee Owner", "Admins see it here"]  # its shares went with it
        assert run(path, "SELECT userId FROM itemOwners WHERE itemKind = 'signin' AND itemId = ?", (signin,)) == [(who.dana,)]
        assert run(path, "SELECT COUNT(*) FROM itemShares") == [(0,)]
        assert p.evaluate("document.activeElement.dataset.changeOwner") == ""
        p.keyboard.press("Escape")
        p.wait_for_selector("#share-dialog", state="hidden")
        p.wait_for_function("() => document.querySelector('#signin-rows .owner-line').textContent.startsWith(\"Dana Lee's\")")
        assert p.errors == []
    finally:
        ctx.close()


def test_whom_a_sign_in_no_longer_reaches_is_told_why(people_grid, browser):  # noqa: F811
    """Dana was made an Operator after Bob shared his sign-in with her and with Testers: she may do more than he
    may, so her checks can't use it. She sees why; Bob's dialog says so of her (not of Testers, who may do no more
    than he may); an admin, who may see every sign-in, is told how to make it reach her. Someone who isn't an admin
    but may see every sign-in is told to share it with a role of theirs."""
    url, path, who = people_grid
    add_item(path, "signin", "prod-admin", owner=who.bob, shares=[("user", who.dana), ("role", permissions.TESTER)])
    run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (who.dana, permissions.OPERATOR, ts(60)))
    stewards = make_user(path, "Sam Steward", [add_role(path, "Sign-in stewards", "mfa.use signins.all")], email="sam@example.com")
    note = ("Dana Lee can't use it: they have permissions its owner doesn't, so only a share from someone with “Every saved "
            "sign-in” reaches them")
    for user, scope, title, notes in (
            (who.dana, "shared", "It's shared with you, but you may do things its owner may not, so it only reaches your "
                                 "checks when someone with “Every saved sign-in” shares it. Such a share stops reaching "
                                 "you once its owner may do less than when it was made.", None),
            (who.bob, None, None, [note + "."]),
            (who.robin, "all", "It isn't shared with you or with Admin. Share it with Admin, or give it to Admins, to let "
                               "your checks use it.", [note + ". Stop sharing and share it again to make it one."]),
            (stewards, "all", "It isn't shared with you or a role you have. Share it with one of your roles, or give it "
                              "to Admins, to let your checks use it.", None)):
        ctx = signed_in_context(browser, url, path, user)
        try:
            p = watched(ctx.new_page())
            p.goto(url + "#/mfa")
            if scope:
                p.wait_for_selector("#mfa-scope [aria-pressed=true]")
                p.click("#mfa-scope [data-scope={}]".format(scope))
            row = p.locator("#signin-rows tr", has_text="prod-admin")
            row.wait_for()
            if title:
                assert row.locator(".owner-line").text_content().endswith(" · your checks can't use it")
                assert row.locator(".owner-line span[title]").last.get_attribute("title") == title
            else:
                assert "can't use it" not in row.locator(".owner-line").text_content()
            if notes:
                row.locator("[data-share-signin]").click()
                p.wait_for_selector("#share-dialog[open] #share-list li")
                assert p.eval_on_selector_all("#share-list .hint.notice", "els => els.map(e => e.textContent)") == notes
                p.keyboard.press("Escape")
            assert p.errors == []
        finally:
            ctx.close()


def test_whom_a_share_no_longer_reaches_is_told_what_would(people_grid, browser):  # noqa: F811
    """Bob's sign-in was shared with Dana and with Robin by someone who may see every sign-in, while Bob was an
    Operator, and by Bob with QA leads, a role that may read the audit log, which he may not. He is a Tester now,
    and Dana an Operator: no share reaches the role's members, Dana or Robin. Bob is told who can make the lapsed
    shares reach again; Robin, who can, how; and since he can't share it with himself, what he can do instead."""
    url, path, who = people_grid
    signin = add_item(path, "signin", "prod-admin", owner=who.bob, shares=[("user", who.dana), ("user", who.robin)],
                      vouched_for=OPERATOR)
    run(path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) VALUES ('signin', ?, 'role', ?, ?)",
        (signin, who.qa, ts(60)))
    run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (who.dana, permissions.OPERATOR, ts(60)))
    lapsed = (" permissions its owner doesn't, and its owner may now do less than when it was shared, so the share no "
              "longer reaches ")
    again = " Someone with “Every saved sign-in” can stop sharing and share it again, which records what its owner may do now."
    role = ("Members of QA leads can't use it: the role has permissions its owner doesn't, so only a share from someone "
            "with “Every saved sign-in” reaches them.")
    for user, notes in (
            (who.bob, ["Dana Lee can't use it: they have" + lapsed + "them." + again,
                       "Robin Hale can't use it: they have" + lapsed + "them." + again, role]),
            (who.robin, ["Dana Lee can't use it: they have" + lapsed + "them. Stop sharing and share it again to record "
                         "what its owner may do now.",
                         "You can't use it: you have" + lapsed + "you. Share it with Admin, or ask someone else with “Every "
                         "saved sign-in” to share it with you.",
                         role + " Stop sharing and share it again to make it one."])):
        ctx = signed_in_context(browser, url, path, user)
        try:
            p = watched(ctx.new_page())
            p.goto(url + "#/mfa")
            if user == who.robin:
                p.wait_for_selector("#mfa-scope [aria-pressed=true]")
                p.click("#mfa-scope [data-scope=all]")
            row = p.locator("#signin-rows tr", has_text="prod-admin")
            row.wait_for()
            row.locator("[data-share-signin]").click()
            p.wait_for_selector("#share-dialog[open] #share-list li")
            assert p.eval_on_selector_all("#share-list .hint.notice", "els => els.map(e => e.textContent)") == notes
            p.keyboard.press("Escape")
            assert p.errors == []
        finally:
            ctx.close()


# ---- owners on the Access page ------------------------------------------------------------------------


def test_tokens_that_act_as_an_admin_are_given_to_people(people_grid, browser, monkeypatch):  # noqa: F811
    url, path, who = people_grid
    monkeypatch.setenv("GATEWAY_CLIENT_TOKEN", "bootstrap-client-token-for-tests")
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/access")
        p.wait_for_selector("#token-rows tr")
        assert p.is_visible("#token-owner-col")
        # What a token nobody owns gets, as the checks' routes decide it: every MFA secret, but of the saved sign-ins
        # only the admins' and those shared with the Admin role. The demo's Grid monitor is nobody's either, but its
        # token scope leaves out both, so it isn't counted.
        assert squash(p.text_content("#acts-banner")) == (
            "1 client token acts as an admin: it was made before single sign-on or with the admin token, so checks using it "
            "get every MFA secret, and the saved sign-ins that are Admins' or shared with Admin. Give it to the person whose "
            "checks use it. GATEWAY_CLIENT_TOKEN also acts as an admin. Once people use client tokens of their own, and the "
            "checks that ask the Grid UI for codes and sign-ins send one as GRID_MFA_TOKEN, take it out of the Grid UI's "
            "environment.")
        cell = token_row(p, "Example checks").locator(".cell-owner")
        assert cell.locator(".tag").text_content() == "Acts as an admin"
        assert cell.locator(".tag").get_attribute("title").startswith("Made before single sign-on, or with the admin token.")
        assert token_row(p, "Staging checks 2025").locator("[data-assign-token]").count() == 0  # expired: nothing to give
        assert token_row(p, "Docker runners").locator(".cell-owner").inner_text() == "Admins"
        # With an Owner column, dates and node names keep to a line and the names wrap instead. Give to… goes beside
        # the tag where there's room, and under it, lined up, where there isn't.
        for name, part in (("WIN-BUILD-01 agent", ".cell-bound code"), ("WIN-BUILD-01 agent", ".cell-expires"),
                           ("Docker runners", ".node-ref"), ("Staging checks 2025", ".cell-created")):
            assert lines(token_row(p, name).locator(part).first) == 1, (name, part)
        tag, give = cell.locator(".tag"), cell.locator("[data-assign-token]")
        for width, beside in ((1440, True), (280, False)):
            p.set_viewport_size({"width": width, "height": 900})
            t, g = tag.bounding_box(), give.bounding_box()
            if beside:
                assert abs((t["y"] + t["height"] / 2) - (g["y"] + g["height"] / 2)) < 1 and g["x"] > t["x"] + t["width"], width
                assert lines(token_row(p, "Staging checks 2025").locator(".tok-name")) == 1  # with room, nothing wraps
            else:
                assert g["y"] >= t["y"] + t["height"] and abs(g["x"] - t["x"]) < 1, width
        p.set_viewport_size({"width": 1280, "height": 900})

        cell.locator("[data-assign-token]").click()
        p.wait_for_selector("#token-owner-dialog[open]")
        assert p.text_content("#token-owner-title") == "Give “Example checks” to someone"
        assert p.evaluate("document.activeElement.id") == "give-token-input"
        p.fill("#give-token-input", "dana")
        p.wait_for_selector("#give-token-list [role=option]:has-text('Dana Lee')")
        p.keyboard.press("ArrowDown")
        p.keyboard.press("Enter")
        p.wait_for_selector("#token-owner-dialog", state="hidden")
        p.wait_for_selector("#token-rows tr:has-text('Example checks') .cell-owner:has-text('Dana Lee')")
        (token_id,) = run(path, "SELECT id FROM accessTokens WHERE name = 'Example checks'")[0]
        assert run(path, "SELECT userId FROM itemOwners WHERE itemKind = 'token' AND itemId = ?", (token_id,)) == [(who.dana,)]
        p.wait_for_function("id => document.activeElement && document.activeElement.dataset.token === String(id)", arg=token_id)
        env = ("GATEWAY_CLIENT_TOKEN acts as an admin. Once people use client tokens of their own, and the checks that ask the "
               "Grid UI for codes and sign-ins send one as GRID_MFA_TOKEN, take it out of the Grid UI's environment.")
        p.wait_for_function("t => document.querySelector('#acts-banner').textContent.replace(/\\s+/g, ' ').trim() === t", arg=env)
        # A token scope that keeps MFA secrets counts again, as far as it goes.
        run(path, "UPDATE accessTokens SET scope = 'grid.view mfa.use' WHERE name = 'Grid monitor'")
        reopen(p, "access")
        p.wait_for_selector("#acts-banner:has-text('1 client token acts')")
        assert squash(p.text_content("#acts-banner")) == (
            "1 client token acts as an admin: it was made before single sign-on or with the admin token, so checks using it "
            "get every MFA secret, and the saved sign-ins that are Admins' or shared with Admin, as far as its token scope "
            "lets them. Give it to the person whose checks use it. " + env.replace("acts", "also acts", 1))
        assert p.errors == []
    finally:
        ctx.close()


def test_the_give_dialog_says_a_scoped_token_keeps_its_limit(people_grid, browser):  # noqa: F811
    """Given to a person, a token acts as them, narrowed by the scope it was made with."""
    url, path, who = people_grid
    limit = "It keeps its limit: it may only see the grid."
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/access")
        p.wait_for_selector("#token-rows tr")
        # A token without a scope: the dialog is as it was.
        token_row(p, "Example checks").locator("[data-assign-token]").click()
        p.wait_for_selector("#token-owner-dialog[open]")
        assert p.locator("#token-owner-dialog .sheet-sub").count() == 1
        assert "keeps its limit" not in p.text_content("#token-owner-dialog")
        p.click("#token-owner-dialog [data-dismiss]")
        p.wait_for_selector("#token-owner-dialog", state="hidden")

        token_row(p, "Grid monitor").locator("[data-assign-token]").click()
        p.wait_for_selector("#token-owner-dialog[open]")
        assert p.text_content("#token-owner-title") == "Give “Grid monitor” to someone"
        assert [squash(t) for t in p.locator("#token-owner-dialog .sheet-sub").all_text_contents()][1:] == [limit]
        assert p.evaluate("document.activeElement.id") == "give-token-input"
        p.fill("#give-token-input", "dana")
        p.wait_for_selector("#give-token-list [role=option]:has-text('Dana Lee')")
        p.keyboard.press("ArrowDown")
        p.keyboard.press("Enter")
        p.wait_for_selector("#token-owner-dialog", state="hidden")
        p.wait_for_selector("#token-rows tr:has-text('Grid monitor') .cell-owner:has-text('Dana Lee')")
        # It is Dana's now, and limited as before.
        assert token_row(p, "Grid monitor").locator(".tok-scope").text_content() == "May only see the grid"
        ((token_id, scope, stored),) = run(path, "SELECT id, scope, tokenHash FROM accessTokens WHERE name = 'Grid monitor'")
        assert scope == "grid.view" and stored.startswith("scoped:")
        assert run(path, "SELECT userId FROM itemOwners WHERE itemKind = 'token' AND itemId = ?", (token_id,)) == [(who.dana,)]
        assert p.errors == []
    finally:
        ctx.close()


def test_a_testers_access_page_warns_from_the_grids_counts_not_from_its_own_list(people_grid, browser):  # noqa: F811
    """A Tester sees only their own tokens, but the first and last token warnings are about every token the hub
    knows: they come from the server's counts."""
    url, path, who = people_grid
    mine, _ = add_client_token(path, "bob-ci", owner=who.bob)
    ctx = signed_in_context(browser, url, path, who.bob)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/access")
        p.wait_for_selector("#token-rows tr")
        assert p.eval_on_selector_all("#token-rows .tok-name", "els => els.map(e => e.textContent)") == ["bob-ci"]
        assert token_row(p, "bob-ci").locator(".cell-owner").inner_text() == "Bob Stone"
        assert p.is_hidden("#add-host-btn") and p.is_hidden("#acts-banner")

        # The demo's client token works too, though he can't see it: his isn't the last.
        token_row(p, "bob-ci").locator("[data-revoke]").click()
        p.wait_for_selector("#confirm-dialog[open]")
        assert "last working" not in p.text_content("#confirm-note")
        p.click("#confirm-dialog [data-dismiss]")
        p.click("#new-token-btn")
        assert p.is_disabled("input[name=kind][value=agent]") and p.is_checked("input[name=kind][value=client]")
        assert p.text_content("#nt-kind-hint") == "Making agent tokens needs “Add hosts”."
        assert p.is_hidden("#nt-first") and p.is_hidden("#nt-node-field")
        assert p.text_content("#nt-client-note") == ("Checks using this token get the MFA secrets and saved sign-ins you can use, "
                                                     "and lose them if you lose access.")
        # Limited to neither MFA secrets nor saved sign-ins, it gets none of them.
        p.check("input[name=limit][value=scope]")
        p.check("#nt-scope-list input[value='grid.view']")
        assert p.is_hidden("#nt-client-note")
        p.check("#nt-scope-list input[value='signins.use']")
        assert p.is_visible("#nt-client-note")
        p.click("#new-token-dialog [data-dismiss]")

        # His is the grid's only client token now.
        run(path, "DELETE FROM accessTokens WHERE kind = 'client' AND id != ?", (mine,))
        reopen(p, "access")
        p.wait_for_selector("#token-rows tr")
        token_row(p, "bob-ci").locator("[data-revoke]").click()
        p.wait_for_selector("#confirm-dialog[open]")
        assert "It's the last working client token" in p.text_content("#confirm-note")
        p.click("#confirm-ok")
        p.wait_for_selector("#token-rows tr[data-status=revoked]")
        delete = token_row(p, "bob-ci").locator("[data-delete-token]")
        assert delete.is_disabled()
        assert delete.get_attribute("title").startswith("It's the last client token on record: deleting it lets checks without a token back in")

        # With no client token at all, the first is for someone who may see every token.
        run(path, "DELETE FROM accessTokens WHERE kind = 'client'")
        reopen(p, "access")
        p.wait_for_selector("#access-state [data-new-token]")
        assert "No access tokens of yours yet" in p.text_content("#access-state")
        p.click("#access-state [data-new-token]")
        assert p.is_disabled("input[name=kind][value=client]") and p.is_disabled("input[name=kind][value=agent]")
        assert p.text_content("#nt-kind-hint").endswith("so someone whose roles include “Every access token” makes it.")
        assert p.is_disabled("#nt-submit")
        p.click("#new-token-dialog [data-dismiss]")
        assert p.errors == []
    finally:
        ctx.close()


# ---- what roles leave out ----------------------------------------------------------------------------


def test_a_viewer_is_told_what_their_roles_leave_out_without_asking(people_grid, browser):  # noqa: F811
    """Decided from the session before anything is fetched, so nothing is refused and nothing is logged."""
    url, path, who = people_grid
    ctx = signed_in_context(browser, url, path, who.vic)
    try:
        p = watched(ctx.new_page())
        asked = []
        p.on("request", lambda r: asked.append(r.url) if re.search(r"/api/(tokens|mfa|signins)", r.url) else None)
        p.goto(url + "#/access")
        p.wait_for_selector("#access-state [data-your-access]")
        assert "Your roles don't include access tokens" in p.text_content("#access-state")
        assert "Ask an admin for a role with “Make client tokens” or “Add hosts”." in p.text_content("#access-state")
        assert p.is_hidden("#new-token-btn") and p.is_hidden("#add-host-btn")

        p.click("[data-nav-view=mfa] >> visible=true")
        p.wait_for_selector("#mfa-empty [data-your-access]")
        assert "Your roles don't include “Use MFA secrets”" in p.text_content("#mfa-secrets")
        assert "Your roles don't include “Use saved sign-ins”" in p.text_content("#signins")
        assert p.is_hidden("#add-mfa-btn") and p.is_hidden("#mfa-toolbar") and p.is_hidden("#mfa-panel")
        p.click("#mfa-empty [data-your-access]")
        p.wait_for_selector("#access-sheet[open]")
        p.keyboard.press("Escape")

        # Grid actions are there, and say why they can't be used.
        p.click("[data-nav-view=overview] >> visible=true")
        p.wait_for_selector("#att-list [data-act=reset]")
        reset = p.locator("#att-list [data-act=reset]").first
        assert reset.is_disabled()
        assert reset.get_attribute("aria-label").startswith("Your roles don't let you reset failures on ")
        assert reset.get_attribute("title") == reset.get_attribute("aria-label")
        # Greyed as any unavailable button is, unlike the signed-out lock, which opens sign-in.
        disabled = p.evaluate("""() => { const b = document.createElement('button'); b.className = 'btn'; b.disabled = true;
            document.body.append(b); const c = getComputedStyle(b).color; b.remove(); return c; }""")
        assert reset.evaluate("b => getComputedStyle(b).color") == disabled
        assert asked == []
        assert p.errors == []
    finally:
        ctx.close()


def test_pages_follow_a_role_given_while_they_are_open(people_grid, browser):  # noqa: F811
    """What a new role allows shows once the tab is shown again (the page asks /api/session then), even where the
    list on the page stays the same."""
    url, path, who = people_grid
    una = make_user(path, "Una Usher", [add_role(path, "Users only", "grid.view mfa.use signins.use")], email="una@example.com")
    run(path, "DELETE FROM accessTokens WHERE kind = 'agent'")  # so Operator shows Bob no more tokens than Tester does
    unas, bobs = signed_in_context(browser, url, path, una), signed_in_context(browser, url, path, who.bob)
    try:
        u, b = watched(unas.new_page()), watched(bobs.new_page())
        u.goto(url + "#/mfa")
        u.wait_for_selector("#mfa-empty:has-text('No MFA secrets are shared with you yet')")
        b.goto(url + "#/access")
        b.wait_for_selector("#access-state:has-text('No access tokens of yours yet')")
        assert b.is_hidden("#add-host-btn")
        for user, role in ((una, permissions.TESTER), (who.bob, permissions.OPERATOR)):
            run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (user, role, ts(0)))
        for p in (u, b):
            set_hidden(p, True)
            set_hidden(p, False)
        u.wait_for_selector("#mfa-empty [data-add-mfa]")  # Tester adds MFA secrets
        b.wait_for_selector("#add-host-btn:visible")  # Operator adds hosts
        assert "No access tokens of yours yet" in b.text_content("#access-state")
        assert u.errors == [] and b.errors == []
    finally:
        unas.close()
        bobs.close()


def test_a_grid_with_only_the_admin_token_shows_no_owners_or_sharing(admin_grid, browser, monkeypatch):  # noqa: F811
    url, path = admin_grid
    monkeypatch.setenv("GRID_SECRETS_KEY", "k" * 32)
    store.create_mfa("intranet", SEED)
    add_item(path, "signin", "prod-admin")
    ctx = _admin_context(browser)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#mfa-rows tr")
        p.wait_for_selector("#signin-rows tr")
        assert p.locator("[data-share-mfa], [data-share-signin], .owner-line").count() == 0
        assert p.is_hidden("#mfa-toolbar")
        assert p.locator("#mfa-rows [data-delete-mfa]").count() == 1 and p.locator("#signin-rows [data-forget-signin]").count() == 1
        assert p.is_visible("#add-mfa-btn")
        assert p.is_visible("[data-admin-nav]")  # the token holder's way to the Admin page

        p.click("[data-nav-view=access] >> visible=true")
        p.wait_for_selector("#token-rows tr")
        assert p.is_hidden("#token-owner-col") and p.locator("#token-rows .cell-owner").count() == 0
        # Without that column the table is laid out as it always was.
        assert p.eval_on_selector("#token-rows .cell-created", "c => getComputedStyle(c).whiteSpace") == "normal"
        assert p.is_hidden("#acts-banner") and p.is_visible("#add-host-btn")
        p.click("#new-token-btn")
        p.check("input[name=kind][value=client]")
        assert not p.is_disabled("input[name=kind][value=agent]") and p.is_hidden("#nt-kind-hint")
        assert p.is_hidden("#nt-client-note")  # made with the admin token, it acts as an admin
        p.click("#new-token-dialog [data-dismiss]")
        assert p.errors == []
    finally:
        ctx.close()


# ---- phones -----------------------------------------------------------------------------------------


def test_the_share_dialog_fits_a_phone(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    secret = store.create_mfa("intranet", SEED, owner_id=who.bob)["id"]
    for grantee_kind, grantee_id in (("user", who.dana), ("role", who.qa), ("user", who.vic)):
        run(path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) VALUES ('mfa', ?, ?, ?, '2026-01-01')",
            (secret, grantee_kind, grantee_id))
    for name in ("Alexandra Montgomery-Whitfield", "Maximilian Featherstonehaugh"):  # long names and emails, to wrap
        make_user(path, name, [permissions.TESTER], email=name.lower().replace(" ", ".") + "@subsidiary-division.example.com")
    ctx = signed_in_context(browser, url, path, who.robin, 375, 800)
    fits = """() => { const d = document.getElementById('share-dialog'), r = d.getBoundingClientRect();
        return document.documentElement.scrollWidth <= innerWidth && r.left >= 0 && r.right <= innerWidth + 0.5
          && d.scrollWidth <= d.clientWidth; }"""
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        for width in (375, 390, 320, 280):
            p.set_viewport_size({"width": width, "height": 800})
            p.click("#mfa-rows [data-share-mfa]")
            p.wait_for_selector("#share-dialog[open]")
            assert p.evaluate("getComputedStyle(document.getElementById('share-dialog')).borderTopLeftRadius") == "12px"  # a bottom sheet
            p.fill(SHARE_FIELD, "example.com")
            p.wait_for_selector("#share-add-list [role=option]:has-text('Featherstonehaugh')")
            assert p.evaluate(fits), width
            p.keyboard.press("Escape")
            p.click("#share-dialog [data-change-owner]")
            p.wait_for_selector("#share-owner-input")
            assert p.evaluate(fits), width
            p.click("#share-dialog [data-dismiss]")
            p.wait_for_selector("#share-dialog", state="hidden")
        assert p.errors == []
    finally:
        ctx.close()


# ---- escaping -----------------------------------------------------------------------------------------


def test_names_from_people_and_the_database_are_escaped(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    email = '"><img src=x onerror="window.__xss=2">@example.com'
    # Eve owns things; Mal, named the same, has nothing, so the pickers offer him.
    eve = make_user(path, HOSTILE, [permissions.TESTER], email=email, subject="eve")
    make_user(path, HOSTILE, [permissions.TESTER], email=email, subject="mal")
    role = add_role(path, HOSTILE, "grid.view mfa.use signins.use")
    add_role(path, HOSTILE + "!", "grid.view")
    secret = store.create_mfa("intranet", SEED, owner_id=eve)["id"]
    run(path, "UPDATE mfaSecrets SET name = ?, issuer = ?, account = ? WHERE id = ?", (HOSTILE, HOSTILE, HOSTILE, secret))
    run(path, "INSERT INTO itemShares (itemKind, itemId, granteeKind, granteeId, sharedAt) VALUES ('mfa', ?, 'role', ?, '2026-01-01')",
        (secret, role))
    add_item(path, "signin", HOSTILE + " sign-in", owner=eve, shares=[("user", who.dana), ("role", role)])
    add_client_token(path, HOSTILE + " token", owner=eve)
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/mfa")
        p.wait_for_selector("#mfa-rows tr")
        p.wait_for_selector("#signin-rows tr")
        assert p.text_content("#mfa-rows .owner-line").startswith(HOSTILE + "'s · shared with 1")
        p.click("#mfa-rows [data-share-mfa]")
        p.wait_for_selector("#share-dialog[open]")
        assert p.text_content("#share-title") == "Share “" + HOSTILE + "”"
        assert HOSTILE in p.text_content("#share-owner") and HOSTILE in p.text_content("#share-list")
        p.fill(SHARE_FIELD, "img")
        p.wait_for_selector("#share-add-list [role=option]:has-text('example.com')")
        assert options(p) == [HOSTILE + "!", HOSTILE]  # the role it isn't shared with yet, and Mal
        p.keyboard.press("Escape")
        p.click("#share-dialog [data-dismiss]")
        p.wait_for_selector("#share-dialog", state="hidden")
        p.click("#signin-rows [data-share-signin]")
        p.wait_for_selector("#share-dialog[open]")
        assert p.text_content("#share-title") == "Share “" + HOSTILE + " sign-in”"
        assert share_rows(p)[2:] == ["Dana Lee dana@example.com", HOSTILE + " role"]
        p.click("#share-dialog [data-change-owner]")
        p.fill("#share-owner-input", "img")
        p.wait_for_selector("#share-owner-list [role=option]:has-text('example.com')")
        p.keyboard.press("Escape")
        p.click("#share-dialog [data-dismiss]")

        p.click("[data-nav-view=access] >> visible=true")
        p.wait_for_selector("#token-rows tr")
        assert HOSTILE in p.locator("#token-rows tr", has_text="onerror").locator(".cell-owner").inner_text()
        token_row(p, "Example checks").locator("[data-assign-token]").click()
        p.fill("#give-token-input", "img")
        p.wait_for_selector("#give-token-list [role=option]")
        assert HOSTILE in p.text_content("#give-token-list")
        p.keyboard.press("Escape")
        p.keyboard.press("Escape")
        p.keyboard.press("Escape")
        p.wait_for_selector("#token-owner-dialog", state="hidden")
        p.wait_for_timeout(300)
        assert p.evaluate("window.__xss") is None
        assert p.evaluate("document.querySelectorAll('img').length") == 0
        assert p.errors == []
    finally:
        ctx.close()


# ---- the Admin page ---------------------------------------------------------------------------


def listed(p, selector):
    """The text of everything selector finds, its spaces squashed."""
    return [squash(t) for t in p.eval_on_selector_all(selector, "els => els.map(e => e.textContent)")]


def people(p):
    return listed(p, "#user-rows .user-name")


def rows_are(p, selector, count):
    p.wait_for_function("a => document.querySelectorAll(a.selector).length === a.count", arg={"selector": selector, "count": count})


def names_are(p, names):
    """Wait for the Users tab to list exactly these people, in this order."""
    p.wait_for_function("names => [...document.querySelectorAll('#user-rows .user-name')].map(e => e.textContent).join('|') === names",
                        arg="|".join(names))


def audit_row(path, action, actor=(None, "Admin", "token"), target=(None, None, None), details=None, seconds_ago=60):
    """An audit log row, as a change writes it: actor is (actorId, actorName, actorMethod), target (kind, id, name)."""
    run(path, "INSERT INTO auditLog (occurredAt, actorId, actorName, actorMethod, address, action, targetKind, targetId, "
              "targetName, details) VALUES (?, ?, ?, ?, '10.0.0.7', ?, ?, ?, ?, ?)",
        (ts(seconds_ago),) + tuple(actor) + (action,) + tuple(target) + (json.dumps(details) if details else None,))


def open_sheet(p, url, user_id):
    p.goto(url + "#/admin?tab=users&user={}".format(user_id))
    p.wait_for_selector("#user-dialog[open] [data-role-check]")


def close_sheet(p):
    """Esc, and the dialog's close event takes the person out of the address: going elsewhere before it's in would
    have it put the Users tab back."""
    p.keyboard.press("Escape")
    p.wait_for_function("() => !location.hash.includes('user=')")


DIALOG_FITS = """id => { const d = document.getElementById(id), r = d.getBoundingClientRect();
    return document.documentElement.scrollWidth <= innerWidth && r.left >= 0 && r.right <= innerWidth + 0.5
      && d.scrollWidth <= d.clientWidth; }"""


def test_the_admin_page_keeps_its_tab_and_the_person_open_in_the_address(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin")
        p.wait_for_selector("#user-rows tr")
        assert p.get_attribute("#admin-tab-users", "aria-selected") == "true"
        assert sorted(people(p)) == ["Bob Stone", "Dana Lee", "Robin Hale", "Vic Viewer"]
        p.goto(url + "#/admin?tab=roles")
        p.wait_for_selector("#role-rows tr")
        assert p.get_attribute("#admin-tab-roles", "aria-selected") == "true" and p.is_hidden("#admin-panel-users")
        # The tablist's keys: arrows move along the tabs and End goes to the last, and the address follows.
        p.focus("#admin-tab-roles")
        p.keyboard.press("ArrowRight")
        p.wait_for_function("() => location.hash === '#/admin?tab=groups'")
        assert p.evaluate("document.activeElement.id") == "admin-tab-groups"
        # Its ring is drawn inside it: the tab strip scrolls sideways, which cuts off anything outside.
        assert p.evaluate("getComputedStyle(document.activeElement).outlineOffset") == "-2px"
        p.keyboard.press("End")
        p.wait_for_selector("#admin-panel-sso:not([hidden]) .sso-facts")
        assert p.evaluate("location.hash") == "#/admin?tab=sso"

        # Someone's sheet, from the address; closing it takes them out of it again.
        p.goto(url + "#/admin?tab=users&user={}".format(who.dana))
        p.wait_for_selector("#user-dialog[open] #user-tab-access")
        assert p.text_content("#user-title") == "Dana Lee"
        assert p.text_content("#user-dialog .sheet-sub") == "dana@example.com · Signed in with single sign-on"
        assert lines(p.locator("#user-dialog .user-facts dt", has_text="Groups at last sign-in")) == 1  # its longest label
        # Its tabs differ in height, but its top stays put: the tabs stay under the pointer.
        tabs_at = "document.getElementById('user-tab-access').getBoundingClientRect().top"
        before = p.evaluate(tabs_at)
        p.click("#user-tab-sessions")
        p.wait_for_selector("#user-part-sessions:not([hidden])")
        assert abs(p.evaluate(tabs_at) - before) < 1
        p.keyboard.press("Escape")
        p.wait_for_function("() => location.hash === '#/admin?tab=users'")  # once the dialog's close event is in
        assert p.is_hidden("#user-dialog")
        # Manage opens it too, and closing it goes back to Manage.
        p.click("#user-rows tr:has-text('Bob Stone') [data-user]")
        p.wait_for_selector("#user-dialog[open] #user-tab-access")
        assert p.evaluate("location.hash") == "#/admin?tab=users&user={}".format(who.bob)
        assert p.text_content("#user-title") == "Bob Stone"
        p.click("#user-dialog [data-dismiss]")
        p.wait_for_function("() => location.hash === '#/admin?tab=users'")
        assert p.evaluate("document.activeElement.dataset.fk") == "manage-{}".format(who.bob)
        # And Enter on a row.
        p.focus("#user-rows [data-user-row='{}']".format(who.vic))
        p.keyboard.press("Enter")
        p.wait_for_selector("#user-dialog[open] #user-tab-access")
        assert p.text_content("#user-title") == "Vic Viewer"
        p.keyboard.press("Escape")
        assert p.errors == []
    finally:
        ctx.close()


def test_people_are_found_filtered_and_paged_through_the_address(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    for i in range(50):
        make_user(path, "Tess {:02d}".format(i), [permissions.TESTER], email="tess{}@example.com".format(i))
    make_user(path, "Lee Chen", [permissions.VIEWER], disabled=True)
    make_user(path, "Alex Moreno")
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=users&sort=name")
        p.wait_for_selector("#user-rows tr")
        assert p.input_value("#user-sort") == "name"
        assert listed(p, "#user-status button") == ["Active55", "Disabled1", "All56"]
        assert len(people(p)) == 50 and people(p)[:5] == ["Alex Moreno", "Bob Stone", "Dana Lee", "Robin Hale", "Tess 00"]
        assert p.text_content("#user-page-at") == "1–50 of 55" and p.is_disabled("#user-pager [data-page='-1']")
        p.click("#user-pager [data-page='1']")
        names_are(p, ["Tess 46", "Tess 47", "Tess 48", "Tess 49", "Vic Viewer"])
        assert p.evaluate("location.hash") == "#/admin?tab=users&sort=name&page=2"
        assert p.text_content("#user-page-at") == "51–55 of 55" and p.is_disabled("#user-pager [data-page='1']")
        # The address keeps the page, as it keeps every filter.
        p.reload()
        p.wait_for_selector("#user-rows tr:has-text('Vic Viewer')")
        assert len(people(p)) == 5

        # What's typed is looked for once typing stops, from the first page.
        p.fill("#user-filter", "DANA")
        names_are(p, ["Dana Lee"])
        assert p.evaluate("location.hash") == "#/admin?tab=users&sort=name&q=DANA"
        assert p.is_hidden("#user-pager")
        p.fill("#user-filter", "")
        p.wait_for_function("() => location.hash === '#/admin?tab=users&sort=name'")
        rows_are(p, "#user-rows tr", 50)

        # Disabled and All, through a reload.
        p.click("#user-status [data-status=disabled]")
        names_are(p, ["Lee Chen"])
        assert p.evaluate("location.hash") == "#/admin?tab=users&sort=name&status=disabled"
        p.reload()
        p.wait_for_selector("#user-rows tr:has-text('Lee Chen')")
        assert p.get_attribute("#user-status [data-status=disabled]", "aria-pressed") == "true" and people(p) == ["Lee Chen"]
        p.click("#user-status [data-status=any]")
        p.wait_for_function("() => location.hash.includes('status=any')")
        p.wait_for_function("() => document.getElementById('user-page-at').textContent === '1–50 of 56'")
        p.reload()
        p.wait_for_selector("#user-rows tr")
        assert p.get_attribute("#user-status [data-status=any]", "aria-pressed") == "true"
        assert p.text_content("#user-page-at") == "1–50 of 56"

        # A role, and no role at all.
        p.select_option("#user-role", str(permissions.VIEWER))
        names_are(p, ["Lee Chen", "Vic Viewer"])
        assert "role=-4" in p.evaluate("location.hash")
        p.select_option("#user-role", "none")
        names_are(p, ["Alex Moreno"])
        assert listed(p, "#user-rows .cell-roles") == ["No roles"]

        # Nobody matches: it says so, and clears the filters.
        p.fill("#user-filter", "zzz")
        p.wait_for_selector("#user-empty [data-clear-users]")
        assert "Nothing matches “zzz” with these filters." in p.text_content("#user-empty") and p.is_hidden("#user-panel")
        p.click("#user-empty [data-clear-users]")
        p.wait_for_function("() => location.hash === '#/admin?tab=users&sort=name'")
        rows_are(p, "#user-rows tr", 50)
        assert p.evaluate("document.activeElement.id") == "user-filter"
        assert p.errors == []
    finally:
        ctx.close()


def test_the_person_sheet_changes_access_at_once(people_grid, browser):  # noqa: F811
    """Roles given and taken straight away, the client tokens a role taken leaves working, a session ended, and
    someone disabled with their tokens; your own sheet changes nothing."""
    url, path, who = people_grid
    for name in ("dana-ci", "dana-nightly"):
        add_client_token(path, name, owner=who.dana)
    add_client_token(path, "bob-ci", owner=who.bob)
    danas, bobs = signed_in_context(browser, url, path, who.dana), signed_in_context(browser, url, path, who.bob)
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        d, b = watched(danas.new_page()), watched(bobs.new_page())
        for other in (d, b):
            other.goto(url)
            other.wait_for_selector("#account-btn.signed-in")
        p = watched(ctx.new_page())
        # Your own sheet: there's nothing for you to change.
        open_sheet(p, url, who.robin)
        assert p.eval_on_selector_all("#user-dialog [data-role-check]", "els => els.every(e => e.disabled)")
        assert "You can't change your own access." in p.text_content("#user-part-access")
        assert p.locator("#user-dialog [data-disable-user]").count() == 0
        p.click("#user-tab-sessions")
        assert "This browser" in p.text_content("#user-part-sessions")
        p.keyboard.press("Escape")
        p.wait_for_selector("#user-dialog", state="hidden")

        # A role given, and taken, at once, from the keyboard: focus stays on the box, and the sheet's status line
        # says what happened (a toast would sit under the sheet, out of reach).
        p.click("#user-rows tr:has-text('Dana Lee') [data-user]")
        p.wait_for_selector("#user-dialog[open] [data-role-check='-2']:enabled")
        assert p.get_attribute("#user-dialog [role=tab][aria-selected=true]", "id") == "user-tab-access"
        p.focus("#user-dialog [data-role-check='-2']")
        p.keyboard.press("Space")
        p.wait_for_selector("#user-said:has-text('Gave Dana Lee the role Operator.')", state="attached")
        assert run(path, "SELECT roleId FROM userRoles WHERE userId = ? ORDER BY roleId", (who.dana,)) == [(-3,), (-2,)]
        p.wait_for_selector("#user-part-access li:has-text('Add hosts')")
        assert squash(p.text_content("#user-part-access li:has-text('Add hosts')")) == "Add hostsvia Operator"
        p.wait_for_selector("#user-dialog [data-role-check='-2']:checked:not([aria-disabled])")
        assert p.evaluate("document.activeElement.dataset.fk") == "role--2"
        p.keyboard.press("Space")
        p.wait_for_selector("#user-said:has-text('Took the role Operator from Dana Lee.')", state="attached")
        p.wait_for_selector("#user-dialog [data-role-check='-2']:not(:checked):not([aria-disabled])")
        assert p.evaluate("document.activeElement.dataset.fk") == "role--2"
        # Taking the role that let her make client tokens: the two she made still open sessions at the hub.
        p.wait_for_selector("#user-dialog [data-role-check='-3']:enabled")
        p.uncheck("#user-dialog [data-role-check='-3']")
        p.wait_for_selector("#user-dialog .user-left")
        left = "Dana Lee can't make client tokens any more, but their 2 active ones still open sessions at the hub."
        assert squash(p.text_content("#user-dialog .user-left")) == left + " Revoke them"
        assert p.text_content("#user-said") == "Took the role Tester from Dana Lee. " + left
        assert "Nothing yet: they have no roles." in p.text_content("#user-part-access")
        p.click("#user-dialog .user-left [data-revoke-tokens]")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "Revoke Dana Lee's 2 client tokens?"
        assert listed(p, "#confirm-list .confirm-name") == ["dana-ci", "dana-nightly"]
        p.click("#confirm-ok")
        p.wait_for_selector("#user-said:has-text(\"Revoked Dana Lee's 2 client tokens.\")", state="attached")
        p.wait_for_selector("#user-dialog .user-left", state="detached")
        assert run(path, "SELECT COUNT(*) FROM accessTokens WHERE name LIKE 'dana-%' AND revokedAt IS NULL") == [(0,)]

        # Ending her session: her browser is signed out at its next request.
        p.click("#user-tab-sessions")
        assert p.locator("#user-part-sessions .user-session").count() == 1
        assert p.get_attribute("#user-part-sessions [data-end-session]", "aria-label").startswith("End the session of ")
        p.click("#user-part-sessions [data-end-session]")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "End this session?"
        p.click("#confirm-ok")
        p.wait_for_selector("#user-part-sessions:has-text(\"None: they aren't signed in anywhere.\")")
        d.reload()
        d.wait_for_selector("#ov-state [data-signin]")
        assert not d.is_visible("#account-btn.signed-in")
        p.keyboard.press("Escape")
        p.wait_for_selector("#user-dialog", state="hidden")

        # Disabling Bob says what it does, and revokes his client token with it.
        p.click("#user-rows tr:has-text('Bob Stone') [data-user]")
        p.wait_for_selector("#user-dialog[open] [data-disable-user]")
        p.click("#user-dialog [data-disable-user]")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "Disable Bob Stone?"
        assert p.text_content("#confirm-text") == ("They can't sign in until an admin enables them. Their session ends now, and their "
                                                   "checks' client tokens stop getting MFA codes and saved sign-ins.")
        assert listed(p, "#confirm-dialog .confirm-facts dt") == ["Sessions", "Client tokens", "Agent tokens", "Owns"]
        assert listed(p, "#confirm-dialog .confirm-facts dd") == ["1", "1 active", "0 active", "0 secrets, 0 sign-ins (they stay theirs)"]
        assert p.eval_on_selector_all("#confirm-dialog [data-check]", "els => els.map(e => [e.dataset.check, e.checked])") == [
            ["client", True]]
        assert listed(p, "#confirm-dialog .confirm-checks label") == [
            "Also revoke their 1 client token (checks using them stop connecting to the hub within about 10 s)"]
        assert p.text_content("#confirm-note") == "Revoking can't be undone."
        p.click("#confirm-ok")
        p.wait_for_selector("#user-said:has-text('Disabled Bob Stone and revoked 1 client token.')", state="attached")
        p.wait_for_selector("#user-dialog [data-enable-user]")
        assert run(path, "SELECT disabledAt IS NOT NULL FROM users WHERE id = ?", (who.bob,)) == [(1,)]
        assert run(path, "SELECT revokedAt IS NOT NULL FROM accessTokens WHERE name = 'bob-ci'") == [(1,)]
        p.keyboard.press("Escape")
        p.wait_for_selector("#user-rows tr:has-text('Bob Stone')", state="detached")  # the list shows the active ones
        b.reload()
        b.wait_for_selector("#ov-state [data-signin]")
        assert p.errors == [] and d.errors == [] and b.errors == []
    finally:
        for c in (ctx, danas, bobs):
            c.close()


def test_what_someone_owns_is_given_to_someone_else(people_grid, browser):  # noqa: F811
    """Move all to…: their MFA secrets and saved sign-ins, never their tokens, and the sign-ins' shares go."""
    url, path, who = people_grid
    store.create_mfa("intranet", SEED, owner_id=who.dana)
    add_item(path, "signin", "prod-admin", owner=who.dana, shares=[("user", who.vic)])
    add_client_token(path, "dana-ci", owner=who.dana)
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        open_sheet(p, url, who.dana)
        p.click("#user-tab-owns")
        assert listed(p, "#user-part-owns .user-owns li") == ["intranet", "prod-admin", "dana-ci · client · active"]
        p.click("#user-part-owns [data-move-items]")
        p.wait_for_selector("#user-move-input")
        assert p.evaluate("document.activeElement.id") == "user-move-input"
        p.fill("#user-move-input", "bob")
        p.wait_for_selector("#user-move-list [role=option]:has-text('Bob Stone')")
        p.keyboard.press("ArrowDown")
        p.keyboard.press("Enter")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "Give what Dana Lee owns to Bob Stone?"
        assert listed(p, "#confirm-dialog .confirm-facts dd") == ["1", "1"]
        assert p.text_content("#confirm-note") == ("Saved sign-ins lose their shares, what's saved for them and any sign-in under way; "
                                                   "MFA secrets keep their shares.")
        p.click("#confirm-ok")
        p.wait_for_selector("#user-said:has-text('Gave 1 MFA secret and 1 saved sign-in to Bob Stone.')", state="attached")
        p.wait_for_selector("#user-part-owns [data-move-items]", state="detached")  # nothing left to give
        assert sorted(run(path, "SELECT itemKind, userId FROM itemOwners")) == [("mfa", who.bob), ("signin", who.bob), ("token", who.dana)]
        assert run(path, "SELECT COUNT(*) FROM itemShares") == [(0,)]
        assert p.errors == []
    finally:
        ctx.close()


def test_someone_who_manages_people_gives_only_what_they_hold(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    lee = make_user(path, "Lee Park", [add_role(path, "People leads", LEADS)], email="lee@example.com")
    ctx = signed_in_context(browser, url, path, lee)
    try:
        p = watched(ctx.new_page())
        open_sheet(p, url, who.dana)
        admin = p.locator("#user-dialog .perm-item:has([data-role-check='-1'])")
        assert admin.locator("input").is_disabled() and admin.get_attribute("title") == "It has permissions you don't have."
        assert admin.locator("small.hint").text_content() == "It has permissions you don't have."
        assert p.is_disabled("#user-dialog [data-role-check='{}']".format(who.qa))  # it reads the audit log
        assert not p.is_disabled("#user-dialog [data-role-check='-2']")  # Operator brings nothing Lee lacks
        assert p.locator("#user-tab-history").count() == 0  # nor may Lee read the audit log
        close_sheet(p)
        # An admin's access is beyond Lee's: their sheet says so, and changes nothing.
        open_sheet(p, url, who.robin)
        assert p.eval_on_selector_all("#user-dialog [data-role-check]", "els => els.every(e => e.disabled)")
        assert "You can't change Robin Hale's access: they have permissions you don't have." in p.text_content("#user-part-access")
        assert p.locator("#user-dialog [data-disable-user]").count() == 0
        close_sheet(p)
        assert p.is_hidden("#admin-tab-audit")

        # Roles: what Lee can't give can't be ticked, edited or deleted either.
        p.goto(url + "#/admin?tab=roles")
        p.wait_for_selector("#role-rows tr")
        qa = p.locator("#role-rows tr", has_text="QA leads")
        for action in ("edit-role", "delete-role"):
            assert qa.locator("[data-{}]".format(action)).is_disabled()
            assert qa.locator("[data-{}]".format(action)).get_attribute("title") == "It has permissions you don't have."
        p.click("#new-role-btn")
        p.wait_for_selector("#role-dialog[open]")
        every = p.locator("#role-dialog .perm-item:has([data-perm='tokens.all'])")
        assert every.locator("input").is_disabled() and every.get_attribute("title") == "You can't give a permission you don't have."
        # A disabled box can't show its title to a keyboard or a screen reader: the reason is said in words too.
        assert p.text_content("#role-perms-note") == "Permissions you don't have are greyed out: you can't give them."
        assert every.locator("input").get_attribute("aria-describedby") == "role-perms-note"
        p.keyboard.press("Escape")
        p.wait_for_selector("#role-dialog", state="hidden")
        # A copy of Admin keeps only what Lee holds.
        p.click("#role-rows [data-copy-role='-1']")
        p.wait_for_selector("#role-dialog[open]")
        assert p.input_value("#role-name") == "Copy of Admin"
        ticked = p.eval_on_selector_all("#role-dialog [data-perm]:checked", "els => els.map(e => e.dataset.perm)")
        assert sorted(ticked) == sorted(permissions.expand(LEADS.split()))
        p.keyboard.press("Escape")
        assert p.errors == []
    finally:
        ctx.close()


def test_the_role_editor(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=roles")
        p.wait_for_selector("#role-rows tr")
        assert listed(p, "#role-rows .role-name") == ["Admin", "Operator", "Tester", "Viewer", "QA leads"]
        tester = p.locator("#role-rows tr[data-role='-3']")
        assert squash(tester.locator(".cell-perms").inner_text()) == "6 · Grid, Tokens, MFA, Sign-ins"
        assert squash(tester.locator(".cell-members").inner_text()) == "2 by hand"
        # A line each: the descriptions take what's left.
        assert lines(tester.locator(".cell-perms")) == 1 and lines(tester.locator(".cell-members")) == 1
        # A built-in role shows what it holds, and changes nothing.
        p.click("#role-rows [data-view-role='-3']")
        p.wait_for_selector("#role-dialog[open]")
        # Focus is on Close, at the foot of the long form, but it opens at its top.
        assert p.evaluate("document.activeElement.matches('#role-dialog [data-dismiss]')")
        assert p.evaluate("document.getElementById('role-dialog').scrollTop") == 0
        assert p.text_content("#role-title") == "The role “Tester”"
        assert p.locator("#role-perms-note").count() == 0  # nothing is held back from an admin
        assert p.text_content("#role-dialog .sheet-sub") == "Built-in roles can't be changed. Duplicate one to make your own."
        assert p.eval_on_selector_all("#role-dialog input", "els => els.every(e => e.disabled)")
        held = sorted(p.eval_on_selector_all("#role-dialog [data-perm]:checked", "els => els.map(e => e.dataset.perm)"))
        assert held == sorted(permissions.expand(permissions.BUILTIN_ROLES[permissions.TESTER][2]))
        # Duplicate: a custom role, with the same ticked.
        p.click("#role-dialog [data-copy-role]")
        p.wait_for_function("() => document.getElementById('role-title').textContent === 'New role'")
        assert p.input_value("#role-name") == "Copy of Tester"
        assert sorted(p.eval_on_selector_all("#role-dialog [data-perm]:checked", "els => els.map(e => e.dataset.perm)")) == held
        # What a permission includes comes ticked, and can't be unticked by itself.
        use = p.locator("#role-dialog .perm-item:has([data-perm='mfa.use'])")
        assert use.locator("input").is_disabled() and use.locator(".perm-by").text_content() == "Included by “Add MFA secrets”"
        p.check("#role-dialog [data-perm='mfa.all']")
        add = p.locator("#role-dialog .perm-item:has([data-perm='mfa.add'])")
        assert add.locator("input").is_checked() and add.locator("input").is_disabled()
        assert add.locator(".perm-by").text_content() == "Included by “Every MFA secret”"
        assert p.evaluate("document.activeElement.dataset.perm") == "mfa.all"
        p.fill("#role-name", "Night shift")
        p.click("#role-submit")
        p.wait_for_selector("#role-dialog", state="hidden")
        p.wait_for_selector("#role-rows tr:has-text('Night shift')")
        assert run(path, "SELECT permissions FROM roles WHERE name = 'Night shift'") == [("grid.view mfa.all signins.add tokens.own",)]

        # Editing says whom it reaches; deleting, what goes with it.
        p.click("#role-rows tr:has-text('Night shift') [data-edit-role]")
        p.wait_for_selector("#role-dialog[open]")
        assert p.text_content("#role-title") == "Edit the role “Night shift”"
        assert "Nobody has this role yet." in p.text_content("#role-dialog")
        p.fill("#role-description", "Runs the grid at night.")
        p.click("#role-submit")
        p.wait_for_selector("#role-rows tr:has-text('Runs the grid at night.')")
        p.click("#role-rows tr:has-text('Night shift') [data-delete-role]")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "Delete the role “Night shift”?"
        assert listed(p, "#confirm-dialog .confirm-facts dd") == ["0", "0", "0 items"]
        p.click("#confirm-ok")
        p.wait_for_selector("#role-rows tr:has-text('Night shift')", state="detached")
        assert run(path, "SELECT COUNT(*) FROM roles WHERE name = 'Night shift'") == [(0,)]

        # A role needs a name: said under the fields, where focus goes, and nothing is asked of the server.
        p.click("#new-role-btn")
        p.wait_for_selector("#role-dialog[open]")
        p.click("#role-submit")
        assert p.text_content("#role-field-error") == "Give the role a name." and p.is_hidden("#role-error")
        assert p.get_attribute("#role-name", "aria-invalid") == "true"
        assert p.get_attribute("#role-name", "aria-describedby") == "role-field-error"
        assert p.evaluate("document.activeElement.id") == "role-name"
        p.keyboard.press("Escape")
        p.wait_for_selector("#role-dialog", state="hidden")
        assert p.errors == []
    finally:
        ctx.close()


def test_changes_the_server_refuses_say_why_and_can_be_tried_again(people_grid, browser):  # noqa: F811
    """Refusals provoked on purpose. A role that MFA secrets Lee can't see are shared with can't be given: its box
    goes back as it was and keeps focus, however often it's tried. A role whose permission was taken from Lee while
    the editor was open is refused, and says why above the buttons, where Lee pressed."""
    url, path, who = people_grid
    add_item(path, "mfa", "intranet", owner=who.bob, shares=[("role", who.qa)])
    auditors = add_role(path, "Auditors", "audit.view")
    lee = make_user(path, "Lee Park", [add_role(path, "People leads", LEADS), auditors], email="lee@example.com")
    ctx = signed_in_context(browser, url, path, lee)
    try:
        p = watched(ctx.new_page())
        open_sheet(p, url, who.vic)
        box = "#user-dialog [data-role-check='{}']".format(who.qa)
        refused = "You can't give the role “QA leads”: MFA secrets you can't see are shared with it."
        p.focus(box)
        for _ in range(2):  # the second refusal draws nothing new, and the box must still come back
            with p.expect_response(lambda r: r.url.endswith("/roles/add")):
                p.keyboard.press("Space")
            p.wait_for_selector(box + ":enabled:not(:checked):not([aria-disabled])")
            p.wait_for_function("text => document.getElementById('user-error').textContent === text", arg=refused)
            assert p.evaluate("document.activeElement.dataset.fk") == "role-{}".format(who.qa)
        assert run(path, "SELECT roleId FROM userRoles WHERE userId = ?", (who.vic,)) == [(permissions.VIEWER,)]
        close_sheet(p)

        p.goto(url + "#/admin?tab=roles")
        p.click("#new-role-btn")
        p.wait_for_selector("#role-dialog[open]")
        p.fill("#role-name", "Night readers")
        p.check("#role-dialog [data-perm='audit.view']")
        run(path, "DELETE FROM userRoles WHERE userId = ? AND roleId = ?", (lee, auditors))
        p.click("#role-submit")
        p.wait_for_selector("#role-error:not([hidden])")
        assert p.text_content("#role-error") == ("You can't give or change the role “Night readers”: it has permissions you don't "
                                                 "have (Read the audit log).")
        assert p.is_hidden("#role-field-error") and p.get_attribute("#role-name", "aria-invalid") is None
        assert p.evaluate("""() => { const e = document.getElementById('role-error').getBoundingClientRect(),
            d = document.getElementById('role-dialog').getBoundingClientRect(); return e.top >= d.top && e.bottom <= d.bottom; }""")
        p.keyboard.press("Escape")
        assert run(path, "SELECT COUNT(*) FROM roles WHERE name = 'Night readers'") == [(0,)]
        assert p.errors and all("403" in e for e in p.errors), p.errors
    finally:
        ctx.close()


def test_groups_are_mapped_to_roles(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    for name, groups in (("Gus", ["grid-qa", "staff"]), ("Hal", ["grid-qa", "staff"]), ("Ivy", ["grid-qa"])):
        make_user(path, name, groups=groups)
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=groups")
        p.wait_for_selector("#group-empty [data-map-group]")
        assert "reads the ‘groups’ claim of their ID token" in p.text_content("#group-intro")
        assert p.evaluate("getComputedStyle(document.querySelector('#group-empty .empty')).borderTopStyle") == "solid"  # a panel
        p.click("#group-empty [data-map-group]")
        p.wait_for_selector("#group-dialog[open]")
        assert p.evaluate("document.activeElement.id") == "group-name"
        # The groups people signed in with, the most common first.
        assert p.eval_on_selector_all("#group-seen option", "els => els.map(e => [e.value, e.textContent])") == [
            ["grid-qa", "seen for 3 people"], ["staff", "seen for 2 people"]]
        p.fill("#group-name", "grid-qa")
        p.select_option("#group-role", str(who.qa))
        p.fill("#group-note", "The QA team")
        p.click("#group-submit")
        p.wait_for_selector("#group-dialog", state="hidden")
        row = p.locator("#group-rows tr", has_text="grid-qa")
        row.wait_for()
        assert [squash(t) for t in row.locator("td").all_inner_texts()][:4] == ["grid-qa", "The QA team", "QA leads", "3"]
        assert run(path, "SELECT groupName, roleId, note FROM groupRoles") == [("grid-qa", who.qa, "The QA team")]
        p.wait_for_function("() => document.activeElement && document.activeElement.dataset.mapping !== undefined")

        # Everyone who signs in: no group to type, and no role that manages people.
        p.click("#map-group-btn")
        p.wait_for_selector("#group-dialog[open]")
        p.check("#group-everyone")
        assert p.is_disabled("#group-name")
        assert listed(p, "#group-role option:disabled") == ["Admin (manages people)"]
        p.select_option("#group-role", str(permissions.VIEWER))
        p.click("#group-submit")
        p.wait_for_selector("#group-rows tr:has-text('Everyone who signs in')")
        assert [squash(t) for t in p.locator("#group-rows tr").first.locator("td").all_inner_texts()][:4] == [
            "Everyone who signs in", "–", "Viewer", "7"]
        assert run(path, "SELECT groupName, roleId FROM groupRoles ORDER BY id")[1] == ("*", permissions.VIEWER)

        p.click("#group-rows tr:has-text('grid-qa') [data-unmap]")
        p.wait_for_selector("#confirm-dialog[open]")
        assert p.text_content("#confirm-title") == "Stop mapping ‘grid-qa’ to the role QA leads?"
        assert p.text_content("#confirm-text") == "The 3 people it matches lose the role at their next request, unless they have it another way."
        p.click("#confirm-ok")
        p.wait_for_selector("#group-rows tr:has-text('grid-qa')", state="detached")
        assert run(path, "SELECT groupName FROM groupRoles") == [("*",)]
        assert p.errors == []
    finally:
        ctx.close()


def test_the_audit_log_reads_as_sentences_and_filters_through_the_address(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    robin, vic = (who.robin, "Robin Hale", "oidc"), (who.vic, "Vic Viewer", "oidc")
    # Vic's first sign-in, with the groups it brought, and a later one with others.
    audit_row(path, "user.created", vic, ("user", who.vic, "Vic Viewer"), {"groups": ["staff", "grid-qa"]}, 8000)
    audit_row(path, "user.groups_changed", vic, ("user", who.vic, "Vic Viewer"), {"added": ["qa"], "removed": ["staff"]}, 7900)
    for i in range(52):
        audit_row(path, "auth.signin", vic, ("user", who.vic, "Vic Viewer"), seconds_ago=7200 + i)
    audit_row(path, "user.role_added", robin, ("user", who.dana, "Dana Lee"), {"role": {"id": -2, "name": "Operator"}}, 300)
    audit_row(path, "mfa.shared", robin, ("mfa", 4, "github-2fa"), {"grantee": {"kind": "role", "id": who.qa, "name": "QA leads"}},
              240)
    audit_row(path, "user.disabled", robin, ("user", 99, "Pat Quinn"), {"sessions": 2, "clientTokens": 3, "agentTokens": 0}, 180)
    audit_row(path, "role.updated", robin, ("role", who.qa, "QA leads"),
              {"before": {"name": "QA", "permissions": ["mfa.use"]}, "after": {"name": "QA leads", "permissions": ["audit.view", "mfa.use"]}},
              120)
    audit_row(path, "token.created", (who.bob, "client token 'ci' (Bob Stone)", "client-token"), ("token", 7, "ci"),
              {"kind": "client", "owner": who.bob}, 90)
    audit_row(path, "auth.breakglass", seconds_ago=60)
    ctx = signed_in_context(browser, url, path, who.robin)
    what = "#audit-rows .audit-line > span"
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=audit")
        p.wait_for_selector("#audit-rows tr")
        assert listed(p, what)[:6] == [
            "signed in with the admin token", "made the client token “ci”",
            "renamed the role QA to QA leads and changed what it lets people do", "disabled Pat Quinn and revoked 3 client tokens",
            "shared “github-2fa” with QA leads", "gave Dana Lee the role Operator"]
        assert listed(p, "#audit-rows .cell-who")[:2] == ["Adminadmin token", "client token 'ci' (Bob Stone)check"]
        assert len(listed(p, what)) == 50
        assert p.text_content("#audit-kept") == "Kept for 365 days (GRID_AUDIT_DAYS)."
        # The expander: a role's permissions before and after.
        row = p.locator("#audit-rows tr", has_text="renamed the role")
        assert row.locator("[data-expand]").get_attribute("aria-label") == (
            "Details: Robin Hale renamed the role QA to QA leads and changed what it lets people do")
        row.locator("[data-expand]").click()
        p.wait_for_selector("#audit-rows tr:has-text('renamed the role') .audit-details:not([hidden])")
        assert row.locator("[data-expand]").get_attribute("aria-expanded") == "true"
        assert row.locator(".cell-when").text_content() != ""  # drawn again, with its age at once
        assert squash(row.locator(".audit-diff").inner_text()) == (
            "Before QA: Use MFA secrets After QA leads: Read the audit log, Use MFA secrets")
        assert p.evaluate("document.activeElement.getAttribute('aria-expanded')") == "true"
        # Open, its when, who and address stay on the sentence's line, not halfway down the details.
        middles = row.evaluate("""tr => ['[data-ago]', '.audit-who > span', '.audit-line > span', '.cell-address code'].map((s) => {
            const b = tr.querySelector(s).getBoundingClientRect(); return b.top + b.height / 2; })""")
        assert max(middles) - min(middles) < 3, middles
        # Older entries, a page at a time.
        p.click("#audit-older")
        rows_are(p, "#audit-rows tr", 60)
        assert p.is_hidden("#audit-older")
        assert listed(p, what)[-2:] == ["signed in with other groups: now in ‘qa’; no longer in ‘staff’",
                                        "signed in for the first time, in ‘staff’, ‘grid-qa’"]

        # Who, and what kind of change: in the address, through a reload.
        p.select_option("#audit-actor", str(who.robin))
        rows_are(p, "#audit-rows tr", 4)
        assert p.evaluate("location.hash") == "#/admin?tab=audit&actor={}".format(who.robin)
        p.click("#audit-category [data-category=role]")
        rows_are(p, "#audit-rows tr", 1)
        p.reload()
        p.wait_for_selector("#audit-rows tr:has-text('renamed the role')")
        assert p.input_value("#audit-actor") == str(who.robin)
        assert p.get_attribute("#audit-category [data-category=role]", "aria-pressed") == "true"
        p.click("#audit-category [data-category=role]")  # again: any kind
        rows_are(p, "#audit-rows tr", 4)
        p.select_option("#audit-actor", "checks")
        rows_are(p, "#audit-rows tr", 1)
        p.select_option("#audit-actor", "")
        p.wait_for_function("() => location.hash === '#/admin?tab=audit'")

        # What it was about, from a link: one person, until cleared.
        p.goto(url + "#/admin?tab=audit&target=user:{}".format(who.dana))
        rows_are(p, "#audit-rows tr", 1)
        assert squash(p.text_content("#audit-target")) == "About Dana Lee"
        p.click("#audit-target [data-clear-target]")
        rows_are(p, "#audit-rows tr", 50)
        assert p.evaluate("location.hash") == "#/admin?tab=audit"
        # Nothing matches: it says so.
        p.click("#audit-category [data-category=grid]")
        p.wait_for_selector("#audit-empty [data-clear-audit]")
        assert "Nothing recorded matches" in p.text_content("#audit-empty")
        p.click("#audit-empty [data-clear-audit]")
        p.wait_for_selector("#audit-rows tr")
        assert p.errors == []
    finally:
        ctx.close()


def test_the_audit_log_says_what_a_scoped_token_may_do(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    made = (who.robin, "Robin Hale", "oidc")
    audit_row(path, "token.created", (None, "Admin", "token"), ("token", 10, "Grid monitor"),
              {"kind": "client", "owner": None, "scope": ["grid.view"]}, 600)
    audit_row(path, "token.created", made, ("token", 11, "Nightly"), {"kind": "client", "owner": who.robin,
                                                                      "scope": ["mfa.use", "sessions.open", "signins.use"]}, 500)
    audit_row(path, "token.created", made, ("token", 12, "Sign-in maker"), {"kind": "client", "scope": ["signins.add", "signins.use"]}, 400)
    audit_row(path, "token.created", made, ("token", 13, "No limit"), {"kind": "client", "owner": who.robin, "scope": None}, 300)
    audit_row(path, "token.created", made, ("token", 14, "Newer ids"), {"kind": "client", "scope": ["tomorrow.only", HOSTILE]}, 200)
    audit_row(path, "token.created", made, ("token", 15, "Nothing"), {"kind": "client", "scope": []}, 100)
    audit_row(path, "token.created", made, ("token", 16, "build-01 agent"), {"kind": "agent", "owner": None}, 50)
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=audit")
        p.wait_for_selector("#audit-rows tr")
        assert listed(p, "#audit-rows .audit-line > span") == [
            "made the agent token “build-01 agent”",
            "made the client token “Nothing”, which may do nothing",
            "made the client token “Newer ids”, which may do nothing",
            "made the client token “No limit”",
            "made the client token “Sign-in maker”, which may only reuse saved sign-ins and add saved sign-ins",
            "made the client token “Nightly”, which may only open browser sessions, get MFA codes and reuse saved sign-ins",
            "made the client token “Grid monitor”, which may only see the grid"]
        assert p.evaluate("window.__xss") is None and p.evaluate("document.querySelectorAll('#audit-rows img').length") == 0
        # The details are what was recorded.
        row = p.locator("#audit-rows tr", has_text="“Sign-in maker”")
        row.locator("[data-expand]").click()
        assert json.loads(row.locator(".audit-raw").text_content()) == {"kind": "client", "scope": ["signins.add", "signins.use"]}
        assert p.errors == []
    finally:
        ctx.close()


def test_the_demo_audit_log_says_what_grid_monitor_may_do(admin_grid, browser):  # noqa: F811
    url, _ = admin_grid
    ctx = _admin_context(browser)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=audit")
        p.wait_for_selector("#audit-rows tr")
        said = listed(p, "#audit-rows .audit-line > span")
        assert "made the client token “Grid monitor”, which may only see the grid" in said
        assert "made the client token “Example checks”" in said  # no scope: as before
        assert p.errors == []
    finally:
        ctx.close()


def test_the_single_sign_on_tab_shows_the_settings_and_what_is_wrong(people_grid, browser, monkeypatch):  # noqa: F811
    url, path, who = people_grid
    make_user(path, "Ann Admin", issuer=os.environ["GRID_OIDC_ISSUER"], subject="00u-ann")  # at the provider the grid uses
    monkeypatch.setenv("GRID_OIDC_ADMINS", "sub:00u-ann, nobody@example.com")
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=sso")
        p.wait_for_selector(".sso-facts")
        facts = dict(zip(listed(p, ".sso-facts dt"), listed(p, ".sso-facts dd")))
        assert facts["Client id"] == "grid-ui" and facts["Issuer"].startswith("http://localhost:")
        assert facts["Redirect URI"] == url + "auth/callback Register this exact address with your identity provider"
        assert facts["Session"] == "12 h, or 120 min without use"
        assert facts["Overview, activity and sessions"] == "Need sign-in"
        assert listed(p, ".sso-admins li") == ["sub:00u-ann · Ann Admin", "nobody@example.com · hasn't signed in yet"]
        assert (facts["Admin token"], facts["Provider sign-out"]) == ("Set", "Off")
        assert p.get_attribute(".sso-facts [data-copy]", "data-copy") == url + "auth/callback"
        assert p.locator(".sso-problem").count() == 0 and p.is_hidden("#admin-banner")
        assert facts["Client authentication"] == ("A client secret, sent with HTTP Basic when the provider lists "
                                                  "client_secret_basic or lists nothing, else in the request body "
                                                  "GRID_OIDC_CLIENT_AUTH=auto")
        assert p.locator(".sso-unused").count() == 0 and "Token file" not in facts
        assert "s3cret-client" not in p.content()

        # What's wrong: in full, for someone who may see people.
        monkeypatch.setenv("GRID_OIDC_CLIENT_SECRET", "")
        p.reload()
        p.wait_for_selector(".sso-problem")
        problem = ("Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the "
                   "Grid UI can prove to the identity provider that it is the client.")
        assert p.text_content(".sso-problem") == problem
        assert p.text_content("#admin-banner") == problem
        facts = dict(zip(listed(p, ".sso-facts dt"), listed(p, ".sso-facts dd")))
        assert facts["Client authentication"] == "Not set GRID_OIDC_CLIENT_AUTH=auto"
        assert p.errors == []
    finally:
        ctx.close()


def test_the_single_sign_on_tab_shows_the_platforms_token_as_its_file_states_it(people_grid, sso_grid, browser, monkeypatch,  # noqa: F811
                                                                                tmp_path):
    """With a token the platform issues: the method, the file and the claims the provider's federated credential must
    name, as text and never the token; a token on offer that isn't used; and what the provider's discovery rules out."""
    from ui import oidc
    from ui.tests import fake_idp

    url, path, who = people_grid
    idp = sso_grid[2]
    cluster, file = fake_idp.FakeCluster(), tmp_path / "platform-token"
    token = cluster.token(subject=HOSTILE)
    file.write_bytes(token.encode())
    hint = ("The identity provider's federated credential must name this issuer, subject and audience exactly. The token itself "
            "is never shown.")

    def facts_after(selector):
        p.reload()
        p.wait_for_selector(selector)
        return dict(zip(listed(p, ".sso-facts dt"), listed(p, ".sso-facts dd")))

    def no_token_shown():
        return all(part not in p.content() for part in [token] + token.split("."))

    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=sso")
        p.wait_for_selector(".sso-facts")

        # A client secret, and a token the platform offers: said, and left alone.
        monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(file))
        facts = facts_after(".sso-unused")
        assert squash(p.text_content(".sso-unused")) == ("The platform offers a token (AZURE_FEDERATED_TOKEN_FILE). It is used only "
                                                         "with GRID_OIDC_CLIENT_AUTH=federated and no client secret.")
        assert facts["Client authentication"].startswith("A client secret, sent with HTTP Basic when the provider lists ")
        assert "Token file" not in facts and p.locator(".sso-problem").count() == 0 and no_token_shown()

        # The secret gone and the method left at auto: the platform's token isn't used for that. What's wrong, and the
        # line that names the setting that would use it.
        monkeypatch.delenv("GRID_OIDC_CLIENT_SECRET")
        facts = facts_after(".sso-problem")
        problem = ("Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the "
                   "Grid UI can prove to the identity provider that it is the client.")
        assert p.text_content(".sso-problem") == problem and p.text_content("#admin-banner") == problem
        assert facts["Client authentication"].startswith("Not set GRID_OIDC_CLIENT_AUTH=auto The platform offers a token")
        assert "Token file" not in facts and no_token_shown()

        # The platform's token in use: what its file states, each as text.
        idp.auth_methods = ["client_secret_basic", "client_secret_post", "private_key_jwt"]
        monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
        facts = facts_after(".sso-facts [data-until]")
        assert facts["Client authentication"] == "A token the platform issues (federated): no client secret GRID_OIDC_CLIENT_AUTH=federated"
        assert facts["Token file"] == str(file) + " From AZURE_FEDERATED_TOKEN_FILE, which Azure workload identity sets in a Kubernetes pod"
        assert not {"Managed identity", "Identity endpoint", "Credential issuer"} & set(facts)
        assert p.locator(".sso-warning").count() == 0
        assert facts["Token issuer"] == cluster.issuer
        assert facts["Token subject"] == HOSTILE
        assert facts["Token audience"] == "api://AzureADTokenExchange"
        assert re.fullmatch(r"in \d+[mh] " + re.escape(hint), facts["Token expires"])
        assert p.get_attribute(".sso-facts [data-until]", "title")
        assert p.locator(".sso-facts .warn-text").count() == 0
        assert p.locator(".sso-unused").count() == 0 and p.locator(".sso-problem").count() == 0 and p.is_hidden("#admin-banner")
        assert no_token_shown()
        assert p.evaluate("window.__xss") is None and p.evaluate("document.querySelectorAll('.sso-facts img').length") == 0

        # A provider that lists no client assertions: its sentence, here only (the session's problem doesn't ask the provider).
        idp.auth_methods = ["client_secret_basic", "client_secret_post"]
        oidc.reset_caches()
        facts = facts_after(".sso-problem")
        assert p.text_content(".sso-problem") == ("The identity provider doesn't take client assertions: private_key_jwt isn't among "
                                                  "the token_endpoint_auth_methods_supported its discovery document lists.")
        assert p.is_hidden("#admin-banner")
        assert facts["Token subject"] == HOSTILE and no_token_shown()

        # A token that has expired: when, marked so, and the problem that says the platform should have replaced it.
        idp.auth_methods = ["client_secret_basic", "client_secret_post", "private_key_jwt"]
        oidc.reset_caches()
        token = cluster.token(lifetime_s=-7200)
        file.write_bytes(token.encode())
        facts = facts_after(".sso-facts .warn-text")
        problem = "AZURE_FEDERATED_TOKEN_FILE can't be used: the token in {} has expired; the platform should have replaced it.".format(file)
        assert p.text_content(".sso-problem") == problem and p.text_content("#admin-banner") == problem
        assert re.fullmatch(r"\d+h ago expired " + re.escape(hint), facts["Token expires"])
        assert facts["Token subject"] == "system:serviceaccount:grid:grid-ui" and no_token_shown()

        # A file named by the Grid UI's own setting that holds no token, and one whose token leaves claims out.
        monkeypatch.delenv("AZURE_FEDERATED_TOKEN_FILE")
        monkeypatch.setenv("GRID_OIDC_FEDERATED_TOKEN_FILE", str(file))
        token = "made-up-text-that-is-no-token"
        file.write_bytes(token.encode())
        facts = facts_after(".sso-problem")
        assert p.text_content(".sso-problem") == ("GRID_OIDC_FEDERATED_TOKEN_FILE can't be used: {} doesn't hold a token (three "
                                                  "base64url parts).".format(file))
        assert facts["Token file"] == str(file) + " GRID_OIDC_FEDERATED_TOKEN_FILE"
        assert [facts[k] for k in ("Token issuer", "Token subject", "Token audience")] == ["The file holds no token"] * 3
        assert facts["Token expires"] == "The file holds no token " + hint
        assert token not in p.content()
        token = cluster.token(subject=None, aud=None, exp=None)
        file.write_bytes(token.encode())
        facts = facts_after(".sso-facts")
        assert p.locator(".sso-problem").count() == 0
        assert (facts["Token issuer"], facts["Token subject"]) == (cluster.issuer, "Not in the token")
        assert facts["Token audience"] == "Not in the token"
        assert facts["Token expires"] == "It doesn't say " + hint
        assert no_token_shown()
        assert p.errors == []
    finally:
        ctx.close()

    # The provider elsewhere over plain http, which only GRID_OIDC_ALLOW_HTTP=1 allows: the warning above the
    # settings, and no banner, since nothing keeps a sign-in from working. Robin is a user of the first provider, so
    # the admin token reads the tab.
    far = fake_idp.serve("0.0.0.0", public_host=non_loopback_address())
    ctx = _admin_context(browser)
    try:
        far.auth_methods = list(idp.auth_methods)
        monkeypatch.setenv("GRID_OIDC_ISSUER", far.base_url)
        monkeypatch.setenv("GRID_OIDC_ALLOW_HTTP", "1")
        oidc.reset_caches()
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=sso")
        p.wait_for_selector(".sso-warning")
        assert p.text_content(".sso-warning") == W1
        assert p.get_attribute(".sso-warning", "role") == "status"
        assert p.locator(".sso-problem").count() == 0 and p.is_hidden("#admin-banner")
        assert listed(p, ".sso-facts dt").index("Token file") > 0 and no_token_shown()
        assert p.errors == []
    finally:
        ctx.close()
        far.stop()


def test_the_single_sign_on_tab_shows_a_managed_identity(people_grid, sso_grid, browser, monkeypatch):  # noqa: F811
    """With a managed identity's token: the identity, the endpoint it comes from and what the federated credential
    must name, then what the token states, as text and never the token or the endpoint's header; what keeps the
    endpoint from giving one; and the warning that comes with an Azure VM's endpoint."""
    from ui import oidc
    from ui.tests import fake_idp

    url, path, who = people_grid
    idp = sso_grid[2]
    endpoint = fake_idp.serve_identity()
    monkeypatch.setitem(oidc.ENTRA_AUDIENCES, "localhost", AUDIENCE)  # the provider stands in for Entra ID
    idp.auth_methods = ["client_secret_basic", "client_secret_post", "private_key_jwt"]
    idp.trust(endpoint.tenant, subject=fake_idp.OBJECT_ID_MI)
    monkeypatch.delenv("GRID_OIDC_CLIENT_SECRET")
    monkeypatch.setenv("GRID_OIDC_CLIENT_AUTH", "federated")
    monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", fake_idp.CLIENT_ID_MI)
    monkeypatch.setenv("IDENTITY_ENDPOINT", endpoint.app_service_url)
    monkeypatch.setenv("IDENTITY_HEADER", endpoint.header)
    hint = "As the token states them. The token itself is never shown."
    credential_hint = ("Add a federated credential with exactly these three values (in the portal: scenario Managed Identity, "
                       "which fills them in). The token's own lines below are for comparing: its subject should be the "
                       "identity's principal id; its issuer and audience may be written differently.")

    def facts_after(selector):
        p.reload()
        p.wait_for_selector(selector)
        return dict(zip(listed(p, ".sso-facts dt"), listed(p, ".sso-facts dd")))

    def nothing_secret_shown():
        content = p.content()
        return endpoint.header not in content and all(
            part not in content for token in endpoint.issued for part in [token] + token.split("."))

    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=sso")
        p.wait_for_selector(".sso-facts")

        # App Service's endpoint, a user-assigned identity: where the token comes from, what the credential must
        # name, and the token's own lines.
        facts = facts_after(".sso-facts [data-until]")
        assert facts["Client authentication"] == "A token the platform issues (federated): no client secret GRID_OIDC_CLIENT_AUTH=federated"
        assert facts["Managed identity"] == "User-assigned, client id {} GRID_OIDC_MANAGED_IDENTITY".format(fake_idp.CLIENT_ID_MI)
        assert facts["Identity endpoint"] == (endpoint.app_service_url + " IDENTITY_ENDPOINT, which App Service, Functions and "
                                              "Container Apps set")
        assert facts["Credential issuer"] == idp.base_url
        assert facts["Credential subject"] == fake_idp.OBJECT_ID_MI
        assert facts["Credential audience"] == AUDIENCE + " " + credential_hint
        assert facts["Token issuer"] == fake_idp.TENANT_ISSUER
        assert facts["Token subject"] == fake_idp.OBJECT_ID_MI
        assert facts["Token audience"] == AUDIENCE
        assert re.fullmatch(r"in \d+[mh] " + re.escape(hint), facts["Token expires"])
        assert "Token file" not in facts and p.locator(".sso-warning").count() == 0
        assert p.locator(".sso-problem").count() == 0 and p.is_hidden("#admin-banner")
        assert len(endpoint.issued) == 1 and nothing_secret_shown()

        # The endpoint refuses, asked again at once: its sentence, and no token where the lines were.
        endpoint.fault = "status_403"
        monkeypatch.setattr(oidc, "IDENTITY_MEMO_S", 0)
        facts = facts_after(".sso-problem")
        assert p.text_content(".sso-problem") == (
            "GRID_OIDC_MANAGED_IDENTITY can't be used: the managed identity endpoint in IDENTITY_ENDPOINT ({}) refused to "
            "give a token: HTTP 403. Check that this VM or app has the user-assigned managed identity with client "
            "id {} assigned to it.".format(endpoint.app_service_url, fake_idp.CLIENT_ID_MI))
        assert facts["Credential subject"] == "No token was given"
        assert facts["Credential issuer"] == idp.base_url
        assert [facts[k] for k in ("Token issuer", "Token subject", "Token audience")] == ["No token was given"] * 3
        assert facts["Token expires"] == "No token was given " + hint
        assert nothing_secret_shown()

        # An Azure VM's endpoint and its system-assigned identity: the hint says who else on the VM can get the token.
        endpoint.fault = None
        endpoint.system = "eeeeeeee-1111-2222-3333-444444444444"
        monkeypatch.delenv("IDENTITY_ENDPOINT")
        monkeypatch.delenv("IDENTITY_HEADER")
        monkeypatch.setattr(oidc, "IMDS_URL", endpoint.imds_url)
        monkeypatch.setenv("GRID_OIDC_MANAGED_IDENTITY", "system")
        facts = facts_after(".sso-facts [data-until]")
        assert facts["Managed identity"] == "The endpoint's default (system-assigned, if turned on) GRID_OIDC_MANAGED_IDENTITY"
        assert facts["Identity endpoint"] == (endpoint.imds_url + " The Azure Instance Metadata Service of this VM. Every program "
                                              "and container on this VM can get this identity's token, the grid's runners "
                                              "included, unless a firewall rule stops them: see the README.")
        assert facts["Credential subject"] == facts["Token subject"] == endpoint.system
        assert p.locator(".sso-problem").count() == 0 and nothing_secret_shown()
        assert p.evaluate("window.__xss") is None
        assert p.errors == []
    finally:
        ctx.close()
        endpoint.stop()


def test_the_admin_page_names_no_one_platform():
    """Sign-in without a client secret works wherever the platform gives the Grid UI a token: the Admin page's words
    never make it AKS's alone."""
    folder = os.path.join(os.path.dirname(__file__), os.pardir, "static", "js", "views", "admin")
    for name in ("signin.js", "users.js"):
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            assert "AKS" not in f.read(), name


def test_with_only_the_admin_token_the_admin_page_shows_the_demo_and_how_to_set_up_sign_in(admin_grid, browser):  # noqa: F811
    url, path = admin_grid
    ctx = _admin_context(browser)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=users&status=any")
        p.wait_for_selector("#user-rows tr")
        assert p.text_content("#admin-banner") == ("Single sign-on isn't set up: people appear here once GRID_OIDC_ISSUER is set and "
                                                   "they sign in.")
        assert sorted(people(p)) == ["Alex Moreno", "Dana Lee", "Lee Chen", "Priya Kumar", "Robin Hale"]
        assert listed(p, "#user-rows tr:has-text('Dana Lee') .cell-roles .pill") == ["Operator· group", "QA leads· group"]
        assert p.get_attribute("#user-rows tr:has-text('Dana Lee') .cell-roles .pill", "title") == "From the group ‘grid-operators’"
        assert "email not verified" in p.text_content("#user-rows tr:has-text('Alex Moreno')")
        p.goto(url + "#/admin?tab=audit")
        p.wait_for_selector("#audit-rows tr")
        assert "mapped the group ‘qa’ to the role QA leads" in listed(p, "#audit-rows .audit-line > span")

        # Nobody yet: what to set up, with this page's own address in it.
        run(path, "DELETE FROM users")
        p.goto(url + "#/admin?tab=users")
        p.wait_for_function("url => (document.querySelector('#user-empty code[data-setup=redirect]') || {}).textContent === url",
                            arg=url + "auth/callback")
        assert p.get_attribute("#user-empty button[data-setup=redirect]", "data-copy") == url + "auth/callback"
        assert "GRID_UI_PUBLIC_URL=" + url in p.text_content("#user-empty code[data-setup=env]")
        assert p.is_hidden("#user-toolbar") and p.is_hidden("#user-panel")
        assert p.errors == []
    finally:
        ctx.close()


def test_signing_out_takes_people_roles_and_tokens_out_of_the_palette(people_grid, browser):  # noqa: F811
    """The tabs keep what they loaded, but whoever comes to the keyboard next mustn't find it."""
    url, path, who = people_grid
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        for hash_, rows in (("#/admin?tab=users", "#user-rows tr"), ("#/admin?tab=roles", "#role-rows tr"), ("#/access", "#token-rows tr")):
            p.goto(url + hash_)
            p.wait_for_selector(rows)

        def groups(q):
            p.keyboard.press("Control+K")
            p.wait_for_selector("#palette[open]")
            p.fill("#pal-input", q)
            found = listed(p, "#pal-list .pal-group")
            p.keyboard.press("Escape")
            p.wait_for_selector("#palette", state="hidden")
            return found

        searches = (("dana", "People"), ("qa leads", "Roles"), ("example checks", "Tokens"))
        assert all(group in groups(q) for q, group in searches)
        p.click("#account-btn")
        p.click("#account-menu [data-menu=signout]")
        p.wait_for_selector("#account-btn:not(.signed-in)")
        assert not any(group in groups(q) for q, group in searches)
        assert p.errors == []
    finally:
        ctx.close()


def test_every_admin_tab_fits_a_phone(people_grid, browser):  # noqa: F811
    url, path, who = people_grid
    long = "Alexandra Montgomery-Whitfield of the Subsidiary Division"
    group = "3f2a9c1e-7b4d-4e5f-9a8b-0c1d2e3f4a5b"
    tess = make_user(path, long, [permissions.TESTER, permissions.VIEWER], groups=[group, "grid-qa"],
                     email="alexandra.montgomery-whitfield@subsidiary-division.example.com")
    add_mapping(path, group, who.qa)
    add_mapping(path, "*", permissions.VIEWER)
    run(path, "UPDATE groupRoles SET note = 'Quality assurance leads across every product line and region' WHERE groupName != '*'")
    add_client_token(path, "a-client-token-with-a-rather-long-name-for-the-nightly-checks", owner=tess)
    agent = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    run(path, "INSERT INTO userSessions (tokenHash, userId, createdAt, lastSeenAt, expiresAt, address, userAgent) VALUES "
              "('a-session', ?, ?, ?, ?, '2001:db8:85a3::8a2e:370:7334', ?)", (tess, ts(600), ts(60), ts(-3600), agent))
    audit_row(path, "user.role_added", (who.robin, "Robin Hale", "oidc"), ("user", tess, long), {"role": {"id": -3, "name": "Tester"}})
    audit_row(path, "role.updated", (who.robin, "Robin Hale", "oidc"), ("role", who.qa, "QA leads"),
              {"before": {"name": "QA", "permissions": ["mfa.use"]}, "after": {"name": "QA leads", "permissions": sorted(permissions.ALL)}})
    ctx = signed_in_context(browser, url, path, who.robin, 375, 800)
    content = {"users": "#user-rows tr", "roles": "#role-rows tr", "groups": "#group-rows tr", "audit": "#audit-rows tr",
               "sso": ".sso-facts"}
    try:
        p = watched(ctx.new_page())
        p.goto(url)
        p.wait_for_selector("#account-btn.signed-in")
        # Phones reach the Admin page from the account menu: the tab bar keeps its five.
        assert p.locator(".tabbar a").count() == 5 and p.is_hidden(".rail")
        p.click("#account-btn")
        p.click("#account-menu [data-menu=admin]")
        p.wait_for_selector("#user-rows tr")
        assert p.evaluate("location.hash") == "#/admin"
        for width in (375, 390, 320, 280):
            p.set_viewport_size({"width": width, "height": 800})
            for tab, selector in content.items():
                p.goto(url + "#/admin?tab=" + tab)
                p.wait_for_selector("#admin-panel-{}:not([hidden]) {}".format(tab, selector))
                if tab == "audit" and not p.is_visible("#audit-rows .audit-details"):
                    p.click("#audit-rows [data-expand] >> nth=0")
                    p.wait_for_selector("#audit-rows .audit-details:not([hidden])")
                assert p.evaluate("document.documentElement.scrollWidth <= innerWidth"), (width, tab)
                if tab == "roles":  # a role's groups wrap between them, never at the hyphen of "Sign-ins"
                    assert set(p.eval_on_selector_all("#role-rows .parts > span", "els => els.map(" + LINES + ")")) == {1}, width
            # The sheets: the person's on each of its tabs, the role editor, and mapping a group.
            open_sheet(p, url, tess)
            assert p.evaluate("getComputedStyle(document.getElementById('user-dialog')).borderTopLeftRadius") == "12px"
            for part in ("access", "sessions", "owns", "history"):
                p.click("#user-tab-" + part)
                assert p.evaluate(DIALOG_FITS, "user-dialog"), (width, part)
            close_sheet(p)
            p.goto(url + "#/admin?tab=roles")
            p.click("#role-rows [data-copy-role='-1']")
            p.wait_for_selector("#role-dialog[open]")
            assert p.evaluate(DIALOG_FITS, "role-dialog"), width
            p.keyboard.press("Escape")
            p.goto(url + "#/admin?tab=groups")
            p.click("#map-group-btn")
            p.wait_for_selector("#group-dialog[open]")
            assert p.evaluate(DIALOG_FITS, "group-dialog"), width
            p.keyboard.press("Escape")
            p.wait_for_selector("#group-dialog", state="hidden")
        assert p.errors == []
    finally:
        ctx.close()


def test_names_on_the_admin_page_are_escaped(people_grid, browser):  # noqa: F811
    """A name claim, an email, groups, role names and notes that are markup stay text everywhere the Admin page
    shows them."""
    url, path, who = people_grid
    email = '"><img src=x onerror="window.__xss=2">@example.com'
    eve = make_user(path, HOSTILE, [permissions.TESTER], groups=[HOSTILE, "qa"], email=email, verified=False, subject="eve")
    role = add_role(path, HOSTILE, "grid.view")
    run(path, "UPDATE roles SET description = ? WHERE id = ?", (HOSTILE, role))
    run(path, "INSERT INTO userRoles (userId, roleId, grantedAt) VALUES (?, ?, ?)", (eve, role, ts(60)))
    add_mapping(path, HOSTILE, role)
    run(path, "UPDATE groupRoles SET note = ? WHERE groupName = ?", (HOSTILE, HOSTILE))
    add_client_token(path, HOSTILE, owner=eve)
    audit_row(path, "user.role_added", (eve, HOSTILE, "oidc"), ("user", eve, HOSTILE), {"role": {"id": role, "name": HOSTILE}})
    audit_row(path, "mfa.shared", (eve, HOSTILE, "oidc"), ("mfa", 3, HOSTILE), {"grantee": {"kind": "user", "id": eve, "name": HOSTILE}})
    audit_row(path, "access.denied", (eve, HOSTILE, "oidc"), details={"route": HOSTILE, "rule": "role"})
    ctx = signed_in_context(browser, url, path, who.robin)
    try:
        p = watched(ctx.new_page())
        p.goto(url + "#/admin?tab=users")
        p.wait_for_selector("#user-rows tr")
        assert HOSTILE in people(p) and email in p.text_content("#user-rows")
        open_sheet(p, url, eve)
        assert p.text_content("#user-title") == HOSTILE
        assert HOSTILE in p.text_content(".user-groups") and HOSTILE in p.text_content("#user-part-access")
        for part in ("owns", "history"):
            p.click("#user-tab-" + part)
            assert HOSTILE in p.text_content("#user-part-" + part)
        close_sheet(p)
        p.goto(url + "#/admin?tab=roles")
        p.wait_for_selector("#role-rows tr")
        assert HOSTILE in listed(p, "#role-rows .role-name")
        p.click("#role-rows [data-edit-role='{}']".format(role))
        p.wait_for_selector("#role-dialog[open]")
        assert p.input_value("#role-name") == HOSTILE and p.input_value("#role-description") == HOSTILE
        p.keyboard.press("Escape")
        p.goto(url + "#/admin?tab=groups")
        p.wait_for_selector("#group-rows tr")
        assert HOSTILE in p.text_content("#group-rows .group-name") and HOSTILE in p.text_content("#group-rows .cell-note")
        p.click("#map-group-btn")
        p.wait_for_selector("#group-dialog[open]")
        assert HOSTILE in p.eval_on_selector_all("#group-seen option", "els => els.map(e => e.value)")
        assert HOSTILE in listed(p, "#group-role option")
        p.keyboard.press("Escape")
        p.goto(url + "#/admin?tab=audit&target=user:{}".format(eve))
        p.wait_for_selector("#audit-rows tr")
        assert squash(p.text_content("#audit-target")) == "About " + HOSTILE
        assert HOSTILE in listed(p, "#audit-actor option")
        p.click("#audit-target [data-clear-target]")
        rows_are(p, "#audit-rows tr", 3)
        for i in range(3):
            p.click("#audit-rows [data-expand] >> nth={}".format(i))
        assert all(HOSTILE in line for line in listed(p, "#audit-rows .audit-line > span"))
        p.wait_for_timeout(300)
        assert p.evaluate("window.__xss") is None
        assert p.evaluate("document.querySelectorAll('img').length") == 0
        assert p.errors == []
    finally:
        ctx.close()
