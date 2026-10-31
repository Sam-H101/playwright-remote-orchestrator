# playwright-remote-orchestrator

## Purpose

* This was built as a method to run multiple tests against any number of servers. It will pick a random server and port and run the checks against them. It was built as playwright currently has a limitation where one can only run checks on the same box as where the code lives. This allows the checks to be put into docker containers and run centeralized while the actual orchestrators are ran in a number of places. 

## Limitations

* Currently it does not know if the remote nodes are up or down. Future state it will use the failure counters + a pinger to mark them up / down.
* Port and host state is kept in MySQL, so every orchestrator container just needs to reach the MySQL service. Ports are claimed with an atomic update, so two containers never get the same port.


## Building 

1. Run docker-compose build

## Running with MySQL

The MySQL service lives in its own compose file, `docker-compose.mysql.yaml`. Start it together with the main file:

```
docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d
```

Credentials default to `orchestrator` / `orchestrator`. Override them with a `.env` file or shell variables: `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD`. The orchestrator containers read `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` and `DB_PASSWORD`, which fall back to the `mysql:` section of `src/database.yaml`.

To use an existing MySQL server instead, skip `docker-compose.mysql.yaml` and set those `DB_*` variables (or `src/database.yaml`) to point at it.


## Running the checks with local server

1. uncomment the CMD line in `docker/python/Dockerfile` (the image ships the Playwright server, no npm install needed)
2. Build the image and it should start if you do docker-compose up
* Note this has not been tested

## Running a remote server

The orchestrator image uses Playwright **1.64.1**. Playwright only accepts connections between a client and server on the same minor version, so every remote node must run the same version (see [here][l1] for install docs).

### Windows

`installer/windows/install-backend.ps1` sets up a Windows host as a remote executioner. It needs Node.js 20+ (or pass `-InstallNode` to get it from winget) and an elevated PowerShell.

```powershell
.\installer\windows\install-backend.ps1 -PortStart 3000 -PortCount 4
```

* Installs `playwright-core` and only the browsers you list (`-Browsers chromium` by default) into `C:\PlaywrightBackend`. Re-running skips anything already at the right version, so upgrades only download what changed.
* `-PlaywrightVersion` defaults to 1.64.1 and must match the orchestrator's Playwright version.
* Runs one `run-server` per port under a supervisor (scheduled task `PlaywrightBackend`, runs as SYSTEM at startup) that restarts crashed servers, and opens the ports in Windows Firewall.
* Health-checks every port before it exits and reports the result: a console summary with the entry to paste into `src/database.yaml`, `C:\PlaywrightBackend\status.json`, an optional JSON POST to `-ReportUrl`, and the exit code (`0` all ports healthy, `2` some ports unhealthy, `1` failed).
* `C:\PlaywrightBackend\backend-status.ps1 [-Json] [-ReportUrl <url>]` checks the host later with the same exit codes. `uninstall-backend.ps1 [-RemoveFiles]` removes it.
* Logs are in `C:\PlaywrightBackend\logs` (`install.log`, `supervisor.log`, `server-<port>.*.log`).

### Other platforms

1. Install Node.js, then install the matching Playwright version and its browsers on the remote executioner host: `npm install playwright@1.64.1 && npx playwright install --with-deps`
2. start the server by running `npx playwright run-server --port <port number> --host 0.0.0.0`
   * Drop the old `--reuse-browser false` flag; current Playwright releases no longer accept it.

### Upgrading Playwright

Bump the version in all places together: `PLAYWRIGHT_VERSION` in `docker/python/Dockerfile`, `playwright==` in `docker/python/requirements.txt`, the default `$PlaywrightVersion` in `installer/windows/install-backend.ps1`, and the `run-server` install on every remote node (re-run the Windows installer with `-PlaywrightVersion <new>`).

## Configuration

1. modify `src/database.yaml` with the servername, start port, and end port
2. The tables are created and seeded automatically the first time a check runs. You can also do it up front by appending `--fr true` to the command and running it once. Re-running it is safe, and it adds rows for any new hosts or ports you add to `src/database.yaml`.

## Grid UI

A management console in the style of a Selenium Grid dashboard lives in `ui/`. It shows every node (a host from `src/database.yaml`), its slots (ports), live sessions (ports currently in use) and failure counts, and refreshes every 5 seconds. It reads the same MySQL database as the orchestrator, with the same `DB_*` settings.

* With docker: `docker compose -f docker-compose.yaml -f docker-compose.mysql.yaml up -d grid_ui mysql`, then open http://localhost:4444
* Locally: `pip install -r ui/requirements.txt`, set the `DB_*` variables (or the `mysql:` section of `src/database.yaml`), then `python -m ui.app` from the repo root

Pages and endpoints:

* **Overview**: one card per node with status (UP / DEGRADED / DOWN), sessions vs. max concurrency, and a map of every slot. Click a slot to see its details, reset its failure count or force release it.
* **Sessions**: sortable table of running sessions with their `ws://` endpoint, start time and duration.
* `GET /status` (or `/api/status`): the same data as JSON, plus a `ready` flag.

Settings (environment variables):

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection, shared with the orchestrator |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file to read the `mysql:` section from |
| `GRID_FAIL_THRESHOLD` | `3` | Failures at which a slot counts as failing |
| `GRID_STALE_AFTER` | `3600` | Seconds after which a running session is flagged as stale (likely a check that died without releasing its port) |
| `GRID_ADMIN_TOKEN` | unset | If set, management actions need this token |
| `GRID_DB_ENGINE` | `mysql` | Set to `sqlite` (with `GRID_DB_PATH`) to read a legacy `runner.db` instead |

The compose file publishes the UI on 127.0.0.1 only. Set `GRID_ADMIN_TOKEN` before exposing it more widely.

## Creating a check

* look at `full/example/example.py` for reference. If you need the logs, first make the log directory then add it to the docker-compose volumes config `- ./logs/example_playwright/:/app/logs/`. 
* This check scrapes docusign and looks to see the latest status on the Docusign Nodes. 







[l1]: https://playwright.dev/docs/intro
