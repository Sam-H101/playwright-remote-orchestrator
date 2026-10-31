# playwright-remote-orchestrator

## Purpose

* Runs Playwright checks in containers and sends each run to a free browser slot on any number of runner hosts (Linux containers, Windows machines, anything that runs Node). Checks live in one place; the browsers run wherever you have capacity.
* Runner hosts register themselves with a central hub and dial **out** to it, so the only port you open is the hub's, on the orchestrator host. Runner hosts open no inbound ports.

## Architecture

```
  example_playwright (main.py + pytest)            grid_ui :4444 (127.0.0.1 only)
     |                \ claim / release a slot           |
     |                 v                                  v
     |          +-------------------------------------------+
     |          |                   mysql                   |
     |          +-------------------------------------------+
     |                 ^ nodes, slots, slot health
     | ws://hub:4500/connect/<node>/<slot>
     v                 |
  +--------------------------+
  |  hub  (gateway/hub.js)   |  <- the ONE published port (GATEWAY_PORT, default 4500)
  +--------------------------+
     ^            ^            ^     each agent dials OUT: /agent (control, heartbeats)
     |            |            |     plus /agent/data/<id> (one per browser session)
  runner x N   Windows host   Linux / macOS host
  (docker)     (installer)    (node gateway/agent.js)

  every agent runs N `playwright run-server` slots on 127.0.0.1:3100.. and restarts any that crash
```

1. A check (`main.py`) claims a free slot in MySQL. A slot is free when it is not in use, its node is online (heard from in the last 60 s), its run-server is healthy and it isn't [failing](#slot-failures).
2. The check connects to `ws://hub:4500/connect/<node>/<slot>`. The hub asks that node's agent to open a session; the agent connects to its local run-server and opens a data channel back to the hub, and the hub joins the two.
3. Agents send a heartbeat with per-slot health every 10 s. After any disconnect or hub restart they reconnect and register again by themselves.

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

## Adding a Windows host

`installer/windows/install-backend.ps1` turns a Windows machine into a runner node. Run it from a checkout of this repo in an elevated PowerShell. It needs Node.js 20+ (or pass `-InstallNode` to get it from winget).

```powershell
.\installer\windows\install-backend.ps1 -HubUrl ws://<orchestrator-host>:4500 -AgentToken <agent token> -Slots 4
```

The agent token is one made for this host on the Grid UI's Access page (see [Access tokens](#access-tokens)), or `GATEWAY_AGENT_TOKEN`.

* The host only needs outbound access to the hub's port. No firewall rule, no inbound port and no `database.yaml` entry: the node registers itself as `-NodeName` (default: the DNS host name).
* Installs `playwright-core`, the agent and only the browsers you list (`-Browsers chromium` by default) into `C:\PlaywrightBackend`. Keep `-InstallDir` short: Windows can't start a browser whose path is over 260 characters.
* Re-running keeps the installed settings (hub, token, node name, slots, base port, browsers, report URL, task name) unless you pass them again, and skips anything already at the right version. To rotate the token, re-run with only `-AgentToken <new token>`. `-PlaywrightVersion` is not kept, so a newer installer upgrades. If a re-run fails before the new backend starts, the previous one is restarted.
* Runs the agent under a supervisor (scheduled task `PlaywrightBackend`, runs as SYSTEM at startup) that restarts it if it exits. `-NoService` runs it for the current session instead and needs no elevation.
* Elevated, the install folder is writable only by SYSTEM and Administrators. A folder other users could change (for example from the older installer) gets a fresh `app\` and `browsers\`. The token is stored in `C:\PlaywrightBackend\agent.token`, readable only by SYSTEM and Administrators, and never appears on a command line or in a log.
* Waits for the node to register and reports the result: a console summary, `status.json`, an optional JSON POST to `-ReportUrl`, and the exit code (`0` registered and every slot healthy, `2` not registered or some slots unhealthy, `1` failed).
* `C:\PlaywrightBackend\backend-status.ps1 [-Json] [-ReportUrl <url>] [-HealthTimeoutSec N]` checks the node later with the same exit codes. `uninstall-backend.ps1 [-RemoveFiles]` removes it.
* Logs are in `C:\PlaywrightBackend\logs` (`install.log`, `supervisor.log`, `agent.log`, `slot-<i>.log`).

## Adding a Linux host with docker

The runner image works on any docker host that can reach the hub's port:

```
git clone <this repo> && cd playwright-remote-orchestrator
docker build -f docker/runner/Dockerfile -t playwright_orchestrator_runner .
echo "GATEWAY_AGENT_TOKEN=<token>" > runner.env && chmod 600 runner.env
docker run -d --name pw-runner --restart unless-stopped --init --shm-size 1g --env-file runner.env \
  -e HUB_URL=ws://<orchestrator-host>:4500 -e NODE_NAME=<stable-name> -e EPHEMERAL=0 -e SLOTS=4 \
  playwright_orchestrator_runner
