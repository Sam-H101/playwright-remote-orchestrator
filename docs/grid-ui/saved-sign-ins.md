# Saved sign-ins

A check that signs in to a site can save the browser's signed-in state (Playwright's `storage_state`: cookies and local storage) in the Grid UI and reuse it on later runs, so most runs skip the sign-in and its MFA prompt.

It matters most against Microsoft Entra ID: signing in on every run types an authenticator code each time and looks, to Microsoft's risk checks, like one account signing in over and over from server addresses, which Microsoft may answer by challenging, flagging or blocking the account. One saved sign-in, reused until Microsoft asks again, is the single sign-in a person's "stay signed in" would have made.

## How a check uses it

`src/signin.py` needs only the standard library and reaches the Grid UI with the same settings as `src/mfa.py` (`GRID_UI_URL`, and `GRID_MFA_TOKEN` or `GATEWAY_CLIENT_TOKEN`).

`signin.reuse()` checks out the saved state, opens a context with it and asks the check whether the app is signed in; only when nothing is saved, or the site asks again, does it sign in and save the new state. One check signs in at a time (it holds a lease) and the others wait for the state it saves, so a burst of runs still makes one sign-in. On a clean exit it saves the state again, since sites roll their cookies as they are used.

```python
import os, re
from playwright.sync_api import expect
from src import mfa, signin

APP = "https://contoso.sharepoint.com/sites/ops"

def signed_in(page):    # open the app: still signed in, or did Microsoft ask again?
    page.goto(APP)
    home = page.get_by_role("heading", name="Operations")
    asked = page.get_by_role("heading", name=re.compile(r"^(Sign in|Enter password|Pick an account)$"))
    expect(home.or_(asked)).to_be_visible()
    return home.is_visible()

def sign_in(page):      # the whole sign-in, on the page reuse() opens for it
    page.goto(APP)
    page.get_by_role("textbox", name=re.compile("email|someone", re.I)).fill("monitor@contoso.com")
    page.get_by_role("button", name="Next").click()
    page.get_by_placeholder("Password").fill(os.environ["MONITOR_PASSWORD"])
    page.get_by_role("button", name="Sign in").click()
    # Push ("Approve sign-in request") is the default for many accounts and needs a phone; switch to the code.
    switch = page.get_by_role("button", name="Sign in another way")
    if switch.is_visible():
        switch.click()
        page.get_by_role("button", name=re.compile("verification code", re.I)).click()
    page.get_by_role("textbox", name=re.compile("code", re.I)).fill(mfa.code("contoso-monitor"))
    page.get_by_role("button", name=re.compile(r"^(Verify|Next|Sign in)$")).click()
    page.get_by_role("button", name="Yes").click()   # "Stay signed in?": the cookies that last

with signin.reuse(browser, "contoso-monitor", sign_in=sign_in, signed_in=signed_in) as s:
    expect(s.page.get_by_role("link", name="Runbooks")).to_be_visible()
```

!!! warning "Not verified against a live tenant"
    This Microsoft flow is written from the screens Microsoft shows and is **not verified against a live tenant**; the headings, button text and the push-to-code switch change between tenants and over time, so match on text with the account you have, keep the locators forgiving, and set the authenticator up so a **verification code** is offered (not only push). The MFA code comes from the Grid UI ([MFA secrets](mfa-secrets.md)); the account's password is the check's to supply (here from the environment).

## Whose checks reuse it {#whose-checks-reuse-it}

With single sign-on, a saved sign-in belongs to the owner of the client token whose check first used its name, when their roles include "Add saved sign-ins" (otherwise a name nobody added answers 404).

