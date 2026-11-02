"""Saved sign-ins for checks: sign in once, keep the browser's cookies in the Grid UI and reuse them until
the site asks to sign in again, so most runs skip the password and the MFA prompt.

Signing in on every run takes a code from the Grid UI's MFA secrets each time and, against Microsoft
Entra ID, looks like what it is: one account signing in over and over from server addresses, which its
risk checks may answer by challenging, flagging or blocking the account. reuse() checks out the state
saved for the account (Playwright's storage_state: cookies and local storage), opens a context with it and
asks the check whether the app is signed in. Only when nothing is saved, or the site asks again, does it
sign in and save the new state. The Grid UI keeps it in its database, encrypted under GRID_SECRETS_KEY
like MFA seeds, and lets one check sign in at a time; the others wait for that check's state:

    import os
    import re
    from playwright.sync_api import expect
    from src import mfa, signin

    APP = "https://contoso.sharepoint.com/sites/ops"

    def sign_in(page):      # the whole sign-in, on the page reuse() opens for it
        page.goto(APP)
        page.get_by_role("textbox", name=re.compile("email", re.I)).fill("monitor@contoso.com")
        page.get_by_role("button", name="Next").click()
        page.get_by_placeholder("Password").fill(os.environ["MONITOR_PASSWORD"])
        page.get_by_role("button", name="Sign in").click()
        page.get_by_placeholder("Code").fill(mfa.code("contoso-monitor"))
        page.get_by_role("button", name="Verify").click()
        page.get_by_role("button", name="Yes").click()  # "Stay signed in?": the cookies that last

    def signed_in(page):    # opens the app: still signed in, or did Microsoft ask again?
        page.goto(APP)
        home = page.get_by_role("heading", name="Operations")
        asked = page.get_by_role("heading", name=re.compile(r"^(Sign in|Enter password|Pick an account)$"))
        expect(home.or_(asked)).to_be_visible()
        return home.is_visible()

    with signin.reuse(browser, "contoso-monitor", sign_in=sign_in, signed_in=signed_in) as s:
        expect(s.page.get_by_role("link", name="Runbooks")).to_be_visible()

The saved cookies sign in as the account for as long as the site takes them, like the password and a
code together, and any client token can check them out. An admin lists them with GET /api/signins and
makes the next run sign in again with POST /api/signins/<id>/forget.

Settings, from the environment, as for src/mfa.py:
  GRID_UI_URL     the Grid UI, as the check reaches it (default http://grid_ui:4444, its compose name)
  GRID_MFA_TOKEN  the client token to ask with; default GATEWAY_CLIENT_TOKEN, which checks already have.
"""
import contextlib
import time
import warnings

from . import _grid_ui


class SigninError(Exception):
    """A saved sign-in could not be checked out, used or saved. .status is the Grid UI's HTTP status (None
    when it wasn't reached or didn't refuse) and .answer its JSON answer ({} when it sent none), e.g.
    {"error": "lease_lost"}. The message says why; it never holds a token or a cookie."""

    def __init__(self, message, status=None, answer=None):
        super().__init__(message)
        self.status = status
        self.answer = answer or {}


class Session:
    """What reuse() gives the with-block: .page in .context, signed in; .reused, True when the saved state
    worked and False after a fresh sign-in; .version, the saved version the context holds, and .signin_id,
    the id of the sign-in it is a version of (save() takes both)."""

    def __init__(self, name, context, page, reused, version, signin_id):
        self.name = name
        self.context = context
        self.page = page
        self.reused = reused
        self.version = version
        self.signin_id = signin_id


def _post(path, body, what, url, token, timeout_s):
    url, token = _grid_ui.settings(url, token, SigninError)
    try:
        return _grid_ui.post(url, token, path, body, timeout_s)
    except _grid_ui.Refused as e:
        raise SigninError("The Grid UI refused to {}: HTTP {} {}".format(what, e.status, e.answer.get("error") or e.reason),
                          e.status, e.answer) from None
    except _grid_ui.Unreachable as e:
        raise SigninError("The Grid UI at {} could not be reached to {}: {}".format(url, what, e)) from None


