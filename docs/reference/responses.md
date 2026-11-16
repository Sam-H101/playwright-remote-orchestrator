# Hub responses and close codes

## HTTP on the hub port

The hub serves on one port (`HUB_PORT`, 4500):

| Path | Is |
| --- | --- |
| `GET /healthz` | Health; no token |
| `GET /api/nodes` | Connected nodes, with names, versions and IP addresses; no token |
| `/connect` (WebSocket) | Clients (see [Routing](../concepts/routing.md)) |
| `/agent`, `/agent/data/<id>` (WebSocket) | Agents |
| `/connect/<node>/<slot>` | Removed: answers 503 with a message pointing to `/connect` |

## Answers to a failed `/connect` {#connect-statuses}

A failed connect gets a plain HTTP answer the Playwright client shows:

| Status | Means |
| --- | --- |
| 400 | Bad request (a bad `node` or browser name) |
| 401 | Missing, bad, revoked or expired client token (also for a client still queued when its token is revoked) |
| 403 | A client token whose [token scope](../concepts/access-tokens.md#token-scopes) leaves out opening browser sessions |
| 404 | Unknown node |
| 428 | No node runs the client's Playwright version (or the `run-server` it was sent to refused its version) |
| 502 | Every slot tried refused the session |
| 503 | No node offers the browser, no free slot in time, queue full, MySQL unreachable, hub shutting down, can't check a managed token, or the removed `/connect/<node>/<slot>` |
| 504 | The last slot tried did not answer |

A 503 for no free slot says what the matching slots are doing, e.g. `no free firefox slot within 60s: 4 matching slots, all busy`. `main.py` puts the hub's answer in `check_info.log`.

A hub from before routing by browser answers a bare `/connect` with 404 `not found`; the current `main.py` says so in its log. Upgrade the hub.

## Agent close codes {#agent-close-codes}

When the hub turns an agent away, the agent logs why and keeps retrying (1 s doubling to 30 s):

| Code | Means |
| --- | --- |
| 4401 | Missing, bad, revoked or expired token |
| 4403 | Token bound to another node name, or the name is reserved for another token |
| 1013 | The hub can't check the token right now (MySQL unreachable) |
| 4409 | Replaced by another agent with the same name; the agent waits 30 s before trying again |

The agent's status file (`--status-file` / `STATUS_FILE`) shows the close code as `lastError`.
