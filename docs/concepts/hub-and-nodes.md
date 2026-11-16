# Hub, nodes and slots

## The hub

The hub (`gateway/hub.js`) is the one address every runner host and every check uses. It listens on one port (`HUB_PORT`, 4500; compose publishes it as `GATEWAY_PORT`) and serves on it:

| Path | Who | What |
| --- | --- | --- |
| `/connect` | Playwright clients (WebSocket) | Open a browser session; the hub picks the slot. See [Routing](routing.md) |
| `/agent` | Agents (WebSocket) | The control channel: registration, heartbeats, session requests |
| `/agent/data/<id>` | Agents (WebSocket) | One data channel per browser session |
| `GET /healthz` | Anyone | Health; needs no token |
| `GET /api/nodes` | Anyone | The connected nodes; needs no token, and shows node names, versions and IP addresses |

The hub keeps nodes, slots, slot health and claims in MySQL (`HUB_DB=mysql`, the default). `HUB_DB=memory` runs it without a database, for testing only. It waits up to about 2 minutes for MySQL at startup and runs `src/schema.sql` (`SCHEMA_PATH`).

The hub itself needs no Playwright: it passes frames through without reading them. It does read each client's version, to route it (see [version](routing.md#version)).

## Nodes and agents

A **node** is a runner host registered with the hub. Its **agent** dials out to the hub's `/agent`, registers under a node name with an [agent token](access-tokens.md), and sends a heartbeat with per-slot health every 10 s. After any disconnect or hub restart it reconnects and registers again by itself. The hub drops an agent that has been silent for `HEARTBEAT_TIMEOUT_S` (30 s).

There are three agents, all speaking the same protocol to the same hub:

| Agent | Runs on | Page |
| --- | --- | --- |
| The runner image (`gateway/agent.js` with Chromium, Firefox and WebKit) | Any docker host | [Docker compose](../deployment/docker-compose.md), [Linux host with docker](../deployment/linux-docker.md) |
| `node gateway/agent.js` | Linux or macOS | [Linux or macOS host without docker](../deployment/linux-macos.md) |
| `pwrunner.exe` (Go) | Windows (also builds for Linux) | [Windows host](../deployment/windows.md) |

### Ephemeral nodes

A node started with `--ephemeral` (`EPHEMERAL=1`, the default in the runner image) is removed by the hub, with its free slots, once it has been gone for `EPHEMERAL_GRACE_S` (120 s), checked at startup and every 60 s; in-use slots go when released. The compose runners are ephemeral and named after their container ID, so recreated runners don't pile up as offline nodes. A non-ephemeral node (`EPHEMERAL=0`) is shown OFFLINE while it is down instead.

## Slots

Each agent runs N slots (`--slots` / `SLOTS`, 1-64, default 4). A slot is one `playwright run-server` process; slot *i* listens on `127.0.0.1:(BASE_PORT + i - 1)` (`BASE_PORT` 3100). The agent restarts any run-server that crashes.

- Run-servers on agent hosts only listen on 127.0.0.1, and each answers only at a random path the agent dials (see [Security](../security.md#per-slot-isolation)).
- A slot's health is only whether its run-server answers. A node whose browsers are missing shows healthy and every session fails.
- One slot runs one browser session at a time. The hub claims it in MySQL's `portUsage` table while the session runs (see [Claims](routing.md#claims)).

## Node names {#node-names}

- Node names are case-insensitive and stored lower-case (`Runner-A` registers as `runner-a`): `[A-Za-z0-9._-]`, up to 128 characters. The default is the host name.
- A name must be unique. A second agent with the same name takes over the node and the first one is disconnected (and waits 30 s before trying again). A node that registered with a token [bound to its name](access-tokens.md#binding) can only be taken over by a token bound to the same name.
- Node names and the `server` names under `hosts:` in `src/database.yaml` share one namespace. `main.py` ignores a `hosts:` entry whose name is a registered node (with a warning to remove it). If a static host is listed before an agent registers under its name, the node takes over that host's slot rows, so give agents and static hosts different names.

## Static hosts

Hosts listed under `hosts:` in `src/database.yaml` (`server`, `port_start`, `port_end`; `port_end` is exclusive) are the legacy per-port setup: a `playwright run-server` on each port, which the hub dials itself. They list no browsers or version, so they take any client, but nodes come first. See [Migrating from the per-port setup](../operations/migrating.md).

## Ports {#ports}

| Host | Inbound | Outbound |
| --- | --- | --- |
| Orchestrator (compose) | `GATEWAY_PORT` (4500) from your runner hosts. The Grid UI listens on 127.0.0.1:4444 only. | the sites your checks test |
| Runner host (container, Windows or Linux/macOS agent) | none | the hub's port, and the sites your checks test |
| Legacy static host (see [Migrating](../operations/migrating.md)) | every run-server port, from the hub | the sites your checks test |

## Several hubs

Several hubs may share one database (a second compose project, say): each claims atomically, frees only its own claims and those of hubs whose lease ran out, and logs a warning naming the others when it starts. Each routes only to the nodes connected to it. See [Claims](routing.md#claims) for the lease.
