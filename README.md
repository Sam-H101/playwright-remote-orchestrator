# playwright-remote-orchestrator

## Purpose

* Runs Playwright checks in containers and sends each run to a free browser slot on any number of runner hosts (Linux containers, Windows machines, anything that runs Node). Checks live in one place; the browsers run wherever you have capacity.
* Runner hosts register themselves with a central hub and dial **out** to it, so the only port you open is the hub's, on the orchestrator host. Runner hosts open no inbound ports.

## Architecture

```
  example_playwright (main.py + pytest)            grid_ui :4444 (127.0.0.1 only)
  and any other Playwright test                            |
     |                                                     v
     | ws://hub:4500/connect        +-------------------------------------------+
     v                              |                   mysql                   |
  +--------------------------+      |    nodes, slots, slot health, claims      |
  |  hub  (gateway/hub.js)   |----->+-------------------------------------------+
  +--------------------------+  claims and releases slots
     ^            ^            ^     <- the ONE published port (GATEWAY_PORT, default 4500)
     |            |            |     each agent dials OUT: /agent (control, heartbeats)
     |            |            |     plus /agent/data/<id> (one per browser session)
  runner x N   Windows host   Linux / macOS host
  (docker)     (installer)    (node gateway/agent.js)

  every agent runs N `playwright run-server` slots on 127.0.0.1:3100.. and restarts any that crash
```

1. A test connects to `ws://hub:4500/connect` with its client token. Playwright's connect already tells the hub which browser it wants and which Playwright version it is.
2. The hub picks a free slot on a node that offers that browser on the same Playwright version and claims it in MySQL. A slot is free when it is not in use, its node is connected, its run-server is healthy and it isn't [failing](#slot-failures). While every matching slot is busy, the client waits.
3. The hub asks that node's agent to open a session; the agent connects to its local run-server and opens a data channel back to the hub, and the hub joins the two. When the session ends, the hub releases the slot.
4. Agents send a heartbeat with per-slot health every 10 s. After any disconnect or hub restart they reconnect and register again by themselves.

