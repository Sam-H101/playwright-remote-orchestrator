# Grid UI

A management console lives in `ui/` (Flask, plain ES modules, no build step). It reads the same MySQL database the checks use. With docker it is the `grid_ui` service, published on `127.0.0.1:4444` only.

## Pages

**Overview**
:   One verdict (`Healthy`, `3 problems need you`, `No free slots`, ...), a list of what needs you (failing and unhealthy slots, stale sessions, offline nodes) with one action each, **Recent runs** (a swimlane per node over the last 1, 5 or 15 minutes, with bursts flagged, and a 15-minute strip of runs finished and busy slots), and the slot matrix: one row per node, one glyph-coded cell per slot, labelled with its slot number (or port, for static hosts).

    Select any node, slot or session to open the **inspector** on the right: what its state means in plain words, who claimed it (**Claimed by**: the hub or another client), failures against `SLOT_FAIL_THRESHOLD`, the next retry, recent events, a **Connect to the grid** block with the hub's one `/connect` address as Playwright JS or Python, PowerShell or bash (with **Only on <node>** to add `?node=`), and its actions. No slot has an address of its own: the hub picks the slot.

**Activity**
:   Every change recorded in the last 15 minutes, filterable by node, slot, kind and time; **Copy as JSON** copies the filtered events. An admin action is one row: the change it caused, with who did it.

**Sessions**
:   Running claims, stale first; release one from its inspector. A session the hub routed is stale only once that hub has stopped renewing its lease (nothing holds the claim then); while the hub runs it is a running session however long it lasts, and releasing it doesn't end it (the hub claims the slot again). Any other claim is stale after `GRID_STALE_AFTER`.

**Access**
:   The hub's managed [access tokens](../concepts/access-tokens.md), plus **Add a host**, which makes an agent token bound to a node name, shows the one install command for the chosen platform and waits for the host to register. It warns before making a second token for a node that already has one. With single sign-on each token shows its owner, whom a client token's checks act as.

**MFA & sign-ins**
:   The authenticator seeds checks get sign-in codes from (see [MFA secrets](mfa-secrets.md)), with the codes handed out recently and to whom, and the [saved sign-ins](saved-sign-ins.md) checks reuse so most runs skip the prompt, with **Forget** to make the next run sign in again. With single sign-on each one shows its owner, and **Share** lets other people and roles use it.

**Admin**
:   People who signed in, their roles and sessions, custom roles, group mappings, the audit log and the single sign-on settings (`g u`; on a phone, in the account menu). See [Users and roles](users-and-roles.md). With only the admin token set, it says how to set single sign-on up.

Every view, filter and object has an address (`#/activity?node=...`, `#/slot/12`, `#/node/<name>`, `#/session/12`). Press `/` or Ctrl K to jump to anything, `?` for the other shortcuts.

Who may see and do what is in [Signing in](signing-in.md) and [Users and roles](users-and-roles.md).

## What the activity can and can't see {#activity}

The Grid UI server records it (`ui/recorder.py`): it compares snapshots of the database, on every page poll and, when no page is open, every `GRID_RECORDER_INTERVAL` seconds (2 by default; 0 turns the background recording off), and keeps the last 15 minutes (at most 500 events) in memory.

- A tab opened after a failed CI run therefore still shows the runs and failures before it; the recording starts when the UI server does, and the page says since when.
- Claim and release both stamp `LastRunDate`, so a run that starts and ends between two snapshots still shows, as *finished between updates* with its end time but no start (drawn as "approx.").
- It also sees connect failures, failure resets, health changes and nodes coming and going.
- It can't see which check ran or why a connection failed (no error text is stored).
- Admin actions are attributed to who signed in, and to "you" in the tab that did them. With single sign-on that is a person's name, so where reading is open, callers who aren't signed in get the actions without who took them, and the page forgets who acted when you sign out.
- With several workers (gunicorn) each keeps its own recording; events are identified by database values, so the page merges them.

Visible pages poll `/api/status` every `GRID_UI_POLL_MS` (1500 by default; every 12 s in a background tab) and never hold a connection open, so it is safe behind gunicorn's sync workers.

## Hub address {#hub-address}

Copyable commands use `GATEWAY_PUBLIC_URL` when it is set. Otherwise they use `GATEWAY_URL`'s scheme and port with the host the page was opened through, since `ws://hub:4500` only resolves inside compose. The **Hub address** field next to any command overrides it for this browser. Snippets read the client token from `GATEWAY_CLIENT_TOKEN` in the environment and never contain it.

## Browser security {#browser-security}

Every response carries:

```text
Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
```