def checkout(name, rejected_version=None, lease_s=None, signin_id=None, url=None, token=None, timeout_s=10):
    """One request for the sign-in saved as `name`, without waiting: the Grid UI's answer, whose "status" is
      "saved"    a state to use: open a context with storage_state=answer["state"] ("id", "version",
                 "savedAt", "expiresAt" say which one it is)
      "sign-in"  nothing usable is saved: this caller signs in and saves the state with answer["leaseToken"]
                 within "leaseForS" seconds (lease_s, default 180); others are told to wait meanwhile
      "wait"     another caller is signing in: ask again in "retryAfterS" seconds
    rejected_version is the version of a state the site refused (it asked to sign in again), and signin_id
    the "id" it came with: the Grid UI drops it, unless someone saved a newer one meanwhile, which comes
    back instead."""
    body = {"name": name}
    if rejected_version is not None:
        body["rejectedVersion"] = rejected_version
    if signin_id is not None:
        body["id"] = signin_id
    if lease_s is not None:
        body["leaseS"] = lease_s
    return _post("/api/signins/checkout", body, "check out the saved sign-in {!r}".format(name), url, token, timeout_s)


def save(name, state, lease_token=None, base_version=None, max_age_s=None, signin_id=None, url=None, token=None,
         timeout_s=30):
    """Saves `state` (a context's storage_state()) as the sign-in `name`: a fresh sign-in with the lease_token
    of a "sign-in" checkout, or the same sign-in with its cookies rolled on, with base_version and signin_id,
    the "version" and "id" it was checked out as. The answer is {"saved": True, "version"}, or {"saved":
    False, "reason": "newer", "version"} when someone saved since base_version (or an admin deleted the
    sign-in it was checked out from). A lease that ran out raises SigninError with status 409. The Grid UI
    hands the state out for max_age_s from now (default 30 days, at most 90)."""
    body = {"name": name, "state": state}
    if lease_token is not None:
        body["leaseToken"] = lease_token
    if base_version is not None:
        body["baseVersion"] = base_version
    if signin_id is not None:
        body["id"] = signin_id
    if max_age_s is not None:
        body["maxAgeS"] = max_age_s
    return _post("/api/signins/save", body, "save the sign-in {!r}".format(name), url, token, timeout_s)


def release(name, lease_token, url=None, token=None, timeout_s=10):
    """Gives back the lease of a sign-in that failed, so the next caller signs in without waiting for it to
    run out. True when the lease was still this caller's."""
    answer = _post("/api/signins/release", {"name": name, "leaseToken": lease_token},
                   "release the sign-in lease of {!r}".format(name), url, token, timeout_s)
    return bool(answer.get("released"))


@contextlib.contextmanager
def reuse(browser, name, sign_in, signed_in, sleep=time.sleep, max_wait_s=300, max_age_s=None, refresh=True,
          lease_s=None, url=None, token=None, **new_context_kwargs):
    """A page signed in to the account whose sign-in the Grid UI keeps as `name`, in a context of its own
    that is closed when the with-block ends.

    signed_in(page) opens the app and says whether it is signed in: False when the site asks to sign in
    again (e.g. the page ends up on login.microsoftonline.com). sign_in(page) does the whole sign-in on that
    page, password and mfa.code() included. reuse() opens a context with the saved state and keeps it when
    signed_in() says so; otherwise, or when nothing is saved, it tells the Grid UI, runs sign_in() in a
    fresh context, checks it with signed_in() and saves the state, which the Grid UI hands out for
    max_age_s (default 30 days) or until a site refuses it. A sign-in that fails gives its lease back and
    raises. While one check signs in, others for the same name wait for its state, for up to max_wait_s.

    When the block ends without an error and refresh is true, the context's state is saved again, since
    sites roll their cookies as they are used; a problem with that only warns. Other keyword arguments go
    to browser.new_context().

    sleep(seconds) does the waiting. If the check answers requests with page.route on another page, pass
    one that keeps it served, e.g. sleep=lambda s: other_page.wait_for_timeout(s * 1000)."""
    if "storage_state" in new_context_kwargs:
        raise TypeError("reuse() opens the context with the saved storage_state itself")
    session = _open(browser, name, sign_in, signed_in, sleep, max_wait_s, max_age_s, lease_s, url, token,
                    new_context_kwargs)
    try:
        yield session
        if refresh:
            _refresh(session, max_age_s, url, token)
    finally:
        session.context.close()


