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

This starts MySQL, the hub on port 4500, two Linux runner containers (4 slots each), the Grid UI on http://localhost:4444 and the example check. Compose refuses to start until both gateway tokens are set: they are the [bootstrap tokens](#access-tokens) the compose runners and checks use. Give your other runner hosts and checks their own tokens from the Grid UI's **Access** page. The `web` service only builds the checks' image (`playwright_orchestrator_web`), so its `python_playwright` container exits right away.

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

Tags: `latest` and a version such as `1.1.0` for each release (git tags `pwrunner/v*` or `v*`), `edge` and `sha-<commit>` for every change on master.

```
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml pull
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml up -d --scale runner=2
```

`IMAGE_TAG` in `.env` pins the version (default `latest`).

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
* **Tokens.** A managed client token revoked or expired while its client waits in the queue gets that client a 401 instead of a session.
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

Use a version tag (`:1.1.0`) instead of `:latest` to pin it; the runner's Playwright version must match your checks' minor version. To build it yourself instead: `docker build -f docker/runner/Dockerfile -t playwright_orchestrator_runner .` from a checkout, and run that image.

`NODE_NAME` keeps the node's name when the container is recreated (the default is the container ID), and `EPHEMERAL=0` makes the hub show it OFFLINE while it is down instead of removing it. Keep `--init` (reaps browser processes) and `--shm-size` (Chromium crashes with docker's 64 MB `/dev/shm`).

## Adding a Linux or macOS host (no docker)

playwright-core and the browsers go outside the checkout, in fixed folders, so `npm ci` after a `git pull` keeps them and the service user finds the browsers:

```
sudo git clone <this repo> /opt/playwright-remote-orchestrator
cd /opt/playwright-remote-orchestrator/gateway && sudo npm ci --omit=dev
sudo npm install --prefix /opt/playwright-core playwright-core@1.64.1
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

* **Bootstrap tokens**: `GATEWAY_AGENT_TOKEN` and `GATEWAY_CLIENT_TOKEN` on the hub. They work even while MySQL is down, and the compose runners and example check use them, so compose requires both. They can't be revoked from the UI: to rotate one, change it in `.env` and run the same `docker compose ... up -d` again, which recreates the hub, the runners and the checks with the new value. A hub run outside compose may leave them unset and use managed tokens only.
* **Managed tokens**: made on the Grid UI's **Access** page, which needs [`GRID_ADMIN_TOKEN`](#security). Agent tokens start with `pwga_`, client tokens with `pwgc_`. MySQL only keeps their sha256 and first 12 characters, so the token is shown once, when it is created, with ready-to-paste commands for that kind. Use it like the env token: `-AgentToken`, `GATEWAY_AGENT_TOKEN` or `--token` for an agent, `GATEWAY_CLIENT_TOKEN` for a check.

On the Access page:

* **New token** takes a name, the kind, an expiry (1-3650 days, or none) and, for an agent token, an optional node name. A token **bound** to a node can only register that name (anything else is closed with 4403). Once the node has registered with it, the name is reserved for it while it is valid: no other token can take the node over, not even the bootstrap token. Give each runner host its own bound token, so a leaked one can't receive other nodes' sessions.
* **Rotate**: create a new token (bound to the same node, for a bound one), put it on the host (re-run the Windows installer with `-AgentToken`, or change the env file and restart the agent), wait until the node shows up under the new token, then revoke the old one.
* **Revoke**: new connections with the token are refused within `TOKEN_CACHE_S` (10 s), and the hub closes the agents still connected with it within `TOKEN_RECHECK_S` (15 s), with 4401. Those agents keep retrying until they get a valid token. Browser sessions that are already open with a revoked client token run to the end; new ones get 401. The confirmation names the nodes that will be disconnected.
* **Expiry** works the same way: an expired token is refused, and connected agents using it are closed at the next re-check.
* **Delete** removes a revoked or expired token from the list. Active tokens must be revoked first.

Once a managed token of a kind exists, the hub requires a valid token of that kind even without the env token. So creating the first agent token on a hub without `GATEWAY_AGENT_TOKEN` disconnects agents that have no token within `TOKEN_RECHECK_S`. It stays that way after every managed token of the kind is revoked or deleted, until the hub restarts with no token of that kind left in the table. Set `GATEWAY_ALLOW_OPEN_AGENTS` / `GATEWAY_ALLOW_OPEN_CLIENTS` on the hub to let tokenless agents or clients back in as soon as every token of the kind is revoked. Compose always sets both env tokens, so none of this changes anything there.

If the hub can't reach MySQL, the bootstrap tokens keep working and managed tokens are refused until it can check them again: agents are closed with 1013 and retry, and checks get 503.

## Ports

| Host | Inbound | Outbound |
| --- | --- | --- |
| Orchestrator (compose) | `GATEWAY_PORT` (4500) from your runner hosts. The Grid UI listens on 127.0.0.1:4444 only. | the sites your checks test |
| Runner host (container, Windows or Linux/macOS agent) | none | the hub's port, and the sites your checks test |
| Legacy static host (see [Migrating](#migrating-from-the-per-port-setup)) | every run-server port, from the hub | the sites your checks test |

Run-servers on agent hosts only listen on 127.0.0.1.

## Security

* Agent tokens decide who can register a node. Anyone with an unbound one (or `GATEWAY_AGENT_TOKEN`) can register under an existing node's name and receive that node's browser sessions, unless that node uses a [bound token](#access-tokens). Treat them as secrets.
* Client tokens decide who can open a browser on your runners. Without them the hub would be an open browser proxy into your runners' network. Checks send theirs as `?gateway_token=`; other clients can also use an `x-gateway-token` header.
* The hub logs a managed token only by its first 12 characters and id, and never logs the bootstrap tokens.
* The hub port speaks plain `ws://` (tokens included). Across any network you don't trust, put a TLS-terminating reverse proxy (nginx, Caddy, Traefik, ...) in front of it, use `wss://` URLs for the agents and `GATEWAY_URL`, and allow WebSocket upgrades and long-lived connections (the agent reconnects if it hears nothing for 45 s). Where you can, also limit the hub port to your runner hosts' addresses.
* `GET /healthz` and `GET /api/nodes` on the hub need no token; they show node names, versions and IP addresses.
* The Grid UI is published on 127.0.0.1 only, and without `GRID_ADMIN_TOKEN` anyone who can open it can release slots and forget nodes. Set `GRID_ADMIN_TOKEN` (the admin sign-in) before exposing it more widely. The Access page needs it in any case: without it the UI refuses to list, create or revoke tokens. Whoever has it can create agent tokens, so guard it like `GATEWAY_AGENT_TOKEN`. Compose passes it to the UI but doesn't require it, because the rest of the UI works without it and the Access page stays locked rather than open. The UI refuses cross-site actions, and accepts actions only through an IP address, `localhost`, the machine's own host names or a name in `GRID_UI_ALLOWED_HOSTS`: list the DNS alias or reverse-proxy name you reach it by.

