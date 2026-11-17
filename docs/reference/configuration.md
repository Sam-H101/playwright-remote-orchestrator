# Configuration reference

Every setting is an environment variable; the agents also take flags (see [Command line](cli.md)). Defaults are those in the code; compose may pass its own (noted where it differs).

## Compose (`.env`) {#compose-env}

`docker compose` reads `.env` from the repository root; copy `.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `GATEWAY_AGENT_TOKEN` | required | Bootstrap token the compose runners send to register |
| `GATEWAY_CLIENT_TOKEN` | required | Bootstrap token the example check sends to open a session |
| `TOKEN_CACHE_S`, `TOKEN_RECHECK_S` | `10`, `15` | [Access token](../concepts/access-tokens.md) timing, see [Hub](#hub) |
| `GATEWAY_PORT` | `4500` | Host port the hub publishes |
| `GATEWAY_URL` | `ws://hub:4500` | Hub URL the UI shows; the checks connect to `<GATEWAY_URL>/connect` |
| `GATEWAY_NODE` | unset | Keep the example check on this node (`?node=`) |
| `RUNNER_SLOTS` | `4` | Slots per runner container |
| `RUNNER_BLOCKED_NETWORKS` | `169.254.0.0/16` | Networks the runners' browsers can't reach, comma-separated, IPv4 or IPv6; empty for none (see [Security](../security.md#network)) |
| `EPHEMERAL_GRACE_S` | `120` | Seconds before the hub removes a gone runner container |
| `GATEWAY_QUEUE_TIMEOUT_S`, `GATEWAY_QUEUE_MAX`, `GATEWAY_CONNECT_ATTEMPTS` | `60`, `200`, `3` | [Waiting and failover](../concepts/routing.md#waiting), see [Hub](#hub) |
| `GATEWAY_HUB_LEASE_S` | `90` | [Hub lease](../concepts/routing.md#claims), for the hub and the UI |
| `SLOT_FAIL_THRESHOLD`, `SLOT_FAIL_COOLDOWN_S` | `3`, `300` | [Slot failures](../concepts/slot-failures.md), for the hub and the UI |
| `GRID_ADMIN_TOKEN` | unset | Token for the UI's management actions: the Access page needs it, or single sign-on. With single sign-on, a break-glass sign-in with every permission |
| `GRID_UI_ALLOWED_HOSTS` | unset | Extra host names the UI accepts actions through |
| `GRID_SECRETS_KEY` | unset | Key the UI encrypts [MFA secrets](../grid-ui/mfa-secrets.md) and [saved sign-ins](../grid-ui/saved-sign-ins.md) with. The MFA page needs it, and so do checks to use saved sign-ins |
| `GRID_MFA_TOKEN` | unset | The client token compose's checks that use MFA codes or saved sign-ins (`poc_totp_login`, `poc_signin_reuse`) ask the Grid UI with, instead of `GATEWAY_CLIENT_TOKEN` |
| `GRID_OIDC_ISSUER`, `GRID_OIDC_CLIENT_ID`, `GRID_OIDC_CLIENT_SECRET` | unset | [Single sign-on](../sso/index.md) for people: the identity provider, and the UI's registration there (the secret, or [the way without one](../sso/secretless.md)) |
| `GRID_UI_PUBLIC_URL` | unset | The address people open the UI at; needed with single sign-on |
| `GRID_OIDC_ADMINS` | unset | People who are always admins |
| `GRID_OIDC_LABEL`, `GRID_OIDC_SCOPES`, `GRID_OIDC_GROUPS_CLAIM`, `GRID_OIDC_CLIENT_AUTH`, `GRID_OIDC_FEDERATED_TOKEN_FILE`, `GRID_OIDC_MANAGED_IDENTITY`, `GRID_OIDC_END_SESSION`, `GRID_OIDC_CA_FILE`, `GRID_OIDC_ALLOW_HTTP`, `GRID_OIDC_CLOCK_SKEW_S` | see [Grid UI](#grid-ui) | The rest of single sign-on's settings |
| `GRID_SESSION_HOURS`, `GRID_SESSION_IDLE_MINUTES` | `12`, `120` | How long a sign-in lasts, and how long unused |
| `GRID_UI_READS` | `signed-in` with single sign-on, else `open` | Whether the overview, activity and sessions need sign-in |
| `GRID_UI_PROXY_HOPS` | `0` | Reverse proxies in front of the UI |
| `GRID_AUDIT_DAYS` | `365` | How long the UI's audit log keeps changes |
| `GRID_UI_LOG_LEVEL`, `GRID_UI_LOG_FORMAT`, `GRID_UI_ACCESS_LOG` | `info`, `text`, `0` | What the UI's log holds, as text or JSON lines, and whether each request gets a line |
| `MYSQL_ROOT_PASSWORD`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` | see `.env.example` | MySQL service credentials (`docker-compose.mysql.yaml`) |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql`, `3306`, `orchestrator` x3 | An existing MySQL server, without `docker-compose.mysql.yaml` |
| `HUB_DB` | `mysql` | `memory` runs the hub without a database (testing only) |
| `IMAGE_TAG` | `latest` | Image version with `docker-compose.ghcr.yaml`: a release (`1.2.0`), `1.2`, `latest` or `edge` |
| `POC_DURATION_S`, `POC_INTERVAL_S`, `POC_GATEWAY_NODE` | `180`, `600`, unset | The proof-of-concept checks of `docker-compose.poc.yaml` |

## Hub {#hub}

Read by `gateway/hub.js` (`gateway/lib/config.js`). Integer settings outside their range stop the hub at startup with an error naming the setting.

| Variable | Default | Purpose |
| --- | --- | --- |
| `HUB_PORT`, `HUB_HOST` | `4500`, `0.0.0.0` | Listen address |
| `GATEWAY_AGENT_TOKEN` | unset | Bootstrap agent token. Agents must send it or a managed agent token. With neither, any agent is accepted (logged as a warning) |
| `GATEWAY_CLIENT_TOKEN` | unset | Bootstrap client token, sent by clients in the `x-gateway-token` header (or `Authorization: Bearer`). With neither it nor a managed client token, clients aren't checked |
| `TOKEN_CACHE_S` | `10` | How long a managed token lookup is cached (an unknown token: 2 s). A revoked token can still open new connections this long |
| `TOKEN_RECHECK_S` | `15` | How often the tokens of connected agents are checked again; revoked or expired ones are closed |
| `GATEWAY_ALLOW_OPEN_AGENTS`, `GATEWAY_ALLOW_OPEN_CLIENTS` | off | `1`: let tokenless agents / clients in again once every managed token of the kind is revoked (see [Access tokens](../concepts/access-tokens.md#when-the-hub-requires-a-token)) |
| `GATEWAY_ALLOW_QUERY_TOKEN` | off | `1`: accept a client token in the URL (`/connect?gateway_token=`), with a warning in the log for each client, while old checks are moved to the header. Off, the hub refuses it with 401: a token in a URL ends up in logs, proxies and shell history |
| `HUB_DB` | `mysql` | `mysql` or `memory` |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql`, `3306`, `orchestrator` x3 | MySQL connection. The hub waits up to about 2 minutes for MySQL at startup |
| `SCHEMA_PATH` | `src/schema.sql` | Schema run at startup |
| `HEARTBEAT_TIMEOUT_S` | `30` | Drop an agent that has been silent this long |
| `OPEN_TIMEOUT_S` | `15` | How long a slot gets to open a session before the client is tried on another one |
| `GATEWAY_QUEUE_TIMEOUT_S` | `60` | How long a `/connect` client waits for a free slot that fits it before a 503 (`0`: no waiting) |
| `GATEWAY_QUEUE_MAX` | `200` | How many clients may wait at once; one more gets a 503 |
| `GATEWAY_CONNECT_ATTEMPTS` | `3` | How many slots one client is tried on before it gets the error (1-20) |
| `GATEWAY_HUB_LEASE_S` | `90` | The hub renews its row in `hubs` every third of this. Another hub frees the claims of a hub not heard from for this long, so keep it well above any MySQL outage you expect to ride out (at least 10) |
| `SLOT_FAIL_THRESHOLD` (or `GRID_FAIL_THRESHOLD`), `SLOT_FAIL_COOLDOWN_S` | `3`, `300` | [Slot failures](../concepts/slot-failures.md): the same names and defaults as the Grid UI |
| `EPHEMERAL_GRACE_S` | `120` | Remove an ephemeral node and its free slots once it has been gone this long (checked at startup and every 60 s; in-use slots go when released) |

The hub's HTTP endpoints and the answers a failed connect gets are in [Hub responses and close codes](responses.md).

## Agent {#agent}

`gateway/agent.js` and `pwrunner run`. Flags override the environment variables.

| Flag | Variable | Default | Purpose |
| --- | --- | --- | --- |
| `--hub` | `HUB_URL` | required | `ws://` or `wss://` hub address |
| `--token` | `GATEWAY_AGENT_TOKEN` | unset | Agent token (bootstrap or managed), sent as `Authorization: Bearer` |
| `--name` | `NODE_NAME` | host name | Node name (see [Node names](../concepts/hub-and-nodes.md#node-names)) |
| `--slots` | `SLOTS` | `4` | run-server processes (1-64) |
| `--base-port` | `BASE_PORT` | `3100` | Slot i listens on 127.0.0.1:(base+i-1) |
| `--playwright-cli` | `PLAYWRIGHT_CLI` | `playwright-core` from node_modules | Path to playwright-core's `cli.js` |
| `--browsers` | `BROWSERS` | `chromium` (`chromium,firefox,webkit` in the runner image) | Browsers installed here; the hub only routes clients that ask for one of them to this node |
| `--status-file` | `STATUS_FILE` | unset | JSON status, rewritten every 5 s |
| `--log-dir` | `LOG_DIR` | unset | run-server output goes to `<dir>/slot-<i>.log` instead of stdout |
| `--ephemeral` | `EPHEMERAL` | off (`1` in the runner image) | The hub removes the node `EPHEMERAL_GRACE_S` after it disconnects |
| `--run-server-user` | `RUN_SERVER_USER` | unset (`pwuser{slot}` in the runner image) | Runs the run-servers, and so the browsers, as this user (Linux and macOS). `{slot}` in the name gives each slot its own (`pwuser{slot}`: `pwuser1` for slot 1, `pwuser2` for slot 2, ...), which every slot needs so that checks running at the same time can't read each other's browser profiles; a plain name is one user for every slot, and the agent warns about it. The agent must start as root, and no user can be root or be another slot's. Unset, they run as the agent's own user and the agent warns at startup (see [Security](../security.md#per-slot-isolation)) |

The run-servers get the agent's environment (`PLAYWRIGHT_BROWSERS_PATH` included) without `GATEWAY_AGENT_TOKEN`.

### pwrunner run {#pwrunner-run-variables}

`pwrunner run` (the Go agent) takes the same flags and variables, plus:

| Flag | Variable | Default | Purpose |
| --- | --- | --- | --- |
| `--node` | `PWRUNNER_NODE` | `runtime\node\node.exe` next to the exe, else `node` on PATH | Node binary for the run-servers |
| `--watchdog` | `PWRUNNER_WATCHDOG` | built-in copy | `parent-watchdog.js` preloaded into each run-server |
| `--config` | | unset | Read defaults from an install's `backend.json` (flags, then variables, then the file) |
| `--log-level` | | | Log verbosity: `info` or `debug` |

Its run-servers get an allow-listed environment (system paths, `TEMP`, proxy variables, `PLAYWRIGHT_BROWSERS_PATH`, `PLAYWRIGHT_DOWNLOAD_HOST`), never the agent token or a machine-wide `NODE_OPTIONS`. The Windows service reads only `backend.json` and `agent.token`, never these variables.

### Runner image only

| Variable | Default | Purpose |
| --- | --- | --- |
| `RUNNER_BLOCKED_NETWORKS` | empty in the image (`169.254.0.0/16` from compose) | Networks closed to the browsers' users (`RUN_SERVER_USER`) by the entrypoint, comma-separated, IPv4 or IPv6. Needs `NET_ADMIN` and `SETPCAP`, which the entrypoint then drops. Needs `RUN_SERVER_USER` |

## Checks (`main.py`) {#checks}

`example_playwright` and other `main.py` containers.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` | `mysql:` section of `src/database.yaml` | MySQL connection, only to create the tables, sync `hosts:` and free stale claims. Each run tries it once (5 s connect timeout); if MySQL is down the check runs anyway |
| `GATEWAY_URL` | `ws://hub:4500` | The hub. The check gets `<GATEWAY_URL>/connect` as `PW_WS_ENDPOINT`; a `/connect/<node>/<slot>` address is refused |
| `GATEWAY_CLIENT_TOKEN` | unset | Client token (bootstrap or managed), passed to the check in its environment; the check sends it in the `x-gateway-token` header, never in `PW_WS_ENDPOINT` |
| `GATEWAY_NODE` | unset | Keep the check on this node or static host, appended as `?node=`. The hub still picks the slot |
| `GRID_UI_URL` | `http://grid_ui:4444` | The Grid UI, as the check reaches it, for [MFA codes](../grid-ui/mfa-secrets.md) (`src/mfa.py`) and [saved sign-ins](../grid-ui/saved-sign-ins.md) (`src/signin.py`) |
| `GRID_MFA_TOKEN` | `GATEWAY_CLIENT_TOKEN` | Client token `src/mfa.py` asks for MFA codes with, and `src/signin.py` checks out and saves sign-ins with |
| `ORCHESTRATOR_CONFIG` | `src/database.yaml` | Config file (`mysql:`, optional `gateway:` with `url`, `client_token` and `node`, and `hosts:`) |
| `CHECK_TIMEOUT_S` | `1800` | Kill a check that runs longer; its browser connection closes and the hub frees the slot |
| `PW_CONNECT_TIMEOUT_S` | `180` | How long the example check waits for the hub to hand it a browser |
| `SLOT_STALE_AFTER_S` | `max(CHECK_TIMEOUT_S, 1800) + 300` | Free a claim this old that isn't the hub's (its claimer died without releasing it). The hub's claims go with its lease instead |

`main.py` sets `PW_WS_ENDPOINT` for the check it runs (see [Writing checks](../writing-checks.md)).

## Grid UI {#grid-ui}

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
| `GRID_RECORDER_INTERVAL` | `2` | Seconds between the [activity](../grid-ui/index.md#activity) recorder's snapshots while no page is open (`0`: none) |
| `GATEWAY_HUB_LEASE_S` | `90` | The hub's value: a session the hub routed is flagged as stale once its hub hasn't renewed its lease for this long |
| `GRID_ADMIN_TOKEN` | unset | The admin token, a sign-in with every permission. If set (or with single sign-on), management actions need sign-in. With neither, the Access, MFA and Admin pages are locked. With single sign-on, it is the break-glass way in |
| `GATEWAY_AGENT_TOKEN` | unset | Only whether it is set is read: the Access page then knows the hub has a bootstrap agent token and doesn't warn that creating or revoking a token changes who gets in. Compose passes `set`, not the value |
| `GATEWAY_CLIENT_TOKEN` | unset | The hub's bootstrap client token. Tells the Access page the same for client tokens (so anyone who may make client tokens may make the grid's first), and lets checks that connect with it ask for [MFA codes](../grid-ui/mfa-secrets.md) and use [saved sign-ins](../grid-ui/saved-sign-ins.md) with it too, acting as an admin (only a value of 16 characters or more). Compose passes the value |
| `GRID_SECRETS_KEY` | unset | Encrypts the MFA secrets' seeds and the saved sign-ins' states (AES-256-GCM, key derived with HKDF); at least 16 characters, e.g. `openssl rand -hex 32`. Unset, the MFA page is locked and checks can't check out or save sign-ins (the page still lists them). Changing it makes the stored seeds and states unreadable until the old key is back: codes are refused, and every check signs in again |
| `GRID_UI_ALLOWED_HOSTS` | unset | Comma list of extra host names actions are accepted through (`*` for any) |
| `GRID_OIDC_ISSUER` | unset | The identity provider's issuer, exactly as its ID tokens state it, e.g. `https://login.microsoftonline.com/<tenant id>/v2.0`, `https://accounts.google.com`, `https://<org>.okta.com` or `https://<host>/realms/<realm>`. Setting it turns on [single sign-on](../sso/index.md), and grid actions then need sign-in, even while the other settings are wrong. Multi-tenant Entra addresses (`common`, `organizations`, `consumers`) are refused |
| `GRID_OIDC_CLIENT_ID` | unset | The UI's client id at the provider, where it is registered as a confidential web client. Needed with the issuer; without it: `Set GRID_OIDC_CLIENT_ID to the Grid UI's client id at the identity provider.` |
| `GRID_OIDC_CLIENT_SECRET` | unset | The client secret. Needed unless `GRID_OIDC_CLIENT_AUTH=federated`, and not allowed with it. Without either: `Set GRID_OIDC_CLIENT_SECRET, or GRID_OIDC_CLIENT_AUTH=federated to use a token the platform issues, so the Grid UI can prove to the identity provider that it is the client.` (with `GRID_OIDC_CLIENT_AUTH=basic` or `post`: `GRID_OIDC_CLIENT_AUTH=<it> needs GRID_OIDC_CLIENT_SECRET.`) |
| `GRID_UI_PUBLIC_URL` | unset | The address people open the UI at, e.g. `https://grid.example.com/` (a path prefix is allowed). Needed with the issuer. Register `<it>auth/callback` as the redirect URI. Sign-in started at another host is sent here first, its host is accepted for actions like one in `GRID_UI_ALLOWED_HOSTS`, and its scheme decides whether the cookies are `Secure`. It must be https unless its host is loopback (or `GRID_OIDC_ALLOW_HTTP=1`) |
| `GRID_OIDC_ADMINS` | unset | Comma list of people who are always admins: `sub:<subject>` (at this issuer), or an email address, which counts only when the ID token says it is verified |
| `GRID_OIDC_LABEL` | `single sign-on` | The sign-in button reads "Sign in with <label>" |
| `GRID_OIDC_SCOPES` | unset | Space-separated scopes asked for on top of `openid email profile`, e.g. `groups` for Okta |
| `GRID_OIDC_GROUPS_CLAIM` | `groups` | Comma list of the ID token's claims whose values [group mappings](../grid-ui/users-and-roles.md#group-mappings) match, e.g. `roles` for Entra app roles. Top-level claims only |
| `GRID_OIDC_CLIENT_AUTH` | `auto` | How the UI authenticates to the token endpoint: `auto`, `basic`, `post` or `federated`. `auto` uses the client secret: `client_secret_basic` when the provider lists it or lists nothing, else `client_secret_post`. Set `basic` or `post` when the client is registered for one secret method only (an Okta app set to "Client secret post"). Set `federated` to send the token the platform issues in place of a secret (see [Without a client secret](../sso/secretless.md)); `auto` never chooses it, and the platform's token is never used unless this says `federated`. `federated` needs one token source (`GRID_OIDC_FEDERATED_TOKEN_FILE`, `GRID_OIDC_MANAGED_IDENTITY`, or the `AZURE_FEDERATED_TOKEN_FILE` of a Kubernetes pod) and no client secret, and sends the token over https (http to a loopback host, or anywhere with `GRID_OIDC_ALLOW_HTTP=1`, warned; a managed identity's token over https to Entra ID only). Any other value: `GRID_OIDC_CLIENT_AUTH is auto, basic, post or federated.` |
| `GRID_OIDC_FEDERATED_TOKEN_FILE` | the `AZURE_FEDERATED_TOKEN_FILE` Azure workload identity sets in a Kubernetes pod | With `GRID_OIDC_CLIENT_AUTH=federated` only: the platform's token file, as a path inside the container, read at every sign-in (at most 16384 bytes, and never kept). Set together with a client secret or `GRID_OIDC_MANAGED_IDENTITY`, or without `federated`, nobody signs in with single sign-on until one of them is removed. Never the pod's Kubernetes API token (`/var/run/secrets/kubernetes.io/serviceaccount/token`) |
| `GRID_OIDC_MANAGED_IDENTITY` | unset | With `GRID_OIDC_CLIENT_AUTH=federated` only, on App Service, Functions, Container Apps or an Azure VM: send the managed identity's token, asked for from the platform's endpoint at every sign-in (never kept). The client id of a user-assigned managed identity, or `system` for the endpoint's default identity. Needs an Entra ID `GRID_OIDC_ISSUER`. On a VM, only where nothing else can reach `169.254.169.254` (see [a managed identity](../sso/secretless.md#managed-identity)). Not for Kubernetes: use the token file there |
| `GRID_OIDC_END_SESSION` | unset | `1`: signing out of the grid also signs out of the provider. No ID token hint is sent, so it works with Entra and Keycloak 18 and later, not with Okta |
| `GRID_OIDC_CA_FILE` | unset | Extra CA certificates (PEM) for a provider behind a private CA, as a path inside the container, added to the system's |
| `GRID_OIDC_ALLOW_HTTP` | unset | `1` allows a plain http issuer, endpoints and public URL that aren't loopback, and with `GRID_OIDC_CLIENT_AUTH=federated` sends the platform's token over that http (warned in the log and on the Admin page). Development only (logged as a warning). Plain http to the provider never goes through `HTTP_PROXY`. Never applies to a managed identity, whose token goes only to Entra ID over https |
| `GRID_OIDC_CLOCK_SKEW_S` | `60` | Leeway for the ID token's times, 0-300 seconds |
| `GRID_SESSION_HOURS` | `12` | How long a sign-in lasts, 1-168 hours |
| `GRID_SESSION_IDLE_MINUTES` | `120` | Ends a session unused this long, 5-1440 minutes (`0`: never). An open page polls, so this counts closed or sleeping tabs |
| `GRID_UI_READS` | `signed-in` with single sign-on, else `open` | `open` or `signed-in`: whether the overview, activity, sessions, `/status` (`/api/status`) and `/api/events` need "See the grid". Any other value means `signed-in` (logged). Without any sign-in set up they are open whatever it says |
| `GRID_UI_PROXY_HOPS` | `0` | How many reverse proxies you trust in front of the UI. Above 0, `X-Forwarded-For` is believed that many hops deep, for the guessing and sign-in limits and the addresses sessions and the audit log keep. Nothing else is taken from forwarded headers |
| `GRID_AUDIT_DAYS` | `365` | How long the audit log keeps rows, 30-3650 days. Any other value means 365 (logged) |
| `GRID_UI_LOG_LEVEL` | `info` | The least a line must be to reach [the log](../grid-ui/index.md#the-log): `debug`, `info`, `warning` or `error`. Any other value means `info` (logged). `warning` and `error` hold gunicorn's own lines to the same; `debug` leaves gunicorn's at info |
| `GRID_UI_LOG_FORMAT` | `text` | `text` or `json`: how each line of the log is written, gunicorn's own included. Any other value means `text` (logged) |
| `GRID_UI_ACCESS_LOG` | `0` | `1` adds a line per request to the log, whatever the level: the method, the path without its query string, the status, how long it took and the client's address. Any other value means `0` (logged) |
| `GRID_DB_ENGINE`, `GRID_DB_PATH` | `mysql` | `sqlite` plus a path reads a sqlite DB instead (demo) |
| `GRID_DEMO_ACTIVITY` | unset | `1`: `python -m ui.demo` runs short bursts of checks every few seconds (as `--activity`) |
| `GRID_SCHEMA_PATH` | `src/schema.sql` | Schema the UI runs at startup (MySQL only) |
| `GRID_UI_HOST`, `GRID_UI_PORT` | `0.0.0.0`, `4444` | Listen address for `python -m ui.app` |

Platform variables the Grid UI reads but you don't set: `AZURE_FEDERATED_TOKEN_FILE` (Azure workload identity in a pod), and `IDENTITY_ENDPOINT` and `IDENTITY_HEADER` (App Service, Functions and Container Apps managed identity). See [Without a client secret](../sso/secretless.md).

A single sign-on setting that is wrong (missing, out of range, a CA file that can't be read) stops people signing in with single sign-on until it is fixed, while grid actions stay protected: the Grid UI's log and the Admin page's **Single sign-on** tab say what is wrong.

## Database config file (`src/database.yaml`)

`main.py` and the Grid UI read `ORCHESTRATOR_CONFIG` (default `src/database.yaml`) for:

- `mysql:` the MySQL connection, used when the `DB_*` variables are not set;
- `gateway:` (optional, `main.py` only) `url`, `client_token` and `node`;
- `hosts:` legacy static hosts (`server`, `port_start`, `port_end`; `port_end` is exclusive). See [Migrating](../operations/migrating.md).
