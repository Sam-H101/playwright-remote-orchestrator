"""Authenticator (TOTP) codes for checks, handed out by the Grid UI's MFA secrets.

Someone stores the account's seed on the Grid UI's MFA page and shares it with whoever's checks need it;
the check asks for a code at the moment the site's prompt is on screen and types it. The seed never
leaves the Grid UI, so neither the check's environment nor the runner ever sees it:

    from src import mfa
    page.get_by_label("Code").fill(mfa.code("intranet", label="nightly login check"))

The Grid UI hands each 30 s window out once, the way sites accept each code once, so checks signing in
to the same account at the same time get different codes; when the current window is taken (or has
less than min_valid_s left), the code comes from the next one and code() waits for that window to open.

Settings, from the environment:
  GRID_UI_URL     the Grid UI, as the check reaches it (default http://grid_ui:4444, its compose name)
  GRID_MFA_TOKEN  the client token to ask with; default GATEWAY_CLIENT_TOKEN, which checks already have.
                  The Grid UI accepts managed client tokens, and the bootstrap one when it is given its value.
                  A client token gets codes for the secrets its owner can use: their own and those shared
                  with them (for any other, the Grid UI answers 403 not_shared). Legacy tokens, tokens made
                  with the admin token, and GATEWAY_CLIENT_TOKEN act as an admin and get every secret's.
"""
import time

from . import _grid_ui

DEFAULT_URL = _grid_ui.DEFAULT_URL


class MfaError(Exception):
    """The Grid UI gave no code. The message says why; it never holds a token."""


def _settings(url, token):
    return _grid_ui.settings(url, token, MfaError)


def issue(name, label=None, min_valid_s=None, url=None, token=None, timeout_s=10):
    """One request for a code, without waiting: the Grid UI's answer, {"code", "step", "waitS",
    "validForS", "period", "digits", ...}. Type the code only after waitS seconds."""
    url, token = _settings(url, token)
    body = {"name": name}
    if label is not None:
        body["label"] = label
    if min_valid_s is not None:
        body["minValidS"] = min_valid_s
    try:
        return _grid_ui.post(url, token, "/api/mfa/code", body, timeout_s)
    except _grid_ui.Refused as e:
        error = MfaError("The Grid UI refused a code for {!r}: HTTP {} {}".format(name, e.status, e.answer.get("error") or e.reason))
        error.status = e.status
        error.retry_after_s = e.answer.get("retry_after_s")
        raise error from None
    except _grid_ui.Unreachable as e:
        raise MfaError("The Grid UI at {} could not be reached for a code: {}".format(url, e)) from None


def code(name, label=None, min_valid_s=None, url=None, token=None, sleep=time.sleep, max_wait_s=120):
    """A code for the secret called `name`, ready to type now: asks the Grid UI, waits for the code's
    window when it comes from the next one, and retries while every window it may use is taken.

    sleep(seconds) does the waiting. In a Playwright check, pass one that keeps the page served, e.g.
    sleep=lambda s: page.wait_for_timeout(s * 1000)."""
    deadline = time.monotonic() + max_wait_s
    while True:
        try:
            answer = issue(name, label, min_valid_s, url, token)
        except MfaError as e:
            if getattr(e, "status", None) != 429 or time.monotonic() + (e.retry_after_s or 1) > deadline:
                raise
            sleep(e.retry_after_s or 1)
            continue
        if answer["waitS"] > 0:
            sleep(answer["waitS"])
        return answer["code"]