- The page loads nothing from other hosts and has no inline scripts or styles; every database value goes through one escaping template helper.
- Actions are refused cross-site and through unknown host names: the UI accepts actions only through an IP address, `localhost`, the machine's own host names, the host of `GRID_UI_PUBLIC_URL` or a name in `GRID_UI_ALLOWED_HOSTS`. List the DNS alias or reverse-proxy name you reach it by. The session cookie's `SameSite=Strict` is a second layer.
- With single sign-on, the session cookie is `HttpOnly`, `SameSite=Strict`, `Secure` whenever `GRID_UI_PUBLIC_URL` is https, and called `__Host-grid-session` when that is https with the path `/`. The login cookie that carries a sign-in through the provider is the same, but `SameSite=Lax` (a Strict one isn't sent on the provider's redirect back) and lasts 10 minutes. `__Host-` stops a sibling subdomain from setting the cookies: without it (plain http, or a public URL with a path), one could plant a login cookie and sign someone in to the grid as another person (login CSRF), and two tabs can't sign in at once.
- The Grid UI checks the ID token's issuer (byte for byte), audience, signature (asymmetric algorithms only, never `none` or `HS*`), times and nonce, sends PKCE, and binds each sign-in's `state` to the browser that started it; none of the provider's error texts is ever put in an answer or a redirect (only `?signin=<why>`).
- Every change to people, roles, tokens, MFA secrets, saved sign-ins and the grid goes in the [audit log](users-and-roles.md#audit-log).
- A database error names its file path or server only to a signed-in admin (and in the server log).
- Force release sends the claim the confirm described and is refused (409) if another check has claimed the slot since.
- The stylesheet uses CSS `light-dark()`, so the UI needs a 2024 or later browser (Chrome/Edge 123, Firefox 120, Safari 17.5).

## The log {#the-log}

The Grid UI writes its log to stderr, where `docker logs`, `kubectl logs` or a cluster's log collector reads it: who signed in with single sign-on, sign-ins that were refused and why, MFA secrets added and deleted and saved sign-ins saved, forgotten and deleted (each with who did it), settings that are wrong, and database errors.

- `GRID_UI_LOG_LEVEL` is the least a line must be (`info`).
- With `GRID_UI_LOG_FORMAT=text` a line is `2026-01-31T09:15:02.417Z INFO ui.app: mfa secret 'github' added by Ada`.
- With `json` it is `{"time":"2026-01-31T09:15:02.417Z","level":"INFO","logger":"ui.app","message":"mfa secret 'github' added by Ada"}`, always with those four keys (the time is UTC) and with `exception` when an error's traceback comes along, so a log collector needs no pattern to take it apart.
- A record never takes a second line: a line break or any other control character in a value (a name from the identity provider, a traceback) is written as its escape (`\n`), so nothing sent to the UI can start a line of its own.
- In the image gunicorn's own lines (start-up, workers, errors) are written the same way (`ui/gunicorn_conf.py`); `python -m ui.app` writes its lines the same way too, after Flask's two start-up lines.
- No line holds a seed, a code, a token, a cookie's value, an authorization code or a saved sign-in's state.
- Of a refusal by the identity provider's token endpoint the log keeps the HTTP status, the error code and the provider's error numbers, with a hint what to check (`the identity provider's token endpoint refused a sign-in's code: HTTP 401 invalid_client (error codes 700213). AADSTS700213: ...`); never the provider's message, the client secret or a token.
- When the provider sends a sign-in back with an error instead of a code, the log keeps its error code and the first 200 characters of its `error_description` (`the identity provider signed nobody in: 'access_denied' 'AADSTS50105: ...`).
- Lines do name people, so keep the log as carefully as the audit log.
- Requests get no line unless `GRID_UI_ACCESS_LOG=1`: then each gets one, whatever the level, with the method, the path without its query string (a sign-in comes back from the provider with its authorization code there), the status, how long it took and the client's address (the one `GRID_UI_PROXY_HOPS` settles), and nothing from headers, cookies or bodies; a JSON line has them as `method`, `path`, `status`, `duration_ms` and `client`.

## Running it

- With docker it is the `grid_ui` service (gunicorn, two workers, as the user `grid`).
- Locally: `pip install -r ui/requirements.txt`, set the `DB_*` variables, then `python -m ui.app` from the repo root (`GRID_UI_HOST`, `GRID_UI_PORT`; default `0.0.0.0:4444`).
- Demo without MySQL: `python -m ui.demo demo.db --heartbeat` (add `--activity`, or `GRID_DEMO_ACTIVITY=1`, to run short bursts of checks every few seconds), then `GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app` (add `GRID_ADMIN_TOKEN=<anything>` to see the demo's tokens; with `GRID_ADMIN_TOKEN` set, the Admin page shows the demo's people and roles).

All of the Grid UI's settings are in [Configuration](../reference/configuration.md#grid-ui).
