# Getting started

This starts the whole grid on one machine with docker compose: MySQL, the hub, Linux runner containers, the Grid UI and an example check.

## Requirements

- Docker with the compose plugin.
- A checkout of the repository (or only the compose files and `.env.example`, if you run the [published images](#published-images)).

## Quick start

```sh
cp .env.example .env     # set GATEWAY_AGENT_TOKEN, GATEWAY_CLIENT_TOKEN and GRID_ADMIN_TOKEN (e.g. openssl rand -hex 32)
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d --build --scale runner=2
```

This starts MySQL, the hub on port 4500, two Linux runner containers (4 slots each), the Grid UI on <http://localhost:4444> and the example check.

- Compose refuses to start until both gateway tokens are set: they are the [bootstrap tokens](concepts/access-tokens.md#bootstrap-tokens) the compose runners and checks use. Give your other runner hosts and checks their own tokens from the Grid UI's **Access** page.
- To let people sign in to the Grid UI with your identity provider rather than share `GRID_ADMIN_TOKEN`, see [Single sign-on](sso/index.md) and [Users and roles](grid-ui/users-and-roles.md).
- The `web` service only builds the checks' image (`playwright_orchestrator_web`), so its `python_playwright` container exits right away.

!!! note "What to check"
    Open the Grid UI at <http://localhost:4444>. The overview shows one row per runner node with one cell per slot. The `example_playwright` container runs `full/example/example.py` every 100 s; its logs are in `./logs/example_playwright/`.

## Scaling and options

- Change the number of runners with `--scale runner=N`. Pass it on every `up`, or compose goes back to one runner. `RUNNER_SLOTS` sets the slots per runner.
- Runner containers are **ephemeral** nodes named after their container ID: when one goes away (scaled down, recreated) the hub removes it and its slots after `EPHEMERAL_GRACE_S` (120 s), so recreated runners don't pile up as offline nodes.
- To use an existing MySQL server instead, leave out `docker-compose.mysql.yaml` and set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD` in `.env`. With `docker-compose.mysql.yaml` the `MYSQL_*` variables set the credentials (default `orchestrator` / `orchestrator`) and `DB_*` is ignored.
- The hub, the checks and the UI all create the tables from `src/schema.sql` on startup; nothing has to be seeded.

## Published images {#published-images}

To run the published images instead of building them, add `-f docker-compose.ghcr.yaml` and use `pull` + `up` without `--build`:

```sh
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml pull
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml -f docker-compose.ghcr.yaml up -d --scale runner=2
```

`IMAGE_TAG` in `.env` pins the version (default `latest`). The images, their tags and how they are released are in [Releases and images](development/releases.md).

## Next steps

- Add runner hosts elsewhere: [Linux with docker](deployment/linux-docker.md), [Linux or macOS without docker](deployment/linux-macos.md), [Windows](deployment/windows.md).
- Write your own checks: [Writing checks](writing-checks.md).
- Before exposing anything beyond this machine, read [Security](security.md).
