# pwrunner

`pwrunner.exe` is the Playwright runner host for Windows: the gateway agent (a drop-in for
`gateway/agent.js` against the unchanged hub), its installer, Windows service, status tool and
uninstaller in one Go executable. Operator documentation is the documentation site's
[Windows host (pwrunner)](../docs/deployment/windows.md) page; the design and delivery phases are in [docs/plans/go-agent.md](../docs/plans/go-agent.md).

## Layout

| Package | Does |
| --- | --- |
| `cmd/pwrunner` | subcommands: `install`, `status`, `uninstall`, `start/stop/restart`, `token`, `selfcheck`, `run`, `service`, `node`, `fetch`, `version` |
| `internal/protocol`, `internal/config` | ports of `gateway/lib/protocol.js` and `config.js`; `backend.json` |
| `internal/hubconn`, `internal/session`, `internal/pipe` | control channel, `open` handling, frame piping |
| `internal/slot`, `internal/status`, `internal/agent` | run-server supervision, `runtime.json`, the wiring |
| `internal/runtime` | pinned Node and playwright-core: fetch, verify, extract, lay down, roll back |
| `internal/install`, `internal/report`, `internal/service`, `internal/winsys` | the Windows install, `status.json`, the SCM service, ACLs/jobs/processes |
| `assets` | `parent-watchdog.js`, a byte-identical copy of `gateway/lib/parent-watchdog.js` (a test enforces it) |

## Who runs the browsers

A check drives a browser on this host, and a browser can read any file (and, on Linux, any `/proc` entry) its
account can through `file://`. If the browsers ran as the agent's account, a check could read the agent token
and register a rogue node under this node's name. If two slots' browsers ran as one account, a check could
read the browser profile (cookies, local storage) of a check running at the same time in the other slot:
Playwright keeps each profile in a temporary folder that only its own user can open. So run-server never gets
the token in its environment (only an allow-list of variables is passed on), and each slot's run-server and
browsers run under an account of their own, not the agent's and not another slot's:

- **Windows service** (`pwrunner install`). The service runs as LocalSystem; slot *i*'s run-server and
  browsers run as a local account of that slot, `pwb-<8 hex digits of the service name>-<i>` (`pwb-1a2b3c4d-1`,
  `pwb-1a2b3c4d-2`, ...), listed slot 1 first as `browserAccounts` in `backend.json`. `install` creates each
  one, or repairs it and sets a new password, on every run: a member of Users only, password never expires
  and can't be changed by the user, hidden from the sign-in screen, allowed to log on as a batch job and
  denied local, Remote Desktop and network logons. It also removes the accounts no longer needed: those of
  slots above a lowered `--slots`, and `pwbrowser-<8 hex digits>`, the one account every slot ran as before
  each slot had its own (an older `backend.json` names it `browserAccount`). Each account's 40-character
  random password is kept only in LSA private data (`L$pwrunner-<account>`), which SYSTEM and Administrators
  can read; it is never logged or written to a file. At start the service signs each slot's account in (a
  batch logon), loads its profile, gives it access to the service's window station and desktop and starts
  that slot's run-server with its token, so the browsers read what Users can: `app\`, `runtime\`,
  `browsers\`, the logs and the JSON files, but not `agent.token`, and not the other slots' profiles. If a
  slot's account or its password is missing (an older install whose exe was swapped without re-running
  `install`, or `slots` raised in `backend.json` by hand), the service does not fall back to LocalSystem or to
  another slot's account: it starts no run-server in that slot, the node registers with the slot unhealthy,
  and `logs\service.log`, `logs\agent.log` and the Event Log say to run `pwrunner install` again.
  `pwrunner selfcheck` checks each slot's account; `pwrunner uninstall` removes them all (the old single
  account too), with their profiles, rights and stored passwords.
- **`pwrunner run` on Linux or macOS.** Start it as root with `--run-server-user <user>` (or
  `RUN_SERVER_USER`). `{slot}` in the name stands for the slot number, so `--run-server-user 'pwuser{slot}'`
  runs slot 1 as `pwuser1`, slot 2 as `pwuser2` and so on: create one user per slot. Each slot's run-server
  starts as its user, with that user's uid and gid, no supplementary groups and its own `HOME`, `USER` and
  `LOGNAME`. root, unknown users and two slots resolving to the same uid are refused, and so is the flag on
  Windows. A name without `{slot}` runs every slot as that one user, and with more than one slot the agent
  warns at start that a check can read the browser profiles of checks running at the same time in the other
  slots. Without the flag the agent warns at start that a check's browser can read the token from `/proc` (it
  reaches the agent by `--token`, `GATEWAY_AGENT_TOKEN` or a token file).
- **`pwrunner run` in a Windows console** (and `install --no-service`): the browsers run as the console's user,
  as before, and the agent warns at start that they can read any file that user can.

Each run-server also listens at a secret path: at every start the agent picks `/` and 32 random bytes in hex
and passes it in run-server's environment only (`GATEWAY_AGENT_RUN_SERVER_PATH`, which the watchdog preload
turns into `run-server --path`), never on its command line, which every user can read. run-server only checks
that a WebSocket comes from a localhost origin, and a check can give its page one, so without the path a
browser in one slot could open a session on another slot's run-server at `ws://127.0.0.1:<port>/` and so run
as that slot's user; now any path but the secret one gets HTTP 400. The agent dials the secret path when the
hub asks for `/`, and masks it (as `/<endpoint>`) in run-server's output before it reaches the slot logs; it
is not in `runtime.json` or anything sent to the hub. With one user for every slot, each slot's browsers can
read the others' run-server environments, and so their paths: give each slot its own user.

