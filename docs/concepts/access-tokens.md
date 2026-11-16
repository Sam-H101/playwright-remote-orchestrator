# Access tokens and scopes

Agents need an **agent token** to register and checks need a **client token** to open a session. The hub accepts two sources for each: bootstrap tokens from its environment, and managed tokens made on the Grid UI's **Access** page.

## Bootstrap tokens {#bootstrap-tokens}

`GATEWAY_AGENT_TOKEN` and `GATEWAY_CLIENT_TOKEN` on the hub.

- They work even while MySQL is down, and the compose runners and example check use them, so compose requires both.
- They can't be revoked from the UI. To rotate one, change it in `.env` and run the same `docker compose ... up -d` again, which recreates the hub, the runners, the checks and the Grid UI (which takes `GATEWAY_CLIENT_TOKEN` for [MFA codes](../grid-ui/mfa-secrets.md) and [saved sign-ins](../grid-ui/saved-sign-ins.md)) with the new value.
- A hub run outside compose may leave them unset and use managed tokens only.
- They have no [token scope](#token-scopes).

## Managed tokens {#managed-tokens}

Made on the Grid UI's **Access** page, by whoever has [`GRID_ADMIN_TOKEN`](../grid-ui/signing-in.md#admin-token) or, with single sign-on, by people whose [roles](../grid-ui/users-and-roles.md) allow it.

- Agent tokens start with `pwga_`, client tokens with `pwgc_`.
- MySQL only keeps their sha256 and first 12 characters, so the token is shown once, when it is created, with ready-to-paste commands for that kind.
- Use it like the env token: `-AgentToken`, `GATEWAY_AGENT_TOKEN` or `--token` for an agent, `GATEWAY_CLIENT_TOKEN` for a check.
- A client token can be made with a **token scope** that limits what it may do (see [Token scopes](#token-scopes)); a token without one does everything its owner may.

## On the Access page

**New token**
:   Takes a name, the kind, an expiry (1-3650 days, or none) and, for an agent token, an optional node name. For a client token, **What it may do** is **Everything** (whatever its owner may do) or **Only what you pick**, which gives the token a token scope: any of Open browser sessions, See the grid, Use MFA secrets, Use saved sign-ins and Add saved sign-ins (which ticks Use saved sign-ins too). It can't be changed afterwards.

**Add a host**
:   Makes an agent token bound to a node name, shows the one install command for the chosen platform and waits for the host to register. It warns before making a second token for a node that already has one.

**Rotate**
:   Create a new token (bound to the same node, for a bound one; with the same scope, for a client token that has one), put it on the host (re-run the Windows installer with `-AgentToken`, or change the env file and restart the agent), wait until the node shows up under the new token, then revoke the old one. For `pwrunner`, `pwrunner token set` replaces the token and restarts the service (see [Windows host](../deployment/windows.md#after-the-install)).

**Revoke**
:   New connections with the token are refused within `TOKEN_CACHE_S` (10 s), and the hub closes the agents still connected with it within `TOKEN_RECHECK_S` (15 s), with 4401. Those agents keep retrying until they get a valid token. Browser sessions that are already open with a revoked client token run to the end; new ones get 401. The confirmation names the nodes that will be disconnected.

**Expiry**
:   Works the same way: an expired token is refused, and connected agents using it are closed at the next re-check.

**Delete**
:   Removes a revoked or expired token from the list. Active tokens must be revoked first.

### Binding an agent token to a node {#binding}

A token **bound** to a node can only register that name (anything else is closed with 4403). Once the node has registered with it, the name is reserved for it while it is valid: no other token can take the node over, not even the bootstrap token. Give each runner host its own bound token, so a leaked one can't receive other nodes' sessions.

## When the hub requires a token

- With neither `GATEWAY_AGENT_TOKEN` nor a managed agent token, any agent is accepted (logged as a warning). With neither `GATEWAY_CLIENT_TOKEN` nor a managed client token, clients aren't checked.
- Once a managed token of a kind exists, the hub requires a valid token of that kind even without the env token. So creating the first agent token on a hub without `GATEWAY_AGENT_TOKEN` disconnects agents that have no token within `TOKEN_RECHECK_S`.
- It stays that way after every managed token of the kind is revoked or deleted, until the hub restarts with no token of that kind left in the table. Set `GATEWAY_ALLOW_OPEN_AGENTS` / `GATEWAY_ALLOW_OPEN_CLIENTS` on the hub to let tokenless agents or clients back in as soon as every token of the kind is revoked.
- Compose always sets both env tokens, so none of this changes anything there.

If the hub can't reach MySQL, the bootstrap tokens keep working and managed tokens are refused until it can check them again: agents are closed with 1013 and retry, and checks get 503.

## Token scopes {#token-scopes}

A token scope limits what one client token may do, so a token that leaks from a pipeline is worth less. It is not single sign-on's scopes (`GRID_OIDC_SCOPES`), which are something else. A scope is a list of these ids:

| Scope id | On the Access page | Lets the token | Enforced by |
| --- | --- | --- | --- |
| `sessions.open` | Open browser sessions | Open a browser session at the hub's `/connect` | the hub |
| `grid.view` | See the grid | Read `/status` (`/api/status`) and `/api/events` where reading needs sign-in | the Grid UI |
| `mfa.use` | Use MFA secrets | Get [MFA codes](../grid-ui/mfa-secrets.md) | the Grid UI |
| `signins.use` | Use saved sign-ins | Check out, save and release [saved sign-ins](../grid-ui/saved-sign-ins.md) | the Grid UI |
| `signins.add` | Add saved sign-ins | Use a saved sign-in's name for the first time. Needs `signins.use` in the scope too | the Grid UI |

- **A scope only takes away.** On every request the token may do what its owner may do at that moment, less what its scope leaves out. It reaches the same MFA secrets and saved sign-ins as its owner, never more: `mfa.use` in the scope of a token whose owner's roles don't include "Use MFA secrets" gets no codes.
- **It is chosen when the token is made and can't be changed.** To change what a token may do, make another and revoke the old one. The audit log records the scope with the token. A token [given to a person](../grid-ui/users-and-roles.md#client-tokens) keeps its scope.
- **Only managed client tokens have one.** Agent tokens and the bootstrap tokens (`GATEWAY_AGENT_TOKEN`, `GATEWAY_CLIENT_TOKEN`) have no scope, and neither do the client tokens made before scopes or with **Everything**.
- **Making one from a script** is `POST /api/tokens/scoped` (see [HTTP API](../grid-ui/api.md#tokens)), not `POST /api/tokens`. It needs "Make client tokens", and the grid's first client token needs "Every access token" whether it has a scope or not (see [Users and roles](../grid-ui/users-and-roles.md)).
- **Opening sessions is not something roles give.** Any valid client token whose scope allows it (or that has no scope) opens sessions, as before: also one whose owner is disabled, until it is revoked. If the only client tokens on record have scopes without `sessions.open`, the hub still demands a token, and only the bootstrap token opens sessions.
- **`grid.view` matters only where reading the grid needs sign-in.** Where `GRID_UI_READS` leaves reading open, anyone reads the grid without a token.
- **What a refused check sees.** From the hub, HTTP 403 `gateway token is not allowed to open browser sessions (its scope leaves that out)`; `main.py` logs it with `GATEWAY_CLIENT_TOKEN's scope leaves out opening browser sessions: use a token whose scope includes it`. Not every Playwright client shows the hub's sentence, but the 403 is always there: at `/connect`, a 403 means the scope and a 401 any other problem with the token. From the Grid UI, HTTP 403 `{"error", "out_of_scope": true, "scope": "<the scope id that is missing>"}`, which `src/mfa.py` and `src/signin.py` raise as their errors. The Access page lists each scoped token with what it may do (`may only see the grid`), and hands out no hub address for one that can't open sessions.
- **Update the hub and the Grid UI first.** Scoped tokens need both at this version or later, on every instance. An older hub or Grid UI answers 401 to a scoped token and never lets one in, and an older Grid UI counts those 401s towards the guessing limit. An older Grid UI can't make one either: it answers 404 to `POST /api/tokens/scoped` and makes nothing, and the page says `The Grid UI that answered is from before token scopes, so it made no token. Try again when the update has finished.` The Grid UI can't tell which version of the hub is running, so finish updating both before handing out scoped tokens, or the checks using them fail until the update is done (and after a rollback, until it is undone).
- **On an existing database** the hub adds the `scope` column to `accessTokens` when it starts. Its database user needs the right to alter that table for this, once; without it the hub stops at startup, so either grant it or run `ALTER TABLE accessTokens ADD COLUMN scope VARCHAR(255) NULL` by hand before updating. Until then the Grid UI answers 503 to a request for a scoped token: `This database's accessTokens table has no scope column yet, so a token can't be given a scope. The gateway hub adds the column when it starts: update the hub, start it, and make the token again.`
- **`May do nothing: its scope was lost.`** A token listed that way was made with a scope that is no longer in the database (the column was dropped or the row changed by hand). It is refused everywhere rather than taken for a token with no limit. Revoke it and make another.

## Owners

With single sign-on each token shows its owner, whom a client token's checks act as. See [Users and roles](../grid-ui/users-and-roles.md#client-tokens).
