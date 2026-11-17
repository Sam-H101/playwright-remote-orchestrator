# Security model

A check controls a real browser on a runner host. Whoever can open a session can make that browser read files and reach addresses as the browser's user. The grid's security rests on four things: who may register a node, who may open a session, what a browser can read on its runner, and what it can reach on the network.

## Tokens

- **Agent tokens decide who can register a node.** Anyone with an unbound one (or `GATEWAY_AGENT_TOKEN`) can register under an existing node's name and receive that node's browser sessions, unless that node uses a [bound token](concepts/access-tokens.md#binding). Treat them as secrets.
- **Client tokens decide who can open a browser on your runners.** Without them the hub would be an open browser proxy into your runners' network. Clients send theirs in the `x-gateway-token` header (or `Authorization: Bearer`). The hub refuses one in the URL (`?gateway_token=`), which logs, proxies and shell history keep, unless `GATEWAY_ALLOW_QUERY_TOKEN` is set; replace a token that has been in a URL. Give each pipeline the narrowest token that works: a leaked client token whose [token scope](concepts/access-tokens.md#token-scopes) is only seeing the grid can't open a browser, get an MFA code or use a saved sign-in.
- **Logging.** The hub logs a managed token only by its first 12 characters and id, never logs the bootstrap tokens, and blanks `gateway_token` in any URL it logs. MySQL keeps only a managed token's sha256 and first 12 characters. `main.py` never logs the client token. The Grid UI's log holds no seed, code, token, cookie value, authorization code or saved sign-in state.
- **Storage on runners.** `pwrunner` keeps the token in `agent.token`, readable only by SYSTEM and Administrators, and never on a command line, in a log or in a JSON file. On Linux, keep it in an env file of mode 600 (`runner.env`, `/etc/playwright-agent.env`).

## Per-slot isolation {#per-slot-isolation}

A check controls a browser on the runner, and that browser can read any file its user can (`file://`), `/proc` included. So the browsers must not run as the agent's user, or a check could read the agent token and use it to receive other checks' sessions.

- **Runner image.** It starts the agent as root and runs each slot's run-server as a user of its own, `pwuser1`, `pwuser2` and so on (`RUN_SERVER_USER=pwuser{slot}`), and compose leaves the agent only the capabilities to switch user and stop them.
- **Linux or macOS host.** Run the agent the same way (see [the systemd example](deployment/linux-macos.md#run-it-as-a-service-systemd)); `pwrunner run` takes the same `--run-server-user`. The agent must start as root, and no slot's user can be root or be another slot's.
- **Windows.** The `pwrunner` service runs each slot's run-server as a local account of its own, which can read what Users can but not `agent.token` or the other slots' profiles (see [Browser accounts](deployment/windows.md#browser-accounts)). `pwrunner run` in a console runs them as the console's user and warns about it.

A user per slot also keeps checks that run at the same time on one node apart: Playwright keeps each browser's profile (cookies, storage) in a folder only its user can open, so with one user for every slot a check could read another's. The agent warns at startup when slots share a user, and when no `RUN_SERVER_USER` is set at all.

- Each run-server only answers at a random path that the agent dials and keeps out of its command line (`GATEWAY_AGENT_RUN_SERVER_PATH` in its environment only), so a browser that reaches another slot's port on `127.0.0.1` can't open a session there; any other path gets HTTP 400. The agent masks the path in run-server's output before it reaches the slot logs.
- The run-servers never get `GATEWAY_AGENT_TOKEN` in their environment. `pwrunner`'s run-servers get an allow-listed environment only.
- Keep files the browsers' users can read free of anything a check shouldn't see: no browser setting stops `file://` reliably, so what that user can read is the limit.

## Network {#network}

That browser can also reach any address the runner can.

- **Compose** puts the runners on a network of their own (`runners`) with the hub and nothing else, so a check can't reach MySQL, the Grid UI or the checks' containers through them.
- **The runner image** closes `RUNNER_BLOCKED_NETWORKS` (comma-separated, IPv4 or IPv6) to the browsers' users with an `iptables`/`ip6tables` REJECT rule per user and network before the agent starts, then drops `NET_ADMIN` for good, so neither the agent nor a browser can open them again. The agent itself can still dial the hub. Compose sets it to link-local (`169.254.0.0/16`) by default, where a cloud VM's metadata service gives out its managed identity's tokens; the image alone sets nothing. Add your internal ranges that checks never need.
- **On a host without Docker**, do the same with the host's firewall: on Linux, an `iptables` rule with `-m owner --uid-owner` for each of the browsers' users (see [Linux or macOS host](deployment/linux-macos.md#block-internal-networks)).
- **On Kubernetes**, use a NetworkPolicy.

## Transport

The hub port speaks plain `ws://` (tokens included). Across any network you don't trust, put a TLS-terminating reverse proxy (nginx, Caddy, Traefik, ...) in front of it, use `wss://` URLs for the agents and `GATEWAY_URL`, and allow WebSocket upgrades and long-lived connections (the agent reconnects if it hears nothing for 45 s). Where you can, also limit the hub port to your runner hosts' addresses.

`GET /healthz` and `GET /api/nodes` on the hub need no token; they show node names, versions and IP addresses.

## The Grid UI

The Grid UI is published on 127.0.0.1 only, and with neither `GRID_ADMIN_TOKEN` nor single sign-on set up anyone who can open it can release slots and forget nodes.

- Before exposing it more widely, set up [single sign-on](sso/index.md), so people sign in as themselves and do what their roles allow, or at least set `GRID_ADMIN_TOKEN` (the admin sign-in, which stays as the break-glass way in once single sign-on is on).
- The Access, MFA and Admin pages need one of them in any case: without either the UI refuses to list, create or revoke tokens.
- Whoever has the admin token can create agent tokens, so guard it like `GATEWAY_AGENT_TOKEN`. Compose passes it to the UI but doesn't require it, because the rest of the UI works without it and the Access page stays locked rather than open.
- The UI refuses cross-site actions, and accepts actions only through an IP address, `localhost`, the machine's own host names, the host of `GRID_UI_PUBLIC_URL` or a name in `GRID_UI_ALLOWED_HOSTS`: list the DNS alias or reverse-proxy name you reach it by.
- Response headers, cookies and ID token checks are described in [Browser security](grid-ui/index.md#browser-security); guessing limits in [Signing in](grid-ui/signing-in.md#guessing).
- "Add hosts" lets someone run a machine that sees everything the checks sent to it do, sign-ins and codes included: give it only to people you'd trust with those.

## Secrets in the Grid UI {#secrets-in-the-grid-ui}

- **MFA secrets** go into the Grid UI and never come out: the seeds are stored encrypted under `GRID_SECRETS_KEY`, and the UI answers with codes and metadata only, never cached, and logs neither seeds nor codes. A client token gets codes for the secrets its owner can use (legacy tokens, tokens made with the admin token and `GATEWAY_CLIENT_TOKEN` act as an admin and get them all), so a client token also works as the second factor of those accounts: store only the accounts your checks sign in with, and treat client tokens accordingly. Keep `GRID_SECRETS_KEY` out of the database's backups; a dump without it holds no usable seed.
- `POST /api/mfa/code` is the one action exempt from the cross-site and host-name rules (with the saved sign-in routes below): it takes nothing but a client token in the `Authorization` header, which a page on another site can't send, and checks reach the UI by its compose name.
- **Saved sign-ins** are kept the same way and are more sensitive still: a saved state is the account's live session cookies, so a check that holds one is signed in as the account with no password and no code. They are encrypted under `GRID_SECRETS_KEY`, the page shows only the cookies' domains and counts (never a value), and a client token checks out the saved sign-ins its owner's checks may use, as it gets the MFA codes its owner may use, so the same caution applies, more so. Only the owner's checks sign in fresh for a sign-in and save it again; checks of the people and roles it is shared with reuse what was saved.
- The checks' `POST /api/signins/checkout`, `/api/signins/save` and `/api/signins/release` are exempt from the cross-site and host-name rules like `/api/mfa/code`, and for the same reason.
- **Forget** a sign-in on the MFA page to stop checks reusing it. Forget only drops the grid's copy and doesn't sign the cookies out: to cut off a copy already checked out (after a client token leaked, say), revoke the account's sessions at the site (in Entra ID, **Revoke sessions** on the user) or reset its password.

## Recommended hardening {#recommended-hardening}

The steps below use only the settings described above. Apply the ones that fit your network.

1. **Set every token.** Set `GATEWAY_AGENT_TOKEN`, `GATEWAY_CLIENT_TOKEN` and `GRID_ADMIN_TOKEN` to long random values (`openssl rand -hex 32`); compose requires the first two. Give each runner host its own [bound agent token](concepts/access-tokens.md#binding), and each pipeline its own client token with the narrowest [token scope](concepts/access-tokens.md#token-scopes) that works, with an expiry.
2. **Block private ranges from the browsers.** Unless your checks test internal sites, extend `RUNNER_BLOCKED_NETWORKS` beyond link-local to the private ranges, for example in `.env`:

    ```sh
    RUNNER_BLOCKED_NETWORKS=169.254.0.0/16,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,100.64.0.0/10,fc00::/7,fe80::/10
    ```

    The rules apply to the browsers' users only, so the agent still reaches the hub; the compose networks (`172.50.221.0/24`, `172.50.222.0/24`) lie outside `172.16.0.0/12`. Leave out any range that holds a site your checks test. For runners started with `docker run`, pass the variable and `--cap-add NET_ADMIN` yourself ([Linux host with docker](deployment/linux-docker.md#keep-the-browsers-off-internal-networks)); on hosts without docker, add the equivalent `iptables` owner rules.
3. **Keep the Grid UI on localhost.** Compose publishes it as `127.0.0.1:4444:4444`. Reach it through a TLS reverse proxy on the same host, set `GRID_UI_PUBLIC_URL` to its https address, `GRID_UI_PROXY_HOPS` to the number of proxies, and `GRID_UI_ALLOWED_HOSTS` to any other name you use. Prefer single sign-on to sharing the admin token.
4. **Narrow the hub port.** The hub must be reachable by every runner host, so compose publishes `GATEWAY_PORT` on all interfaces. Limit it to your runner hosts' addresses with a firewall. If every runner and client is on the same host as a TLS proxy (or the hub is only used by the compose runners and checks), publish it on 127.0.0.1 instead with an override file:

    ```yaml
    # docker-compose.localhost.yaml
    services:
      hub:
        ports: !override
          - "127.0.0.1:${GATEWAY_PORT:-4500}:4500"
    ```

    Add `-f docker-compose.localhost.yaml` to every compose command (`!override` replaces the port list instead of adding to it, and needs Docker Compose 2.24 or later). The compose runners and checks reach the hub over the compose networks and are not affected.
5. **Use TLS across untrusted networks.** Put a TLS-terminating proxy in front of the hub and use `wss://` for agents and `GATEWAY_URL` (see [Transport](#transport)).
6. **Run one user per slot** on every runner host (`RUN_SERVER_USER=<name>{slot}`, or `pwrunner install` on Windows), and keep secrets out of files those users can read.
7. **Protect `GRID_SECRETS_KEY`.** Store it outside the database and its backups, and keep the same key across restarts.
8. **On an Azure VM**, if the Grid UI uses a managed identity, block every other container from `169.254.169.254` (see [Blocking the other containers](sso/secretless.md#blocking-imds)), and never assign the identity to a runner's VM.
9. **Keep the log and audit log private.** Both name people; the audit log keeps them `GRID_AUDIT_DAYS`.
