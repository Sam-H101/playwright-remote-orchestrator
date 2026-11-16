# Command line

## Agent: `node agent.js` {#agent}

Run from `gateway/`. Flags override the environment variables; the full table with defaults is in [Configuration](configuration.md#agent).

```text
Usage: node agent.js --hub ws://hub:4500 [options]

  --hub <url>             HUB_URL              hub to dial out to (required)
  --token <token>         GATEWAY_AGENT_TOKEN  sent as "Authorization: Bearer <token>"
  --name <name>           NODE_NAME            node name, case-insensitive (default: host name)
  --slots <n>             SLOTS                run-server processes, 1-64 (default 4)
  --base-port <port>      BASE_PORT            slot i listens on 127.0.0.1:<base+i-1> (default 3100)
  --playwright-cli <path> PLAYWRIGHT_CLI       playwright-core cli.js (default: resolved from node_modules)
  --browsers <list>       BROWSERS             browsers installed here (default chromium); the hub only
                                               routes clients asking for one of them to this node
  --status-file <path>    STATUS_FILE          JSON status, rewritten every few seconds
  --log-dir <dir>         LOG_DIR              run-server output goes to <dir>/slot-<i>.log
  --ephemeral             EPHEMERAL=1          the hub removes this node once it has been gone for
                                               EPHEMERAL_GRACE_S (for containers named after their ID)
  --run-server-user <u>   RUN_SERVER_USER      run run-server, and so the browsers, as this user (Linux and
                                               macOS; the agent must start as root). {slot} in the name gives
                                               each slot its own user (pwuser{slot}: pwuser1, pwuser2, ...)
```

- Flags take `--flag value` or `--flag=value`; `--ephemeral` alone means true, `--ephemeral=false` turns it off. Boolean variables take `1`/`true`/`yes`/`on` or `0`/`false`/`no`/`off`.
- Without `--playwright-cli`, the agent resolves `playwright-core` from the working directory, `gateway/`, or `runner/playwright` (where `npm ci` installs the pinned version in a checkout).
- When the hub turns the agent away, it logs why and keeps retrying (see [close codes](responses.md#agent-close-codes)).

## pwrunner

`pwrunner.exe` on Windows (also built for Linux). `pwrunner <command> --help` prints a command's options.

| Command | Does |
| --- | --- |
| `install` | Install or upgrade this host as a runner node (Windows service) |
| `status` | Report the node's health (exit 0 healthy, 2 problem, 1 not installed) |
| `uninstall` | Stop and remove the service (`--remove-files`: and the install folder) |
| `start`, `stop`, `restart` | Control the installed service |
| `token` | `set --stdin` \| `set --file <path>` \| `clear` \| `show-prefix` |
| `run` | Run the agent in the foreground (flags, then `HUB_URL` and the other variables, then `--config backend.json`) |
| `fetch` | Download and verify the pinned Node and playwright-core into `<install dir>\cache` |
| `node` | Run the private node with the run-servers' environment: `pwrunner node -- <args>` |
| `selfcheck` | Check an install (`--quick`: no network) |
| `version` | Print the version, Go toolchain and the pinned runtime |

Every command that works on an install takes `--install-dir <path>`: default this exe's folder if it holds `backend.json`, else `C:\PlaywrightBackend` (`/opt/pwrunner` on Linux).

### `pwrunner install` {#pwrunner-install}

| Flag | Does |
| --- | --- |
| `--hub-url <url>` | `ws://` or `wss://` address of the hub (kept on a re-run) |
| `--token-stdin` | Read the agent token from standard input (preferred) |
| `--token-file <path>` | Read the agent token from a file |
| `--token <token>` | Agent token on the command line; prefer the two above (`""` deletes the installed one) |
| `--name <name>` | Node name (default: host name; kept on a re-run) |
| `--slots <n>` (alias `--port-count`) | run-server processes, 1-64 (default 4) |
| `--base-port <port>` (alias `--port-start`) | Slot i listens on `127.0.0.1:<base+i-1>` (default 3100) |
| `--browsers <a,b>` | Browsers to install (default `chromium`) |
| `--report-url <url>` | POST the install report there |
| `--health-timeout <sec>` | How long to wait for the node to serve (default 60) |
| `--service-name <name>` | Windows service name (default `PlaywrightBackend`) |
| `--no-service` | Run the agent in the background for this session instead of as a service |
| `--download-host <url>` | `PLAYWRIGHT_DOWNLOAD_HOST` for the browser download |
| `--browsers-from <dir>` | Copy browsers from a folder instead of downloading them |
| `--skip-browsers` | Don't install browsers |
| `--force-reset` | Reset the install folder as if other users could have changed it |
| `--json` | Print the report as JSON |
| `--playwright-version <v>` | playwright-core version (default: the pinned one); not kept on a re-run |
| `--playwright-integrity <sha512-...>` | npm integrity of an unpinned playwright-core version |
| `--node-version <v>` | Node version (default: the pinned one) |
| `--node-sha256 <hex>` | SHA-256 of an unpinned Node version's archive |
| `--offline-dir <dir>` | Folder holding the Node archive and playwright-core tarball (the release's `pwrunner-runtime-<version>-windows-amd64.zip` contents) |
| `--node-mirror <url>` | Node download mirror (default `https://nodejs.org/dist`) |
| `--npm-registry <url>` | npm registry (default `https://registry.npmjs.org`) |

`--service-account` is refused: the service runs as LocalSystem and each slot's browsers as its own account (see [Browser accounts](../deployment/windows.md#browser-accounts)).

Exit codes: `0` registered and every slot healthy, `2` not registered or some slots unhealthy, `1` failed.

### `pwrunner status`

`--json`, `--report-url <url>` (default: `backend.json`'s `reportUrl`), `--health-timeout <sec>` (keep re-reading for up to this long while not healthy; default 0). Exit codes as for `install`, `1` = not installed.

### `pwrunner start` / `stop` / `restart`

`--timeout <sec>`: how long to wait (default 30).

### `pwrunner token`

`set --stdin`, `set --file <path>`, `clear`, `show-prefix`. Setting the token restarts the service; an empty token is refused (use `clear`). `show-prefix` prints only a managed token's 12-character label.

### `pwrunner uninstall`

`--remove-files` also deletes the install folder, browsers included; `--yes` doesn't ask for confirmation.

### `pwrunner selfcheck`

Checks each slot's browser account, the folder and token permissions, the runtime, the slot ports, the hub and the Node end-of-life date. `--quick` skips the checks that need the network.

### `pwrunner fetch`

Takes `--install-dir` and the runtime source flags of `install` (`--playwright-version` to `--npm-registry`).

### `pwrunner run` {#pwrunner-run}

The same flags and variables as `node agent.js`, plus `--node`, `--watchdog`, `--config <backend.json>` and `--log-level info|debug` (see [Configuration](configuration.md#pwrunner-run-variables)). On Linux or macOS, start it as root with `--run-server-user 'pwuser{slot}'` and one user per slot; root, unknown users and two slots resolving to the same uid are refused, and so is the flag on Windows.

## Orchestrator: `main.py` {#main-py}

```text
python3 /app/main.py --fil <check.py> --fol <folder> [-i <seconds>] [--fr true]
```

| Flag | Required | Does |
| --- | --- | --- |
| `--fil FILE` | yes | Test file to run (pytest) |
| `--fol FOLDER` | yes | Check folder; logs go to `<fol>/logs/` |
| `-i INTERVAL` | no | Run the check every INTERVAL seconds; without it, run once and exit 0 (passed) or 1 (failed, or it got no browser) |
| `--fr true` | no | Create the tables and sync `hosts:`, then exit |

See [Writing checks](../writing-checks.md#running-checks-with-mainpy).

## Grid UI demo: `python -m ui.demo`

`python -m ui.demo demo.db --heartbeat [--activity]` writes a sqlite demo grid; `--activity` (or `GRID_DEMO_ACTIVITY=1`) runs short bursts of checks every few seconds. Serve it with `GRID_DB_ENGINE=sqlite GRID_DB_PATH=demo.db python -m ui.app`. See [Grid UI](../grid-ui/index.md#running-it).