## Grid UI

A management console lives in `ui/` (Flask, plain ES modules, no build step). It reads the same MySQL database the checks use.

* **Overview**: one verdict (`Healthy`, `3 problems need you`, `No free slots`, ...), a list of what needs you (failing and unhealthy slots, stale sessions, offline nodes) with one action each, **Recent runs** (a swimlane per node over the last 1, 5 or 15 minutes, with bursts flagged, and a 15-minute strip of runs finished and busy slots), and the slot matrix: one row per node, one glyph-coded cell per slot, labelled with its slot number (or port, for static hosts). Select any node, slot or session to open the **inspector** on the right: what its state means in plain words, who claimed it (**Claimed by**: the hub or another client), failures against `SLOT_FAIL_THRESHOLD`, the next retry, recent events, a **Connect to the grid** block with the hub's one `/connect` address as Playwright JS or Python, PowerShell or bash (with **Only on <node>** to add `?node=`), and its actions. No slot has an address of its own: the hub picks the slot.
* **Activity**: every change recorded in the last 15 minutes, filterable by node, slot, kind and time; **Copy as JSON** copies the filtered events. An admin action is one row: the change it caused, with who did it.
* **Sessions**: running claims, stale first; release one from its inspector. A session the hub routed is stale only once that hub has stopped renewing its lease (nothing holds the claim then); while the hub runs it is a running session however long it lasts, and releasing it doesn't end it (the hub claims the slot again). Any other claim is stale after `GRID_STALE_AFTER`.
* **Access**: the hub's managed [access tokens](#access-tokens), plus **Add a host**, which makes an agent token bound to a node name, shows the one install command for the chosen platform and waits for the host to register. It warns before making a second token for a node that already has one.
* Every view, filter and object has an address (`#/activity?node=...`, `#/slot/12`, `#/node/<name>`, `#/session/12`). Press `/` or Ctrl K to jump to anything, `?` for the other shortcuts.