- **Only the owner's checks sign in fresh for it and save it again.** For one that is Admins', the checks of anyone with "Every saved sign-in" do, and those of client tokens that act as an admin.
- A check that stops being one of those while it signs in (its token's owner loses "Every saved sign-in", say) saves nothing: its save answers 409 `lease_lost`, or 404 as for a missing name when it may no longer use the sign-in at all, and ends its lease, as its release still does, so the next check that may sign in for it does so at once.
- **The checks of the people and roles it is shared with reuse what was saved.** While nothing usable is, their checkout answers 409 `owner_signs_in` until the owner's own check has signed in, and the save after a reuse is skipped quietly (`"reason": "shared"`). Someone who may do something its owner may not is reached only by a share someone with "Every saved sign-in" made, and only while its owner keeps at least the permissions they had then (see [Sharing](users-and-roles.md#sharing)).
- **Any other check** gets the same answer as for a name nobody added (404 "There is no saved sign-in called '…' that this client token's owner may use", or 403 `out_of_scope` from a checkout whose token scope leaves out `signins.add`), so it can't tell a sign-in it may not use from one that doesn't exist. The one exception is a checkout by a token that may add sign-ins: it would have started a free name, so it learns the name is taken anyway, and gets 403 `not_shared`.
- A client token whose [token scope](../concepts/access-tokens.md#token-scopes) leaves out `signins.use` gets 403 `out_of_scope` from all three routes, and a name nobody added also needs `signins.add` in the scope.
- "Every saved sign-in" lets admins see and manage every sign-in on the page, but their checks use only their own, Admins' sign-ins and those shared with them, the Admin role or another role of theirs by someone with "Every saved sign-in": otherwise whoever took a name first could save an account of their own under it and have every admin's check run as that account. To let any admin's checks sign in for a sign-in, give it to Admins; to let them reuse one a person owns, share it with the Admin role.
- Names are taken for everyone on the grid, so a name someone took to get in the way is fixed by deleting the sign-in or changing its owner. Someone without "Every saved sign-in" owns at most 50.

## Whose client token it was

Who took an MFA code, and who saved, reused or is signing in with a saved sign-in, shows to everyone the item is shared with, except a client token's name: only the token's owner and people with "Every access token" (`tokens.all`) see it, and anyone else sees "a client token of <owner>" (or "another person's client token"). The Grid UI records a check's client token by its id, so a renamed or deleted token is still told apart; what was recorded before this version is matched by name against the tokens there are now.

## What `storage_state` captures

Cookies and `localStorage`, which is what keeps a browser signed in. It does **not** capture `sessionStorage`, which a new context starts empty. An MSAL single-page app caches its access tokens in `sessionStorage`, so the first load after a reuse finds none and bounces to `login.microsoftonline.com`, where the saved Microsoft cookie signs it back in with no prompt, then returns to the app. So the reuse still works, through that cookie, but `signed_in()` must wait for the app's own content and tolerate the brief sign-in redirect rather than fail on it.

## Where it is kept

Each saved sign-in is a row in `savedSignins`, the state encrypted with AES-256-GCM under a key derived from `GRID_SECRETS_KEY` (HKDF-SHA256) and bound to the sign-in's name: the same scheme as an MFA seed but under its own key, so stored seeds are unaffected.

- The **MFA & sign-ins** page lists the ones you can use (with "Every saved sign-in", all of them) with the cookies' domains, counts and who used it, never a cookie value.
- **Forget** drops the state (and bumps its version, so a check still holding the old one can't save it back) and the next run signs in again.
- **Delete** removes the row; a check that uses the name again adds a new one under a new id, which checks send with the version they save, so a check still holding the deleted state can't save it back either.
- The checks use `POST /api/signins/checkout`, `/api/signins/save` and `/api/signins/release` with `Authorization: Bearer <client token>`; the page uses `GET /api/signins`, `POST /api/signins/<id>/forget` and `POST /api/signins/<id>/delete`.

## Treat a saved sign-in as a credential

It is the account's live session, so a check whose client token may use it is signed in as the account without the password or a code, just as it gets the MFA codes its owner may use (see [Security](../security.md#secrets-in-the-grid-ui)).

- Share it only with people you'd trust with the account.
- Save only the accounts your checks sign in with, and keep `GRID_SECRETS_KEY` out of the database's backups (a dump without it holds no usable cookie).
- **Forget** a sign-in to stop checks reusing it. Forget only drops the grid's copy and doesn't sign the cookies out: to cut off a copy already checked out (after a client token leaked, say), revoke the account's sessions at the site (in Entra ID, **Revoke sessions** on the user) or reset its password.
- Without the key, or with one under 16 characters, checkout and save get 503 (the page still lists the sign-ins, and Forget and Delete work). If the key changes, the saved states no longer decrypt, and each check's next run signs in again.

## Proof of concept

`full/poc/poc_signin_reuse.py` proves the whole cycle against a fake app behind a Microsoft-style identity provider: reuse with no MFA, a revocation that forces one fresh sign-in, silent renewal after the app's own cookie expires, the lease that serialises concurrent runs, and the refresh-save. Its rounds count on having their saved sign-in (`POC_SIGNIN_NAME`) to themselves, so `full/poc/run_poc.sh` keeps its own, `poc-idp-session-manual`, apart from the scheduled `poc_signin_reuse`'s `poc-idp-session`.