def _open(browser, name, sign_in, signed_in, sleep, max_wait_s, max_age_s, lease_s, url, token, context_kwargs):
    """reuse()'s signed-in Session: the saved state if the site takes it, else a fresh sign-in."""
    deadline = time.monotonic() + max_wait_s
    rejected = rejected_id = None
    refused = set()
    while True:
        answer = checkout(name, rejected, lease_s, rejected_id, url, token)
        # Sent once: dropping a state keeps its version, so a second report would count another refusal.
        rejected = rejected_id = None
        status = answer.get("status")
        if status == "wait":
            retry_s = answer.get("retryAfterS") or 1
            if time.monotonic() + retry_s > deadline:
                raise SigninError("Another check has been signing in to {!r} for longer than max_wait_s ({} s)."
                                  .format(name, max_wait_s))
            sleep(retry_s)
        elif status == "saved":
            version, signin_id = answer["version"], answer["id"]
            if (signin_id, version) in refused:
                raise SigninError("The Grid UI handed out the sign-in {!r} version {} again after the site refused it."
                                  .format(name, version))
            context = browser.new_context(storage_state=answer["state"], **context_kwargs)
            try:
                page = context.new_page()
                works = signed_in(page)
            except BaseException:
                context.close()
                raise
            if works:
                return Session(name, context, page, True, version, signin_id)
            # The site asked to sign in again: the next checkout says so, and its answer who signs in.
            context.close()
            refused.add((signin_id, version))
            rejected, rejected_id = version, signin_id
        elif status == "sign-in":
            lease, signin_id = answer["leaseToken"], answer["id"]
            context = browser.new_context(**context_kwargs)
            try:
                page = context.new_page()
                sign_in(page)
                if not signed_in(page):
                    raise SigninError("sign_in() finished, but signed_in() says the page isn't signed in as {!r}."
                                      .format(name))
                saved = save(name, context.storage_state(), lease_token=lease, max_age_s=max_age_s, url=url, token=token)
            except BaseException:
                _release(name, lease, url, token)
                context.close()
                raise
            return Session(name, context, page, False, saved["version"], signin_id)
        else:
            raise SigninError("The Grid UI answered the checkout of {!r} with an unknown status {!r}.".format(name, status))


def _release(name, lease_token, url, token):
    # Only warns: the sign-in's own error is the one to raise, and the lease runs out by itself.
    try:
        release(name, lease_token, url, token)
    except SigninError as e:
        warnings.warn("{} (it runs out by itself)".format(e))


def _refresh(session, max_age_s, url, token):
    """Saves the context's state again, with the cookies the site rolled on meanwhile. Only warns when that
    fails: the check passed, and the state saved before still works."""
    try:
        answer = save(session.name, session.context.storage_state(), base_version=session.version,
                      max_age_s=max_age_s, signin_id=session.signin_id, url=url, token=token)
    except Exception as e:
        warnings.warn("The sign-in {!r} wasn't saved again after the check: {}".format(session.name, e))
        return
    if answer.get("saved"):
        session.version = answer["version"]
    elif answer.get("reason") != "newer":
        # "newer": another check saved a fresher state meanwhile, which is kept.
        warnings.warn("The Grid UI didn't save the sign-in {!r} again after the check: {}"
                      .format(session.name, answer.get("reason")))