**What the activity can and can't see.** The Grid UI server records it (`ui/recorder.py`): it compares snapshots of the database, on every page poll and, when no page is open, every `GRID_RECORDER_INTERVAL` seconds (2 by default; 0 turns the background recording off), and keeps the last 15 minutes (at most 500 events) in memory. A tab opened after a failed CI run therefore still shows the runs and failures before it; the recording starts when the UI server does, and the page says since when. Claim and release both stamp `LastRunDate`, so a run that starts and ends between two snapshots still shows, as *finished between updates* with its end time but no start (drawn as "approx."). It also sees connect failures, failure resets, health changes and nodes coming and going. It can't see which check ran or why a connection failed (no error text is stored). Admin actions are attributed to who signed in, and to "you" in the tab that did them. With several workers (gunicorn) each keeps its own recording; events are identified by database values, so the page merges them. Visible pages poll `/api/status` every `GRID_UI_POLL_MS` (1500 by default; every 12 s in a background tab) and never hold a connection open, so it is safe behind gunicorn's sync workers.

**Hub address.** Copyable commands use `GATEWAY_PUBLIC_URL` when it is set. Otherwise they use `GATEWAY_URL`'s scheme and port with the host the page was opened through, since `ws://hub:4500` only resolves inside compose. The **Hub address** field next to any command overrides it for this browser. Snippets read the client token from `GATEWAY_CLIENT_TOKEN` in the environment and never contain it.

**Signing in.** Admin actions (release, reset, forget) and the Access page need an admin. Today the only sign-in is `GRID_ADMIN_TOKEN`: the account chip's **Sign in** asks for it once per browser tab. Without `GRID_ADMIN_TOKEN` the grid actions are open to anyone who can load the page (the header says **Unprotected**) and token management is refused. The code keeps sign-in in two places so another provider (OAuth/SSO) can be added without touching the views: `ui/auth.py` (a `Principal`, the `PROVIDERS` list and the single `admin_required` gate every admin route uses) and `ui/static/js/auth.js` (state, credentials, the sign-in sheet, sign-out). A new provider implements `configured()`, `attempted(request)`, `authenticate(request)` (returning a `Principal` or `None`) and `describe()`, is appended to `PROVIDERS`, and gets an entry for its `kind` in `SHEETS` in `auth.js`; `GET /api/session` tells the page who is signed in and which providers to offer, and the account menu shows the provider's label. After 20 wrong admin credentials from one address within 5 minutes, the UI answers 429 to that address until the oldest ages out.