## Build

Go 1.26 or newer. From this folder:

```powershell
go build -o ..\dist\pwrunner.exe .\cmd\pwrunner                     # slim build: downloads the runtime at install
go run .\tools\fetchbundle --out .\internal\runtime\files            # pinned Node + playwright-core, hash-verified
go build -tags embedruntime -o ..\dist\pwrunner.exe .\cmd\pwrunner   # runtime embedded (about 42 MB)
.\build\build.ps1 -Version 1.1.0                                     # every release artefact + SHA256SUMS.txt into dist\
```

Node's pinned version and hashes live in `internal/runtime/pins.go`. `go run ./tools/updatepins --node <v>`
prints a new block for it; update the Node file names in `internal/runtime/embed_windows_*.go` to match.
playwright-core is pinned in `playwright/` (`package.json`, with its integrity in `package-lock.json`),
which Dependabot updates; the build picks it up from there.

## Test

```powershell
cd ..\gateway ; npm ci ; cd ..\runner          # the hub and the fake run-server need gateway/node_modules
gofmt -l . ; go vet ./... ; staticcheck ./... ; go test ./...
go test -tags integration ./test/integration/...                    # pwrunner against the real gateway/hub.js
go test -run TestLayDownReal -tags realruntime ./internal/runtime/ -args -dir <scratch folder>
cd ..\gateway ; $env:AGENT_CMD = "..\dist\pwrunner.exe run" ; node --test test\agent-contract.test.js
```

The `winsys` tests that change ACLs and the install tests need an elevated prompt and skip otherwise.
`-race` needs cgo, so CI runs it on Linux only. Never point a test at a live hub: use `HUB_DB=memory` and a
free port (see the plan, §4).

## Release

Releases are cut from tags `pwrunner/v<MAJOR.MINOR.PATCH>`, pushed by hand:

```powershell
git tag pwrunner/v1.3.0 ; git push origin pwrunner/v1.3.0
```

Use the next version number nobody has: the `vX.Y.Z` releases that every merge to master makes
(`.github/workflows/release.yml`) share the same numbers, and a `pwrunner/v*` tag also publishes the
Docker images as that version. If it's the newest release, it also gets `:latest`. The next automatic release
counts on from it.

`.github/workflows/runner-go.yml` then builds the embedded amd64/arm64 exes, the slim exe, the two Linux
builds, `pwrunner-runtime-<v>-windows-amd64.zip` (for `install --offline-dir`) and `SHA256SUMS.txt`, and
publishes them as a GitHub release. `gh workflow run runner-go.yml -f dry_run=true` builds the same set
without releasing (artefacts on the run). The builds are not code-signed yet; operators verify the
download against `SHA256SUMS.txt`.
