# Troubleshooting

Start with the Grid UI's **Overview**: its list of what needs you names failing and unhealthy slots, stale sessions and offline nodes, each with one action. Select an object to open the inspector, which says what its state means.

## Where the logs are

| Component | Log |
| --- | --- |
| Hub | stdout (`docker logs playwright_hub`): one line per routing decision and per client that gave up waiting, never a token |
| Agent (`agent.js`, runner image) | stdout; run-server output in `<LOG_DIR>/slot-<i>.log` when `--log-dir` is set. Status in `STATUS_FILE` (`/tmp/gateway-agent/status.json` in the runner image) |
| pwrunner | `C:\PlaywrightBackend\logs\` (`install.log`, `service.log`, `agent.log`, `slot-<i>.log`) and the Windows Event Log under `PlaywrightBackend` |
| Checks (`main.py`) | `<fol>/logs/check_info.log` and `error_info.log`; `./logs/<check>/` in compose, with failure screenshots |
| Grid UI | stderr (`docker logs playwright_grid_ui`); see [the log](../grid-ui/index.md#the-log) |

## A check got no browser

`check_info.log` says why, from the hub's answer. The common ones:

| The hub answered | Cause and fix |
| --- | --- |
| 503 `no free <browser> slot within 60s: ... all busy` | Every slot that fits stayed busy or failing for `GATEWAY_QUEUE_TIMEOUT_S`. Add slots or runners, or spread the checks out |
| 503 at once, for a browser no node offers | No connected node lists that browser in `BROWSERS`. Install it on a node and list it |
| 428 | No node runs the client's Playwright major.minor version. Upgrade the client or the runners to match ([Upgrading Playwright](upgrading-playwright.md)) |
| 404 | Unknown node in `?node=` / `GATEWAY_NODE` |
| 401 | Missing, bad, revoked or expired client token |
| 403 | The client token's scope leaves out `sessions.open` |
| 502 / 504 | The slots tried refused the session or didn't answer; they count as [slot failures](../concepts/slot-failures.md) |
| 503 for MySQL | The hub can't reach MySQL to pick or claim a slot, or to check a managed token |

The full list is in [Hub responses](../reference/responses.md#connect-statuses). If the client gives up before the hub answers, its own `connect` timeout is shorter than `GATEWAY_QUEUE_TIMEOUT_S` plus `GATEWAY_CONNECT_ATTEMPTS` x `OPEN_TIMEOUT_S`.

## A node doesn't register, or keeps disconnecting

The agent logs the close code and retries (1 s doubling to 30 s):

- **4401**: the token is missing, bad, revoked or expired. Make a new one on the Access page.
- **4403**: the token is bound to another node name, or this name is reserved for another token. Use the token made for this node, or another name.
- **1013**: the hub can't check managed tokens (MySQL down). It retries by itself.
- **4409**: another agent registered under the same name and took over the node. Give each host its own name.

Also check that the host reaches the hub's port (`ws://` or `wss://`), and that the hub's `HEARTBEAT_TIMEOUT_S` isn't shorter than the network allows. On Windows, `pwrunner status` and `pwrunner selfcheck` check the install, the accounts, the ports and the hub.

## A slot is failing

A slot with `SLOT_FAIL_THRESHOLD` (3) consecutive connect failures is skipped, except for one try every `SLOT_FAIL_COOLDOWN_S`. It heals once a session gets through, or right away with **Reset** in the Grid UI. Look at the slot's `slot-<i>.log` for why its run-server refused. Checks of a previous release counting their own failures can also leave slots failing ([Migrating](migrating.md)).

## Slots are healthy but every session fails

A slot's health is only whether its run-server answers. A node whose browsers are missing shows healthy and every session fails. On a Linux or macOS host, use the same `PLAYWRIGHT_BROWSERS_PATH` for the browser install and the service. On Windows, a service whose browser accounts are missing (an exe swapped without re-running `install`) starts no browsers: run `pwrunner install` again.

## Chromium crashes in a container

Give the container `--shm-size 1g` (compose: `shm_size: 1gb`); docker's default 64 MB `/dev/shm` is too small. Keep `--init` so browser processes are reaped.

## The runner container won't start

The runner's entrypoint exits with a message when:

- `RUNNER_BLOCKED_NETWORKS` is set but `RUN_SERVER_USER` isn't, or names no user that exists;
- it can't add the firewall rule: the container needs `NET_ADMIN` for `RUNNER_BLOCKED_NETWORKS`, or set the variable empty;
- the container has `NET_ADMIN` without `SETPCAP`, so it couldn't drop `NET_ADMIN`: add `SETPCAP` or drop `NET_ADMIN`.

The agent itself stops with an error for an invalid setting (a non-`ws://` hub URL, a bad node name, a slot count outside 1-64, a missing `cli.js`, a `RUN_SERVER_USER` that is root, unknown or shared between slots).

## Grid UI problems

| Symptom | Cause |
| --- | --- |
| Header says **Unprotected** | Neither `GRID_ADMIN_TOKEN` nor single sign-on is set: anyone who can load the page can run grid actions |
| Access, MFA or Admin page refuses everything | Neither `GRID_ADMIN_TOKEN` nor single sign-on is set |
| MFA page locked, code requests 503 | `GRID_SECRETS_KEY` unset or shorter than 16 characters |
| Code requests 409 | `GRID_SECRETS_KEY` changed; set the old key again, or re-add the secrets ([Encryption](../grid-ui/mfa-secrets.md#encryption)) |
| 429 from the UI | 20 wrong tokens from one address within 5 minutes ([Guessing limits](../grid-ui/signing-in.md#guessing)). Behind a proxy, set `GRID_UI_PROXY_HOPS` |
| Actions refused through a DNS alias or proxy name | Add the name to `GRID_UI_ALLOWED_HOSTS`, or set `GRID_UI_PUBLIC_URL` |
| Single sign-on fails | See [Single sign-on troubleshooting](../sso/troubleshooting.md) |
| Copied commands show `ws://hub:4500` or the wrong host | Set `GATEWAY_PUBLIC_URL`, or the **Hub address** field next to the command ([Hub address](../grid-ui/index.md#hub-address)) |
| Checks get 401 from the UI after removing `GATEWAY_CLIENT_TOKEN` from `grid_ui` | Give those checks a client token of their own as `GRID_MFA_TOKEN` ([Client tokens](../grid-ui/users-and-roles.md#client-tokens)) |

## Database

- The hub waits up to about 2 minutes for MySQL at startup.
- A hub that can't alter `accessTokens` to add the `scope` column stops at startup: grant the right once, or run `ALTER TABLE accessTokens ADD COLUMN scope VARCHAR(255) NULL` by hand.
- A database upgraded from MySQL 5.7 as latin1 needs `mfaSecrets`, `mfaIssues` and `savedSignins` converted to utf8mb4 ([Tables](../grid-ui/users-and-roles.md#tables)).
- Several hubs on one database each log a warning naming the others when they start; that is expected.
