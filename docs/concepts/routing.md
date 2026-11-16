# Routing and version matching

Every test connects to the same address, the hub's `/connect`, and the hub picks the runner and the slot. How to connect from Python and JavaScript is in [Writing checks](../writing-checks.md); this page explains what the hub does with the connection.

## Browser {#browser}

`browserType.connect` sends the browser type in the `x-playwright-browser` header (from JS and Python alike); a `?browser=` query parameter wins over it, as it does in `run-server`.

- The hub only picks slots on nodes that list that browser (`BROWSERS`, `--browsers` or `-Browsers` on the node; the runner image lists `chromium,firefox,webkit`).
- A node that lists a Chromium channel (`chrome`, `msedge`, ...) serves `chromium` too.
- A client that names no browser can get any node.

## Version {#version}

Playwright's `User-Agent` carries its version (`Playwright/1.64.1 (...)`). The hub only picks nodes on the same major.minor version, the same check `run-server` would fail with HTTP 428.

A node whose version can't be read, and a static host, take any client; if its `run-server` then refuses the client's version (HTTP 428), the client gets that 428 at once. It is the client's version that is wrong, so no slot is counted as failing and no other slot is tried.

During a rolling Playwright upgrade, clients therefore go to the nodes that match them. See [Upgrading Playwright](../operations/upgrading-playwright.md).

## Which slot {#which-slot}

Among the free slots that fit, the hub takes:

1. the nodes that offer the fewest other browsers (so a chromium session goes to a chromium-only node before it takes a slot a firefox or webkit client could have used),
2. then the node with the largest share of its slots free (random among equals),
3. then a random free slot on it.

Static hosts list no browsers and take any client, but only when no node has a free slot.

A slot is free when it is not in use, its node is connected, its run-server is healthy and it isn't [failing](slot-failures.md).

## One node {#one-node}

`?node=<name>` (any case) keeps the client on that node or static host (`GATEWAY_NODE` for the checks `main.py` runs). The slot is always the hub's choice; there is no way to ask for one.

## Waiting {#waiting}

While every slot that fits is busy, the client waits: the hub holds its WebSocket handshake and hands the next slot that fits to the oldest waiting client.

- After `GATEWAY_QUEUE_TIMEOUT_S` (60 s) it gets a 503 that says what those slots are doing, e.g. `no free firefox slot within 60s: 4 matching slots, all busy`.
- At most `GATEWAY_QUEUE_MAX` (200) clients wait at once. A client that disconnects leaves the queue.
- A request that nothing connected or known to the hub could ever serve is refused at once instead: an unknown node (404), a browser no node lists (503), a Playwright version no node runs (428). A known node that is offline is waited for.

!!! warning "Client timeouts"
    Playwright's `connect` waits as long as it takes by default. If you pass a `timeout`, make it longer than `GATEWAY_QUEUE_TIMEOUT_S` plus `GATEWAY_CONNECT_ATTEMPTS` x `OPEN_TIMEOUT_S` (the example check uses 180 s), or the client gives up before the hub answers.

## Failover {#failover}

When the chosen slot can't open the session (its run-server refuses it for a reason other than the client's version, the agent reports a failure, no answer within `OPEN_TIMEOUT_S`, a static host refuses or can't be reached), that counts as a connect failure on the slot ([Slot failures](slot-failures.md)), the slot is released, and the client is tried on another slot, up to `GATEWAY_CONNECT_ATTEMPTS` (3) slots. Then it gets the last error.

## Claims {#claims}

The hub claims the slot in `portUsage` the way every claimer does (`inUse` 0 to 1 with a conditional update, and `LastRunDate`), so nothing double-books it, and marks the row `claimedBy = hub:<hub>:<n>`. It releases the slot when either side closes, when the open fails, when the client leaves early and when the hub stops.

- A hub's claims last as long as its sessions, however long they run: the checks' stale-claim sweep leaves them alone.
- Each hub keeps a lease, its row in the `hubs` table, renewed every `GATEWAY_HUB_LEASE_S` / 3 (30 s). A hub frees the claims of any hub whose lease ran out (one that crashed), when it starts and every 30 s, so a crash leaves nothing claimed for long.
- Every 30 s the hub also retries releases MySQL refused, and checks its live claims: a live session's slot released under it (from the Grid UI, say) is claimed again, and a slot another claimer took over is routed to nobody else until that session ends.
- If it can't reach MySQL to pick or claim a slot, the client gets a 503.

Keep `GATEWAY_HUB_LEASE_S` well above any MySQL outage you expect to ride out: another hub frees the claims of a hub not heard from for this long.

## Tokens {#tokens}

- A managed client token revoked or expired while its client waits in the queue gets that client a 401 instead of a session.
- A client token whose [token scope](access-tokens.md#token-scopes) leaves out opening browser sessions gets a 403, `gateway token is not allowed to open browser sessions (its scope leaves that out)`, and `main.py`'s log line for it says to use a token whose scope includes opening sessions.

## Logging

The hub logs one line per routing decision (`client #12 10.0.0.7 -> runner-7f3a9c/3 (firefox, waited 2.1s)`; the number tells clients at the same address apart) and per client that gave up waiting; never a token.

## Removed per-slot endpoints

The old `/connect/<node>/<slot>` endpoints are gone: they answer 503 with a message pointing to `/connect`. (503 rather than 410 so checks of the previous release, which count anything but 401, 403 and 503 against the slot they claimed, don't fail healthy slots while they are being replaced; see [Migrating](../operations/migrating.md).)

Every HTTP status a failed connect can get is listed in [Hub responses and close codes](../reference/responses.md).