```

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
| Legacy static host (see [Migrating](#migrating-from-the-per-port-setup)) | every run-server port, from the orchestrator | the sites your checks test |

Run-servers on agent hosts only listen on 127.0.0.1.

## Security

* Agent tokens decide who can register a node. Anyone with an unbound one (or `GATEWAY_AGENT_TOKEN`) can register under an existing node's name and receive that node's browser sessions, unless that node uses a [bound token](#access-tokens). Treat them as secrets.
* Client tokens decide who can open a browser on your runners. Without them the hub would be an open browser proxy into your runners' network. Checks send theirs as `?gateway_token=`; other clients can also use an `x-gateway-token` header.
* The hub logs a managed token only by its first 12 characters and id, and never logs the bootstrap tokens.
* The hub port speaks plain `ws://` (tokens included). Across any network you don't trust, put a TLS-terminating reverse proxy (nginx, Caddy, Traefik, ...) in front of it, use `wss://` URLs for the agents and `GATEWAY_URL`, and allow WebSocket upgrades and long-lived connections (the agent reconnects if it hears nothing for 45 s). Where you can, also limit the hub port to your runner hosts' addresses.
* `GET /healthz` and `GET /api/nodes` on the hub need no token; they show node names, versions and IP addresses.
* The Grid UI has no login and is published on 127.0.0.1 only. Set `GRID_ADMIN_TOKEN` before exposing it more widely. The Access page needs it in any case: without it the UI refuses to list, create or revoke tokens. Whoever has it can create agent tokens, so guard it like `GATEWAY_AGENT_TOKEN`. Compose passes it to the UI but doesn't require it, because the rest of the UI works without it and the Access page stays locked rather than open. The UI refuses cross-site actions, and accepts actions only through an IP address, `localhost`, the machine's own host names or a name in `GRID_UI_ALLOWED_HOSTS`: list the DNS alias or reverse-proxy name you reach it by.

## Grid UI

A management console lives in `ui/`. It reads the same MySQL database and refreshes every 5 seconds.

