# playwright-remote-orchestrator

A Playwright grid. Checks connect to one hub, which hands each one a free browser slot on any number of runner hosts: Linux containers, Windows machines, anything that runs Node. Checks live in one place; the browsers run wherever you have capacity.

Runner hosts register themselves with the hub and dial **out** to it, so the only port you open is the hub's, on the orchestrator host. Runner hosts open no inbound ports.

**Documentation: <https://playwright-remote-orchestration.sdevwork.com>** (source in [`docs/`](docs/index.md)).

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
  (docker)     (pwrunner)     (node gateway/agent.js)

  every agent runs N `playwright run-server` slots on 127.0.0.1:3100.. and restarts any that crash
```

1. A test connects to `ws://hub:4500/connect` with its client token. Playwright's connect already tells the hub which browser it wants and which Playwright version it is.
2. The hub picks a free slot on a node that offers that browser on the same Playwright version and claims it in MySQL. While every matching slot is busy, the client waits.
3. The hub asks that node's agent to open a session; the agent connects to its local run-server and opens a data channel back to the hub, and the hub joins the two. When the session ends, the hub releases the slot.
4. Agents send a heartbeat with per-slot health every 10 s, and reconnect and register again by themselves after any disconnect or hub restart.

| Component | Where |
| --- | --- |
| Hub | `gateway/hub.js` |
| Agent | `gateway/agent.js` (Linux, macOS, the runner image), `runner/` (`pwrunner.exe` for Windows) |
| Grid UI | `ui/` (Flask) |
| Orchestrator | `main.py`, `src/` |
| Images and stack | `docker/`, `docker-compose*.yaml` |

## Quick start (docker compose)

```
cp .env.example .env     # set GATEWAY_AGENT_TOKEN, GATEWAY_CLIENT_TOKEN and GRID_ADMIN_TOKEN (e.g. openssl rand -hex 32)
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2
```

This starts MySQL, the hub on port 4500, two Linux runner containers (4 slots each), the Grid UI on http://localhost:4444 and the example check. Compose refuses to start until both gateway tokens are set. Give your other runner hosts and checks their own tokens from the Grid UI's **Access** page.

To run the published images instead of building them, add `-f docker-compose.ghcr.yaml` and use `pull` + `up` without `--build`.

Connect any Playwright client to the hub:

```python
import os
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect(f"ws://<orchestrator-host>:4500/connect?gateway_token={os.environ['GATEWAY_CLIENT_TOKEN']}")
```

## Documentation

| Topic | Page |
| --- | --- |
| The stack on one machine, published images | [Getting started](docs/getting-started.md) |
| Hub, nodes, slots, routing, version matching, slot failures | [Concepts](docs/concepts/hub-and-nodes.md) |
| Agent and client tokens, token scopes | [Access tokens](docs/concepts/access-tokens.md) |
| Compose, Linux and macOS hosts, Windows (`pwrunner`), Kubernetes | [Deployment](docs/deployment/docker-compose.md) |
| Overview, activity, sessions, MFA secrets, saved sign-ins, people and roles, audit log, HTTP API | [Grid UI](docs/grid-ui/index.md) |
| OpenID Connect, Entra ID without a client secret (AKS, managed identity) | [Single sign-on](docs/sso/index.md) |
| Connecting from Python and JavaScript, `main.py` checks | [Writing checks](docs/writing-checks.md) |
| Per-slot isolation, network isolation, hardening | [Security](docs/security.md) |
| Every variable and flag | [Configuration](docs/reference/configuration.md), [Command line](docs/reference/cli.md) |
| Troubleshooting, upgrading Playwright, migrating | [Operations](docs/operations/troubleshooting.md) |
| Repository layout, tests, releases | [Development](docs/development/index.md) |

Build the site locally without installing anything: `docker run --rm -v "$PWD:/docs" squidfunk/mkdocs-material:9.8.0 build --strict`.

## Licensing

<!-- Not "License": GitHub's license detection reads a "License" section too, and takes "MIT" in it for the plain MIT license. -->
MIT No Attribution (MIT-0): see [LICENSE](LICENSE). Use, change and share it however you like, in open or closed projects, without crediting it or including the license. It comes as is, without warranty, and its authors aren't liable for any claim or damages arising from it.

The third-party software the images and the Windows runner bundle (Node.js, Playwright, the browsers and the rest) keeps its own licenses.
