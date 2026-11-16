# Writing checks

Any Playwright client can use the grid: it connects to the hub's `/connect` with a client token, launches the browser it wants, and the hub hands it a free slot. The orchestrator (`main.py`) is one such client; it runs pytest checks on an interval.

## Connecting from any Playwright client

Every test connects to the same address, the hub, and the hub picks the runner.

=== "JavaScript"

    ```js
    const { chromium } = require('playwright');

    const HUB = 'ws://<orchestrator-host>:4500';
    const browser = await chromium.connect(`${HUB}/connect?gateway_token=${process.env.GATEWAY_CLIENT_TOKEN}`);
    ```

=== "Python"

    ```python
    import os
    from playwright.sync_api import sync_playwright

    HUB = "ws://<orchestrator-host>:4500"
    with sync_playwright() as p:
        browser = p.firefox.connect(f"{HUB}/connect?gateway_token={os.environ['GATEWAY_CLIENT_TOKEN']}")
    ```

- The same URL works for `chromium`, `firefox` and `webkit`: launch the browser you want and the hub sends you to a node that has it ([Browser](concepts/routing.md#browser)).
- The token can go in an `x-gateway-token` header instead (`connect(url, headers={"x-gateway-token": token})`), which keeps it out of the URL.
- Add `&node=<name>` to keep the client on one node ([One node](concepts/routing.md#one-node)).
- Your client's Playwright must be on the same major.minor version as the runners ([Version](concepts/routing.md#version)).
- If you pass a `timeout` to `connect`, make it longer than `GATEWAY_QUEUE_TIMEOUT_S` plus `GATEWAY_CONNECT_ATTEMPTS` x `OPEN_TIMEOUT_S` (the example check uses 180 s), or the client gives up while the hub is still finding it a slot ([Waiting](concepts/routing.md#waiting)).
- The Grid UI's inspector shows the hub's `/connect` address as Playwright JS or Python, PowerShell or bash, ready to copy ([Hub address](grid-ui/index.md#hub-address)).

Give each pipeline the narrowest client token that works: a token whose [token scope](concepts/access-tokens.md#token-scopes) is only `sessions.open` can open browsers and nothing else.

## A check for the orchestrator

A check is a pytest file. Look at `full/example/example.py`: it opens status.docusign.com (now redirected to health.docusign.com) and fails if any of the eSignature environments NA1-NA4 isn't `available`. Change `PRODUCT` / `MONITORED_ENVIRONMENTS` at the top to watch other sites.

- Connect with `playwright.chromium.connect(os.environ["PW_WS_ENDPOINT"])` (or `firefox`, `webkit`). `main.py` sets it to `<GATEWAY_URL>/connect?gateway_token=<GATEWAY_CLIENT_TOKEN>`, plus `&node=<GATEWAY_NODE>` if that is set, and the hub picks a free slot for that browser.
- Never build a slot address: `/connect/<node>/<slot>` is refused (503). `host` and `port` are no longer set.
- If the connect itself fails, raise `Exception("hub_Failure")` when the hub answered or could not be reached, else `Exception("connect_Failure")`. Either way `main.py` logs that the check got no browser, and why. Any other failure is the site under test.
- Without `PW_WS_ENDPOINT` the example launches a local Chromium, so `pytest full/example/example.py` works on a dev machine.
- The example waits `PW_CONNECT_TIMEOUT_S` (180 s) for the hub to hand it a browser.

### Signing in to the site under test

- For sites with two-step verification, get TOTP codes from the Grid UI with `src/mfa.py`: see [MFA secrets](grid-ui/mfa-secrets.md).
- To sign in once and reuse the browser's signed-in state on later runs, use `src/signin.py`: see [Saved sign-ins](grid-ui/saved-sign-ins.md).

Both reach the Grid UI at `GRID_UI_URL` (`http://grid_ui:4444` in compose) with `GRID_MFA_TOKEN`, else `GATEWAY_CLIENT_TOKEN`.

## Running checks with `main.py` {#running-checks-with-mainpy}

```sh
python3 /app/main.py --fil <check.py> --fol /app/ -i <seconds>
```

- `-i` runs the check every interval; without `-i` it runs once and exits 0 (passed) or 1 (failed, or it got no browser).
- `--fr true` creates the tables and syncs `hosts:`, then exits.
- `main.py` claims no slot and counts no failures: the hub does both, and holds the check's connect while every slot that fits is busy.
- A check that runs longer than `CHECK_TIMEOUT_S` (1800) is killed; its browser connection closes and the hub frees the slot.
- MySQL is used only to create the tables, sync `hosts:` and free stale claims. Each run tries it once (5 s connect timeout); if MySQL is down the check runs anyway.

All of `main.py`'s settings are in [Configuration](reference/configuration.md#checks).

### Logs

Logs go to `<fol>/logs/` (`check_info.log`, `error_info.log`). `check_info.log` has a line per run with the check, its result and how long it took, and says why a check got no browser, from the hub's answer, for example:

```text
example.py got no browser after 60.4s, so the site was not checked: the hub answered 503: no free chromium slot within 60s: 4 matching slots, all busy (every slot that fits stayed busy or failing for the hub's GATEWAY_QUEUE_TIMEOUT_S; add slots or spread the checks out)
```

The client token never appears in the logs.

### Adding a check to compose

To run another check, copy the `example_playwright` service, mount the check file and change `--fil`. The `./logs/<check>/:/app/logs/` mount keeps its logs and failure screenshots. `docker-compose.poc.yaml` has several more examples (see [Docker compose](deployment/docker-compose.md#proof-of-concept-checks)).