* **Overview**: one card per node with its status, last seen, platform, Playwright version, browsers and a chip per slot (free, in use, unhealthy, failing, offline). Gateway nodes are ONLINE / DEGRADED / OFFLINE, legacy static hosts UP / DEGRADED / DOWN; ephemeral nodes are tagged. Click a slot for details, its endpoint, when a failing slot is next tried, and to reset its failure count or force release it. **Forget** removes an offline node and its slots.
* **Sessions**: sortable table of running sessions with their endpoint, start time and duration.
* **Access**: the hub's managed [access tokens](#access-tokens), with the nodes using each one. It asks for `GRID_ADMIN_TOKEN` and explains the setting when it isn't set.
* `GET /status` (or `/api/status`): the same data as JSON, plus a `ready` flag (at least one slot can be picked). `POST /api/nodes/forget {"host": "<name>"}` forgets a node (409 while it is online or in use).
* `GET /api/tokens`, `POST /api/tokens {"name", "kind", "nodeName"?, "expiresInDays"?}` (answers the token once), `POST /api/tokens/<id>/revoke` and `POST /api/tokens/<id>/delete` (409 while active). All of them need the `X-Grid-Token` header.
* With docker it is the `grid_ui` service. Locally: `pip install -r ui/requirements.txt`, set the `DB_*` variables, then `python -m ui.app` from the repo root.
* Demo without MySQL: `python -m ui.demo demo.db --heartbeat`, then `GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app` (add `GRID_ADMIN_TOKEN=<anything>` to see the demo's tokens).

## Slot failures

* A check that can't reach its slot raises `connect_Failure`; that adds one to the slot's `failCount`. Any run that reaches the Playwright server (passed, failed or timed out) resets it to 0, so `failCount` counts consecutive connect failures.
* At `SLOT_FAIL_THRESHOLD` (3) the slot is **failing**: slot selection skips it, except for one retry every `SLOT_FAIL_COOLDOWN_S` (300 s) after its last run. It heals by itself once a run gets through, or right away with **Reset** in the Grid UI.
* A check that the hub itself refused (missing, bad, revoked or expired client token, hub unreachable, shutting down or unable to check the token) raises `hub_Failure` instead; that is logged as a configuration problem and not counted against the slot.
* Compose passes the same `SLOT_FAIL_*` values to the checks and the UI, so what the UI shows as failing is what selection skips.

## Configuration

### Compose (`.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_AGENT_TOKEN` | required | Bootstrap token the compose runners send to register |
| `GATEWAY_CLIENT_TOKEN` | required | Bootstrap token the example check sends to open a session |
| `TOKEN_CACHE_S`, `TOKEN_RECHECK_S` | `10`, `15` | [Access token](#access-tokens) timing, see [Hub](#hub) |
| `GATEWAY_PORT` | `4500` | Host port the hub publishes |
| `GATEWAY_URL` | `ws://hub:4500` | Hub URL the checks use and the UI shows |
| `RUNNER_SLOTS` | `4` | Slots per runner container |
| `EPHEMERAL_GRACE_S` | `120` | Seconds before the hub removes a gone runner container |
| `SLOT_FAIL_THRESHOLD`, `SLOT_FAIL_COOLDOWN_S` | `3`, `300` | [Slot failures](#slot-failures), for the checks and the UI |
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
| `OPEN_TIMEOUT_S` | `15` | How long a client waits for the agent to open its session |
| `EPHEMERAL_GRACE_S` | `120` | Remove an ephemeral node and its free slots once it has been gone this long (checked at startup and every 60 s; in-use slots go when released) |

HTTP on the same port: `GET /healthz`, `GET /api/nodes`, and the WebSocket paths `/connect/<node>/<slot>` (checks), `/agent` and `/agent/data/<id>` (agents). A failed connect gets a plain HTTP answer the Playwright client shows: 400 bad request, 401 missing, bad, revoked or expired client token, 404 unknown node or slot, 502 the slot's run-server refused, 503 hub shutting down or can't check a managed token, 504 timeout.

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
| `--browsers` | `BROWSERS` | `chromium` | Browsers reported to the hub (informational) |
| `--status-file` | `STATUS_FILE` | unset | JSON status, rewritten every 5 s |
| `--log-dir` | `LOG_DIR` | unset | run-server output goes to `<dir>/slot-<i>.log` instead of stdout |
| `--ephemeral` | `EPHEMERAL` | off (`1` in the runner image) | The hub removes the node `EPHEMERAL_GRACE_S` after it disconnects |

`PLAYWRIGHT_BROWSERS_PATH` is passed through to the run-servers.

When the hub turns the agent away, it logs why and keeps retrying (1 s doubling to 30 s): 4401 missing, bad, revoked or expired token; 4403 token bound to another node name, or the name is reserved for another token; 1013 the hub can't check the token right now. 4409 (replaced by another agent with the same name) waits 30 s first. The status file's `lastError` shows the close code.

### Checks (`example_playwright` and other `main.py` containers)

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection |
| `GATEWAY_URL` | `ws://hub:4500` | Hub the checks connect through |
| `GATEWAY_CLIENT_TOKEN` | unset | Client token (bootstrap or managed), appended to the endpoint as `?gateway_token=` |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file (`mysql:`, optional `gateway:` and `hosts:`) |
| `CHECK_TIMEOUT_S` | `1800` | Kill a check that runs longer and release its slot |
| `SLOT_FAIL_THRESHOLD` | `3` (or `GRID_FAIL_THRESHOLD`) | Consecutive connect failures at which a slot is skipped |
| `SLOT_FAIL_COOLDOWN_S` | `300` | How often a failing slot is retried |
| `SLOT_STALE_AFTER_S` | `max(CHECK_TIMEOUT_S, 1800) + 300` | Free a claim this old (its orchestrator died mid-check). Keep it above every orchestrator's `CHECK_TIMEOUT_S` |
| `NO_SLOT_TIMEOUT_S` | `300` | A single run (no `-i`) gives up after waiting this long for a free slot |

`python3 /app/main.py --fil <check.py> --fol /app/ -i <seconds>` runs the check every interval; without `-i` it runs once and exits 0 (passed) or 1 (failed or no slot). `--fr true` creates the tables and syncs `hosts:`, then exits. When no slot is free it waits and retries (1 s doubling to 60 s). Logs go to `<fol>/logs/` (`check_info.log`, `error_info.log`).

### Grid UI

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection, shared with the checks |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file to read the `mysql:` section from |
| `GATEWAY_URL` | `ws://hub:4500` | Base of the endpoints shown for gateway slots |
| `SLOT_FAIL_THRESHOLD`, `SLOT_FAIL_COOLDOWN_S` | `3` (or `GRID_FAIL_THRESHOLD`), `300` | Must match the checks' values |
| `GRID_STALE_AFTER` | `3600` | Seconds after which a running session is flagged as stale |
| `GRID_ADMIN_TOKEN` | unset | If set, management actions need this token. Unset, the Access page is locked |
| `GATEWAY_AGENT_TOKEN`, `GATEWAY_CLIENT_TOKEN` | unset | Only whether they are set is read: the Access page then knows the hub has a bootstrap token and doesn't warn that creating or revoking a token changes who gets in. Compose passes `set`, not the value |
| `GRID_UI_ALLOWED_HOSTS` | unset | Comma list of extra host names actions are accepted through (`*` for any) |
| `GRID_DB_ENGINE`, `GRID_DB_PATH` | `mysql` | `sqlite` plus a path reads a sqlite DB instead (demo) |
| `GRID_SCHEMA_PATH` | `src/schema.sql` | Schema the UI runs at startup (MySQL only) |
| `GRID_UI_HOST`, `GRID_UI_PORT` | `0.0.0.0`, `4444` | Listen address for `python -m ui.app` |

## Writing a check

* A check is a pytest file. Look at `full/example/example.py`: it opens status.docusign.com (now redirected to health.docusign.com) and fails if any of the eSignature environments NA1-NA4 isn't `available`. Change `PRODUCT` / `MONITORED_ENVIRONMENTS` at the top to watch other sites.
* Connect with `playwright.chromium.connect(os.environ["PW_WS_ENDPOINT"])`. `main.py` sets it to the claimed slot's full URL, token included. It also still sets `host` and `port`, but those can't reach a gateway slot.
* If the connect itself fails, raise `Exception("connect_Failure")`, or `Exception("hub_Failure")` when the hub refused it (see [Slot failures](#slot-failures)). Any other failure is the site under test.
* Without `PW_WS_ENDPOINT` the example launches a local Chromium, so `pytest full/example/example.py` works on a dev machine.
* To run another check, copy the `example_playwright` service, mount the check file and change `--fil`. The `./logs/<check>/:/app/logs/` mount keeps its logs and failure screenshots.

## Upgrading Playwright

The checks' client and every run-server must be on the same minor version (1.64.1 today; see [here][l1]). Bump these together:

* `PLAYWRIGHT_VERSION` in `docker/python/Dockerfile` and `playwright==` in `docker/python/requirements.txt` (checks)
* `PLAYWRIGHT_VERSION` in `docker/runner/Dockerfile` (runner containers: base image and `playwright-core`), then rebuild every runner image
* the default `-PlaywrightVersion` in `installer/windows/install-backend.ps1`, then re-run the installer on every Windows host (or pass `-PlaywrightVersion <new>`)
* `npm install --prefix /opt/playwright-core playwright-core@<new>` and the browser install on every Linux/macOS agent host
* the `run-server` install on any legacy static host

The hub doesn't care: it passes frames through without reading them.

## Migrating from the per-port setup

* **Windows hosts**: delete the host from `hosts:` in `src/database.yaml`, then re-run the new installer with `-HubUrl` and `-AgentToken`. It stops the old run-servers on ports 3000+ and removes the old `Playwright Backend (<TaskName>)` inbound firewall rule.
* `main.py` removes idle static slots that are no longer listed under `hosts:` when it starts. If the node registers under the old entry's name it takes over those rows instead (see [Node names](#node-names)).
* **Static hosts** still work: list them under `hosts:` in `src/database.yaml` (`server`, `port_start`, `port_end`; `port_end` is exclusive) and run `npx playwright run-server --port <port> --host 0.0.0.0` on each port. Checks connect to them directly at `ws://<server>:<port>`, so those ports must be open, and the orchestrator can't tell whether they are up until a connect fails. Prefer the agent.
* `full/example/example.py` reads `PW_WS_ENDPOINT`. Older checks that build `ws://$host:$port` themselves must be changed to use it.

## Tests

* Gateway: `cd gateway && npm ci && npm test`. `GATEWAY_TEST_MYSQL=1` plus `DB_*` also runs the MySQL registry tests; they drop the gateway tables in `DB_NAME`, so point it at a scratch database.
* Orchestrator and Grid UI: `pip install -r ui/requirements.txt pytest && python -m pytest tests ui/tests`. The example-check and browser tests run when the `playwright` package is installed; the MySQL tests run when `ORCHESTRATOR_TEST_DB` (plus `DB_*`) points at a scratch database.
* Windows installer: `.github/workflows/windows-backend-installer.yml` installs a node against a hub on a Windows runner and connects Playwright through it.

None of these need docker.

[l1]: https://playwright.dev/docs/intro
