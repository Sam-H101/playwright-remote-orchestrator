# Grid UI HTTP API

The page uses the same JSON routes scripts and checks can call. Credentials are one of:

| Credential | Sent as | Who |
| --- | --- | --- |
| Session cookie | the cookie single sign-on sets | People in a browser |
| Admin token | `X-Grid-Token: <GRID_ADMIN_TOKEN>` | Scripts and break-glass |
| Client token | `Authorization: Bearer <client token>` | Checks and scripts; narrowed by its [token scope](../concepts/access-tokens.md#token-scopes) |

Actions are refused cross-site and through host names the UI doesn't know (see [Browser security](index.md#browser-security)), except the checks' routes `POST /api/mfa/code`, `/api/signins/checkout`, `/api/signins/save` and `/api/signins/release`, which take nothing but a client token in the `Authorization` header, which a page on another site can't send.

Permission names below are those in [Users and roles](users-and-roles.md#permissions).

## Grid {#grid}

- `GET /status` (or `/api/status`): the grid as JSON, plus a `ready` flag (at least one slot can be picked), `grid_name`, `public_gateway_url` and `poll_ms`.
    - `?soft=1` reports a database outage as `{"unavailable": true, "error": ...}` with HTTP 200 instead of a 503 (the page uses it).
    - `?events=<epoch ms>` adds the recorded `events` observed since then and the `recorder`'s start, drops and capacity samples (`0`: all 15 minutes); `GET /api/events?since=` returns the same on its own.
    - Where reading needs sign-in (`GRID_UI_READS`), these need "See the grid": a session, the admin token, or a client token sent as `Authorization: Bearer <token>`, for scripts (one with a token scope needs `grid.view` in it). With `?soft=1` that refusal is HTTP 200 `{"refused": "signedout" | "forbidden", "error"}` with no grid data, so the page's polls log no errors.
    - With single sign-on, where reading is open, callers who aren't signed in get the events without who took each action.
- `POST /api/slots/<id>/release {"claimed_at": "<the session's started>"}` releases only that claim.
- `POST /api/nodes/forget {"host": "<name>"}` forgets a node (409 while it is online or in use).
- Releasing, the failure resets and forgetting need "Run the grid".

## Session and sign-in {#session}

- `GET /api/session`: `{signedIn, name, method, adminConfigured, providers, user, permissions, roles, admin, readsOpen, unavailable}` for the credentials sent. `permissions` is what the request may do now, `user` and `roles` are a signed-in person's (each role with where it came from), and `unavailable` says the database couldn't be asked about a session cookie.
- `GET /api/permissions` lists every permission and what the built-in roles hold (open: it says nothing about anyone).
- `GET /auth/login?return=<page hash>` sends the browser to the identity provider (or back to the page with `?signin=<why>` when it can't), and `GET /auth/callback` finishes the sign-in with a redirect back to the page (with `?signin=<why>` when it didn't work).
- `POST /api/session/signout {"everywhere"?: true}` signs out.

## Access tokens {#tokens}

- `GET /api/tokens`, `POST /api/tokens {"name", "kind", "nodeName"?, "expiresInDays"?}` (answers the token once), `POST /api/tokens/<id>/revoke` and `POST /api/tokens/<id>/delete` (409 while active) need "Make client tokens", "Add hosts" or "Every access token", and each one shows and changes only the tokens that allows; everyone gets the counts of every kind (`kindCounts`).
- Making a client token needs "Make client tokens", an agent token "Add hosts".
- `POST /api/tokens` makes a token with no limit, and refuses (400) a body with any key other than those four: `There is no token setting '<key>' here.`, and for `scope`, `A token with a scope is made with POST /api/tokens/scoped.` Before token scopes, unknown keys were ignored, so a script that sends an extra key must drop it.
- `POST /api/tokens/scoped {"name", "expiresInDays"?, "scope"}` makes a client token with a token scope and answers like `POST /api/tokens`: `scope` is a non-empty list of scope ids (`["sessions.open", "mfa.use"]`) and must be there, `signins.add` needs `signins.use` beside it, and any other key, an unknown id or an empty list is refused (400).
- Every token in an answer has `scope`: `null` for no limit, or the sorted list. A token whose scope was lost has `"scope": []` and `"scopeLost": true`.
- `POST /api/tokens/<id>/owner {"userId"}` gives a client token nobody owns to someone ("Every access token").

For example, a token that may only open browser sessions, for 90 days:

```sh
curl -X POST https://grid.example.com/api/tokens/scoped -H "X-Grid-Token: $GRID_ADMIN_TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "nightly pipeline", "expiresInDays": 90, "scope": ["sessions.open"]}'
```

## MFA secrets {#mfa}

- `GET /api/mfa` (secrets and the last 100 hand-outs, never a seed), `GET /api/mfa/<id>/code` (the current code, using nothing up) and `POST /api/mfa/<id>/use` (a code handed out like a check's) need "Use MFA secrets".
- `POST /api/mfa {"name", "secret", "issuer"?, "account"?, "algorithm"?, "digits"?, "period"?}`, `POST /api/mfa/<id>/delete` and `POST /api/mfa/<id>/shares/add` or `/shares/remove {"user": <id>}` or `{"role": <id>}` need "Add MFA secrets", and to own the secret or hold "Every MFA secret".
- `POST /api/mfa/<id>/owner {"userId": <id> | null}` (null: Admins) needs "Every MFA secret".
- A secret you may not see answers as one that isn't there (404).
- `POST /api/mfa/code` is the checks': `Authorization: Bearer <client token>` and `{"name", "label"?, "minValidS"?}` (see [MFA secrets](mfa-secrets.md)).

## Saved sign-ins {#signins}

- `GET /api/signins` (saved sign-ins and who used them, never a cookie) needs "Use saved sign-ins".
- `POST /api/signins/<id>/forget` (drop the state so the next run signs in again), `POST /api/signins/<id>/delete` and `/shares/add` or `/shares/remove` need "Add saved sign-ins", and to own the sign-in or hold "Every saved sign-in".
- `POST /api/signins/<id>/owner` needs "Every saved sign-in".
- The checks' routes are `POST /api/signins/checkout`, `POST /api/signins/save` and `POST /api/signins/release` (see [Saved sign-ins](saved-sign-ins.md)).

## People, roles and the audit log {#admin}

- `GET /api/directory?q=` (the roles, and the people whose name or email holds `q`, to share with or give to) needs any of "Add MFA secrets", "Add saved sign-ins", "Every access token" or "See people and roles".
- The Admin page's routes, `/api/admin/users`, `/api/admin/roles`, `/api/admin/groups`, `/api/admin/audit` and `/api/admin/sign-in`, need "See people and roles" to read (the audit log, "Read the audit log") and "Manage people and roles" to change (moving someone's items also needs "Every MFA secret" and "Every saved sign-in").

## Refusals {#refusals}

| Refusal | Answer |
| --- | --- |
| Missing permission | 403 `{"error", "forbidden": true, "permission"}` |
| Not signed in | 403 `{"error", "token_required": true}`, with `reason` (`expired`, `idle`, `revoked` or `disabled`) when a session stopped working |
| Client token's scope leaves the request out | 403 `{"error", "out_of_scope": true, "scope"}`, with the scope id that is missing |
| Client token whose owner is disabled or unknown | 403 `owner_disabled` |
| Too many wrong tokens from one address | 429 (see [Guessing limits](signing-in.md#guessing)) |

With only `GRID_ADMIN_TOKEN` set, every refusal is the same as before single sign-on.
