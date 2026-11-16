# MFA secrets

Checks that sign in to a site with two-step verification get authenticator (TOTP, RFC 6238) codes from the Grid UI instead of holding the seed themselves. Someone stores the account's enrolment once and shares it with whoever's checks need it; a check asks for a code while the site's prompt is on screen and types it. The seed stays in the Grid UI: the check, the runner and the browser only ever see 6 digits.

## Storing a secret

On the Grid UI's **MFA** page (it needs "Use MFA secrets", adding needs "Add MFA secrets", and it is locked until `GRID_SECRETS_KEY` is set), add a secret with:

- a **name**: 1-64 letters, digits, `.`, `_` and `-`; checks ask for codes by it. A name is taken for everyone on the grid, whoever's secret it is;
- either the `otpauth://totp/...` URI in the site's enrolment QR code, or the base32 seed the site shows instead of the QR code (any case, spaces and dashes allowed).

A URI also brings the issuer, account, algorithm, digits and period; a bare seed gets SHA-1, 6 digits and 30 s, which nearly every site uses. Scan the same QR code with a phone too if people also sign in as that account. To change a seed, delete the secret and add it again.

Whoever may use the secret can look at the current code (which uses nothing up, and says whether a check already had it) or take one to type by hand, handed out like a check's. With single sign-on the secret is yours: **Share** it with the people or roles whose checks need it (see [Users and roles](users-and-roles.md#sharing)).

## Getting a code from a check

`src/mfa.py` needs only the standard library. The checks `main.py` runs have `src` at `/app/src` and run from `/app`, so:

```python
from src import mfa

page.get_by_label("Code").fill(mfa.code("intranet", label="nightly login check",
                                        sleep=lambda s: page.wait_for_timeout(s * 1000)))
```

- `mfa.code(name)` returns a code that works now, with at least `min_valid_s` (5) of its window left, after waiting for its window if it comes from the next one. Pass a `sleep` that keeps the page served, as above, if the check answers requests with `page.route`.
- `mfa.issue(name)` asks once without waiting and returns the UI's answer.
- Both raise `mfa.MfaError` saying why there is no code (unknown secret, wrong token, unreachable UI, key changed); it never holds the token.
- Over HTTP it is `POST /api/mfa/code` with `Authorization: Bearer <client token>` and `{"name", "label"?, "minValidS"?}`.
- `full/poc/poc_totp_login.py` signs in to a fake intranet this way.

Checks outside compose reach the UI at `GRID_UI_URL`; compose publishes it on 127.0.0.1 only (see [Security](../security.md) before exposing it).

## Once per window

Sites accept each code once, so the UI hands out each 30 s window of a secret once.

- When the current window is taken, or has less than `minValidS` left, the answer is the next free window's code, before that window opens: `waitS` is how long to wait before typing it (`mfa.code()` waits), `validForS` how long it works from now.
- Two checks that sign in to the same account at once get different codes, the second about one window later.
- Codes are handed out at most two windows past the current one; beyond that the UI answers 429 with `retry_after_s`, and `mfa.code()` waits and asks again for up to `max_wait_s` (120 s).
- The UI records each hand-out (window, time, token name and the check's `label`) in the database (`mfaIssues`) for 7 days, not in its log; codes themselves are never stored.

## Who may ask

A check asks with its client token: a managed client token from the Access page, or the bootstrap `GATEWAY_CLIENT_TOKEN`, which compose passes to the UI for this (the UI ignores a value shorter than 16 characters). `mfa.code()` sends `GRID_MFA_TOKEN`, else `GATEWAY_CLIENT_TOKEN`, so compose checks need nothing extra while compose gives the UI `GATEWAY_CLIENT_TOKEN` (see [Users and roles](users-and-roles.md#client-tokens) before taking it out).

- Wrong tokens count towards the admin sign-in limit (429 after 20 from one address in 5 minutes).
- A client token gets codes for the secrets its owner can use: their own, those shared with them or a role of theirs, and with "Every MFA secret" every one. Legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get them all.
- A secret that is someone else's and isn't shared with the token's owner answers exactly like a name nobody added (404, "There is no MFA secret called '…' that this client token's owner may use"), so a check can't learn which names exist.
- A token whose owner's roles don't include "Use MFA secrets" gets 403 with `forbidden`; a token whose [token scope](../concepts/access-tokens.md#token-scopes) leaves out `mfa.use`, 403 with `out_of_scope`.

## Clock

Codes are computed from the Grid UI host's clock, and checked by the site against its own: keep the Grid UI host in sync (NTP). Most sites also accept the window before and after.

## Encryption

Seeds are stored in `mfaSecrets` encrypted with AES-256-GCM under a key derived from `GRID_SECRETS_KEY` (HKDF-SHA256), bound to the secret's name.

- If the key changes, the stored seeds no longer decrypt: code requests get 409 saying so, and nothing is used up. Set the old key again, or delete the secrets and add them again from their enrolments (if you no longer have one, enrol the account again on its site).
- Without the key (or with one under 16 characters) the MFA page stays locked and code requests get 503.
- Keep `GRID_SECRETS_KEY` out of the database's backups; a dump without it holds no usable seed.

!!! warning "A client token is also a second factor"
    A client token gets codes for the secrets its owner can use, so it also works as the second factor of those accounts. Store only the accounts your checks sign in with, and treat client tokens accordingly. See [Security](../security.md#secrets-in-the-grid-ui).