**Security.** Every response carries `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`. The page loads nothing from other hosts and has no inline scripts or styles; every database value goes through one escaping template helper. Actions are still refused cross-site and through unknown host names (see [Security](#security)). A database error names its file path or server only to a signed-in admin (and in the server log). Force release sends the claim the confirm described and is refused (409) if another check has claimed the slot since. The stylesheet uses CSS `light-dark()`, so the UI needs a 2024 or later browser (Chrome/Edge 123, Firefox 120, Safari 17.5).

* `GET /status` (or `/api/status`): the same data as JSON, plus a `ready` flag (at least one slot can be picked), `grid_name`, `public_gateway_url` and `poll_ms`. `?soft=1` reports a database outage as `{"unavailable": true, "error": ...}` with HTTP 200 instead of a 503 (the page uses it). `?events=<epoch ms>` adds the recorded `events` observed since then and the `recorder`'s start, drops and capacity samples (`0`: all 15 minutes); `GET /api/events?since=` returns the same on its own. `POST /api/slots/<id>/release {"claimed_at": "<the session's started>"}` releases only that claim. `POST /api/nodes/forget {"host": "<name>"}` forgets a node (409 while it is online or in use).
* `GET /api/session`: `{signedIn, name, method, adminConfigured, providers}` for the credentials sent.
* `GET /api/tokens`, `POST /api/tokens {"name", "kind", "nodeName"?, "expiresInDays"?}` (answers the token once), `POST /api/tokens/<id>/revoke` and `POST /api/tokens/<id>/delete` (409 while active). All of them need an admin (`X-Grid-Token` today).
* With docker it is the `grid_ui` service. Locally: `pip install -r ui/requirements.txt`, set the `DB_*` variables, then `python -m ui.app` from the repo root.
* Demo without MySQL: `python -m ui.demo demo.db --heartbeat` (add `--activity`, or `GRID_DEMO_ACTIVITY=1`, to run short bursts of checks every few seconds), then `GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app` (add `GRID_ADMIN_TOKEN=<anything>` to see the demo's tokens).

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
| `GRID_ADMIN_TOKEN` | unset | Token for the UI's management actions. The Access page needs it |
| `GRID_UI_ALLOWED_HOSTS` | unset | Extra host names the UI accepts actions through |
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

HTTP on the same port: `GET /healthz`, `GET /api/nodes`, and the WebSocket paths `/connect` (clients, see [Connecting tests](#connecting-tests)), `/agent` and `/agent/data/<id>` (agents). A failed connect gets a plain HTTP answer the Playwright client shows: 400 bad request (a bad `node` or browser name), 401 missing, bad, revoked or expired client token (also for a client still queued when its token is revoked), 404 unknown node, 428 no node runs the client's Playwright version (or the `run-server` it was sent to refused its version), 502 every slot tried refused the session, 503 no node offers the browser, no free slot in time, queue full, MySQL unreachable, hub shutting down, can't check a managed token, or the removed `/connect/<node>/<slot>`, 504 the last slot tried did not answer.

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
| `GATEWAY_HUB_LEASE_S` | `90` | The hub's value: a session the hub routed is flagged as stale once its hub hasn't renewed its lease for this long |
| `GRID_ADMIN_TOKEN` | unset | If set, management actions need this token. Unset, the Access page is locked |
| `GATEWAY_AGENT_TOKEN`, `GATEWAY_CLIENT_TOKEN` | unset | Only whether they are set is read: the Access page then knows the hub has a bootstrap token and doesn't warn that creating or revoking a token changes who gets in. Compose passes `set`, not the value |
| `GRID_UI_ALLOWED_HOSTS` | unset | Comma list of extra host names actions are accepted through (`*` for any) |
| `GRID_DB_ENGINE`, `GRID_DB_PATH` | `mysql` | `sqlite` plus a path reads a sqlite DB instead (demo) |
| `GRID_SCHEMA_PATH` | `src/schema.sql` | Schema the UI runs at startup (MySQL only) |
| `GRID_UI_HOST`, `GRID_UI_PORT` | `0.0.0.0`, `4444` | Listen address for `python -m ui.app` |

## Writing a check

* A check is a pytest file. Look at `full/example/example.py`: it opens status.docusign.com (now redirected to health.docusign.com) and fails if any of the eSignature environments NA1-NA4 isn't `available`. Change `PRODUCT` / `MONITORED_ENVIRONMENTS` at the top to watch other sites.
* Connect with `playwright.chromium.connect(os.environ["PW_WS_ENDPOINT"])` (or `firefox`, `webkit`). `main.py` sets it to `<GATEWAY_URL>/connect?gateway_token=<GATEWAY_CLIENT_TOKEN>`, plus `&node=<GATEWAY_NODE>` if that is set, and the hub picks a free slot for that browser. Never build a slot address: `/connect/<node>/<slot>` is refused (503). `host` and `port` are no longer set.
* If the connect itself fails, raise `Exception("hub_Failure")` when the hub answered or could not be reached, else `Exception("connect_Failure")`. Either way `main.py` logs that the check got no browser, and why. Any other failure is the site under test.
* Without `PW_WS_ENDPOINT` the example launches a local Chromium, so `pytest full/example/example.py` works on a dev machine.
* To run another check, copy the `example_playwright` service, mount the check file and change `--fil`. The `./logs/<check>/:/app/logs/` mount keeps its logs and failure screenshots.

## Upgrading Playwright

The checks' client and every run-server must be on the same minor version (1.64.1 today; see [here][l1]). Bump these together:

* `PLAYWRIGHT_VERSION` in `docker/python/Dockerfile` and `playwright==` in `docker/python/requirements.txt` (checks)
* `PLAYWRIGHT_VERSION` in `docker/runner/Dockerfile` (runner containers: base image and `playwright-core`), then rebuild every runner image
* the pins in `runner/internal/runtime/pins.go` (`go run ./tools/updatepins --node <v> --playwright <new>` prints them; also update the file names in `embed_windows_*.go`), release a new `pwrunner.exe`, and re-run `pwrunner install` on every Windows host (or pass `--playwright-version <new> --playwright-integrity <sha512-...>` to an existing exe)
* `npm install --prefix /opt/playwright-core playwright-core@<new>` and the browser install on every Linux/macOS agent host
* the `run-server` install on any legacy static host

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
* Orchestrator and Grid UI: `pip install -r ui/requirements.txt pytest && python -m pytest tests ui/tests`. The example-check and browser tests run when the `playwright` package is installed; the MySQL tests run when `ORCHESTRATOR_TEST_DB` (plus `DB_*`) points at a scratch database.
* Agent contract: `cd gateway && node --test test/agent-contract.test.js` runs any agent as a child process against an in-process hub (`AGENT_CMD`, default `node agent.js`; `AGENT_CMD="..\dist\pwrunner.exe run"` for the Go agent).
* Go runner: `cd runner && go test ./...`, and `go test -tags integration ./test/integration/...` against the real hub (needs `gateway/node_modules`). See `runner/README.md`.
* Windows install: `.github/workflows/windows-backend-installer.yml` migrates a PowerShell install to `pwrunner.exe` against a hub on a Windows runner and connects Playwright through it.

None of these need docker (the Go tests need Go 1.26 or newer).

[l1]: https://playwright.dev/docs/intro
