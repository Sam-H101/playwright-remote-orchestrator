# Docker compose

The orchestrator host runs the stack from the compose files at the repository root. The [quick start](../getting-started.md) shows the commands; this page describes what they start and how to change it.

## Compose files

| File | Adds |
| --- | --- |
| `docker-compose.yaml` | The hub, the runner, the checks' image (`web`), the example check (`example_playwright`) and the Grid UI (`grid_ui`) |
| `docker-compose.mysql.yaml` | A MySQL 8.4 server (`mysql`, data in the `mysql_data` volume) and the `DB_*` settings that point the hub, the UI and the example check at it |
| `docker-compose.ghcr.yaml` | The published images from GitHub Container Registry instead of local builds, at `IMAGE_TAG` (default `latest`) |
| `docker-compose.poc.yaml` | The long proof-of-concept checks in `full/poc`, each run by `main.py` on a schedule like `example_playwright` |

Settings come from `.env` (copy `.env.example`). Every variable is listed in [Configuration](../reference/configuration.md#compose-env).

```sh
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2
```

## Services

| Service | Container | Image | Ports | Networks |
| --- | --- | --- | --- | --- |
| `hub` | `playwright_hub` | `playwright_orchestrator_hub` (`docker/hub/Dockerfile`) | `${GATEWAY_PORT:-4500}:4500` | `playwright`, `runners` |
| `runner` | named after the container ID | `playwright_orchestrator_runner` (`docker/runner/Dockerfile`) | none | `runners` |
| `web` | `python_playwright` | `playwright_orchestrator_web` (`docker/python/Dockerfile`); builds the image and exits | none | `playwright` |
| `example_playwright` | `example_playwright` | `playwright_orchestrator_web` | none | `playwright` |
| `grid_ui` | `playwright_grid_ui` | `docker/ui/Dockerfile` | `127.0.0.1:4444:4444` | `playwright` |
| `mysql` (`docker-compose.mysql.yaml`) | `playwright_mysql` | `mysql:8.4` | none | `playwright` |

- The hub has a health check on `GET /healthz`; the runners start once it is healthy. Its `start_period` (120 s) covers the hub waiting for MySQL on first boot.
- The runners have no `container_name` and no ports, so `--scale runner=N` works. Each registers under its container hostname as an [ephemeral node](../concepts/hub-and-nodes.md#ephemeral-nodes).
- The runners run with `init: true` (reaps browser processes) and `shm_size: 1gb` (Chromium crashes with docker's 64 MB `/dev/shm`).
- `example_playwright` mounts `./src`, `./main.py` and `./full/example/example.py` and runs `python3 /app/main.py --fil example.py --fol /app/ -i 100`. Its logs and failure screenshots go to `./logs/example_playwright/`.
- `grid_ui` mounts `./src/database.yaml` read-only. For a provider behind a private CA, `docker-compose.yaml` has a commented example of mounting a CA file for `GRID_OIDC_CA_FILE`.

## Networks

| Network | Subnet | Members |
| --- | --- | --- |
| `playwright` | `172.50.221.0/24` | hub, checks, Grid UI, MySQL |
| `runners` | `172.50.222.0/24` | hub and runners only |

A check controls a browser on a runner, so the runners sit on a network of their own with the hub and nothing else: a check can't reach MySQL, the Grid UI or the checks' containers through them. See [Security](../security.md#network).

## The runner container's privileges {#the-runner-containers-privileges}

The agent starts as root only to run each slot's browsers as a user of their own (`pwuser1`, `pwuser2`, ...: `RUN_SERVER_USER=pwuser{slot}` in the image), out of reach of its token and of the other slots' checks. Compose gives it:

```yaml
cap_drop: [ALL]
cap_add: [SETUID, SETGID, KILL, NET_ADMIN, SETPCAP]
security_opt: ["no-new-privileges:true"]
```

`SETUID`, `SETGID` and `KILL` let the agent switch user and stop its run-servers. `NET_ADMIN` and `SETPCAP` are for the image's entrypoint (`docker/runner/entrypoint.sh`), which closes `RUNNER_BLOCKED_NETWORKS` to the browsers' users with `iptables`/`ip6tables` owner rules and then drops both capabilities from the bounding set before the agent starts. With `RUNNER_BLOCKED_NETWORKS` empty, `NET_ADMIN` and `SETPCAP` can go.

## Scaling

- Change the number of runners with `--scale runner=N`. Pass it on every `up`, or compose goes back to one runner.
- `RUNNER_SLOTS` sets the slots per runner (default 4).
- Recreated or removed runners are removed by the hub after `EPHEMERAL_GRACE_S` (120 s).

## Database

- With `docker-compose.mysql.yaml`, the `MYSQL_*` variables set the credentials (default `orchestrator` / `orchestrator`) and `DB_*` is ignored.
- To use an existing MySQL server instead, leave out `docker-compose.mysql.yaml` and set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` in `.env`.
- The hub, the checks and the UI all create the tables from `src/schema.sql` on startup; nothing has to be seeded.
- People's names go into some existing tables, so they must be utf8mb4 (MySQL 8's default). See [Users and roles](../grid-ui/users-and-roles.md#tables) for a database upgraded from MySQL 5.7.

## Proof-of-concept checks

```sh
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.poc.yaml up -d
```

Each check holds one slot for about `POC_DURATION_S` (180 s) per run (`poc_cross_browser` takes a Chromium, then a Firefox, then a WebKit slot), then sleeps `POC_INTERVAL_S` (600 s). Logs and failure screenshots go to `./logs/<check>/`. `POC_GATEWAY_NODE` pins them all to one node. Stop them with the same `-f` list and `rm -sf <service>...`, or `down` for the whole stack. The file is written for the bundled MySQL (`docker-compose.mysql.yaml`); with your own server set `DB_*` in it instead.

## Rotating the bootstrap tokens

Change `GATEWAY_AGENT_TOKEN` or `GATEWAY_CLIENT_TOKEN` in `.env` and run the same `docker compose ... up -d` again. That recreates the hub, the runners, the checks and the Grid UI with the new value. See [Access tokens](../concepts/access-tokens.md#bootstrap-tokens).

## Upgrading

`git pull` and `docker compose ... up -d --build`, or with `docker-compose.ghcr.yaml`, `pull` and `up -d`. That upgrades the hub, the checks, the Grid UI and the compose runners together. An `IMAGE_TAG` of `X.Y` never moves to the next minor version, which is what a Playwright update is: use `latest` or the new release. See [Upgrading Playwright](../operations/upgrading-playwright.md).

## Hardening

The defaults keep the Grid UI on 127.0.0.1 and the runners on their own network. Before exposing the hub or the UI beyond a lab, work through [Recommended hardening](../security.md#recommended-hardening).
