# Linux or macOS host without docker

Run `gateway/agent.js` directly with Node. playwright-core (the version `runner/playwright` pins) and the browsers go outside the checkout, in fixed folders, so `npm ci` after a `git pull` keeps them and the service user finds the browsers.

## Install

```sh
sudo git clone <this repo> /opt/playwright-remote-orchestrator
cd /opt/playwright-remote-orchestrator/gateway && sudo npm ci --omit=dev
sudo mkdir -p /opt/playwright-core && sudo cp /opt/playwright-remote-orchestrator/runner/playwright/package*.json /opt/playwright-core/
cd /opt/playwright-core && sudo npm ci --omit=dev
sudo PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright node /opt/playwright-core/node_modules/playwright-core/cli.js install --with-deps chromium
```

On macOS drop `--with-deps`. Install every browser you list in `BROWSERS` (the default is `chromium`).

## Run it as a service (systemd) {#run-it-as-a-service-systemd}

The agent reconnects to the hub by itself; run it at boot.

1. Put `GATEWAY_AGENT_TOKEN=<token>` in `/etc/playwright-agent.env` (mode 600, owned by root).
2. Create a user for each slot's browsers:

    ```sh
    for i in 1 2 3 4; do sudo useradd --system --create-home pwagent$i; done
    ```

3. Add `/etc/systemd/system/playwright-agent.service`:

    ```ini
    [Unit]
    Description=Playwright runner agent
    Wants=network-online.target
    After=network-online.target

    [Service]
    User=root
    CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_KILL
    NoNewPrivileges=yes
    WorkingDirectory=/opt/playwright-remote-orchestrator/gateway
    EnvironmentFile=/etc/playwright-agent.env
    Environment=HUB_URL=ws://<orchestrator-host>:4500 SLOTS=4 RUN_SERVER_USER=pwagent{slot}
    Environment=PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
    Environment=PLAYWRIGHT_CLI=/opt/playwright-core/node_modules/playwright-core/cli.js
    ExecStart=/usr/bin/env node agent.js
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
    ```

4. `sudo systemctl enable --now playwright-agent`.

The agent starts as root with only the capabilities to switch user and stop its run-servers, and runs slot 1's as `pwagent1`, slot 2's as `pwagent2` and so on (`RUN_SERVER_USER=pwagent{slot}`), so a check's browser can read neither the token nor the browser profile of a check running in another slot (see [Security](../security.md#per-slot-isolation)). Raising `SLOTS` needs a user for each new slot first.

On macOS use a launchd job with the same environment.

!!! warning "Keep `PLAYWRIGHT_BROWSERS_PATH` the same"
    A slot's health is only whether its run-server answers, so a node whose browsers are missing shows healthy and every session fails. Use the same `PLAYWRIGHT_BROWSERS_PATH` for the install and the service.

## Block internal networks

The runner image closes `RUNNER_BLOCKED_NETWORKS` to the browsers' users itself; on a host without docker, do the same with the host's firewall. On Linux, that is an `iptables` rule with `-m owner --uid-owner` for each of the browsers' users, for example:

```sh
for i in 1 2 3 4; do
  sudo iptables -A OUTPUT -m owner --uid-owner pwagent$i -d 169.254.0.0/16 -j REJECT
done
```

This is the same rule the runner image's entrypoint adds for each blocked network; add one per range your checks never need (see [Recommended hardening](../security.md#recommended-hardening)) and keep the rules across reboots with your distribution's tool.

## Options

`node agent.js --help` lists the options. All of them are in [Command line](../reference/cli.md#agent) and [Configuration](../reference/configuration.md#agent).

## Upgrading

After a `git pull`, repeat the copy, `npm ci` and browser install steps above, then restart the agent. See [Upgrading Playwright](../operations/upgrading-playwright.md).

## `pwrunner run` instead of Node

`pwrunner` also builds for Linux. `pwrunner run` takes the same flags and variables as `agent.js`, `--run-server-user` included; see [Command line](../reference/cli.md#pwrunner-run).
