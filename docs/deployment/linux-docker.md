# Linux host with docker

The published runner image works on any docker host that can reach the hub's port; nothing has to be checked out or built. The image holds the agent (`gateway/agent.js`) and Chromium, Firefox and WebKit, for amd64 and arm64.

## Run a runner

Make an agent token for this host on the Grid UI's **Access** page (**Add a host** binds it to the node name), then:

```sh
read -rsp 'Agent token: ' T && printf 'GATEWAY_AGENT_TOKEN=%s\n' "$T" > runner.env && chmod 600 runner.env && unset T; echo
docker run -d --name pw-runner --restart unless-stopped --init --shm-size 1g --env-file runner.env \
  -e HUB_URL=ws://<orchestrator-host>:4500 -e NODE_NAME=<stable-name> -e EPHEMERAL=0 -e SLOTS=4 \
  ghcr.io/sam-h101/playwright-remote-orchestrator/runner:latest
```

The token goes into `runner.env` (mode 600) rather than onto the command line.

- Use a version tag (`:1.2.0`) instead of `:latest` to pin it; the runner's Playwright version must match your checks' minor version.
- `NODE_NAME` keeps the node's name when the container is recreated (the default is the container ID), and `EPHEMERAL=0` makes the hub show it OFFLINE while it is down instead of removing it.
- Keep `--init` (reaps browser processes) and `--shm-size` (Chromium crashes with docker's 64 MB `/dev/shm`).

To build it yourself instead: `docker build -f docker/runner/Dockerfile -t playwright_orchestrator_runner .` from a checkout, and run that image.

## Keep the browsers off internal networks

To keep the browsers off the host's cloud metadata service or internal ranges your checks never need, add `--cap-add NET_ADMIN -e RUNNER_BLOCKED_NETWORKS=169.254.0.0/16,<ranges>`. The image's entrypoint closes them to the browsers' users and drops the capability again before the agent starts (see [Security](../security.md#network)).

The entrypoint drops `NET_ADMIN` with `setpriv`, which needs `SETPCAP` in the container's bounding set. Docker's default capability set includes `SETPCAP`; if you start the container with `--cap-drop ALL`, add `SETPCAP` back along with `NET_ADMIN` (and `SETUID`, `SETGID` and `KILL` for the agent, as [compose does](docker-compose.md#the-runner-containers-privileges)). The entrypoint refuses to start with `NET_ADMIN` but without `SETPCAP`.

## Upgrading

Pull the new image and recreate the container (the same `docker run`). See [Upgrading Playwright](../operations/upgrading-playwright.md).

## Settings

The container takes the [agent's variables](../reference/configuration.md#agent) (`HUB_URL`, `GATEWAY_AGENT_TOKEN`, `NODE_NAME`, `SLOTS`, `BROWSERS`, `EPHEMERAL`, ...) plus `RUNNER_BLOCKED_NETWORKS`. In the image `BROWSERS` is `chromium,firefox,webkit`, `EPHEMERAL` is `1`, `RUN_SERVER_USER` is `pwuser{slot}` (users `pwuser1` to `pwuser64` exist in the image), `STATUS_FILE` is `/tmp/gateway-agent/status.json` and the container's health check reads it.

!!! warning "`RUNNER_BLOCKED_NETWORKS` is empty in the image"
    The image itself sets `RUNNER_BLOCKED_NETWORKS=` (nothing blocked). `docker-compose.yaml` is what defaults it to `169.254.0.0/16`. A runner started with `docker run` blocks nothing unless you pass the variable (and `NET_ADMIN`) as above.