See [Connecting tests](#connecting-tests) for the details.

## Quick start (docker compose)

```
cp .env.example .env     # set GATEWAY_AGENT_TOKEN, GATEWAY_CLIENT_TOKEN and GRID_ADMIN_TOKEN (e.g. openssl rand -hex 32)
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2
```

This starts MySQL, the hub on port 4500, two Linux runner containers (4 slots each), the Grid UI on http://localhost:4444 and the example check. Compose refuses to start until both gateway tokens are set: they are the [bootstrap tokens](#access-tokens) the compose runners and checks use. Give your other runner hosts and checks their own tokens from the Grid UI's **Access** page. To let people sign in to the Grid UI with your identity provider rather than share `GRID_ADMIN_TOKEN`, see [Users and roles](#users-and-roles). The `web` service only builds the checks' image (`playwright_orchestrator_web`), so its `python_playwright` container exits right away.

* Change the number of runners with `--scale runner=N`. Pass it on every `up`, or compose goes back to one runner. `RUNNER_SLOTS` sets the slots per runner.
* Runner containers are **ephemeral** nodes named after their container ID: when one goes away (scaled down, recreated) the hub removes it and its slots after `EPHEMERAL_GRACE_S` (120 s), so recreated runners don't pile up as offline nodes.
* To use an existing MySQL server instead, leave out `docker-compose.mysql.yaml` and set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` in `.env`. With `docker-compose.mysql.yaml` the `MYSQL_*` variables set the credentials (default `orchestrator` / `orchestrator`) and `DB_*` is ignored.
* The hub, the checks and the UI all create the tables from `src/schema.sql` on startup; nothing has to be seeded.
* To run the published images instead of building them, add `-f docker-compose.ghcr.yaml` and use `pull` + `up` without `--build` (see [Published images](#published-images)).

## Published images

CI builds every image and publishes it to GitHub Container Registry (`.github/workflows/docker-images.yml`):

| Image | Is |
| --- | --- |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/hub` | the gateway hub (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/runner` | a Linux runner node: the agent and Chromium, Firefox and WebKit (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/grid-ui` | the Grid UI (amd64, arm64) |
| `ghcr.io/sam-h101/playwright-remote-orchestrator/checks` | the image `main.py` checks run in (amd64) |

Tags: a version such as `1.2.0` for each [release](#releases), `1.2` for the newest 1.2.x, `latest` for the newest release, and `edge` and `sha-<commit>` for every change on master that touches the images. The packages are public: pulling needs no login. Each one's page is under **Packages** on the repository page.

```
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml pull
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml up -d --scale runner=2
```

`IMAGE_TAG` in `.env` pins the version (default `latest`).

### Releases

Every merge to master is a release (`.github/workflows/release.yml`):
- The next version is tagged `vX.Y.Z`.
- A GitHub Release lists the pull requests merged since the last one.
- The images above are published as `X.Y.Z`, `X.Y` and `latest`.

A pull request's label picks the version:
- `release:minor` or `release:major`; otherwise it's a patch.
- `release:skip` makes no release, for changes that ship nothing (docs, CI). What it changed goes out with the next release. If an earlier merge is still waiting to go out, it releases that.

The labels exist in the repository; any other label counts as a patch. Each pull request's **next version** check says, in its summary, what merging it would release and which merges it counted. That's as of its last push or label change: re-run it after other merges.

A release takes master as it is when its run starts, so merges that land while a release is running go out together in the next one, and a commit that a release already contains is never released again. If a release's images fail to build, re-run the failed jobs. `latest` only ever moves to the newest release, and `X.Y` to the newest of that X.Y, so re-running an older release can't take them back.

The Windows runner's binaries are released by hand: push a `pwrunner/vX.Y.Z` tag, and `.github/workflows/runner-go.yml` builds and attaches them. That tag publishes the images too, so give it the next free version number. The automatic releases count on from it.

Dependabot (`.github/dependabot.yml`) opens update pull requests every Monday at 06:00 UTC, for the gateway's npm packages, the runner's Go modules, the Python requirements, Playwright, the images' base images and the workflows' actions. Merging one releases a patch. Its GitHub Actions updates come labelled `release:skip`, and its Playwright updates `release:minor` (see [Upgrading Playwright](#upgrading-playwright)). An update to the hub, runner or checks image is tested by building the images and running a check through the hub and a runner with them. That check doesn't include the Grid UI, so try a Grid UI update before merging it.

## Connecting tests

Every test connects to the same address, the hub, and the hub picks the runner:

```js
const HUB = 'ws://<orchestrator-host>:4500';
const browser = await chromium.connect(`${HUB}/connect?gateway_token=${process.env.GATEWAY_CLIENT_TOKEN}`);
```

```python
import os
from playwright.sync_api import sync_playwright

HUB = "ws://<orchestrator-host>:4500"
with sync_playwright() as p:
    browser = p.firefox.connect(f"{HUB}/connect?gateway_token={os.environ['GATEWAY_CLIENT_TOKEN']}")
```

The same URL works for `chromium`, `firefox` and `webkit`: launch the browser you want and the hub sends you to a node that has it. The token can go in an `x-gateway-token` header instead (`connect(url, headers={"x-gateway-token": token})`). The checks `main.py` runs get this URL, token included, in `PW_WS_ENDPOINT` (see [Writing a check](#writing-a-check)).

* **Browser.** `browserType.connect` sends the browser type in the `x-playwright-browser` header (from JS and Python alike); a `?browser=` query parameter wins over it, as it does in `run-server`. The hub only picks slots on nodes that list that browser (`BROWSERS`, `--browsers` or `-Browsers` on the node; the runner image lists `chromium,firefox,webkit`). A node that lists a Chromium channel (`chrome`, `msedge`, ...) serves `chromium` too. A client that names no browser can get any node.
* **Version.** Playwright's `User-Agent` carries its version (`Playwright/1.64.1 (...)`). The hub only picks nodes on the same major.minor version, the same check `run-server` would fail with HTTP 428. A node whose version can't be read, and a static host, take any client; if its `run-server` then refuses the client's version (HTTP 428), the client gets that 428 at once. It is the client's version that is wrong, so no slot is counted as failing and no other slot is tried.
* **Which slot.** Among the free slots that fit, the hub takes the nodes that offer the fewest other browsers (so a chromium session goes to a chromium-only node before it takes a slot a firefox or webkit client could have used), then the node with the largest share of its slots free (random among equals), then a random free slot on it. [Static hosts](#migrating-from-the-per-port-setup) list no browsers and take any client, but only when no node has a free slot.
* **One node.** `?node=<name>` (any case) keeps the client on that node or static host (`GATEWAY_NODE` for the checks `main.py` runs). The slot is always the hub's choice; there is no way to ask for one.
* **Waiting.** While every slot that fits is busy, the client waits: the hub holds its WebSocket handshake and hands the next slot that fits to the oldest waiting client. After `GATEWAY_QUEUE_TIMEOUT_S` (60 s) it gets a 503 that says what those slots are doing, e.g. `no free firefox slot within 60s: 4 matching slots, all busy`. At most `GATEWAY_QUEUE_MAX` (200) clients wait at once. A client that disconnects leaves the queue. A request that nothing connected or known to the hub could ever serve is refused at once instead: an unknown node (404), a browser no node lists (503), a Playwright version no node runs (428). A known node that is offline is waited for. Playwright's `connect` waits as long as it takes by default; if you pass a `timeout`, make it longer than `GATEWAY_QUEUE_TIMEOUT_S` plus `GATEWAY_CONNECT_ATTEMPTS` x `OPEN_TIMEOUT_S` (the example check uses 180 s), or the client gives up before the hub answers.
* **Failover.** When the chosen slot can't open the session (its run-server refuses it for a reason other than the client's version, the agent reports a failure, no answer within `OPEN_TIMEOUT_S`, a static host refuses or can't be reached), that counts as a connect failure on the slot ([Slot failures](#slot-failures)), the slot is released, and the client is tried on another slot, up to `GATEWAY_CONNECT_ATTEMPTS` (3) slots. Then it gets the last error.
* **Claims.** The hub claims the slot in `portUsage` the way every claimer does (`inUse` 0 to 1 with a conditional update, and `LastRunDate`), so nothing double-books it, and marks the row `claimedBy = hub:<hub>:<n>`. It releases the slot when either side closes, when the open fails, when the client leaves early and when the hub stops. A hub's claims last as long as its sessions, however long they run: the checks' stale-claim sweep leaves them alone. Each hub keeps a lease, its row in the `hubs` table, renewed every `GATEWAY_HUB_LEASE_S` / 3 (30 s); a hub frees the claims of any hub whose lease ran out (one that crashed), when it starts and every 30 s, so a crash leaves nothing claimed for long. Every 30 s the hub also retries releases MySQL refused, and checks its live claims: a live session's slot released under it (from the Grid UI, say) is claimed again, and a slot another claimer took over is routed to nobody else until that session ends. If it can't reach MySQL to pick or claim a slot, the client gets a 503.
* **Several hubs** may share one database (a second compose project, say): each claims atomically, frees only its own claims and those of hubs whose lease ran out, and logs a warning naming the others when it starts. Each routes only to the nodes connected to it.
* **Tokens.** A managed client token revoked or expired while its client waits in the queue gets that client a 401 instead of a session. A client token whose [token scope](#access-tokens) leaves out opening browser sessions gets a 403, `gateway token is not allowed to open browser sessions (its scope leaves that out)`, and `main.py`'s log line for it says to use a token whose scope includes opening sessions.
* The hub logs one line per routing decision (`client #12 10.0.0.7 -> runner-7f3a9c/3 (firefox, waited 2.1s)`; the number tells clients at the same address apart) and per client that gave up waiting; never a token.
* The old `/connect/<node>/<slot>` endpoints are gone: they answer 503 with a message pointing to `/connect`. (503 rather than 410 so checks of the previous release, which count anything but 401, 403 and 503 against the slot they claimed, don't fail healthy slots while they are being replaced; see [Migrating](#migrating-from-the-per-port-setup).)

## Adding a Windows host

`pwrunner.exe` turns a Windows machine into a runner node. It is one file: it carries a private Node 22 and `playwright-core` (hash-pinned), installs itself as a Windows service, and needs no Node, npm, winget or repository checkout on the host. Download `pwrunner-<version>-windows-amd64.exe` (or `-arm64`) from the [releases](../../releases) and check it against `SHA256SUMS.txt`, then in an elevated PowerShell:

```powershell
Get-Content .\agent-token.txt | .\pwrunner.exe install --hub-url ws://<orchestrator-host>:4500 --token-stdin --name win-07 --slots 4
```

The agent token is one made for this host on the Grid UI's Access page (see [Access tokens](#access-tokens)), or `GATEWAY_AGENT_TOKEN`. `--token-file <path>` works too; `--token <value>` is accepted but puts the token on a command line.

* The host only needs outbound access to the hub's port. No firewall rule, no inbound port and no `database.yaml` entry: the node registers itself as `--name` (default: the host name).
* Installs into `C:\PlaywrightBackend` (`--install-dir`): `pwrunner.exe`, the private Node in `runtime\`, `playwright-core` in `app\`, and only the browsers you list (`--browsers chromium` by default) in `browsers\`. Keep the folder short: Windows can't start a browser whose path is over 260 characters. `--offline-dir <dir>` installs from the release's `pwrunner-runtime-<version>-windows-amd64.zip` contents without downloading; `--browsers-from <dir>` copies browsers instead of downloading them.
* Runs the agent as the Windows service `PlaywrightBackend` (`--service-name`), LocalSystem, automatic start, restarted by the service manager if it exits. `--no-service` runs it in the background for the current session instead and needs no elevation.
* Re-running keeps the installed settings (hub, token, node name, slots, base port, browsers, report URL, service name) unless you pass them again, and skips the runtime when it is already at the pinned version. `C:\PlaywrightBackend\pwrunner.exe install` with no flags re-applies the install. `--playwright-version` is not kept, so a newer exe upgrades. If an upgrade fails, `app\` and `runtime\` are rolled back and the previous backend is restarted.
* Elevated, the install folder is writable only by SYSTEM and Administrators. A folder other users could change (for example from the older installer) is reset and gets a fresh `app\` and `browsers\`. The token is stored in `agent.token`, readable only by SYSTEM and Administrators, and never appears on a command line, in a log or in a JSON file.
* An install made by the older PowerShell installer is migrated in place: its scheduled task and supervisor are removed and its scripts are moved to `C:\PlaywrightBackend\legacy\`.
* Waits for the node to register and reports the result: a console summary, `status.json`, an optional JSON POST to `--report-url`, and the exit code (`0` registered and every slot healthy, `2` not registered or some slots unhealthy, `1` failed).

Afterwards:

| Command | Does |
| --- | --- |
| `pwrunner status [--json] [--report-url <url>] [--health-timeout <sec>]` | Node health with the same exit codes (`1` = not installed) |
| `pwrunner start` / `stop` / `restart` | Controls the service |
| `pwrunner token set --stdin` / `set --file <path>` / `clear` / `show-prefix` | Rotates the token and restarts the service; `show-prefix` prints only a managed token's 12-character label |
| `pwrunner selfcheck [--quick]` | Checks the folder and token permissions, the runtime, the slot ports, the hub and the Node end-of-life date |
| `pwrunner uninstall [--remove-files] [--yes]` | Removes the service; `--remove-files` also deletes the folder, browsers included |

Logs are in `C:\PlaywrightBackend\logs` (`install.log`, `service.log`, `agent.log`, rotated at 10 MiB, and `slot-<i>.log`); the service also writes start, stop and repeated-crash events to the Windows Event Log under `PlaywrightBackend`.

To go back to the PowerShell installer, run `pwrunner uninstall`, then `installer\windows\install-backend.ps1` from a checkout as before (see `installer/windows/DEPRECATED.md`).

## Adding a Linux host with docker

The published runner image works on any docker host that can reach the hub's port; nothing has to be checked out or built:

```
read -rsp 'Agent token: ' T && printf 'GATEWAY_AGENT_TOKEN=%s\n' "$T" > runner.env && chmod 600 runner.env && unset T; echo
docker run -d --name pw-runner --restart unless-stopped --init --shm-size 1g --env-file runner.env \
  -e HUB_URL=ws://<orchestrator-host>:4500 -e NODE_NAME=<stable-name> -e EPHEMERAL=0 -e SLOTS=4 \
  ghcr.io/sam-h101/playwright-remote-orchestrator/runner:latest
```

Use a version tag (`:1.2.0`) instead of `:latest` to pin it; the runner's Playwright version must match your checks' minor version. To build it yourself instead: `docker build -f docker/runner/Dockerfile -t playwright_orchestrator_runner .` from a checkout, and run that image.

`NODE_NAME` keeps the node's name when the container is recreated (the default is the container ID), and `EPHEMERAL=0` makes the hub show it OFFLINE while it is down instead of removing it. Keep `--init` (reaps browser processes) and `--shm-size` (Chromium crashes with docker's 64 MB `/dev/shm`).

## Adding a Linux or macOS host (no docker)

playwright-core (the version `runner/playwright` pins) and the browsers go outside the checkout, in fixed folders, so `npm ci` after a `git pull` keeps them and the service user finds the browsers:

```
sudo git clone <this repo> /opt/playwright-remote-orchestrator
cd /opt/playwright-remote-orchestrator/gateway && sudo npm ci --omit=dev
sudo mkdir -p /opt/playwright-core && sudo cp /opt/playwright-remote-orchestrator/runner/playwright/package*.json /opt/playwright-core/
cd /opt/playwright-core && sudo npm ci --omit=dev
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright node /opt/playwright-core/node_modules/playwright-core/cli.js install --with-deps chromium
```

On macOS drop `--with-deps`. Then run the agent at boot; it reconnects to the hub by itself. With systemd, put `GATEWAY_AGENT_TOKEN=<token>` in `/etc/playwright-agent.env` (mode 600), create a user (`sudo useradd --system --create-home pwagent`) and add `/etc/systemd/system/playwright-agent.service`:

```ini
[Unit]
Description=Playwright runner agent
Wants=network-online.target
After=network-online.target

[Service]
User=pwagent
WorkingDirectory=/opt/playwright-remote-orchestrator/gateway
EnvironmentFile=/etc/playwright-agent.env
Environment=HUB_URL=ws://<orchestrator-host>:4500 SLOTS=4
Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
Environment=PLAYWRIGHT_CLI=/opt/playwright-core/node_modules/playwright-core/cli.js
ExecStart=/usr/bin/env node agent.js
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now playwright-agent`. On macOS use a launchd job with the same environment. `node agent.js --help` lists the options (see [Agent](#agent) below). A slot's health is only whether its run-server answers, so a node whose browsers are missing shows healthy and every session fails: keep `PLAYWRIGHT_BROWSERS_PATH` the same for the install and the service.

## Node names

* Node names are case-insensitive and stored lower-case (`Runner-A` registers as `runner-a`): `[A-Za-z0-9._-]`, up to 128 characters.
* A name must be unique. A second agent with the same name takes over the node and the first one is disconnected (and waits 30 s before trying again). A node that registered with a token [bound to its name](#access-tokens) can only be taken over by a token bound to the same name.
* Node names and the `server` names under `hosts:` in `src/database.yaml` share one namespace. `main.py` ignores a `hosts:` entry whose name is a registered node (with a warning to remove it). If a static host is listed before an agent registers under its name, the node takes over that host's slot rows, so give agents and static hosts different names.

## Access tokens

Agents need an **agent token** to register and checks need a **client token** to open a session. The hub accepts two sources for each:

* **Bootstrap tokens**: `GATEWAY_AGENT_TOKEN` and `GATEWAY_CLIENT_TOKEN` on the hub. They work even while MySQL is down, and the compose runners and example check use them, so compose requires both. They can't be revoked from the UI: to rotate one, change it in `.env` and run the same `docker compose ... up -d` again, which recreates the hub, the runners, the checks and the Grid UI (which takes `GATEWAY_CLIENT_TOKEN` for [MFA codes](#mfa-secrets) and [saved sign-ins](#saved-sign-ins)) with the new value. A hub run outside compose may leave them unset and use managed tokens only.
* **Managed tokens**: made on the Grid UI's **Access** page, by whoever has [`GRID_ADMIN_TOKEN`](#security) or, with single sign-on, by people whose [roles](#users-and-roles) allow it. Agent tokens start with `pwga_`, client tokens with `pwgc_`. MySQL only keeps their sha256 and first 12 characters, so the token is shown once, when it is created, with ready-to-paste commands for that kind. Use it like the env token: `-AgentToken`, `GATEWAY_AGENT_TOKEN` or `--token` for an agent, `GATEWAY_CLIENT_TOKEN` for a check. A client token can be made with a **token scope** that limits what it may do (see [Token scopes](#token-scopes)); a token without one does everything its owner may.

On the Access page:

* **New token** takes a name, the kind, an expiry (1-3650 days, or none) and, for an agent token, an optional node name. A token **bound** to a node can only register that name (anything else is closed with 4403). Once the node has registered with it, the name is reserved for it while it is valid: no other token can take the node over, not even the bootstrap token. Give each runner host its own bound token, so a leaked one can't receive other nodes' sessions. For a client token, **What it may do** is **Everything** (whatever its owner may do) or **Only what you pick**, which gives the token a token scope: any of Open browser sessions, See the grid, Use MFA secrets, Use saved sign-ins and Add saved sign-ins (which ticks Use saved sign-ins too). It can't be changed afterwards.
* **Rotate**: create a new token (bound to the same node, for a bound one; with the same scope, for a client token that has one), put it on the host (re-run the Windows installer with `-AgentToken`, or change the env file and restart the agent), wait until the node shows up under the new token, then revoke the old one.
* **Revoke**: new connections with the token are refused within `TOKEN_CACHE_S` (10 s), and the hub closes the agents still connected with it within `TOKEN_RECHECK_S` (15 s), with 4401. Those agents keep retrying until they get a valid token. Browser sessions that are already open with a revoked client token run to the end; new ones get 401. The confirmation names the nodes that will be disconnected.
* **Expiry** works the same way: an expired token is refused, and connected agents using it are closed at the next re-check.
* **Delete** removes a revoked or expired token from the list. Active tokens must be revoked first.

Once a managed token of a kind exists, the hub requires a valid token of that kind even without the env token. So creating the first agent token on a hub without `GATEWAY_AGENT_TOKEN` disconnects agents that have no token within `TOKEN_RECHECK_S`. It stays that way after every managed token of the kind is revoked or deleted, until the hub restarts with no token of that kind left in the table. Set `GATEWAY_ALLOW_OPEN_AGENTS` / `GATEWAY_ALLOW_OPEN_CLIENTS` on the hub to let tokenless agents or clients back in as soon as every token of the kind is revoked. Compose always sets both env tokens, so none of this changes anything there.

If the hub can't reach MySQL, the bootstrap tokens keep working and managed tokens are refused until it can check them again: agents are closed with 1013 and retry, and checks get 503.

### Token scopes

A token scope limits what one client token may do, so a token that leaks from a pipeline is worth less. It is not single sign-on's scopes (`GRID_OIDC_SCOPES`), which are something else. A scope is a list of these ids:

| Scope id | On the Access page | Lets the token | Enforced by |
| --- | --- | --- | --- |
| `sessions.open` | Open browser sessions | Open a browser session at the hub's `/connect` | the hub |
| `grid.view` | See the grid | Read `/status` (`/api/status`) and `/api/events` where reading needs sign-in | the Grid UI |
| `mfa.use` | Use MFA secrets | Get [MFA codes](#mfa-secrets) | the Grid UI |
| `signins.use` | Use saved sign-ins | Check out, save and release [saved sign-ins](#saved-sign-ins) | the Grid UI |
| `signins.add` | Add saved sign-ins | Use a saved sign-in's name for the first time. Needs `signins.use` in the scope too | the Grid UI |

* **A scope only takes away.** On every request the token may do what its owner may do at that moment, less what its scope leaves out. It reaches the same MFA secrets and saved sign-ins as its owner, never more: `mfa.use` in the scope of a token whose owner's roles don't include “Use MFA secrets” gets no codes.
* **It is chosen when the token is made and can't be changed.** To change what a token may do, make another and revoke the old one. The audit log records the scope with the token. A token [given to a person](#users-and-roles) keeps its scope.
* **Only managed client tokens have one.** Agent tokens and the bootstrap tokens (`GATEWAY_AGENT_TOKEN`, `GATEWAY_CLIENT_TOKEN`) have no scope, and neither do the client tokens made before scopes or with **Everything**.
* **Making one from a script** is `POST /api/tokens/scoped` (see [Grid UI](#grid-ui)), not `POST /api/tokens`. It needs “Make client tokens”, and the grid's first client token needs “Every access token” whether it has a scope or not (see [Users and roles](#users-and-roles)).
* **Opening sessions is not something roles give.** Any valid client token whose scope allows it (or that has no scope) opens sessions, as before: also one whose owner is disabled, until it is revoked. If the only client tokens on record have scopes without `sessions.open`, the hub still demands a token, and only the bootstrap token opens sessions.
* **`grid.view` matters only where reading the grid needs sign-in.** Where `GRID_UI_READS` leaves reading open, anyone reads the grid without a token.
* **What a refused check sees.** From the hub, HTTP 403 `gateway token is not allowed to open browser sessions (its scope leaves that out)`; `main.py` logs it with `GATEWAY_CLIENT_TOKEN's scope leaves out opening browser sessions: use a token whose scope includes it`. Not every Playwright client shows the hub's sentence, but the 403 is always there: at `/connect`, a 403 means the scope and a 401 any other problem with the token. From the Grid UI, HTTP 403 `{"error", "out_of_scope": true, "scope": "<the scope id that is missing>"}`, which `src/mfa.py` and `src/signin.py` raise as their errors. The Access page lists each scoped token with what it may do (`may only see the grid`), and hands out no hub address for one that can't open sessions.
* **Update the hub and the Grid UI first.** Scoped tokens need both at this version or later, on every instance. An older hub or Grid UI answers 401 to a scoped token and never lets one in, and an older Grid UI counts those 401s towards the guessing limit. An older Grid UI can't make one either: it answers 404 to `POST /api/tokens/scoped` and makes nothing, and the page says `The Grid UI that answered is from before token scopes, so it made no token. Try again when the update has finished.` The Grid UI can't tell which version of the hub is running, so finish updating both before handing out scoped tokens, or the checks using them fail until the update is done (and after a rollback, until it is undone).
* **On an existing database** the hub adds the `scope` column to `accessTokens` when it starts. Its database user needs the right to alter that table for this, once; without it the hub stops at startup, so either grant it or run `ALTER TABLE accessTokens ADD COLUMN scope VARCHAR(255) NULL` by hand before updating. Until then the Grid UI answers 503 to a request for a scoped token: `This database's accessTokens table has no scope column yet, so a token can't be given a scope. The gateway hub adds the column when it starts: update the hub, start it, and make the token again.`
* **`May do nothing: its scope was lost.`** A token listed that way was made with a scope that is no longer in the database (the column was dropped or the row changed by hand). It is refused everywhere rather than taken for a token with no limit. Revoke it and make another.

## Ports

| Host | Inbound | Outbound |
| --- | --- | --- |
| Orchestrator (compose) | `GATEWAY_PORT` (4500) from your runner hosts. The Grid UI listens on 127.0.0.1:4444 only. | the sites your checks test |
| Runner host (container, Windows or Linux/macOS agent) | none | the hub's port, and the sites your checks test |
| Legacy static host (see [Migrating](#migrating-from-the-per-port-setup)) | every run-server port, from the hub | the sites your checks test |

Run-servers on agent hosts only listen on 127.0.0.1.

## Security

* Agent tokens decide who can register a node. Anyone with an unbound one (or `GATEWAY_AGENT_TOKEN`) can register under an existing node's name and receive that node's browser sessions, unless that node uses a [bound token](#access-tokens). Treat them as secrets.
* Client tokens decide who can open a browser on your runners. Without them the hub would be an open browser proxy into your runners' network. Checks send theirs as `?gateway_token=`; other clients can also use an `x-gateway-token` header. Give each pipeline the narrowest token that works: a leaked client token whose [token scope](#token-scopes) is only seeing the grid can't open a browser, get an MFA code or use a saved sign-in.
* The hub logs a managed token only by its first 12 characters and id, and never logs the bootstrap tokens.
* The hub port speaks plain `ws://` (tokens included). Across any network you don't trust, put a TLS-terminating reverse proxy (nginx, Caddy, Traefik, ...) in front of it, use `wss://` URLs for the agents and `GATEWAY_URL`, and allow WebSocket upgrades and long-lived connections (the agent reconnects if it hears nothing for 45 s). Where you can, also limit the hub port to your runner hosts' addresses.
* `GET /healthz` and `GET /api/nodes` on the hub need no token; they show node names, versions and IP addresses.
* The Grid UI is published on 127.0.0.1 only, and with neither `GRID_ADMIN_TOKEN` nor single sign-on set up anyone who can open it can release slots and forget nodes. Before exposing it more widely, set up [single sign-on](#users-and-roles), so people sign in as themselves and do what their roles allow, or at least set `GRID_ADMIN_TOKEN` (the admin sign-in, which stays as the break-glass way in once single sign-on is on). The Access, MFA and Admin pages need one of them in any case: without either the UI refuses to list, create or revoke tokens. Whoever has the admin token can create agent tokens, so guard it like `GATEWAY_AGENT_TOKEN`. Compose passes it to the UI but doesn't require it, because the rest of the UI works without it and the Access page stays locked rather than open. The UI refuses cross-site actions, and accepts actions only through an IP address, `localhost`, the machine's own host names, the host of `GRID_UI_PUBLIC_URL` or a name in `GRID_UI_ALLOWED_HOSTS`: list the DNS alias or reverse-proxy name you reach it by.
* [MFA secrets](#mfa-secrets) go into the Grid UI and never come out: the seeds are stored encrypted under `GRID_SECRETS_KEY`, and the UI answers with codes and metadata only, never cached, and logs neither seeds nor codes. A client token gets codes for the secrets its owner can use (legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get them all), so a client token also works as the second factor of those accounts: store only the accounts your checks sign in with, and treat client tokens accordingly. Keep `GRID_SECRETS_KEY` out of the database's backups; a dump without it holds no usable seed. `POST /api/mfa/code` is the one action exempt from the cross-site and host-name rules: it takes nothing but a client token in the `Authorization` header, which a page on another site can't send, and checks reach the UI by its compose name.
* [Saved sign-ins](#saved-sign-ins) are kept the same way and are more sensitive still: a saved state is the account's live session cookies, so a check that holds one is signed in as the account with no password and no code. They are encrypted under `GRID_SECRETS_KEY`, the page shows only the cookies' domains and counts (never a value), and a client token checks out the saved sign-ins its owner's checks may use, as it gets the MFA codes its owner may use — so the same caution applies, more so. Only the owner's checks sign in fresh for a sign-in and save it again; checks of the people and roles it is shared with reuse what was saved (see [Saved sign-ins](#saved-sign-ins)). The checks' `POST /api/signins/checkout`, `/api/signins/save` and `/api/signins/release` are exempt from the cross-site and host-name rules like `/api/mfa/code`, and for the same reason. **Forget** a sign-in on the MFA page to stop checks reusing it. Forget only drops the grid's copy and doesn't sign the cookies out: to cut off a copy already checked out (after a client token leaked, say), revoke the account's sessions at the site (in Entra ID, **Revoke sessions** on the user) or reset its password.

## Grid UI

A management console lives in `ui/` (Flask, plain ES modules, no build step). It reads the same MySQL database the checks use.

* **Overview**: one verdict (`Healthy`, `3 problems need you`, `No free slots`, ...), a list of what needs you (failing and unhealthy slots, stale sessions, offline nodes) with one action each, **Recent runs** (a swimlane per node over the last 1, 5 or 15 minutes, with bursts flagged, and a 15-minute strip of runs finished and busy slots), and the slot matrix: one row per node, one glyph-coded cell per slot, labelled with its slot number (or port, for static hosts). Select any node, slot or session to open the **inspector** on the right: what its state means in plain words, who claimed it (**Claimed by**: the hub or another client), failures against `SLOT_FAIL_THRESHOLD`, the next retry, recent events, a **Connect to the grid** block with the hub's one `/connect` address as Playwright JS or Python, PowerShell or bash (with **Only on <node>** to add `?node=`), and its actions. No slot has an address of its own: the hub picks the slot.
* **Activity**: every change recorded in the last 15 minutes, filterable by node, slot, kind and time; **Copy as JSON** copies the filtered events. An admin action is one row: the change it caused, with who did it.
* **Sessions**: running claims, stale first; release one from its inspector. A session the hub routed is stale only once that hub has stopped renewing its lease (nothing holds the claim then); while the hub runs it is a running session however long it lasts, and releasing it doesn't end it (the hub claims the slot again). Any other claim is stale after `GRID_STALE_AFTER`.
* **Access**: the hub's managed [access tokens](#access-tokens), plus **Add a host**, which makes an agent token bound to a node name, shows the one install command for the chosen platform and waits for the host to register. It warns before making a second token for a node that already has one. With single sign-on each token shows its owner, whom a client token's checks act as.
* **MFA & sign-ins**: the authenticator seeds checks get sign-in codes from (see [MFA secrets](#mfa-secrets)), with the codes handed out recently and to whom, and the [saved sign-ins](#saved-sign-ins) checks reuse so most runs skip the prompt, with **Forget** to make the next run sign in again. With single sign-on each one shows its owner, and **Share** lets other people and roles use it.
* **Admin**: people who signed in, their roles and sessions, custom roles, group mappings, the audit log and the single sign-on settings (`g u`; on a phone, in the account menu). See [Users and roles](#users-and-roles). With only the admin token set, it says how to set single sign-on up.
* Every view, filter and object has an address (`#/activity?node=...`, `#/slot/12`, `#/node/<name>`, `#/session/12`). Press `/` or Ctrl K to jump to anything, `?` for the other shortcuts.

**What the activity can and can't see.** The Grid UI server records it (`ui/recorder.py`): it compares snapshots of the database, on every page poll and, when no page is open, every `GRID_RECORDER_INTERVAL` seconds (2 by default; 0 turns the background recording off), and keeps the last 15 minutes (at most 500 events) in memory. A tab opened after a failed CI run therefore still shows the runs and failures before it; the recording starts when the UI server does, and the page says since when. Claim and release both stamp `LastRunDate`, so a run that starts and ends between two snapshots still shows, as *finished between updates* with its end time but no start (drawn as "approx."). It also sees connect failures, failure resets, health changes and nodes coming and going. It can't see which check ran or why a connection failed (no error text is stored). Admin actions are attributed to who signed in, and to "you" in the tab that did them. With single sign-on that is a person's name, so where reading is open, callers who aren't signed in get the actions without who took them, and the page forgets who acted when you sign out. With several workers (gunicorn) each keeps its own recording; events are identified by database values, so the page merges them. Visible pages poll `/api/status` every `GRID_UI_POLL_MS` (1500 by default; every 12 s in a background tab) and never hold a connection open, so it is safe behind gunicorn's sync workers.

**Hub address.** Copyable commands use `GATEWAY_PUBLIC_URL` when it is set. Otherwise they use `GATEWAY_URL`'s scheme and port with the host the page was opened through, since `ws://hub:4500` only resolves inside compose. The **Hub address** field next to any command overrides it for this browser. Snippets read the client token from `GATEWAY_CLIENT_TOKEN` in the environment and never contain it.

**Signing in.** Once `GRID_ADMIN_TOKEN` or single sign-on is set up, grid actions (release, reset, forget) and the Access, MFA and Admin pages need someone signed in, with a [role](#users-and-roles) that allows them. Without either, the grid actions are open to anyone who can load the page (the header says **Unprotected**), and token, MFA and people management are refused.

* **Single sign-on.** With `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_UI_PUBLIC_URL` and one way for the Grid UI to prove it is the client (a client secret, or a token the platform issues, so that no secret exists at all: on AKS or any Kubernetes, on App Service, Functions, Container Apps or an Azure VM with a managed identity, or from any token file: see [without a client secret](#single-sign-on-without-a-client-secret)) set, people sign in through your OpenID Connect identity provider (Entra ID, Google, Okta, Keycloak or any other that follows the standard; see the [recipes](#identity-provider-recipes)). The Grid UI is a confidential client and uses the authorization code flow with PKCE on the server: the browser goes to the provider and back to `<GRID_UI_PUBLIC_URL>auth/callback`, and the Grid UI redeems the code itself. Exactly one way of proving it is the client is used; if more than one is set, or the one set can't be used, nobody signs in with single sign-on until that is fixed, the Grid UI says why, and it never tries the other. It uses only the ID token, once its signature, issuer, audience, times and nonce check out: the provider's access and refresh tokens are dropped at once, and its userinfo endpoint is never asked. Setting `GRID_OIDC_ISSUER` protects the grid at once, even while the rest is wrong: the sign-in sheet then says single sign-on isn't set up correctly, and the Grid UI's log, the Admin page's **Single sign-on** tab and the admin token's holder get what to fix.
* **The public address.** Sign-in always happens at `GRID_UI_PUBLIC_URL`. Started at any other address (`http://localhost:4444`, say), it is sent there first, because the cookie that carries a sign-in through the provider belongs to one address. Behind a proxy that rewrites the `Host` header, have it pass the header on.
* **The admin token** (`GRID_ADMIN_TOKEN`) is the break-glass sign-in: it holds every permission, works without the database, and the account chip's **Sign in** asks for it once per browser tab (with single sign-on, under **Use the admin token instead**). Signing in with it on the page is recorded in the audit log (`auth.breakglass`, at most once an hour per address and worker), and every change made with it is recorded as the admin token's.
* **Sessions.** Signing in with single sign-on sets a session cookie that lasts `GRID_SESSION_HOURS` (12) and ends after `GRID_SESSION_IDLE_MINUTES` (120) unused. An open page keeps polling, so only closed or sleeping tabs go idle. The database keeps only the cookie's sha256. **Sign out** ends this browser's session and **Sign out everywhere** every one of yours; on the Admin page, people who manage people end those of anyone whose permissions are all within theirs (see [Users and roles](#users-and-roles)), and disabling someone ends theirs. A session that stops working (expired, idle, ended, or its user disabled) stops at the next request, and the page says why. So does every session started through another issuer once `GRID_OIDC_ISSUER` changes: nobody signs in through that one any more, and the Admin page no longer counts or lists those sessions. What someone may do is worked out again on every request, so a change to their roles applies at once.
* **Guessing.** After 20 wrong admin tokens (or client tokens, from checks) from one address within 5 minutes, the UI answers 429 to that address until the oldest ages out. Session cookies can't be guessed and never count. At most 60 sign-ins may start from one address in 10 minutes (an IPv6 address counts as its /64). Both are counted in memory per process, so with gunicorn's two workers the limits are up to twice as high. Behind a reverse proxy, set `GRID_UI_PROXY_HOPS`, or everyone shares the proxy's address for both limits (and in sessions and the audit log). An answer to a request whose admin token was wrong carries `X-Grid-Token-Refused: 1`, even when a session cookie or open reading let the request through, and the page then forgets the token it held: a tab still holding one from before `GRID_ADMIN_TOKEN` changed would otherwise lock its address out with its polls.
* **Another provider.** The code keeps sign-in in two places so another provider can be added without touching the views: `ui/auth.py` (a `Principal(name, method, user_id, permissions, role_ids, email, scope)`, the `PROVIDERS` list, and the gates every route uses: `permission_required(*perms)`, which needs any one of them (none: anyone signed in), and `admin_required()`, which needs every permission) and `ui/static/js/auth.js` (state, credentials, the sign-in sheet, sign-out). A new provider implements `configured()`, `attempted(request)`, `authenticate(request)` (returning a `Principal` or `None`) and `describe()` (`{id, kind, label}`), is appended to `PROVIDERS`, and is taught to `auth.js`, whose sheet knows the kinds `secret` (kept for the tab) and `redirect` (a link to the identity provider). A `Principal` that says nothing about permissions holds them all, as every principal did before roles. `scope`, the last field, is only ever set for a client token with a [token scope](#token-scopes): a sign-in provider leaves it out. Set `guessable = False` on a provider whose wrong credentials can't be guesses (a session token), so they never count towards the limit, and `uses_database = True` on one whose `authenticate` asks the database, so error texts never ask it again. `GET /api/session` tells the page who is signed in, which providers to offer and what the request may do, and the account menu shows the provider's label.

**Security.** Every response carries `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`. The page loads nothing from other hosts and has no inline scripts or styles; every database value goes through one escaping template helper. Actions are still refused cross-site and through unknown host names (see [Security](#security)); the session cookie's `SameSite=Strict` is a second layer. With single sign-on, the session cookie is `HttpOnly`, `SameSite=Strict`, `Secure` whenever `GRID_UI_PUBLIC_URL` is https, and called `__Host-grid-session` when that is https with the path `/`. The login cookie that carries a sign-in through the provider is the same, but `SameSite=Lax` (a Strict one isn't sent on the provider's redirect back) and lasts 10 minutes. `__Host-` stops a sibling subdomain from setting the cookies: without it (plain http, or a public URL with a path), one could plant a login cookie and sign someone in to the grid as another person (login CSRF), and two tabs can't sign in at once. The Grid UI checks the ID token's issuer (byte for byte), audience, signature (asymmetric algorithms only, never `none` or `HS*`), times and nonce, sends PKCE, and binds each sign-in's `state` to the browser that started it; none of the provider's error texts is ever put in an answer or a redirect (only `?signin=<why>`). Every change to people, roles, tokens, MFA secrets, saved sign-ins and the grid goes in the [audit log](#users-and-roles). A database error names its file path or server only to a signed-in admin (and in the server log). Force release sends the claim the confirm described and is refused (409) if another check has claimed the slot since. The stylesheet uses CSS `light-dark()`, so the UI needs a 2024 or later browser (Chrome/Edge 123, Firefox 120, Safari 17.5).

**The log.** The Grid UI writes its log to stderr, where `docker logs`, `kubectl logs` or a cluster's log collector reads it: who signed in with single sign-on, sign-ins that were refused and why, MFA secrets added and deleted and saved sign-ins saved, forgotten and deleted (each with who did it), settings that are wrong, and database errors. `GRID_UI_LOG_LEVEL` is the least a line must be (`info`). With `GRID_UI_LOG_FORMAT=text` a line is `2026-01-31T09:15:02.417Z INFO ui.app: mfa secret 'github' added by Ada`; with `json` it is `{"time":"2026-01-31T09:15:02.417Z","level":"INFO","logger":"ui.app","message":"mfa secret 'github' added by Ada"}`, always with those four keys (the time is UTC) and with `exception` when an error's traceback comes along, so a log collector needs no pattern to take it apart. A record never takes a second line: a line break or any other control character in a value (a name from the identity provider, a traceback) is written as its escape (`\n`), so nothing sent to the UI can start a line of its own. In the image gunicorn's own lines (start-up, workers, errors) are written the same way (`ui/gunicorn_conf.py`); `python -m ui.app` writes its lines the same way too, after Flask's two start-up lines. No line holds a seed, a code, a token, a cookie's value, an authorization code or a saved sign-in's state. Of a refusal by the identity provider's token endpoint the log keeps the HTTP status, the error code and the provider's error numbers, with a hint what to check (`the identity provider's token endpoint refused a sign-in's code: HTTP 401 invalid_client (error codes 700213). AADSTS700213: ...`); never the provider's message, the client secret or a token. When the provider sends a sign-in back with an error instead of a code, the log keeps its error code and the first 200 characters of its `error_description` (`the identity provider signed nobody in: 'access_denied' 'AADSTS50105: ...`). Lines do name people, so keep the log as carefully as the audit log. Requests get no line unless `GRID_UI_ACCESS_LOG=1`: then each gets one, whatever the level, with the method, the path without its query string (a sign-in comes back from the provider with its authorization code there), the status, how long it took and the client's address (the one `GRID_UI_PROXY_HOPS` settles), and nothing from headers, cookies or bodies; a JSON line has them as `method`, `path`, `status`, `duration_ms` and `client`.

* `GET /status` (or `/api/status`): the same data as JSON, plus a `ready` flag (at least one slot can be picked), `grid_name`, `public_gateway_url` and `poll_ms`. `?soft=1` reports a database outage as `{"unavailable": true, "error": ...}` with HTTP 200 instead of a 503 (the page uses it). `?events=<epoch ms>` adds the recorded `events` observed since then and the `recorder`'s start, drops and capacity samples (`0`: all 15 minutes); `GET /api/events?since=` returns the same on its own. Where reading needs sign-in (`GRID_UI_READS`), these need “See the grid”: a session, the admin token, or a client token sent as `Authorization: Bearer <token>`, for scripts (one with a [token scope](#token-scopes) needs `grid.view` in it). With `?soft=1` that refusal is HTTP 200 `{"refused": "signedout" | "forbidden", "error"}` with no grid data, so the page's polls log no errors. With single sign-on, where reading is open, callers who aren't signed in get the events without who took each action. `POST /api/slots/<id>/release {"claimed_at": "<the session's started>"}` releases only that claim. `POST /api/nodes/forget {"host": "<name>"}` forgets a node (409 while it is online or in use). Releasing, the failure resets and forgetting need “Run the grid”.
* `GET /api/session`: `{signedIn, name, method, adminConfigured, providers, user, permissions, roles, admin, readsOpen, unavailable}` for the credentials sent. `permissions` is what the request may do now, `user` and `roles` are a signed-in person's (each role with where it came from), and `unavailable` says the database couldn't be asked about a session cookie. `GET /api/permissions` lists every permission and what the built-in roles hold (open: it says nothing about anyone). `GET /auth/login?return=<page hash>` sends the browser to the identity provider (or back to the page with `?signin=<why>` when it can't), and `GET /auth/callback` finishes the sign-in with a redirect back to the page (with `?signin=<why>` when it didn't work). `POST /api/session/signout {"everywhere"?: true}` signs out.
* `GET /api/tokens`, `POST /api/tokens {"name", "kind", "nodeName"?, "expiresInDays"?}` (answers the token once), `POST /api/tokens/<id>/revoke` and `POST /api/tokens/<id>/delete` (409 while active) need “Make client tokens”, “Add hosts” or “Every access token”, and each one shows and changes only the tokens that allows; everyone gets the counts of every kind (`kindCounts`). Making a client token needs “Make client tokens”, an agent token “Add hosts”. `POST /api/tokens` makes a token with no limit, and **now refuses (400) a body with any key other than those four**: `There is no token setting '<key>' here.`, and for `scope`, `A token with a scope is made with POST /api/tokens/scoped.` Before, unknown keys were ignored, so a script that sends an extra key must drop it. `POST /api/tokens/scoped {"name", "expiresInDays"?, "scope"}` makes a client token with a [token scope](#token-scopes) and answers like `POST /api/tokens`: `scope` is a non-empty list of scope ids (`["sessions.open", "mfa.use"]`) and must be there, `signins.add` needs `signins.use` beside it, and any other key, an unknown id or an empty list is refused (400). For example, `curl -X POST https://grid.example.com/api/tokens/scoped -H "X-Grid-Token: $GRID_ADMIN_TOKEN" -H "Content-Type: application/json" -d '{"name": "nightly pipeline", "expiresInDays": 90, "scope": ["sessions.open"]}'`. Every token in an answer has `scope`: `null` for no limit, or the sorted list. A token whose scope was lost has `"scope": []` and `"scopeLost": true`. `POST /api/tokens/<id>/owner {"userId"}` gives a client token nobody owns to someone (“Every access token”).
* `GET /api/mfa` (secrets and the last 100 hand-outs, never a seed), `GET /api/mfa/<id>/code` (the current code, using nothing up) and `POST /api/mfa/<id>/use` (a code handed out like a check's) need “Use MFA secrets”. `POST /api/mfa {"name", "secret", "issuer"?, "account"?, "algorithm"?, "digits"?, "period"?}`, `POST /api/mfa/<id>/delete` and `POST /api/mfa/<id>/shares/add` or `/shares/remove {"user": <id>}` or `{"role": <id>}` need “Add MFA secrets”, and to own the secret or hold “Every MFA secret”. `POST /api/mfa/<id>/owner {"userId": <id> | null}` (null: Admins) needs “Every MFA secret”. A secret you may not see answers as one that isn't there (404). `POST /api/mfa/code` is the checks' (see [MFA secrets](#mfa-secrets)).
* `GET /api/signins` (saved sign-ins and who used them, never a cookie) needs “Use saved sign-ins”. `POST /api/signins/<id>/forget` (drop the state so the next run signs in again), `POST /api/signins/<id>/delete` and `/shares/add` or `/shares/remove` need “Add saved sign-ins”, and to own the sign-in or hold “Every saved sign-in”. `POST /api/signins/<id>/owner` needs “Every saved sign-in”. The checks' routes are `POST /api/signins/checkout`, `POST /api/signins/save` and `POST /api/signins/release` (see [Saved sign-ins](#saved-sign-ins)).
* `GET /api/directory?q=` (the roles, and the people whose name or email holds `q`, to share with or give to) needs any of “Add MFA secrets”, “Add saved sign-ins”, “Every access token” or “See people and roles”. The Admin page's routes, `/api/admin/users`, `/api/admin/roles`, `/api/admin/groups`, `/api/admin/audit` and `/api/admin/sign-in`, need “See people and roles” to read (the audit log, “Read the audit log”) and “Manage people and roles” to change (moving someone's items also needs “Every MFA secret” and “Every saved sign-in”).
* A refusal for want of a permission is 403 `{"error", "forbidden": true, "permission"}`; one for want of sign-in is 403 `{"error", "token_required": true}`, with `reason` (`expired`, `idle`, `revoked` or `disabled`) when a session stopped working; one for a client token whose [token scope](#token-scopes) leaves the request out is 403 `{"error", "out_of_scope": true, "scope"}`, with the scope id that is missing. With only `GRID_ADMIN_TOKEN` set, every refusal is the same as before single sign-on.
* With docker it is the `grid_ui` service. Locally: `pip install -r ui/requirements.txt`, set the `DB_*` variables, then `python -m ui.app` from the repo root.
* Demo without MySQL: `python -m ui.demo demo.db --heartbeat` (add `--activity`, or `GRID_DEMO_ACTIVITY=1`, to run short bursts of checks every few seconds), then `GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app` (add `GRID_ADMIN_TOKEN=<anything>` to see the demo's tokens; with GRID_ADMIN_TOKEN set, the Admin page shows the demo's people and roles).

## Users and roles

With single sign-on (see [Signing in](#grid-ui)), everyone the identity provider signs in gets a user on the grid at their first sign-in, and may do what their roles allow. Roles come from the Admin page, from groups the provider says they are in, and from `GRID_OIDC_ADMINS`. Restrict who may sign in at the provider (assign the application only to the people who need it): anyone it signs in for this client gets a user, with no roles unless a mapping gives them some. Users are never deleted: disable them instead. The admin token, client tokens nobody owns and `GATEWAY_CLIENT_TOKEN` keep every permission.

Each permission brings what it includes:

| Permission | Lets them | Includes |
| --- | --- | --- |
| See the grid (`grid.view`) | Open the overview, activity and sessions when the grid needs sign-in to view them (`GRID_UI_READS`) | |
| Run the grid (`grid.operate`) | Release slots, reset failure counts and forget nodes | See the grid |
| Make client tokens (`tokens.own`) | Make client tokens for their own checks, and see, revoke and delete their own. A check using one acts as them | |
| Add hosts (`tokens.agents`) | Make agent tokens, and see, revoke and delete every agent token. **A host sees everything the checks sent to it do, sign-ins and codes included**: give this only to people you'd trust with those | |
| Every access token (`tokens.all`) | See, revoke and delete everyone's tokens, give a client token nobody owns to a person, and make the grid's first client token or delete its last | Make client tokens, Add hosts |
| Use MFA secrets (`mfa.use`) | See the MFA secrets they own or that are shared with them, show and use their codes, and let their checks get codes | |
| Add MFA secrets (`mfa.add`) | Add MFA secrets (they own them), and share and delete their own | Use MFA secrets |
| Every MFA secret (`mfa.all`) | See, use, share, delete and change the owner of every MFA secret | Add MFA secrets |
| Use saved sign-ins (`signins.use`) | See the saved sign-ins they own or that are shared with them, and let their checks reuse them | |
| Add saved sign-ins (`signins.add`) | Let their checks start saved sign-ins under new names (they own them), and forget, share and delete their own | Use saved sign-ins |
| Every saved sign-in (`signins.all`) | See, forget, share, delete and change the owner of every saved sign-in. Their checks still use only the ones [Saved sign-ins](#saved-sign-ins) says | Add saved sign-ins |
| See people and roles (`users.view`) | See users, roles, group mappings and the single sign-on settings | |
| Manage people and roles (`users.manage`) | Disable and enable users, end their sessions, revoke their tokens, give and take roles, make, change and delete custom roles and map groups, but only with permissions they have themselves | See people and roles |
| Read the audit log (`audit.view`) | Read every recorded change | |

The built-in roles can't be changed or deleted (**Duplicate** one to make your own):

| Role | Permissions | For |
| --- | --- | --- |
| Admin | All of them | Everything, including people, roles and every secret |
| Operator | Run the grid, Make client tokens, Add hosts, Add MFA secrets, Add saved sign-ins | Running the grid and writing checks. Adding hosts lets them run a machine that sees everything the checks sent to it do, sign-ins and codes included |
| Tester | See the grid, Make client tokens, Add MFA secrets, Add saved sign-ins | Writing checks |
| Viewer | See the grid | Seeing the grid and changing nothing. Matters only when the grid needs sign-in to view it |

* **Custom roles.** Make them on the Admin page's **Roles** tab, with any of the permissions. A name is 1-64 characters without control or invisible characters, and can't be another role's, a built-in role's or "Admins" (the owner of what nobody owns). Names are compared ignoring case and compatibility forms, so `ＱＡ leads` is taken when `qa leads` is. A change applies at once to everyone with the role, people signed in now included.
* **Nobody hands out more than they have.** Giving, taking, changing, mapping or deleting a role needs every permission it brings, and changing someone's access (their roles, disabling or enabling them, ending their sessions, revoking their tokens, moving their items) needs every permission they have, counted as if they were enabled: only a full admin enables a disabled admin again. Giving a role, or mapping a group to it, is also refused while MFA secrets or saved sign-ins you can't see are shared with it, since a role brings those too. Nobody changes their own roles, disables themselves or revokes their own tokens on the Admin page; the admin token, which is nobody, may. What the person making a change may do is worked out again as it is made, so a request from someone demoted a moment ago gets what they have now. These refusals are 403s, recorded in the audit log as `access.denied`.
* **Group mappings.** On the **Group mappings** tab, map a value of the ID token's groups claim (`GRID_OIDC_GROUPS_CLAIM`, `groups` by default) to a role: a person has the role for as long as their latest sign-in carried the value. Map group ids (an Entra group's object id), not names, which anyone who may create a group could copy. `*`, "Everyone who signs in", gives a role to everyone, which is how new people get a default role, but never a role that manages people. A change to a mapping applies at once; a change to someone's groups at the provider, at their next sign-in. A role someone has through a group can only be taken away by removing the mapping or their membership.
* **`GRID_OIDC_ADMINS`** lists people who are always admins, which is how the first admin gets in: `sub:<subject>`, or an email address. An email counts only when the ID token says `email_verified: true`, since otherwise anyone who could put that address on an account at the provider would be an admin. Entra ID doesn't verify email addresses, so list subjects there: each person's is in **Your access** (the account menu), even before they have a role, and on the Admin page.
* **The last admin.** A change that would leave no enabled user who holds every permission is refused (409): disabling, taking a role, deleting a mapping, and changing or deleting a role. Only users of the current `GRID_OIDC_ISSUER` count (those of another issuer can't sign in), and the admin token doesn't: it is for when nobody can sign in. A grid with no admin yet (being set up) can still be changed.
* **Disabling** someone ends their sessions, stops them signing in, and stops their client tokens getting anything from the Grid UI (403 `owner_disabled`). What they own stays theirs, with its shares. The hub doesn't know about users, though: their tokens still open browser sessions at `/connect` until they are revoked, so the disable dialog offers to revoke them, and the hub turns revoked ones away within `TOKEN_CACHE_S`. Taking away “Make client tokens” doesn't revoke the tokens someone made either: the Admin page counts them and offers to.
* **Owners.** With single sign-on, MFA secrets, saved sign-ins and access tokens have owners. An MFA secret belongs to whoever added it, a saved sign-in to the owner of the client token whose check first used its name, and a token to whoever made it. What was added with the admin token, by a client token nobody owns or `GATEWAY_CLIENT_TOKEN`, or before single sign-on, is **Admins'**.
* **Sharing.** **Share** an MFA secret or a saved sign-in with people or roles, and they and their checks may use it: never change, forget, delete or share it on. A share still needs the permission to use what it is, and one with a role covers its members now and later. A saved sign-in can be shared only with people and roles whose permissions are all within yours, unless you hold “Every saved sign-in” (otherwise 403, recorded as `access.denied`): their checks would run as whatever account its owner's checks saved for it. So only people with “Every saved sign-in” share a sign-in with the Admin role. The same holds whenever a check uses it: a share reaches only people whose permissions are all within its owner's, unless someone with “Every saved sign-in” made it, and then only for as long as its owner keeps at least the permissions they had when it was made (`itemShares.vouchedFor` records them). So a sign-in shared with Viewer, or with any other role an admin also has, never reaches the admin's checks unless someone with “Every saved sign-in” made it (then it reaches every member, admins included, for as long as its owner keeps the permissions they had then); someone given more than its owner after it was shared with them, or with a role that was given more, stops getting it, unless someone with “Every saved sign-in” made the share; and an admin who shared their own sign-in with the Admin role and is then made a Tester can't put an account of their choosing on the admins' checks: the share stops reaching them. The Share dialog names the people, and the roles as a whole, it no longer reaches, and says when that is because its owner may now do less than when it was shared. It doesn't name a role's members who have more through another role (admins among them): their own MFA page says their checks can't use it. Someone with “Every saved sign-in” can stop sharing and share it again to make it reach them, which records what its owner may do now.
* **Changing an owner** needs “Every MFA secret” or “Every saved sign-in”, and gives an item to a person or to Admins. A saved sign-in loses its shares, any sign-in under way and its saved state with its owner: that state is an account the old owner's checks signed in as, and the new owner's checks sign in afresh. An MFA secret keeps its shares. **Move all to…** on a person's sheet does the same for every MFA secret and saved sign-in they own; their tokens stay theirs (revoke them, or have the new owner make their own).
* **After the upgrade, every existing MFA secret and saved sign-in is Admins'.** Share each one once with the roles or people whose checks need it, or change its owner.
* **A client token acts as whoever made it**, with their permissions at the time of each request, narrowed by its [token scope](#token-scopes) when it has one: its checks get the MFA secrets and saved sign-ins that person may use. A token given to a person keeps its scope. Legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get every MFA secret, and the saved sign-ins that are Admins', or shared with Admin for as long as each one's owner keeps at least the permissions they had when it was shared (see Sharing). The Access page counts the client tokens that act as an admin: **Give to…** each one to the person whose checks use it (only ever that way round: a token someone owns never changes hands). Once people use their own tokens, `GATEWAY_CLIENT_TOKEN` can come out of `grid_ui`'s environment, but compose's checks ask the Grid UI with it: give those that use MFA codes or saved sign-ins (`poc_totp_login`, `poc_signin_reuse`) a client token of their own as `GRID_MFA_TOKEN` in `.env` first, or they get 401, and count as wrong tokens towards the guessing limit. Without it, the Access page also asks for “Every access token” to make the grid's first client token or delete its last (below). A token whose owner is disabled, or no longer known, gets nothing (403 `owner_disabled`), never an admin's access.
* **The grid's first client token, and its last,** need “Every access token” (or `GATEWAY_CLIENT_TOKEN` given to the UI): the hub turns away checks without a client token as soon as one exists, revoked or expired ones included, so making the first or deleting the last changes things for everyone's checks. Revoking is never restricted, so anyone can revoke a leaked token of theirs.
* **The audit log** (`auditLog`) records sign-ins, sign-outs and refused sign-ins, every change to people, roles, mappings, tokens, MFA secrets, saved sign-ins and the grid, codes shown or used on the page, and the refusals above, with who, from which address and when. It never holds a seed, code, state, token or cookie. The **Audit log** tab (“Read the audit log”) lists it newest first, narrowed by who and by nine categories. Rows older than `GRID_AUDIT_DAYS` (365) are deleted as new ones are added; nothing else changes or removes them, though anyone who can write the database can.
* **Personal data.** For each person: the provider's subject, their name, email and whether it is verified, their groups at their latest sign-in, and when they first signed in, last signed in and were last seen. For each session: the address and browser (user agent) it signed in from; sessions that ended over 7 days ago are deleted as new ones start. Audit rows, which hold names and addresses, are kept `GRID_AUDIT_DAYS`. A sign-in on its way through the provider is deleted after 10 minutes.
* **Tables.** Users, roles and their mappings, sessions, sign-ins on their way, owners, shares and the audit log are nine new tables in `src/schema.sql`, which the hub, the checks and the Grid UI (on MySQL, when its database user may create tables) run at startup. They are utf8mb4, so names, emails and groups in any language fit. People's names also go into existing tables (whom a code was handed to, who saved or used a sign-in), so those must be utf8mb4 too, MySQL 8's default: for a database upgraded from MySQL 5.7 as latin1, run `ALTER TABLE <table> CONVERT TO CHARACTER SET utf8mb4;` for `mfaSecrets`, `mfaIssues` and `savedSignins`. Until the new tables are there, nobody can sign in with single sign-on, and on a database that has users but no `itemOwners` table, client tokens get 503 rather than be taken for an admin's.

### Identity provider recipes

Register the Grid UI with the provider as a confidential web application, and the redirect URI `<GRID_UI_PUBLIC_URL>auth/callback` (for `GRID_UI_PUBLIC_URL=https://grid.example.com/`, `https://grid.example.com/auth/callback`). Set `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_OIDC_CLIENT_SECRET` (or, with Entra ID, none: [below](#single-sign-on-without-a-client-secret)), `GRID_UI_PUBLIC_URL` and `GRID_OIDC_ADMINS`, then:

* **Entra ID.** `GRID_OIDC_ISSUER=https://login.microsoftonline.com/<tenant id>/v2.0`: your tenant's, since the multi-tenant `common`, `organizations` and `consumers` addresses are refused. In the app registration, a **Web** redirect URI and a client secret, or a federated credential and no secret at all ([below](#single-sign-on-without-a-client-secret)). Under **Token configuration**, add the groups claim (security groups, as object ids) and map those ids; or define app roles, assign them and set `GRID_OIDC_GROUPS_CLAIM=roles`. Past 200 groups Entra leaves them out of the ID token, and the Grid UI then counts none of that person's groups (their sheet on the Admin page says so): use app roles, or limit the claim to the groups assigned to the application. Turn on **Assignment required** on the enterprise application to limit who signs in, and list subjects in `GRID_OIDC_ADMINS`, not email addresses. Provider sign-out (`GRID_OIDC_END_SESSION=1`) works: add `GRID_UI_PUBLIC_URL` itself as a redirect URI too, so Entra sends people back to it.
* **Google.** `GRID_OIDC_ISSUER=https://accounts.google.com`, an OAuth client of type **Web application**, and an **Internal** consent screen so only your organization signs in. Google sends no groups: give roles by hand, or to everyone with the `*` mapping. Leave `GRID_OIDC_END_SESSION` unset: Google has no end-session endpoint.
* **Okta.** `GRID_OIDC_ISSUER=https://<org>.okta.com` (the org authorization server) or `https://<org>.okta.com/oauth2/<server id>` (a custom one), and an OIDC **Web Application** assigned to the people and groups who may sign in. For groups, add a groups claim filter for the ID token, and `GRID_OIDC_SCOPES=groups` where the claim needs the scope. If the application's client authentication is **Client secret post**, set `GRID_OIDC_CLIENT_AUTH=post`. Leave `GRID_OIDC_END_SESSION` unset: Okta's end-session endpoint needs an ID token hint, which the Grid UI doesn't keep.
* **Keycloak.** `GRID_OIDC_ISSUER=https://<host>/realms/<realm>`, and an OpenID Connect client with **Client authentication** on. For groups, a **Group Membership** mapper with Token Claim Name `groups`, **Full group path** off and **Add to ID token** on. For realm roles instead, a **User Realm Role** mapper with Token Claim Name `roles`, **Multivalued** and **Add to ID token** on, and `GRID_OIDC_GROUPS_CLAIM=roles` (only top-level claims are read, so the default `realm_access.roles` isn't). Provider sign-out works on Keycloak 18 and later, which asks people to confirm, since no ID token hint is sent: add `GRID_UI_PUBLIC_URL` to **Valid post logout redirect URIs**. The Grid UI fetches the issuer's discovery document and keys itself, so the issuer's host must resolve from the `grid_ui` container to the same Keycloak the browser reaches: in compose, set `KC_HOSTNAME` to a name both use, and give `grid_ui` an `extra_hosts` entry or a network alias for it.
* **A private CA.** For a provider whose certificate a private CA issued, mount the CA certificates (PEM) into the container (`docker-compose.yaml` has a commented example) and point `GRID_OIDC_CA_FILE` at them; they are added to the system's.

### Single sign-on without a client secret

The Grid UI can sign people in through Entra ID (or another provider that takes the same kind of credential) with no client secret anywhere: not in a Kubernetes Secret, not in a key vault, not in the app registration. The platform the Grid UI runs on gives it a short-lived token that says who it is; the Grid UI presents that token when it redeems a sign-in's code, and Entra accepts it because the app registration says which token to trust (a federated identity credential: an issuer, a subject and an audience). Microsoft calls this workload identity federation.

The platform's token comes from one of:

* **a file** the platform keeps fresh: on [AKS](#on-aks), on any other Kubernetes cluster, or one you refresh yourself from any issuer the provider trusts ([any other platform](#any-other-platform-a-token-file)), a provider on `127.0.0.1` among them ([local development](#local-development));
* **an Azure managed identity**, on App Service, Functions, Container Apps or an Azure VM ([a managed identity](#app-service-functions-container-apps-or-an-azure-vm-a-managed-identity)).

In every case `GRID_OIDC_CLIENT_AUTH=federated` is required, and no client secret may be set. Nothing turns this on by itself: not a pod label, not a managed identity on the VM, not the variables the platform sets. The Grid UI sends the platform's token only when this setting says so, so that a token meant for Entra is never handed to whatever provider the Grid UI happens to be pointed at. Exactly one source is used: with more than one set, nobody signs in with single sign-on until all but one are removed, and the Grid UI never falls back from one to another.

**Not yet tried against a real tenant.** The Grid UI's side is tested against fakes only: a fake cluster, a fake managed identity endpoint and a fake identity provider. Microsoft documents a platform's token, and a managed identity's, as the client credential for the client credentials grant, and a client credential in place of a secret when a code is redeemed, but no page shows the two together for a sign-in, and that hasn't been tried against Entra. Nor have the address App Service, Functions and Container Apps give their managed identity endpoint, or what a managed identity's token says about itself (its issuer, subject and audience). So on the first cluster, app or VM, try it with a throwaway app registration before moving a grid that people use. If Entra refuses the token when a code is redeemed (an `invalid_client`, or an error about the assertion, while the same federated credential works for the client credentials grant), this version has no other way to sign in without a client secret: keep using one.

#### On AKS

You need AKS 1.22 or later, Azure CLI 2.47.0 or later, and the right to change the app registration.

1. **Turn on the cluster's OIDC issuer and workload identity**, and note the issuer URL:

   ```sh
   az aks update -g <resource group> -n <cluster> --enable-oidc-issuer --enable-workload-identity
   az aks show -g <resource group> -n <cluster> --query oidcIssuerProfile.issuerUrl -o tsv
   # https://westeurope.oic.prod-aks.azure.com/11111111-2222-3333-4444-555555555555/66666666-7777-8888-9999-000000000000/
   ```

   Keep it exactly as printed, trailing `/` included.

2. **Register the Grid UI in Entra ID** as in the [Entra recipe](#identity-provider-recipes) above (a **Web** redirect URI `<GRID_UI_PUBLIC_URL>auth/callback`, the groups claim, **Assignment required**), and add **no** client secret.

3. **Add the federated credential** to that app registration: under **Certificates & secrets**, **Federated credentials**, **Add credential**, scenario **Kubernetes accessing Azure resources**; or:

   ```sh
   cat > credential.json <<'EOF'
   {
     "name": "grid-ui-on-aks",
     "issuer": "https://westeurope.oic.prod-aks.azure.com/11111111-2222-3333-4444-555555555555/66666666-7777-8888-9999-000000000000/",
     "subject": "system:serviceaccount:grid:grid-ui",
     "description": "The Grid UI's pod in the namespace grid",
     "audiences": ["api://AzureADTokenExchange"]
   }
   EOF
   az ad app federated-credential create --id <application (client) id> --parameters credential.json
   ```

   * `issuer` is the URL from step 1, character for character.
   * `subject` is `system:serviceaccount:<namespace>:<service account name>`.
   * The audience is `api://AzureADTokenExchange` (`api://AzureADTokenExchangeUSGov` in Azure Government, `api://AzureADTokenExchangeChina` in Azure operated by 21Vianet; neither cloud has been tried).

   Entra compares all three exactly and case-sensitively, and saves a credential with a typo without complaint: the mistake only shows when someone signs in.

4. **Give the Grid UI a service account, label its pod, and say `federated`:**

   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: grid-ui
     namespace: grid
     annotations:
       azure.workload.identity/client-id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"   # the application (client) id
   ---
   apiVersion: apps/v1
   kind: Deployment
   metadata:
     name: grid-ui
     namespace: grid
   spec:
     selector:
       matchLabels: { app: grid-ui }
     template:
       metadata:
         labels:
           app: grid-ui
           azure.workload.identity/use: "true"      # without this label the pod gets no token
       spec:
         serviceAccountName: grid-ui
         containers:
           - name: grid-ui
             image: <the Grid UI image>
             env:
               - { name: GRID_OIDC_ISSUER, value: "https://login.microsoftonline.com/ffffffff-0000-1111-2222-333333333333/v2.0" }
               - { name: GRID_OIDC_CLIENT_ID, value: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" }
               - { name: GRID_OIDC_CLIENT_AUTH, value: "federated" }      # required: the token is never used without it
               - { name: GRID_UI_PUBLIC_URL, value: "https://grid.example.com/" }
               - { name: GRID_OIDC_ADMINS, value: "sub:<your subject>" }
               # ... the rest of the Grid UI's settings, as in docker-compose.yaml. No GRID_OIDC_CLIENT_SECRET.
   ```

   The label makes AKS mount the token and set `AZURE_FEDERATED_TOKEN_FILE` in the pod, which the Grid UI reads for the path: there is no path to configure. **`GRID_OIDC_CLIENT_AUTH=federated` is required.** The label alone turns nothing on, and `auto` never chooses the platform's token: the Grid UI sends it only when this setting says `federated`, so that a pod labelled for another reason never hands that token to whatever provider it is pointed at. The Grid UI takes the issuer and the client id from its own settings only; the `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_AUTHORITY_HOST` that AKS also sets are not used. Labels and annotations are read when a pod is created: after changing them, delete the pod so a new one starts. The image runs as the user `grid`, not root, and that user must be able to read the token file. Don't set `GRID_OIDC_MANAGED_IDENTITY` in a pod: the Grid UI refuses it in any pod, because the managed identity endpoint a pod can reach is the node's.

5. **Check it.** Sign in with the admin token (or as someone who may see people and roles) and open **Admin**, **Single sign-on**. "Client authentication" reads "A token the platform issues (federated): no client secret", and below it are the token's file, issuer, subject, audience and expiry. Compare issuer, subject and audience with the federated credential of step 3: they must be the same, letter for letter. Then sign in with single sign-on. A federated credential can take a few minutes to take effect after it is added.

**Another token file.** `GRID_OIDC_FEDERATED_TOKEN_FILE` names the file when it isn't the one AKS announces: a pod that uses an AKS identity binding gets a token for the audience `api://AKSIdentityBinding`, which an app registration's federated credential doesn't take. Project a second service-account token for the right audience and point the setting at it:

```yaml
        volumes:
          - name: entra-token
            projected:
              sources:
                - serviceAccountToken: { audience: "api://AzureADTokenExchange", expirationSeconds: 3600, path: token }
        # and in the container:
        #   volumeMounts: [{ name: entra-token, mountPath: /var/run/secrets/grid-oidc, readOnly: true }]
        #   env: GRID_OIDC_FEDERATED_TOKEN_FILE=/var/run/secrets/grid-oidc/token
```

**Never point it at the pod's Kubernetes API token** (`/var/run/secrets/kubernetes.io/serviceaccount/token`). That token is for the cluster's own API server; sent to an identity provider it gives someone outside the cluster a credential for the cluster, and Entra refuses it anyway because its audience is wrong. The file must hold a token projected for the identity provider's audience, as above. "Token audience" on the Single sign-on tab shows which one the Grid UI holds. The Grid UI doesn't check a token file's audience itself: with `federated` it sends the token to whichever provider `GRID_OIDC_ISSUER` names.

#### App Service, Functions, Container Apps or an Azure VM: a managed identity

The Grid UI asks the platform for its managed identity's token each time someone signs in, and Entra takes it because the app registration trusts that managed identity.

**Where this is safe.** A managed identity's token can be had by anything that can ask the platform's endpoint:

* On **App Service, Functions and Container Apps** the endpoint's key (`IDENTITY_HEADER`) is given to the app's own containers only. These are the recommended places.
* On an **Azure VM**, every program and container on the VM can get the VM's identities' tokens; Microsoft: "All code/scripts running on a virtual machine can request and retrieve tokens for any managed identities available on it." With the compose stack that includes the grid's runners, whose browsers any grid user drives, and `example_playwright`. Whoever gets the token can sign in to Entra as the Grid UI's app registration until it expires, which can be many hours: Azure keeps a managed identity's token for around 24 hours, and it can't be made to give out a fresh one sooner. So on a VM, use a managed identity only when the Grid UI is the only thing that can reach `169.254.169.254`: either nothing else runs on the VM, or every other container is blocked from that address (below). A program running on the VM itself, or a container with `network_mode: host`, can't be blocked that way; if there is one, use a secret or a token file instead. Assign the identity to the Grid UI's VM only, never to a runner's.

**Blocking the other containers (compose on a VM).** Give `grid_ui` a fixed address with an override file next to `docker-compose.yaml`:

```yaml
# docker-compose.identity.yaml: grid_ui at a fixed address, so a firewall rule can tell it apart.
services:
  grid_ui:
    networks:
      playwright:
        ipv4_address: 172.50.221.250
```

start the stack with `-f docker-compose.yaml -f docker-compose.identity.yaml` (and `-f docker-compose.mysql.yaml` if you use it), and drop every other container's traffic to the Instance Metadata Service:

```sh
sudo iptables -I DOCKER-USER -d 169.254.169.254 ! -s 172.50.221.250 -j DROP
```

The rule covers every Docker network on the VM, and lets nothing through but the one address (Docker gives other containers addresses from the bottom of the subnet, so `.250` stays free). It needs Docker's iptables firewall, the default. The rule isn't kept across a reboot: keep it with your distribution's tool (`iptables-persistent`, a systemd unit), and add it before the stack starts. Check it from a runner (the address answers instance metadata, which is no credential; `blocked` is what you want to see):

```sh
docker compose exec runner node -e "fetch('http://169.254.169.254/metadata/instance?api-version=2021-02-01',{headers:{Metadata:'true'},signal:AbortSignal.timeout(3000)}).then(r=>console.log(r.status),()=>console.log('blocked'))"
```

1. **Give the app or VM a user-assigned managed identity**, and note its client id, principal id and resource id:

   ```sh
   az identity create -g <resource group> -n grid-ui
   az identity show -g <resource group> -n grid-ui --query "{clientId: clientId, principalId: principalId, id: id}" -o json
   # then one of:
   az vm identity assign -g <resource group> -n <vm> --identities <the identity's resource id>
   az webapp identity assign -g <resource group> -n <app> --identities <the identity's resource id>
   az containerapp identity assign -g <resource group> -n <app> --user-assigned <the identity's resource id>
   ```

   `GRID_OIDC_MANAGED_IDENTITY=system` asks for the endpoint's default identity instead: the system-assigned one if it is turned on (on a VM with no system-assigned identity and one user-assigned one, that one). The Single sign-on tab's "Credential subject" row shows which identity answered. A system-assigned identity may work as the credential, but Microsoft's page on trusting a managed identity lists user-assigned identities only, and it hasn't been tried. The identity and the app registration must be in the same tenant.

2. **Register the Grid UI in Entra ID** as in the [Entra recipe](#identity-provider-recipes) above, and add **no** client secret.

3. **Add the federated credential** to that app registration: under **Certificates & secrets**, **Federated credentials**, **Add credential**, scenario **Managed Identity**, and select the identity; or:

   ```sh
   cat > credential.json <<'EOF'
   {
     "name": "grid-ui-managed-identity",
     "issuer": "https://login.microsoftonline.com/ffffffff-0000-1111-2222-333333333333/v2.0",
     "subject": "<the identity's principal id>",
     "description": "The Grid UI's managed identity",
     "audiences": ["api://AzureADTokenExchange"]
   }
   EOF
   az ad app federated-credential create --id <application (client) id> --parameters credential.json
   ```

   * `issuer` is your tenant's `https://login.microsoftonline.com/<tenant id>/v2.0`, the same as `GRID_OIDC_ISSUER`.
   * `subject` is the identity's **principal (object) id**, not its client id.
   * The audience is always `api://AzureADTokenExchange` (`api://AzureADTokenExchangeUSGov` in Azure Government, `api://AzureADTokenExchangeChina` in Azure operated by 21Vianet; neither cloud has been tried), whatever audience the token itself states on the Single sign-on tab. The Grid UI asks for the audience of the cloud `GRID_OIDC_ISSUER` is in.

4. **Set** `GRID_OIDC_CLIENT_AUTH=federated` and `GRID_OIDC_MANAGED_IDENTITY=<the identity's client id>`, with `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_UI_PUBLIC_URL` and `GRID_OIDC_ADMINS` as usual and no `GRID_OIDC_CLIENT_SECRET`: as app settings on App Service and Functions, as environment variables on Container Apps, in `.env` for compose on a VM. There is no address to set. On App Service, Functions and Container Apps the platform sets `IDENTITY_ENDPOINT` and `IDENTITY_HEADER` and the Grid UI uses them (restart the app after turning its identity on, or they aren't there and the Grid UI says so); on a VM it asks the Instance Metadata Service at `http://169.254.169.254`, which a compose container reaches through the default network. It asks over plain http at the local address the platform set (`localhost`, or a loopback, link-local or private IP address, never a public one), through no proxy whatever `HTTP_PROXY` says, and follows no redirect. `GRID_OIDC_MANAGED_IDENTITY` needs an Entra ID issuer over https: the Grid UI never sends a managed identity's token anywhere else, and only to the token endpoint on the issuer's own host. `GRID_OIDC_ALLOW_HTTP` changes none of this.

5. **Check it** as in step 5 on AKS: the Single sign-on tab shows "Managed identity", "Identity endpoint", the three "Credential" rows (the federated credential to add: compare them with step 3), and the token's issuer, subject and audience as the token states them. The token's subject should be the principal id; its issuer and audience may be written differently from the credential's, which is expected. Then sign in with single sign-on.

Not supported: an Azure Arc-enabled server's identity, Service Fabric's, and the older `MSI_ENDPOINT` (the Grid UI says so by name), and a Kubernetes pod (use the token file; the Grid UI refuses a managed identity in a pod).

#### Any other platform: a token file

`GRID_OIDC_FEDERATED_TOKEN_FILE` takes a file holding a JWT from any issuer the identity provider trusts. The Grid UI reads it at every sign-in, refuses it once it has expired, and keeps it nowhere, so whatever writes the file must replace it before it expires.

* **Another Kubernetes cluster** (self-managed, EKS, GKE, ...): with the Azure workload identity webhook installed, as on AKS; or without it, a projected service-account token for the audience `api://AzureADTokenExchange` (the volume under [Another token file](#on-aks) above) and `GRID_OIDC_FEDERATED_TOKEN_FILE` pointing at it. The federated credential's issuer is the cluster's service account issuer, its subject `system:serviceaccount:<namespace>:<service account>`. Entra fetches the issuer's discovery document and keys itself, so they must be reachable from the internet, and the token must be signed with RS256.
* **A file you refresh yourself** (a sidecar or a timer that writes a token from your own issuer): the same, with the issuer and subject that issuer puts in its tokens.

The Single sign-on tab shows the token's issuer, subject and audience: the federated credential must name those three exactly.

#### Local development

* **The Grid UI on this machine, with Entra** (`GRID_UI_PUBLIC_URL=http://127.0.0.1:<port>/` or `http://localhost:<port>/`): allowed without any setting and without a warning, because only the browser's hop to the Grid UI is plain http, and it stays on this machine. The issuer is Entra's usual https one; the token comes from a file, or from a managed identity when this machine is an Azure VM or app that has one. Add the redirect URI `http://localhost:<port>/auth/callback` (or the `127.0.0.1` form) to the app registration; Entra takes plain http in a redirect URI only for a loopback address, and its portal may only take `localhost`. The registered URI's host must be the one in `GRID_UI_PUBLIC_URL` (`localhost` with `localhost`, `127.0.0.1` with `127.0.0.1`), so when the portal only takes `localhost`, set `GRID_UI_PUBLIC_URL=http://localhost:<port>/`. The Single sign-on tab shows the redirect URI the Grid UI sends.
* **A provider on this machine** (`127.0.0.1`, `localhost`, `[::1]`): plain http is allowed without any setting, and goes straight to it, never through `HTTP_PROXY`. Point `GRID_OIDC_FEDERATED_TOKEN_FILE` at a token from an issuer that provider trusts. "This machine" is where the Grid UI's own process runs. In compose that is the `grid_ui` container, so a provider on the host isn't at `127.0.0.1` for it: give the provider a name both the browser and `grid_ui` reach (an `extra_hosts` entry such as `host.docker.internal:host-gateway`, or a network alias), which isn't loopback and so needs `GRID_OIDC_ALLOW_HTTP=1` (next bullet), or run the Grid UI outside compose.
* **A provider elsewhere over http** (a Keycloak at `http://keycloak:8080` in compose): set `GRID_OIDC_ALLOW_HTTP=1`. The Grid UI then sends the token over plain http, says so in its log and on the Single sign-on tab, and anyone on the network between them can read the token and use it as the Grid UI's credential until it expires, including the other containers on the same Docker network (the runners among them). Use a token from an issuer nothing else trusts, and never leave this setting on outside development. A managed identity's token never goes over plain http, with or without the setting.
* Entra ID trusts only an issuer it can reach, so with Entra even development needs a token from a public issuer (a cluster's, a managed identity's).
* The Grid UI sends the token as a JWT client assertion (RFC 7523), which a provider's discovery document calls `private_key_jwt`. A provider other than Entra must take such an assertion from your token's issuer; how each one is set up for that isn't covered here and hasn't been tried.

#### When it doesn't work

Signing in fails, and the sign-in sheet says single sign-on isn't set up correctly, that the Grid UI couldn't confirm who you are, or (while the managed identity endpoint can't give a token for a while) that something can't be reached right now. What is wrong with the settings, the token file or the managed identity endpoint is on the Single sign-on tab and in the Grid UI's log; what Entra refused is in the log only (see [the log](#grid-ui)). The Grid UI never falls back to another method.

| The Single sign-on tab or the log says | Do this |
|---|---|
| `Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, ...` | `GRID_OIDC_CLIENT_AUTH=federated` is missing. If the tab also says "The platform offers a token", the pod is ready and that setting is all that is left |
| `GRID_OIDC_CLIENT_AUTH=federated needs a token to send: ...` | No source: set `GRID_OIDC_MANAGED_IDENTITY` or `GRID_OIDC_FEDERATED_TOKEN_FILE`. On Kubernetes, the pod has no token: the label `azure.workload.identity/use: "true"` is missing from the pod template (not the Deployment's own labels), workload identity isn't enabled on the cluster, or the pod predates the label. Delete the pod |
| `Set only one of GRID_OIDC_CLIENT_SECRET, GRID_OIDC_FEDERATED_TOKEN_FILE and GRID_OIDC_MANAGED_IDENTITY: ...` or `GRID_OIDC_CLIENT_AUTH=federated doesn't go with GRID_OIDC_CLIENT_SECRET, ...` | Remove all but one: when moving from a client secret, remove the secret |
| `... is used only with GRID_OIDC_CLIENT_AUTH=federated: ...` | Set `GRID_OIDC_CLIENT_AUTH=federated`, or unset the setting named |
| `GRID_OIDC_MANAGED_IDENTITY is system, or the client id ...` | Use the identity's client id (a GUID), not its name or principal id |
| `GRID_OIDC_MANAGED_IDENTITY doesn't go with the AZURE_FEDERATED_TOKEN_FILE ...` | This is a Kubernetes pod: unset `GRID_OIDC_MANAGED_IDENTITY` and use the pod's token |
| `GRID_OIDC_MANAGED_IDENTITY needs an Entra ID GRID_OIDC_ISSUER ...` | A managed identity works with Entra ID only, over https |
| `GRID_OIDC_MANAGED_IDENTITY can't be used: ` followed by `... Service Fabric ...`, `... Azure Arc ...`, `MSI_ENDPOINT or MSI_SECRET is set ...`, `IDENTITY_ENDPOINT is set without the IDENTITY_HEADER ...` or `IDENTITY_ENDPOINT is <address>, and the Grid UI asks ... only over plain http at a local address ...` | This platform's managed identity isn't supported, or its endpoint isn't at a local address or has a query: use a token file or a secret. If the address is your platform's real one, open an issue with it |
| `... IDENTITY_ENDPOINT isn't an address the Grid UI can read ...` | Something changed the variable the platform set (an unclosed `[`, a port that isn't a number up to 65535): leave it as the platform sets it |
| `... IDENTITY_HEADER isn't a usable header value` | Something changed the variable the platform set (a space or line break added): leave it as the platform sets it |
| `... this is App Service, Functions or Container Apps, but IDENTITY_ENDPOINT isn't set ...` | Turn the app's managed identity on, then restart the app |
| `... this is a Kubernetes pod ...` | Unset `GRID_OIDC_MANAGED_IDENTITY` and use the pod's token file |
| `... the Azure Instance Metadata Service (http://169.254.169.254) didn't answer within 5 s` (or `refused the connection`, or `can't be reached`; on App Service, Functions and Container Apps it names `the managed identity endpoint in IDENTITY_ENDPOINT (...)` instead) | Not an Azure VM, or something blocks `169.254.169.254` (a firewall, the `DOCKER-USER` rule above without `grid_ui`'s address, a container network without a route). On App Service, Functions or Container Apps, restart the app. For a minute after, a sign-in is turned away at once without asking again, and the sign-in sheet says to try again in a minute |
| `... answered HTTP 429 ...`, or another `... try again in a minute ...` | Azure is busy or updating the endpoint: try again in a minute. If it keeps happening (a 500 in particular), check that the identity is turned on and assigned to this VM or app |
| `... refused to give a token: HTTP 400 ... Check that this VM or app has ...` | The identity with that client id isn't assigned to this VM or app (or no system-assigned identity is on). App Service and Container Apps may need a restart after assigning one. The Grid UI notices the fix within a minute |
| `... answered with a redirect (HTTP ...), which isn't followed`, `... answered more than 65536 bytes`, `... didn't answer with a JSON object` or `... answered without a token ...` | Something other than the platform's endpoint answers at that address (a proxy, or another program on the VM): the endpoint itself answers JSON with an `access_token` |
| `... the token ... gave has already expired; check this machine's clock` | Set this machine's clock right (NTP) |
| `... the discovery document's token_endpoint isn't https on <host>, GRID_OIDC_ISSUER's host ...` | `GRID_OIDC_ISSUER` isn't Entra's own issuer, or something rewrote its discovery document: a managed identity's token goes only to Entra |
| `GRID_OIDC_ISSUER must be an https URL (or set GRID_OIDC_ALLOW_HTTP=1 for development).`, `... the discovery document's token_endpoint isn't an https URL` (or `authorization_endpoint`, `jwks_uri`) or `... token_endpoint is plain http on a host that isn't loopback ...` | Use https; for development only, see [Local development](#local-development) |
| `GRID_OIDC_ALLOW_HTTP=1 and GRID_OIDC_CLIENT_AUTH=federated: the platform's token goes to the identity provider over plain http, ...` | A warning, not a fault: sign-ins work. Remove `GRID_OIDC_ALLOW_HTTP` outside development |
| `The identity provider doesn't take client assertions: ...` | The provider's discovery document lists its token endpoint's methods without `private_key_jwt`. Entra lists it: check `GRID_OIDC_ISSUER` |
| `... can't be used: there is no file ...`, `... may not read ...`, `... is empty`, `... holds more than 16384 bytes` or `... doesn't hold a token (three base64url parts)` | The file named isn't a token the Grid UI can read: check the path, and that the user `grid` may read it (on Kubernetes, the pod's `securityContext`) |
| `... can't be used: the token in ... has expired; the platform should have replaced it.` | Whatever writes the file stopped. On Kubernetes the kubelet isn't refreshing the token: delete the pod; if it returns, look at the node |
| Log: refused, `error codes 700211` | The credential's issuer is wrong. For a managed identity it is `GRID_OIDC_ISSUER` ("Credential issuer"); for a token file compare with "Token issuer", trailing `/` included |
| Log: refused, `error codes 700213` | The credential's subject isn't the token's. Compare with "Token subject": for a managed identity its principal id, on Kubernetes namespace and service account name |
| Log: refused, `error codes 700212` | The credential's audience is wrong. It should be the audience for your cloud (`api://AzureADTokenExchange` in the public cloud); for a managed identity that, whatever the token's own audience line says. If "Token audience" reads `api://AKSIdentityBinding`, the pod uses an AKS identity binding: see [Another token file](#on-aks) |
| Log: refused, `error codes 70021` | No credential matches. For a managed identity, compare with the three "Credential" rows; for a token file, with the token's three. One added a few minutes ago may not have spread yet: try again |
| Log: refused, `error codes 70025` | The app registration has no federated credential at all (it went to another application): add the one the "Credential" rows show (managed identity) or the token's three (token file) |
| Log: refused, `error codes 700016` | `GRID_OIDC_CLIENT_ID` isn't an application of the tenant in `GRID_OIDC_ISSUER` |
| Log: refused, `error codes 700024` | The token was outside its valid time: check this machine's clock, and that the platform still gives out fresh tokens |
| Log: refused, `error codes 700238` | The tenant doesn't take tokens from this issuer for federated credentials: ask whoever administers the tenant |

`https://login.microsoftonline.com/error?code=<number>` gives Microsoft's text for any other number. The Grid UI keeps only the number, never Entra's message. Microsoft says its error numbers can change, so treat the hints as a place to start.

#### What is and isn't kept

The Grid UI reads the token from its file, or asks the managed identity endpoint for it, each time someone signs in (a file also when it checks the settings; the endpoint also at the start of a sign-in and when the Single sign-on tab is opened, at most once a minute) and keeps it nowhere: not in memory between sign-ins, not in the database, not in a log or a page. For a minute it remembers whether the endpoint gave a token, and that token's issuer, subject, audience and expiry, never the token. It sends the token only to the token endpoint that `GRID_OIDC_ISSUER`'s discovery document names, over https (plain http only to a loopback host, or anywhere with `GRID_OIDC_ALLOW_HTTP=1`, which it warns about; plain http never through a proxy). A managed identity's token goes only to Entra ID over https, on the issuer's own host. The Single sign-on tab shows the token's issuer, subject, audience and expiry, and for a managed identity its client id and endpoint, which are names, not credentials, to everyone who may see people and roles (“See people and roles”), not only to admins. `IDENTITY_HEADER` is never shown or logged.

#### Moving an existing grid from a client secret

Update the Grid UI to this version first, still with the secret. Then give it its token and add the federated credential, keeping the secret: label the pod (the tab then says the platform offers a token), or assign the managed identity (on a VM, after the firewall rule above). Then, in one deployment, remove the secret and set `GRID_OIDC_CLIENT_AUTH=federated` (and `GRID_OIDC_MANAGED_IDENTITY`); sign in; and only then delete the secret in Entra. A Grid UI older than this one can't sign anyone in without a secret, so a rollback to one after the secret is gone, or an update that removes the secret while older pods still run, fails sign-ins until only this version runs.

## MFA secrets

Checks that sign in to a site with two-step verification get authenticator (TOTP, RFC 6238) codes from the Grid UI instead of holding the seed themselves. Someone stores the account's enrolment once and shares it with whoever's checks need it; a check asks for a code while the site's prompt is on screen and types it. The seed stays in the Grid UI: the check, the runner and the browser only ever see 6 digits.

* **Storing a secret.** On the Grid UI's **MFA** page (it needs “Use MFA secrets”, adding needs “Add MFA secrets”, and it is locked until `GRID_SECRETS_KEY` is set), add a secret with a name (1-64 letters, digits, `.`, `_` and `-`; checks ask for codes by it) and either the `otpauth://totp/...` URI in the site's enrolment QR code or the base32 seed the site shows instead of the QR code (any case, spaces and dashes allowed). A URI also brings the issuer, account, algorithm, digits and period; a bare seed gets SHA-1, 6 digits and 30 s, which nearly every site uses. Scan the same QR code with a phone too if people also sign in as that account. To change a seed, delete the secret and add it again. Whoever may use the secret can look at the current code (which uses nothing up, and says whether a check already had it) or take one to type by hand, handed out like a check's. With single sign-on the secret is yours: **Share** it with the people or roles whose checks need it (see [Users and roles](#users-and-roles)). A name is taken for everyone on the grid, whoever's secret it is.
* **Getting a code.** `src/mfa.py` needs only the standard library. The checks `main.py` runs have `src` at `/app/src` and run from `/app`, so:

  ```python
  from src import mfa

  page.get_by_label("Code").fill(mfa.code("intranet", label="nightly login check",
                                          sleep=lambda s: page.wait_for_timeout(s * 1000)))
  ```

  `mfa.code(name)` returns a code that works now, with at least `min_valid_s` (5) of its window left, after waiting for its window if it comes from the next one. Pass a `sleep` that keeps the page served, as above, if the check answers requests with `page.route`. `mfa.issue(name)` asks once without waiting and returns the UI's answer. Both raise `mfa.MfaError` saying why there is no code (unknown secret, wrong token, unreachable UI, key changed); it never holds the token. Over HTTP it is `POST /api/mfa/code` with `Authorization: Bearer <client token>` and `{"name", "label"?, "minValidS"?}`. `full/poc/poc_totp_login.py` signs in to a fake intranet this way.
* **Once per window.** Sites accept each code once, so the UI hands out each 30 s window of a secret once. When the current window is taken, or has less than `minValidS` left, the answer is the next free window's code, before that window opens: `waitS` is how long to wait before typing it (`mfa.code()` waits), `validForS` how long it works from now. Two checks that sign in to the same account at once get different codes, the second about one window later. Codes are handed out at most two windows past the current one; beyond that the UI answers 429 with `retry_after_s`, and `mfa.code()` waits and asks again for up to `max_wait_s` (120 s). The UI records each hand-out (window, time, token name and the check's `label`) in the database (`mfaIssues`) for 7 days, not in its log; codes themselves are never stored.
* **Who may ask.** A check asks with its client token: a managed client token from the Access page, or the bootstrap `GATEWAY_CLIENT_TOKEN`, which compose passes to the UI for this (the UI ignores a value shorter than 16 characters). `mfa.code()` sends `GRID_MFA_TOKEN`, else `GATEWAY_CLIENT_TOKEN`, so compose checks need nothing extra while compose gives the UI `GATEWAY_CLIENT_TOKEN` (see [Users and roles](#users-and-roles) before taking it out). Wrong tokens count towards the admin sign-in limit (429 after 20 from one address in 5 minutes). A client token gets codes for the secrets its owner can use: their own, those shared with them or a role of theirs, and with “Every MFA secret” every one. Legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get them all. A secret that is someone else's and isn't shared with the token's owner answers 403 with `not_shared`; a token whose owner's roles don't include “Use MFA secrets”, 403 with `forbidden`; a token whose [token scope](#token-scopes) leaves out `mfa.use`, 403 with `out_of_scope`.
* **Clock.** Codes are computed from the Grid UI host's clock, and checked by the site against its own: keep the Grid UI host in sync (NTP). Most sites also accept the window before and after.
* **Encryption.** Seeds are stored in `mfaSecrets` encrypted with AES-256-GCM under a key derived from `GRID_SECRETS_KEY` (HKDF-SHA256), bound to the secret's name. If the key changes, the stored seeds no longer decrypt: code requests get 409 saying so, and nothing is used up. Set the old key again, or delete the secrets and add them again from their enrolments (if you no longer have one, enrol the account again on its site). Without the key (or with one under 16 characters) the MFA page stays locked and code requests get 503.

Checks outside compose reach the UI at `GRID_UI_URL`; compose publishes it on 127.0.0.1 only (see [Security](#security) before exposing it).

## Saved sign-ins

A check that signs in to a site can save the browser's signed-in state (Playwright's `storage_state`: cookies and local storage) in the Grid UI and reuse it on later runs, so most runs skip the sign-in and its MFA prompt. It matters most against Microsoft Entra ID: signing in on every run types an authenticator code each time and looks, to Microsoft's risk checks, like one account signing in over and over from server addresses, which Microsoft may answer by challenging, flagging or blocking the account. One saved sign-in, reused until Microsoft asks again, is the single sign-in a person's "stay signed in" would have made.

* **How a check uses it.** `src/signin.py` needs only the standard library and reaches the Grid UI with the same settings as `src/mfa.py` (`GRID_UI_URL`, and `GRID_MFA_TOKEN` or `GATEWAY_CLIENT_TOKEN`). `signin.reuse()` checks out the saved state, opens a context with it and asks the check whether the app is signed in; only when nothing is saved, or the site asks again, does it sign in and save the new state. One check signs in at a time (it holds a lease) and the others wait for the state it saves, so a burst of runs still makes one sign-in. On a clean exit it saves the state again, since sites roll their cookies as they are used.

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

  This Microsoft flow is written from the screens Microsoft shows and is **not verified against a live tenant**; the headings, button text and the push-to-code switch change between tenants and over time, so match on text with the account you have, keep the locators forgiving, and set the authenticator up so a **verification code** is offered (not only push). The MFA code comes from the Grid UI ([MFA secrets](#mfa-secrets)); the account's password is the check's to supply (here from the environment).

* **Whose checks reuse it.** With single sign-on, a saved sign-in belongs to the owner of the client token whose check first used its name, when their roles include “Add saved sign-ins” (otherwise a name nobody added answers 404). Only the owner's checks sign in fresh for it and save it again; for one that is Admins', the checks of anyone with “Every saved sign-in” do, and those of client tokens that act as an admin. A check that stops being one of those while it signs in (its token's owner loses “Every saved sign-in”, say) saves nothing: its save answers 409 `lease_lost`, or 403 `not_shared` when it may no longer use the sign-in at all, and ends its lease, as its release still does, so the next check that may sign in for it does so at once. The checks of the people and roles it is shared with reuse what was saved: while nothing usable is, their checkout answers 409 `owner_signs_in` until the owner's own check has signed in, and the save after a reuse is skipped quietly (`"reason": "shared"`). Someone who may do something its owner may not is reached only by a share someone with “Every saved sign-in” made, and only while its owner keeps at least the permissions they had then (see [Users and roles](#users-and-roles)). Any other check gets 403 `not_shared`. A client token whose [token scope](#token-scopes) leaves out `signins.use` gets 403 `out_of_scope` from all three routes, and a name nobody added also needs `signins.add` in the scope. “Every saved sign-in” lets admins see and manage every sign-in on the page, but their checks use only their own, Admins' sign-ins and those shared with them, the Admin role or another role of theirs by someone with “Every saved sign-in”: otherwise whoever took a name first could save an account of their own under it and have every admin's check run as that account. To let any admin's checks sign in for a sign-in, give it to Admins; to let them reuse one a person owns, share it with the Admin role. Names are taken for everyone on the grid, so a name someone took to get in the way is fixed by deleting the sign-in or changing its owner. Someone without “Every saved sign-in” owns at most 50.

* **What `storage_state` captures.** Cookies and `localStorage`, which is what keeps a browser signed in. It does **not** capture `sessionStorage`, which a new context starts empty. An MSAL single-page app caches its access tokens in `sessionStorage`, so the first load after a reuse finds none and bounces to `login.microsoftonline.com` — where the saved Microsoft cookie signs it back in with no prompt, then returns to the app. So the reuse still works, through that cookie, but `signed_in()` must wait for the app's own content and tolerate the brief sign-in redirect rather than fail on it.

* **Where it is kept.** Each saved sign-in is a row in `savedSignins`, the state encrypted with AES-256-GCM under a key derived from `GRID_SECRETS_KEY` (HKDF-SHA256) and bound to the sign-in's name — the same scheme as an MFA seed but under its own key, so stored seeds are unaffected. The **MFA & sign-ins** page lists the ones you can use (with “Every saved sign-in”, all of them) with the cookies' domains, counts and who used it, never a cookie value; **Forget** drops the state (and bumps its version, so a check still holding the old one can't save it back) and the next run signs in again. **Delete** removes the row; a check that uses the name again adds a new one under a new id, which checks send with the version they save, so a check still holding the deleted state can't save it back either. The checks use `POST /api/signins/checkout`, `/api/signins/save` and `/api/signins/release` with `Authorization: Bearer <client token>`; the page uses `GET /api/signins`, `POST /api/signins/<id>/forget` and `POST /api/signins/<id>/delete`.

* **Treat a saved sign-in as a credential.** It is the account's live session, so a check whose client token may use it is signed in as the account without the password or a code, just as it gets the MFA codes its owner may use (see [Security](#security)). Share it only with people you'd trust with the account. Save only the accounts your checks sign in with, keep `GRID_SECRETS_KEY` out of the database's backups (a dump without it holds no usable cookie), and **Forget** a sign-in to stop checks reusing it (it doesn't sign the cookies out; see [Security](#security)). Without the key, or with one under 16 characters, checkout and save get 503 (the page still lists the sign-ins, and Forget and Delete work). If the key changes, the saved states no longer decrypt, and each check's next run signs in again.

* `full/poc/poc_signin_reuse.py` proves the whole cycle against a fake app behind a Microsoft-style identity provider: reuse with no MFA, a revocation that forces one fresh sign-in, silent renewal after the app's own cookie expires, the lease that serialises concurrent runs, and the refresh-save. Its rounds count on having their saved sign-in (`POC_SIGNIN_NAME`) to themselves, so `full/poc/run_poc.sh` keeps its own, `poc-idp-session-manual`, apart from the scheduled `poc_signin_reuse`'s `poc-idp-session`.

## Slot failures

* When the hub can't open a session on the slot it picked (see [Failover](#connecting-tests)), that adds one to the slot's `failCount`. Any session that reaches the Playwright server resets it to 0, so `failCount` counts consecutive connect failures.
* At `SLOT_FAIL_THRESHOLD` (3) the slot is **failing**: the hub skips it, except for one try every `SLOT_FAIL_COOLDOWN_S` (300 s) after its last run. It heals by itself once a session gets through, or right away with **Reset** in the Grid UI.
* Only the hub counts: a client never names a slot, so a refusal it gets (bad token, no free slot in time, unknown node, a `run-server` 428 for its Playwright version, ...) says nothing about one slot, and a check whose site fails got its browser, so the slot worked. Checks of the previous release (an old `main.py` still running against the same database) counted their own connect failures; stop or upgrade them with the hub (see [Migrating](#migrating-from-the-per-port-setup)), and **Reset** any slot they left failing.
* Compose passes the same `SLOT_FAIL_*` values to the hub and the UI, so what the UI shows as failing is what the hub skips.

## Configuration

### Compose (`.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_AGENT_TOKEN` | required | Bootstrap token the compose runners send to register |
| `GATEWAY_CLIENT_TOKEN` | required | Bootstrap token the example check sends to open a session |
| `TOKEN_CACHE_S`, `TOKEN_RECHECK_S` | `10`, `15` | [Access token](#access-tokens) timing, see [Hub](#hub) |
| `GATEWAY_PORT` | `4500` | Host port the hub publishes |
| `GATEWAY_URL` | `ws://hub:4500` | Hub URL the UI shows; the checks connect to `<GATEWAY_URL>/connect` |
| `GATEWAY_NODE` | unset | Keep the example check on this node (`?node=`) |
| `RUNNER_SLOTS` | `4` | Slots per runner container |
| `EPHEMERAL_GRACE_S` | `120` | Seconds before the hub removes a gone runner container |
| `GATEWAY_QUEUE_TIMEOUT_S`, `GATEWAY_QUEUE_MAX`, `GATEWAY_CONNECT_ATTEMPTS` | `60`, `200`, `3` | [Waiting and failover](#connecting-tests), see [Hub](#hub) |
| `GATEWAY_HUB_LEASE_S` | `90` | [Hub lease](#connecting-tests), for the hub and the UI |
| `SLOT_FAIL_THRESHOLD`, `SLOT_FAIL_COOLDOWN_S` | `3`, `300` | [Slot failures](#slot-failures), for the hub and the UI |
| `GRID_ADMIN_TOKEN` | unset | Token for the UI's management actions: the Access page needs it, or single sign-on. With single sign-on, a break-glass sign-in with every permission |
| `GRID_UI_ALLOWED_HOSTS` | unset | Extra host names the UI accepts actions through |
| `GRID_SECRETS_KEY` | unset | Key the UI encrypts [MFA secrets](#mfa-secrets) and [saved sign-ins](#saved-sign-ins) with. The MFA page needs it, and so do checks to use saved sign-ins |
| `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_OIDC_CLIENT_SECRET` | unset | [Single sign-on](#users-and-roles) for people: the identity provider, and the UI's registration there (the secret, or [the way without one](#single-sign-on-without-a-client-secret)) |
| `GRID_UI_PUBLIC_URL` | unset | The address people open the UI at; needed with single sign-on |
| `GRID_OIDC_ADMINS` | unset | People who are always admins |
| `GRID_OIDC_LABEL`, `GRID_OIDC_SCOPES`, `GRID_OIDC_GROUPS_CLAIM`, `GRID_OIDC_CLIENT_AUTH`, `GRID_OIDC_FEDERATED_TOKEN_FILE`, `GRID_OIDC_MANAGED_IDENTITY`, `GRID_OIDC_END_SESSION`, `GRID_OIDC_CA_FILE`, `GRID_OIDC_ALLOW_HTTP`, `GRID_OIDC_CLOCK_SKEW_S` | see [Grid UI](#grid-ui-1) | The rest of single sign-on's settings |
| `GRID_SESSION_HOURS`, `GRID_SESSION_IDLE_MINUTES` | `12`, `120` | How long a sign-in lasts, and how long unused |
| `GRID_UI_READS` | `signed-in` with single sign-on, else `open` | Whether the overview, activity and sessions need sign-in |
| `GRID_UI_PROXY_HOPS` | `0` | Reverse proxies in front of the UI |
| `GRID_AUDIT_DAYS` | `365` | How long the UI's audit log keeps changes |
| `GRID_UI_LOG_LEVEL`, `GRID_UI_LOG_FORMAT`, `GRID_UI_ACCESS_LOG` | `info`, `text`, `0` | What the UI's log holds, as text or JSON lines, and whether each request gets a line |
| `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` | see `.env.example` | MySQL service credentials (`docker-compose.mysql.yaml`) |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql`, `3306`, `orchestrator` x3 | An existing MySQL server, without `docker-compose.mysql.yaml` |
| `HUB_DB` | `mysql` | `memory` runs the hub without a database (testing only) |

### Hub

| Variable | Default | Purpose |
| --- | --- | --- |
| `HUB_PORT`, `HUB_HOST` | `4500`, `0.0.0.0` | Listen address |
| `GATEWAY_AGENT_TOKEN` | unset | Bootstrap agent token. Agents must send it or a managed agent token. With neither, any agent is accepted (logged as a warning) |
| `GATEWAY_CLIENT_TOKEN` | unset | Bootstrap client token (`?gateway_token=` or `x-gateway-token`). With neither it nor a managed client token, clients aren't checked |
| `TOKEN_CACHE_S` | `10` | How long a managed token lookup is cached (an unknown token: 2 s). A revoked token can still open new connections this long |
| `TOKEN_RECHECK_S` | `15` | How often the tokens of connected agents are checked again; revoked or expired ones are closed |
| `GATEWAY_ALLOW_OPEN_AGENTS`, `GATEWAY_ALLOW_OPEN_CLIENTS` | off | `1`: let tokenless agents / clients in again once every managed token of the kind is revoked (see [Access tokens](#access-tokens)) |
| `HUB_DB` | `mysql` | `mysql` or `memory` |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql`, `3306`, `orchestrator` x3 | MySQL connection. The hub waits up to about 2 minutes for MySQL at startup |
| `SCHEMA_PATH` | `src/schema.sql` | Schema run at startup |
| `HEARTBEAT_TIMEOUT_S` | `30` | Drop an agent that has been silent this long |
| `OPEN_TIMEOUT_S` | `15` | How long a slot gets to open a session before the client is tried on another one |
| `GATEWAY_QUEUE_TIMEOUT_S` | `60` | How long a `/connect` client waits for a free slot that fits it before a 503 (`0`: no waiting) |
| `GATEWAY_QUEUE_MAX` | `200` | How many clients may wait at once; one more gets a 503 |
| `GATEWAY_CONNECT_ATTEMPTS` | `3` | How many slots one client is tried on before it gets the error |
| `GATEWAY_HUB_LEASE_S` | `90` | The hub renews its row in `hubs` every third of this. Another hub frees the claims of a hub not heard from for this long, so keep it well above any MySQL outage you expect to ride out |
| `SLOT_FAIL_THRESHOLD` (or `GRID_FAIL_THRESHOLD`), `SLOT_FAIL_COOLDOWN_S` | `3`, `300` | [Slot failures](#slot-failures): the same names and defaults as the Grid UI |
| `EPHEMERAL_GRACE_S` | `120` | Remove an ephemeral node and its free slots once it has been gone this long (checked at startup and every 60 s; in-use slots go when released) |

HTTP on the same port: `GET /healthz`, `GET /api/nodes`, and the WebSocket paths `/connect` (clients, see [Connecting tests](#connecting-tests)), `/agent` and `/agent/data/<id>` (agents). A failed connect gets a plain HTTP answer the Playwright client shows: 400 bad request (a bad `node` or browser name), 401 missing, bad, revoked or expired client token (also for a client still queued when its token is revoked), 403 a client token whose [token scope](#token-scopes) leaves out opening browser sessions, 404 unknown node, 428 no node runs the client's Playwright version (or the `run-server` it was sent to refused its version), 502 every slot tried refused the session, 503 no node offers the browser, no free slot in time, queue full, MySQL unreachable, hub shutting down, can't check a managed token, or the removed `/connect/<node>/<slot>`, 504 the last slot tried did not answer.

### Agent

Flags override the environment variables.

| Flag | Variable | Default | Purpose |
| --- | --- | --- | --- |
| `--hub` | `HUB_URL` | required | `ws://` or `wss://` hub address |
| `--token` | `GATEWAY_AGENT_TOKEN` | unset | Agent token (bootstrap or managed), sent as `Authorization: Bearer` |
| `--name` | `NODE_NAME` | host name | Node name (see [Node names](#node-names)) |
| `--slots` | `SLOTS` | `4` | run-server processes (1-64) |
| `--base-port` | `BASE_PORT` | `3100` | Slot i listens on 127.0.0.1:(base+i-1) |
| `--playwright-cli` | `PLAYWRIGHT_CLI` | `playwright-core` from node_modules | Path to playwright-core's `cli.js` |
| `--browsers` | `BROWSERS` | `chromium` (`chromium,firefox,webkit` in the runner image) | Browsers installed here; the hub only routes clients that ask for one of them to this node |
| `--status-file` | `STATUS_FILE` | unset | JSON status, rewritten every 5 s |
| `--log-dir` | `LOG_DIR` | unset | run-server output goes to `<dir>/slot-<i>.log` instead of stdout |
| `--ephemeral` | `EPHEMERAL` | off (`1` in the runner image) | The hub removes the node `EPHEMERAL_GRACE_S` after it disconnects |

`PLAYWRIGHT_BROWSERS_PATH` is passed through to the run-servers.

`pwrunner run` (the Go agent) takes the same flags and variables, plus:

| Flag | Variable | Default | Purpose |
| --- | --- | --- | --- |
| `--node` | `PWRUNNER_NODE` | `runtime\node\node.exe` next to the exe, else `node` on PATH | Node binary for the run-servers |
| `--watchdog` | `PWRUNNER_WATCHDOG` | built-in copy | `parent-watchdog.js` preloaded into each run-server |
| `--config` | | unset | Read defaults from an install's `backend.json` (flags, then variables, then the file) |

Its run-servers get an allow-listed environment (system paths, `TEMP`, proxy variables, `PLAYWRIGHT_BROWSERS_PATH`, `PLAYWRIGHT_DOWNLOAD_HOST`), never the agent token or a machine-wide `NODE_OPTIONS`. The Windows service reads only `backend.json` and `agent.token`, never these variables.

When the hub turns the agent away, it logs why and keeps retrying (1 s doubling to 30 s): 4401 missing, bad, revoked or expired token; 4403 token bound to another node name, or the name is reserved for another token; 1013 the hub can't check the token right now. 4409 (replaced by another agent with the same name) waits 30 s first. The status file's `lastError` shows the close code.

### Checks (`example_playwright` and other `main.py` containers)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection, only to create the tables, sync `hosts:` and free stale claims. Each run tries it once (5 s connect timeout); if MySQL is down the check runs anyway |
| `GATEWAY_URL` | `ws://hub:4500` | The hub. The check gets `<GATEWAY_URL>/connect` as `PW_WS_ENDPOINT`; a `/connect/<node>/<slot>` address is refused |
| `GATEWAY_CLIENT_TOKEN` | unset | Client token (bootstrap or managed), appended as `?gateway_token=` |
| `GATEWAY_NODE` | unset | Keep the check on this node or static host, appended as `?node=`. The hub still picks the slot |
| `GRID_UI_URL` | `http://grid_ui:4444` | The Grid UI, as the check reaches it, for [MFA codes](#mfa-secrets) (`src/mfa.py`) and [saved sign-ins](#saved-sign-ins) (`src/signin.py`) |
| `GRID_MFA_TOKEN` | `GATEWAY_CLIENT_TOKEN` | Client token `src/mfa.py` asks for MFA codes with, and `src/signin.py` checks out and saves sign-ins with |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file (`mysql:`, optional `gateway:` with `url`, `client_token` and `node`, and `hosts:`) |
| `CHECK_TIMEOUT_S` | `1800` | Kill a check that runs longer; its browser connection closes and the hub frees the slot |
| `PW_CONNECT_TIMEOUT_S` | `180` | How long the example check waits for the hub to hand it a browser |
| `SLOT_STALE_AFTER_S` | `max(CHECK_TIMEOUT_S, 1800) + 300` | Free a claim this old that isn't the hub's (its claimer died without releasing it). The hub's claims go with its lease instead |

`python3 /app/main.py --fil <check.py> --fol /app/ -i <seconds>` runs the check every interval; without `-i` it runs once and exits 0 (passed) or 1 (failed, or it got no browser). `--fr true` creates the tables and syncs `hosts:`, then exits. `main.py` claims no slot and counts no failures: the hub does both, and holds the check's connect while every slot that fits is busy. Logs go to `<fol>/logs/` (`check_info.log`, `error_info.log`). `check_info.log` has a line per run with the check, its result and how long it took, and says why a check got no browser, from the hub's answer: for example `example.py got no browser after 60.4s, so the site was not checked: the hub answered 503: no free chromium slot within 60s: 4 matching slots, all busy (every slot that fits stayed busy or failing for the hub's GATEWAY_QUEUE_TIMEOUT_S; add slots or spread the checks out)`. The client token never appears in the logs.

### Grid UI

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection, shared with the checks |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file to read the `mysql:` section from |
| `GATEWAY_URL` | `ws://hub:4500` | The hub's address; the UI shows `<GATEWAY_URL>/connect` as the one address checks connect to |
| `GATEWAY_PUBLIC_URL` | unset | Hub address for the commands the UI offers, as runner hosts and checks outside compose reach it (e.g. `ws://grid.lab:4500`). Unset: `GATEWAY_URL`'s scheme and port on the page's own host |
| `GRID_NAME` | unset | Name shown in the header (default: the page's host) |
| `SLOT_FAIL_THRESHOLD`, `SLOT_FAIL_COOLDOWN_S` | `3` (or `GRID_FAIL_THRESHOLD`), `300` | Must match the hub's values |
| `GRID_STALE_AFTER` | `3600` | Seconds after which a session not claimed by the hub is flagged as stale |
| `GRID_UI_POLL_MS` | `1500` | How often a visible page polls `/api/status`, in milliseconds (at least 1000; a background tab every 12 s) |
| `GRID_RECORDER_INTERVAL` | `2` | Seconds between the [activity](#grid-ui) recorder's snapshots while no page is open (`0`: none) |
| `GATEWAY_HUB_LEASE_S` | `90` | The hub's value: a session the hub routed is flagged as stale once its hub hasn't renewed its lease for this long |
| `GRID_ADMIN_TOKEN` | unset | The admin token, a sign-in with every permission. If set (or with single sign-on), management actions need sign-in. With neither, the Access, MFA and Admin pages are locked. With single sign-on, it is the break-glass way in |
| `GATEWAY_AGENT_TOKEN` | unset | Only whether it is set is read: the Access page then knows the hub has a bootstrap agent token and doesn't warn that creating or revoking a token changes who gets in. Compose passes `set`, not the value |
| `GATEWAY_CLIENT_TOKEN` | unset | The hub's bootstrap client token. Tells the Access page the same for client tokens (so anyone who may make client tokens may make the grid's first), and lets checks that connect with it ask for [MFA codes](#mfa-secrets) and use [saved sign-ins](#saved-sign-ins) with it too, acting as an admin (only a value of 16 characters or more). Compose passes the value |
| `GRID_SECRETS_KEY` | unset | Encrypts the [MFA secrets](#mfa-secrets)' seeds and the [saved sign-ins](#saved-sign-ins)' states (AES-256-GCM, key derived with HKDF); at least 16 characters, e.g. `openssl rand -hex 32`. Unset, the MFA page is locked and checks can't check out or save sign-ins (the page still lists them). Changing it makes the stored seeds and states unreadable until the old key is back: codes are refused, and every check signs in again |
| `GRID_UI_ALLOWED_HOSTS` | unset | Comma list of extra host names actions are accepted through (`*` for any) |
| `GRID_OIDC_ISSUER` | unset | The identity provider's issuer, exactly as its ID tokens state it, e.g. `https://login.microsoftonline.com/<tenant id>/v2.0`, `https://accounts.google.com`, `https://<org>.okta.com` or `https://<host>/realms/<realm>`. Setting it turns on [single sign-on](#users-and-roles), and grid actions then need sign-in, even while the other settings are wrong. Multi-tenant Entra addresses (`common`, `organizations`, `consumers`) are refused |
| `GRID_OIDC_CLIENT_ID` | unset | The UI's client id at the provider, where it is registered as a confidential web client. Needed with the issuer; without it: `Set GRID_OIDC_CLIENT_ID to the Grid UI's client id at the identity provider.` |
| `GRID_OIDC_CLIENT_SECRET` | unset | The client secret. Needed unless `GRID_OIDC_CLIENT_AUTH=federated`, and not allowed with it. Without either: `Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the Grid UI can prove to the identity provider that it is the client.` (with `GRID_OIDC_CLIENT_AUTH=basic` or `post`: `GRID_OIDC_CLIENT_AUTH=<it> needs GRID_OIDC_CLIENT_SECRET.`) |
| `GRID_UI_PUBLIC_URL` | unset | The address people open the UI at, e.g. `https://grid.example.com/` (a path prefix is allowed). Needed with the issuer. Register `<it>auth/callback` as the redirect URI. Sign-in started at another host is sent here first, its host is accepted for actions like one in `GRID_UI_ALLOWED_HOSTS`, and its scheme decides whether the cookies are `Secure`. It must be https unless its host is loopback (or `GRID_OIDC_ALLOW_HTTP=1`) |
| `GRID_OIDC_ADMINS` | unset | Comma list of people who are always admins: `sub:<subject>` (at this issuer), or an email address, which counts only when the ID token says it is verified |
| `GRID_OIDC_LABEL` | `single sign-on` | The sign-in button reads "Sign in with <label>" |
| `GRID_OIDC_SCOPES` | unset | Space-separated scopes asked for on top of `openid email profile`, e.g. `groups` for Okta |
| `GRID_OIDC_GROUPS_CLAIM` | `groups` | Comma list of the ID token's claims whose values [group mappings](#users-and-roles) match, e.g. `roles` for Entra app roles. Top-level claims only |
| `GRID_OIDC_CLIENT_AUTH` | `auto` | How the UI authenticates to the token endpoint: `auto`, `basic`, `post` or `federated`. `auto` uses the client secret: `client_secret_basic` when the provider lists it or lists nothing, else `client_secret_post`. Set `basic` or `post` when the client is registered for one secret method only (an Okta app set to "Client secret post"). Set `federated` to send the token the platform issues in place of a secret (see [without a client secret](#single-sign-on-without-a-client-secret)); `auto` never chooses it, and the platform's token is never used unless this says `federated`. `federated` needs one token source (`GRID_OIDC_FEDERATED_TOKEN_FILE`, `GRID_OIDC_MANAGED_IDENTITY`, or the `AZURE_FEDERATED_TOKEN_FILE` of a Kubernetes pod) and no client secret, and sends the token over https (http to a loopback host, or anywhere with `GRID_OIDC_ALLOW_HTTP=1`, warned; a managed identity's token over https to Entra ID only). Any other value: `GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated.` |
| `GRID_OIDC_FEDERATED_TOKEN_FILE` | the `AZURE_FEDERATED_TOKEN_FILE` Azure workload identity sets in a Kubernetes pod | With `GRID_OIDC_CLIENT_AUTH=federated` only: the platform's token file, as a path inside the container, read at every sign-in (at most 16384 bytes, and never kept). Set together with a client secret or `GRID_OIDC_MANAGED_IDENTITY`, or without `federated`, nobody signs in with single sign-on until one of them is removed. Never the pod's Kubernetes API token (`/var/run/secrets/kubernetes.io/serviceaccount/token`) |
| `GRID_OIDC_MANAGED_IDENTITY` | unset | With `GRID_OIDC_CLIENT_AUTH=federated` only, on App Service, Functions, Container Apps or an Azure VM: send the managed identity's token, asked for from the platform's endpoint at every sign-in (never kept). The client id of a user-assigned managed identity, or `system` for the endpoint's default identity. Needs an Entra ID `GRID_OIDC_ISSUER`. On a VM, only where nothing else can reach `169.254.169.254` (see [a managed identity](#app-service-functions-container-apps-or-an-azure-vm-a-managed-identity)). Not for Kubernetes: use the token file there |
| `GRID_OIDC_END_SESSION` | unset | `1`: signing out of the grid also signs out of the provider. No ID token hint is sent, so it works with Entra and Keycloak 18 and later, not with Okta |
| `GRID_OIDC_CA_FILE` | unset | Extra CA certificates (PEM) for a provider behind a private CA, as a path inside the container, added to the system's |
| `GRID_OIDC_ALLOW_HTTP` | unset | `1` allows a plain http issuer, endpoints and public URL that aren't loopback, and with `GRID_OIDC_CLIENT_AUTH=federated` sends the platform's token over that http (warned in the log and on the Admin page). Development only (logged as a warning). Plain http to the provider never goes through `HTTP_PROXY`. Never applies to a managed identity, whose token goes only to Entra ID over https |
| `GRID_OIDC_CLOCK_SKEW_S` | `60` | Leeway for the ID token's times, 0-300 seconds |
| `GRID_SESSION_HOURS` | `12` | How long a sign-in lasts, 1-168 hours |
| `GRID_SESSION_IDLE_MINUTES` | `120` | Ends a session unused this long, 5-1440 minutes (`0`: never). An open page polls, so this counts closed or sleeping tabs |
| `GRID_UI_READS` | `signed-in` with single sign-on, else `open` | `open` or `signed-in`: whether the overview, activity, sessions, `/status` (`/api/status`) and `/api/events` need “See the grid”. Any other value means `signed-in` (logged). Without any sign-in set up they are open whatever it says |
| `GRID_UI_PROXY_HOPS` | `0` | How many reverse proxies you trust in front of the UI. Above 0, `X-Forwarded-For` is believed that many hops deep, for the guessing and sign-in limits and the addresses sessions and the audit log keep. Nothing else is taken from forwarded headers |
| `GRID_AUDIT_DAYS` | `365` | How long the audit log keeps rows, 30-3650 days. Any other value means 365 (logged) |
| `GRID_UI_LOG_LEVEL` | `info` | The least a line must be to reach [the log](#grid-ui): `debug`, `info`, `warning` or `error`. Any other value means `info` (logged). `warning` and `error` hold gunicorn's own lines to the same; `debug` leaves gunicorn's at info |
| `GRID_UI_LOG_FORMAT` | `text` | `text` or `json`: how each line of the log is written, gunicorn's own included. Any other value means `text` (logged) |
| `GRID_UI_ACCESS_LOG` | `0` | `1` adds a line per request to the log, whatever the level: the method, the path without its query string, the status, how long it took and the client's address. Any other value means `0` (logged) |
| `GRID_DB_ENGINE`, `GRID_DB_PATH` | `mysql` | `sqlite` plus a path reads a sqlite DB instead (demo) |
| `GRID_SCHEMA_PATH` | `src/schema.sql` | Schema the UI runs at startup (MySQL only) |
| `GRID_UI_HOST`, `GRID_UI_PORT` | `0.0.0.0`, `4444` | Listen address for `python -m ui.app` |

A single sign-on setting that is wrong (missing, out of range, a CA file that can't be read) stops people signing in with single sign-on until it is fixed, while grid actions stay protected: the Grid UI's log and the Admin page's **Single sign-on** tab say what is wrong.

## Writing a check

* A check is a pytest file. Look at `full/example/example.py`: it opens status.docusign.com (now redirected to health.docusign.com) and fails if any of the eSignature environments NA1-NA4 isn't `available`. Change `PRODUCT` / `MONITORED_ENVIRONMENTS` at the top to watch other sites.
* Connect with `playwright.chromium.connect(os.environ["PW_WS_ENDPOINT"])` (or `firefox`, `webkit`). `main.py` sets it to `<GATEWAY_URL>/connect?gateway_token=<GATEWAY_CLIENT_TOKEN>`, plus `&node=<GATEWAY_NODE>` if that is set, and the hub picks a free slot for that browser. Never build a slot address: `/connect/<node>/<slot>` is refused (503). `host` and `port` are no longer set.
* If the connect itself fails, raise `Exception("hub_Failure")` when the hub answered or could not be reached, else `Exception("connect_Failure")`. Either way `main.py` logs that the check got no browser, and why. Any other failure is the site under test.
* Without `PW_WS_ENDPOINT` the example launches a local Chromium, so `pytest full/example/example.py` works on a dev machine.
* To run another check, copy the `example_playwright` service, mount the check file and change `--fil`. The `./logs/<check>/:/app/logs/` mount keeps its logs and failure screenshots.

## Upgrading Playwright

The checks' client and every run-server must be on the same minor version (see [here][l1]). The version is pinned once, in `runner/playwright/package.json` (with its integrity in `package-lock.json`), and every image and `pwrunner` built from this repo is built with it:

* The runner image installs that playwright-core and its browsers.
* The checks image installs the Python `playwright` with the same major.minor, the newest up to that version (the Python package skips some of npm's patch releases), and its browsers.
* `pwrunner` embeds it and installs it by default. The Windows install test and the deprecated `installer/windows/install-backend.ps1` default to it too.

Dependabot proposes each new version as one pull request that changes those two files, two weeks after it comes out. Its checks build the images and run a check through the hub on a runner in Chromium, Firefox and WebKit, and build `pwrunner` and install it on Windows. The Python package usually comes out a week or two after npm's, but it has taken five; until it has, the checks image fails with `No Playwright X.Y up to X.Y.Z on PyPI?`. Leave the pull request open and re-run its failed jobs once it's out.

The pull request is labelled `release:minor`. After merging it, upgrade every place Playwright runs:

* The orchestrator: `git pull` and `docker compose ... up -d --build`, or with `docker-compose.ghcr.yaml`, `pull` and `up -d`. That upgrades the checks and the compose runners together. An `IMAGE_TAG` of `X.Y` never moves to the next minor version, which is what a Playwright update is: use `latest` or the new release.
* Windows hosts: release a new `pwrunner` (a `pwrunner/vX.Y.Z` tag, see [Releases](#releases)) and re-run `pwrunner install` with it on every host, or pass `--playwright-version <new> --playwright-integrity <sha512-...>` (the `integrity` in `package-lock.json`) to the existing exe.
* Linux/macOS agent hosts: after a `git pull`, repeat the copy, `npm ci` and browser install steps from [Adding a Linux or macOS host](#adding-a-linux-or-macos-host-no-docker), then restart the agent.
* Runner containers on other docker hosts: pull the new image and recreate the container (the same `docker run`).
* Legacy static hosts: the `run-server` install.
* Any other Playwright client that connects to the hub: the same minor version.

To move to a version by hand, change it in `runner/playwright/package.json` and run `npm install` in that folder, which updates `package-lock.json`.

The hub itself needs no upgrade: it passes frames through without reading them. It does read each client's version, and only routes it to nodes on the same minor version, so during a rolling upgrade clients go to the nodes that match them.

## Migrating from the per-port setup

* **Windows hosts**: delete the host from `hosts:` in `src/database.yaml`, then run `pwrunner install --hub-url ... --token-stdin`. It stops the old run-servers, removes the old scheduled task and the `Playwright Backend (<TaskName>)` inbound firewall rule, and moves the PowerShell scripts to `legacy\`.
* `main.py` removes idle static slots that are no longer listed under `hosts:` when it starts. If the node registers under the old entry's name it takes over those rows instead (see [Node names](#node-names)).
* **Static hosts** still work: list them under `hosts:` in `src/database.yaml` (`server`, `port_start`, `port_end`; `port_end` is exclusive) and run `npx playwright run-server --port <port> --host 0.0.0.0` on each port. `/connect` clients reach them through the hub, which dials `ws://<server>:<port>` itself (with the client's headers, minus the gateway token), so those ports must be open to the hub. A static host lists no browsers or version, so it takes any client, but nodes come first; the hub can't tell whether it is up until a connect fails. Prefer the agent.
* `full/example/example.py` reads `PW_WS_ENDPOINT`. Older checks that build `ws://$host:$port` or `/connect/<node>/<slot>` themselves must be changed to use it, and a `GATEWAY_URL` that holds a slot address must go back to the hub's address (`main.py` refuses to run with one).
* **Upgrade every `main.py` with the hub.** A `main.py` of the previous release still claims slots itself, connects to `/connect/<node>/<slot>` and counts its own connect failures. Against the new hub its checks get no browser, and they hold slots while they try. The hub answers that route with 503, which those checks take for the hub's refusal rather than a slot failure, but stop or upgrade every orchestrator that shares the database before or together with the hub (in compose, `docker compose up -d --build` does both). If one ran for a while anyway, **Reset** the slots it left failing in the Grid UI.
* A hub from before routing by browser answers a bare `/connect` with 404 `not found`; the new `main.py` says so in its log. Upgrade the hub.

## Tests

* Gateway: `cd gateway && npm ci && npm test`. `GATEWAY_TEST_MYSQL=1` plus `DB_*` also runs the MySQL registry tests; they drop the gateway tables in `DB_NAME`, so point it at a scratch database.
* Orchestrator and Grid UI: `pip install -r ui/requirements.txt pytest && python -m pytest tests ui/tests`. The example-check and browser tests run when the `playwright` package is installed; the MySQL tests run when `ORCHESTRATOR_TEST_DB` (plus `DB_*`) points at a scratch database; the tests of gunicorn's log lines (`ui/tests/test_logs.py`) run when `gunicorn` is installed. CI runs them all, the Grid UI's inside the Grid UI image (`.github/workflows/python-tests.yml`). Single sign-on, people, roles and sharing are tested by `ui/tests/test_oidc.py`, `test_accounts.py`, `test_sharing.py` and `test_accounts_mysql.py`, and in the browser by `test_browser_oidc.py` and `test_browser_admin.py`, against a fake identity provider the tests run in-process (`ui/tests/fake_idp.py`), so they need no network and no real provider. The browser tests serve it at `localhost` while the Grid UI is at `127.0.0.1`, so the two are cross-site, as a real provider is.
* Agent contract: `cd gateway && node --test test/agent-contract.test.js` runs any agent as a child process against an in-process hub (`AGENT_CMD`, default `node agent.js`; `AGENT_CMD="..\dist\pwrunner.exe run"` for the Go agent).
* Go runner: `cd runner && go test ./...`, and `go test -tags integration ./test/integration/...` against the real hub (needs `gateway/node_modules`). See `runner/README.md`.
* Windows install: `.github/workflows/windows-backend-installer.yml` migrates a PowerShell install to `pwrunner.exe` against a hub on a Windows runner and connects Playwright through it.

None of these need docker (the Go tests need Go 1.26 or newer).

## Licensing

<!-- Not "License": GitHub's license detection reads a "License" section too, and takes "MIT" in it for the plain MIT license. -->
MIT No Attribution (MIT-0): see [LICENSE](LICENSE). Use, change and share it however you like, in open or closed projects, without crediting it or including the license. It comes as is, without warranty, and its authors aren't liable for any claim or damages arising from it.

The third-party software the images and the Windows runner bundle (Node.js, Playwright, the browsers and the rest) keeps its own licenses.

[l1]: https://playwright.dev/docs/intro
