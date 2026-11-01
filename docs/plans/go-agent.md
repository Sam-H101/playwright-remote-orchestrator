# Plan: `pwrunner` — a Go-built Windows executable replacing the Node.js runner host

Status: approved plan, not yet started. Repository `C:\Users\sam\source\repos\playwright-remote-orchestrator`, branch `master` at `a58ea1f`.

This document is written for the implementer: an autonomous engineer who will carry it out in a later session with no memory of the discussion that produced it and nobody to ask. It is meant to be decision-complete. Where it says "decided", do not reopen the decision; where it says "open question", assume the stated default and continue.

Working rules for the implementer:

1. Work on Windows 11 with PowerShell 5.1 and Git Bash. Docker exists only inside the WSL distro `vostok`, where the user's **live** grid (hub, MySQL, Grid UI, Docker runners) runs. Never start, stop, rebuild or reconfigure anything in that distro, never point anything at its hub or its MySQL, and never use port 4500 on the Windows side (WSL forwards it). Local hubs for development and tests use `HUB_DB=memory` and a free port (`HUB_PORT=0` in tests, `HUB_PORT=14500` for manual runs).
2. The hub (`gateway/hub.js`, `gateway/lib/*`) and the wire protocol do not change in any phase of this plan. The Go agent is a drop-in for `gateway/agent.js` against the existing hub. `docker/runner/Dockerfile` and the live grid keep working throughout; `gateway/agent.js` stays in the repository (the Docker runner uses it) until the optional Phase 6.
3. Access tokens are never printed, logged, written into any JSON, put on a command line, or sent anywhere but the hub's `Authorization` header. The token file ACL (SYSTEM + Administrators only) and the "token never on a command line" invariants are kept exactly.
4. Commits, code comments, PR titles and PR bodies contain no AI or assistant attribution, trailers, co-author lines, or references of any kind. Commit messages follow the repository's existing style: an imperative subject line (≤ 72 chars), a blank line, and an explanatory body that says what changed and why (see `git log -3` for examples).
5. Every phase ends with acceptance criteria that are checked by running the commands given. A phase is not done until every command passes.

---

## 1. Goal and non-goals

**Goal.** Replace the Node.js agent and the PowerShell installer/supervisor on Windows runner hosts with one Go-built executable, `pwrunner.exe`, that:

- implements the agent protocol natively (control channel, data channels, session piping, slot supervision, `runtime.json`) as a drop-in for `gateway/agent.js` against the unchanged hub;
- is also the installer, the Windows service (SCM, LocalSystem, recovery actions, Event Log), the status tool and the uninstaller — the operator installs nothing else, not Node, not npm, not winget;
- ships (embedded, hash-pinned) or fetches (hash-pinned) a private Node 22 runtime and `playwright-core`, because `playwright run-server` is JavaScript and cannot be replaced (see §2);
- keeps every observable contract of today's Windows install: `backend.json`/`runtime.json`/`status.json` shapes and paths, install-dir and token ACLs, exit codes, install step names and the log strings CI greps for;
- fixes the known gaps of the PowerShell stack that a rewrite absorbs naturally (no SCM, PowerShell 5.1 quirks, no verified artefacts, incomplete env scrub, no rollback, unbounded logs, no elevation check on uninstall).

**Non-goals (explicitly out of scope).**

- Rewriting the hub in Go (discussed in Phase 6 only as "what it would take"; not planned).
- Changing the wire protocol, `gateway/lib/protocol.js`, the MySQL schema, the Grid UI, `main.py` or the Docker images (Phase 6 optionally swaps the Docker runner's agent; off by default).
- Replacing Playwright's `run-server` with a Go implementation (impossible without reimplementing Playwright).
- Headed browsers under the service (Session 0 has no interactive desktop; unchanged from today's SYSTEM task).
- A node `drain` command, launch-option filtering at the hub, metrics, self-update: listed in the appendix as follow-ups; the design leaves room for them.

---

## 2. Current state

What runs on a Windows runner host today (all paths relative to the repo unless absolute):

| Tier | Today | File(s) | After this plan |
|---|---|---|---|
| Installer | `installer/windows/install-backend.ps1` (482 lines) + `installer/windows/PlaywrightBackend.psm1` (361 lines, ACLs via runtime-compiled C#) | elevated PowerShell, needs a repo checkout and global Node ≥ 20 (`winget` optional) | `pwrunner.exe install` |
| Supervisor | scheduled task `PlaywrightBackend` (SYSTEM, AtStartup) running hidden `powershell.exe -ExecutionPolicy Bypass -File run-backend.ps1` (99 lines); kill-on-close Job Object via `Add-Type` C# | `installer/windows/run-backend.ps1` | Windows service `PlaywrightBackend` = `pwrunner.exe service` |
| Agent | `node.exe app\gateway\agent.js …` (490 lines + `lib/{config,log,pipe,protocol,parent-watchdog}.js`), `ws` 8.22.0 | `gateway/agent.js`, `gateway/lib/` | Go agent core inside the same `pwrunner.exe` process |
| Run-servers | `node.exe app\node_modules\playwright-core\cli.js run-server --port <p> --host 127.0.0.1` ×N, `parent-watchdog.js` preloaded via `NODE_OPTIONS` | installed by `npm install` from the global Node | **unchanged command**, but run with the private `runtime\node\node.exe` and a `playwright-core` extracted from a hash-verified tarball (no npm) |
| Browsers | `node cli.js install chromium` into `C:\PlaywrightBackend\browsers` (`PLAYWRIGHT_BROWSERS_PATH`) | install-backend.ps1:362-367 | unchanged mechanism, driven by the exe |
| Status / uninstall | `backend-status.ps1` (52 lines), `uninstall-backend.ps1` (49 lines) | installer/windows | `pwrunner.exe status` / `pwrunner.exe uninstall` (+ 3-line `.ps1` shims for one release) |
| CI | `.github/workflows/windows-backend-installer.yml` (14 steps, windows-latest, PowerShell 5.1) | | rewritten for the exe, same assertions |

What stays Node.js, and why:

- **The hub** (`gateway/hub.js`, registry, tokens, routing). Not asked for, highest risk to the live grid, no operator-visible benefit. Untouched.
- **Playwright `run-server`**: a hidden Commander subcommand of `playwright-core/cli.js`; the whole server (WebSocket server, dispatcher, browser launching) is `lib/coreBundle.js`. The Go exe therefore ships a private `node.exe` (Node 22.23.4, 35.6 MB zip, only `node.exe` + `LICENSE` extracted) and spawns it exactly as `agent.js:111` does. `playwright-core` 1.64.1 has no dependencies and no native modules, so it is extracted straight from the registry tarball; npm never runs.
- **The Docker runner** (`docker/runner/Dockerfile`, `CMD ["node","agent.js"]`): unchanged; it is the live grid.

Facts the plan relies on (verified 2026-10-31):

- `run-server` options: `--port --host --path --max-clients --mode --artifacts-dir --unsafe`; it prints `Listening on <wsEndpoint>`; plain `GET /` answers `Running`, `GET /json` answers `{"wsEndpointPath": …}` (readiness probe). It has **no graceful stop**: it registers `process.stdin.on('close')` but never reads stdin, so closing a pipe does not end it. `agent.js:113` gives it no stdin at all (`'ignore'`) and stops it with `taskkill /T /F`; the Go agent does the same (§7.6).
- Run-server reads `user-agent` (428 on major.minor mismatch with an ASCII box whose first lines are `Playwright version mismatch:` / `  - server version: v…` / `  - client version: v…`), `x-playwright-browser`, `x-playwright-proxy`, `x-playwright-launch-options` (or the `?browser= ?proxy= ?launch-options=` query params). These must reach it byte for byte.
- `PLAYWRIGHT_BROWSERS_PATH` is read at process start by both `install` and `run-server`; set it in both child environments.
- Go is **not** installed on the dev machine; Node v20.17.0 is on PATH; Python 3.12.6 exists.

---

## 3. Decisions already made

1. **Exe name and product name: `pwrunner` / `pwrunner.exe`.** One binary for every subcommand. Reason: one artefact to copy, one name for operators and the CI to grep; "backend" is kept only in file names that already exist (`backend.json`, `PlaywrightBackend` service).

2. **Go module at `runner/` with module path `github.com/Sam-H101/playwright-remote-orchestrator/runner`, `go 1.26` in `go.mod`.** Reason: the repo stays polyglot during migration; a sub-module keeps `go build ./...` from touching `gateway/`; `golang.org/x/sys` v0.49.0 and `github.com/coder/websocket` v1.8.15 both require Go ≥ 1.26. The toolchain is Go 1.27.x. Embedded files (`parent-watchdog.js`, the runtime bundle) live inside `runner/` because `go:embed` cannot reach `..`; a Go test asserts `runner/assets/parent-watchdog.js` is byte-identical to `gateway/lib/parent-watchdog.js` (the gateway copy stays the source of truth).

3. **Package layout as in §5** (`cmd/pwrunner` + `internal/{cli,config,protocol,hubconn,session,pipe,slot,status,agent,runtime,service,install,report,winsys,proc,logging}`). Reason: each package maps to one section of the protocol spec or the install inventory so a reviewer can check a package against one table; `winsys` is the only package with `//go:build windows` files plus `!windows` stubs, so everything else builds and is unit-tested on Linux too.

4. **Libraries: `github.com/coder/websocket v1.8.15` and `golang.org/x/sys v0.49.0`; everything else is the standard library.** Reason: coder/websocket is context-based, defaults to compression disabled (= `perMessageDeflate:false`), exposes `Reader`/`Writer` so 256 MiB frames stream without a second copy, exposes `OnPingReceived`/`OnPongReceived` for liveness and returns the `*http.Response` of a failed dial (the 428 body). `x/sys/windows` + `svc` + `svc/mgr` + `svc/eventlog` cover Job Objects, ACLs/SDDL, SCM and Event Log without cgo. No MySQL driver, no xz library (Linux uses the `.tar.gz` Node archive).

5. **Service model: a real Windows service named `PlaywrightBackend`, LocalSystem, `StartType=Automatic` (not delayed), recovery actions restart after 5 s / 60 s / 300 s with a 86 400 s reset period, `SetRecoveryActionsOnNonCrashFailures(true)`, Event Log source `PlaywrightBackend`.** The agent runs **in-process** in the service (no separate supervisor process); only run-server children are supervised with the existing 5 s → 300 s backoff. Reason: the scheduled task gave no stop signal, no recovery semantics, no `sc query`, and ran a hidden `powershell.exe -ExecutionPolicy Bypass`; LocalSystem is the same account and Session 0 situation as today's SYSTEM task, so headless Chromium behaviour is proven. `install` detects and removes the legacy task and its supervisor.

6. **Node runtime acquisition: hash-pinned, with the embedded build as the published Windows artefact.** Pins: Node `v22.23.4` (`node-v22.23.4-win-x64.zip` sha256 `cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20`; win-arm64 `a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111`; linux-x64 and linux-arm64 use the `.tar.gz` variants, hashes read from `https://nodejs.org/dist/v22.23.4/SHASUMS256.txt` at pin time), `playwright-core 1.64.1` (`https://registry.npmjs.org/playwright-core/-/playwright-core-1.64.1.tgz`, integrity `sha512-BC4s8y6SEVA6Q1rSxZTto8hI1KUI3xXlHGZgvGqP4IqPdI/a6FX9H29IDNz7vKo9l21b6LfYosScPL6M3xxyDQ==`). Build tag `embedruntime` embeds the zip + tgz for `windows/amd64` and `windows/arm64` (exe ≈ 48–50 MB); a build without the tag is the "slim" exe (≈ 8–10 MB) that downloads the same files at install and verifies the same pins. Linux builds never embed. Reason: the user asked for one exe; the hash is re-checked at install either way, so embedding costs nothing in trust; the slim build and `--offline-dir` cover air-gapped and patch-release cases. Only `node.exe` + `LICENSE` are extracted from the zip; `package/` from the tgz.

7. **Config format: `backend.json` in the install dir, ASCII, 2-space JSON, a superset of today's fields** (§7.11). Legacy `taskName` and `portCount` are read and never written. The token stays in `agent.token`. Reason: `backend-status.ps1` and operators' scripts keep parsing it; the service reads it at start so the service's argv is `service --install-dir <dir>` and nothing else.

8. **Install dir default `C:\PlaywrightBackend`; layout in §7.11** with `app\node_modules\playwright-core\cli.js` and `app\lib\parent-watchdog.js` kept under `app\` (so the CI owner assertion on `app\node_modules\playwright-core\cli.js` and the `\app\` command-line discovery survive), `runtime\node\node.exe`, `browsers\`, `logs\`, `app.prev\` for rollback. Reason: short paths (MAX_PATH), CI continuity.

9. **Install step names and strings are kept verbatim for one release:** steps `preflight`, `stop existing backend`, `install node packages`, `install agent`, `install browsers`, `write config`, `remove legacy firewall rule`, `start backend`; log strings `already installed, skipping npm` (emitted when the laid-down runtime matches the pins) and `from scratch` (untrusted-folder reset); the "Keeping the installed settings: …" and "Changing X from 'a' to 'b'" lines; the `Get-AgentProblem` messages; the console summary `Result: X (h/t slots healthy, Ns)`. Reason: `status.json.failedStep` assertions and CI greps pass unchanged.

10. **Versioning: `-ldflags "-s -w -X main.version=<v>"`; releases are git tags `pwrunner/v<MAJOR.MINOR.PATCH>` starting at `pwrunner/v1.1.0`; dev builds report `1.1.0-dev+<short sha>`.** The agent reports `agentVersion = main.version` (≤ 32 chars) and `platform` = `win32`/`linux`/`darwin` from `runtime.GOOS`. Reason: `/api/nodes` and the Grid UI show which implementation a node runs; `1.0.0` stays the Node agent's version.

11. **Logging: one line per event `[yyyy-MM-dd HH:mm:ss] LEVEL message` (ASCII, local time as today), files `logs\install.log`, `logs\service.log`, `logs\agent.log` size-rotated (`logMaxBytes` default 10 MiB, `logKeep` default 5, both fields in `backend.json`), `logs\slot-<i>.log` with the one-generation `.prev` rotation on each run-server start (as `agent.js:100-127`).** Console mode logs to stderr. Reason: closes the unbounded-growth gap; slot logs keep the contract tools expect.

12. **Release artefacts (GitHub release on the tag):** `pwrunner-<ver>-windows-amd64.exe` (embedded), `pwrunner-<ver>-windows-arm64.exe` (embedded), `pwrunner-<ver>-windows-amd64-slim.exe`, `pwrunner-<ver>-linux-amd64`, `pwrunner-<ver>-linux-arm64`, `pwrunner-runtime-<ver>-windows-amd64.zip` (the node zip + tgz for `--offline-dir`), `SHA256SUMS.txt`. Authenticode signing is a release-pipeline concern (open question §13); unsigned builds are published with their SHA-256.

13. **Process discovery on Windows is by image path** (`CreateToolhelp32Snapshot` + `OpenProcess` + `QueryFullProcessImageName`, image under the install dir), not by WMI command-line substring. Only the one-time migration from the legacy `powershell.exe … run-backend.ps1` supervisor reads command lines (through `powershell.exe -NoProfile -Command Get-CimInstance Win32_Process …`). Reason: faster, no PEB parsing, and it replaces the WMI command-line substring scan of `PlaywrightBackend.psm1:221-233`, which could match unrelated processes.

14. **Child environment is an explicit allow-list**, not a scrub list. Every value is copied from the agent process's own environment unless stated: `SystemRoot`, `SystemDrive`, `windir`, `PATH`, `TEMP`, `TMP`, `PATHEXT`, `ProgramData`, `LOCALAPPDATA` (if present), `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY` (each in upper and lower case, if present: Playwright's browser download resolves its proxy from them, and today's installer inherits them), `PLAYWRIGHT_BROWSERS_PATH` (service and install mode: always `<D>\browsers`; `run` and `node` console mode: the process's own value when set, else unset), `PLAYWRIGHT_DOWNLOAD_HOST` (from `backend.json.downloadHost`, else the process's own value), `NODE_OPTIONS` + `GATEWAY_AGENT_NODE_OPTIONS` (built by the agent for the watchdog preload, §7.6; `orig` is the agent process's own `NODE_OPTIONS`, which in service mode is always empty because the service's own environment is built from `backend.json`, not the machine environment), never `PW_VERSION_OVERRIDE`, `BROWSERS`, `EPHEMERAL`, `HUB_URL` or `GATEWAY_AGENT_TOKEN`. The agent process itself, when started by the SCM, receives the token by reading `agent.token` into memory; run-server children never get `GATEWAY_AGENT_TOKEN`. Reason: today's `run-backend.ps1:72` scrubs only seven names, so a machine-wide `NODE_OPTIONS`, `BROWSERS` or `EPHEMERAL` reaches the SYSTEM task's agent.

15. **Contract tests use default timings on both implementations.** `gateway/agent.js` accepts no timing flags (`config.js:65-76` rejects unknown arguments with exit 2), so the new black-box suite `gateway/test/agent-contract.test.js` runs both `node agent.js` and `pwrunner run` with their default timings and sizes its own timeouts accordingly (register ≤ 15 s, slot restart ≤ 20 s, reconnect ≤ 10 s). The Go agent has a hidden `--timings <json>` flag used only by Go unit/integration tests. Reason: equivalence is only proven when both run the same way.

16. **The `coder/websocket` dial keeps only a bounded prefix of a non-101 response body.** The implementer verifies the retained size in `$(go env GOMODCACHE)/github.com/coder/websocket@v1.8.15/dial.go` and records it in a comment; the agent forwards what is available (the hub's `versionRefusal` needs only the two `server version: v…` / `client version: v…` lines, which are within the first ~200 bytes of Playwright's 428 box). A unit test asserts those lines fit in the retained prefix. The 2000-character forwarding of `agent.js:368-378` is best-effort in Go.

---

## 4. Prerequisites on the dev machine

Run in an elevated PowerShell 5.1 unless stated.

```powershell
# 1. Go toolchain (winget lags go.dev: 1.27.0 is enough; x/sys and coder/websocket need >= 1.26)
winget install --id GoLang.Go --exact --accept-package-agreements --accept-source-agreements
# open a new shell so PATH is reloaded, then:
go version            # expect: go version go1.27.x windows/amd64
go env GOPATH GOMODCACHE

# 2. Static tools (installed under $(go env GOPATH)\bin; add it to PATH)
go install honnef.co/go/tools/cmd/staticcheck@latest
go install golang.org/x/vuln/cmd/govulncheck@latest

# 3. Node (already present) and the gateway's node_modules (the fake run-server fixture needs `ws`)
node --version        # expect v20.17.0 or newer
Set-Location C:\Users\sam\source\repos\playwright-remote-orchestrator\gateway
npm ci
npm test              # baseline: all gateway tests pass before you change anything
```

Running the Node hub locally for manual tests (never on 4500; WSL forwards the live hub's port there):

```powershell
Set-Location C:\Users\sam\source\repos\playwright-remote-orchestrator\gateway
$env:HUB_DB = 'memory'
$env:HUB_PORT = '14500'
$env:GATEWAY_AGENT_TOKEN = 'dev-agent-token'
node hub.js
# log line to look for: "hub listening on 0.0.0.0:14500 (db: memory, agent token env, ...)"
# in another shell: Invoke-RestMethod http://127.0.0.1:14500/healthz   -> ok=True
#                   Invoke-RestMethod http://127.0.0.1:14500/api/nodes -> nodes=[]
```

`HUB_PORT=0` is valid (`gateway/lib/config.js:27` accepts 0..65535) and the hub logs the bound port (`hub.js:1020`: `hub listening on <host>:<port> …`); the Go integration harness uses it and parses that line.

Things that must not be done on this machine: `wsl -d vostok …` for anything but reading, `docker …` against the grid, binding port 4500, pointing any test at `ws://<anything>:4500`.

---

## 5. Repository layout to create

```
runner/                                     Go module (github.com/Sam-H101/playwright-remote-orchestrator/runner)
  go.mod                                    module path, go 1.26, requires coder/websocket v1.8.15, x/sys v0.49.0
  go.sum
  README.md                                 one page: build, test, release commands (points to this plan)
  cmd/pwrunner/main.go                      subcommand dispatch; `var version = "dev"`; detects svc.IsWindowsService()
  internal/cli/cli.go                       flag parser for `--k v` and `--k=v`, usage text, exit code 2 on bad usage
  internal/cli/cli_test.go
  internal/config/config.go                 Settings, Timings; flag > env > backend.json precedence; validation = lib/config.js
  internal/config/backend.go                backend.json read/merge-on-rerun/write (keep rules of §7.11)
  internal/config/config_test.go            every error string and exit-2 case from config.js
  internal/protocol/protocol.go             constants, NAME_RE, canonicalName, sanitizeName, forwardHeaders, stripQuery
  internal/protocol/close.go                sendableCloseCode, closeReason (<= 123 UTF-8 bytes)
  internal/protocol/frames.go               Register/Heartbeat/OpenFailed/Registered/Open/Error structs + ParseMessage
  internal/protocol/protocol_test.go        table tests ported from gateway/test/protocol.test.js
  internal/hubconn/hubconn.go               control connection: dial, register, heartbeat, backoff, hub-silence watchdog
  internal/hubconn/backoff.go               jittered backoff + 4409 floor
  internal/hubconn/hubconn_test.go          against an in-process fake hub (httptest + coder/websocket Accept)
  internal/session/session.go               one `open`: validate, dial upstream, dial data channel, pipe, open-failed mapping
  internal/session/session_test.go
  internal/pipe/pipe.go                     bidirectional streaming copy, type preservation, close mirroring
  internal/pipe/pipe_test.go                5 MiB frame, close codes, 48 MiB burst with stalled reader
  internal/slot/slot.go                     run-server supervision: spawn, health probe, restart backoff, stop
  internal/slot/logfile.go                  slot-<i>.log with .prev rotation
  internal/slot/slot_test.go                uses gateway/test/fixtures/fake-playwright/cli.js with the system node
  internal/status/status.go                 runtime.json model (field order fixed) + atomic writer
  internal/status/status_test.go            golden file for key order and ISO timestamp format
  internal/agent/agent.go                   wires hubconn + slot + session + status; Start/Stop; observables for tests
  internal/agent/agent_test.go
  internal/runtime/pins.go                  pinned versions, URLs, hashes for node (4 targets) and playwright-core
  internal/runtime/fetch.go                 download to <dir>\cache\*.part, hash while streaming, --offline-dir lookup
  internal/runtime/extract.go               zip (node.exe + LICENSE only) and tgz (package/ only), zip-slip safe
  internal/runtime/layout.go                app.new -> app -> app.prev swap + rollback; manifest.json
  internal/runtime/browsers.go              `node cli.js install <browsers>` with PLAYWRIGHT_BROWSERS_PATH / DOWNLOAD_HOST
  internal/runtime/embed_windows_amd64.go   //go:build embedruntime && windows && amd64 : go:embed files/node-v22.23.4-win-x64.zip files/playwright-core-1.64.1.tgz
  internal/runtime/embed_windows_arm64.go   //go:build embedruntime && windows && arm64
  internal/runtime/embed_none.go            //go:build !embedruntime : Embedded() returns (nil, false)
  internal/runtime/files/.gitignore         `*` — fetched by tools/fetchbundle, never committed
  internal/runtime/runtime_test.go          synthetic zip/tgz fixtures, hash mismatch, zip-slip
  internal/service/service_windows.go       svc.Handler, SCM install/update/delete/start/stop, recovery actions, eventlog
  internal/service/service_other.go         //go:build !windows : console-only stubs
  internal/service/console.go               console mode: Ctrl-C / SIGTERM -> Stop
  internal/install/install.go               step runner with timings; the install/upgrade flow of §7.11
  internal/install/steps.go                 one function per step, names as constants
  internal/install/legacy.go                legacy scheduled task + supervisor + firewall rule detection and removal
  internal/install/uninstall.go
  internal/install/install_test.go
  internal/report/report.go                 status.json / status output, Get-AgentProblem texts, POST to report URL
  internal/report/runtimefile.go            runtime.json reader (3 retries 100 ms), alive check (pid + image + <= 30 s)
  internal/report/report_test.go
  internal/winsys/acl_windows.go            SDDL constants, Protect, PathProblem, ResetUntrusted, CreateLockedDir, token file
  internal/winsys/job_windows.go            Job Objects: create, kill-on-close, assign, terminate
  internal/winsys/procs_windows.go          process enumeration by image path, kill, wait
  internal/winsys/privs_windows.go          AdjustTokenPrivileges (SeTakeOwnership, SeRestore), IsElevated
  internal/winsys/stub_other.go             //go:build !windows : no-op implementations
  internal/winsys/acl_windows_test.go       temp dir with planted Users ACE; junction not followed
  internal/proc/proc_windows.go             spawn hidden (CREATE_NO_WINDOW), kill tree via job, stdin close
  internal/proc/proc_other.go               setpgid, SIGTERM then SIGKILL after 5 s
  internal/logging/logging.go               line logger, size rotation (maxBytes, keep)
  internal/logging/logging_test.go
  assets/parent-watchdog.js                 byte-identical copy of gateway/lib/parent-watchdog.js (test enforces)
  assets/assets.go                          //go:embed parent-watchdog.js
  assets/assets_test.go                     compares with ../../gateway/lib/parent-watchdog.js
  tools/fetchbundle/main.go                 downloads + verifies the pinned node zip/tgz into internal/runtime/files
  tools/updatepins/main.go                  reads SHASUMS256.txt + registry dist.integrity, prints a pins.go snippet
  test/integration/harness.go               starts `node ../../gateway/hub.js` (HUB_DB=memory, HUB_PORT=0), parses port
  test/integration/agent_test.go            //go:build integration : register/heartbeat/open/pipe/close/backpressure/4409/token
  build/build.ps1                           build matrix (PowerShell 5.1 compatible), ldflags, tags
  build/manifest.xml                        longPathAware + asInvoker manifest (embedded via go:generate with a resource tool; see §7.9)
gateway/test/agent-contract.test.js         NEW black-box suite parametrised by AGENT_CMD (default `node agent.js`)
.github/workflows/runner-go.yml             NEW: vet/lint/unit (ubuntu + windows), integration, Windows e2e, release
.github/workflows/windows-backend-installer.yml   REWRITTEN in Phase 4 for the exe (same assertions)
installer/windows/backend-status.ps1        Phase 5: 3-line shim calling pwrunner.exe status
installer/windows/uninstall-backend.ps1     Phase 5: 3-line shim calling pwrunner.exe uninstall
installer/windows/DEPRECATED.md             Phase 5
docs/plans/go-agent.md                      this file
```

`.gitignore` additions (repo root): `runner/internal/runtime/files/*`, `runner/dist/`.

---

## 6. Agent protocol specification

The implementer codes against the text below. It is reproduced verbatim from the protocol analysis of `hub.js` at `a58ea1f`; the heading levels inside it are its own. Where the text says "the agent" or "the reference agent" it means `gateway/agent.js`, whose behaviour the Go agent must reproduce.

<!-- begin verbatim protocol specification -->

# Agent protocol specification (from hub.js at a58ea1f)

Source files (all under `C:\Users\sam\source\repos\playwright-remote-orchestrator\`): `gateway/hub.js` (hub), `gateway/lib/protocol.js` (shared constants/helpers), `gateway/agent.js` (reference agent), `gateway/lib/pipe.js`, `gateway/lib/config.js`, `gateway/lib/parent-watchdog.js`, `gateway/lib/tokens.js`, `gateway/lib/routing.js`, `gateway/test/helpers.js`, `gateway/test/gateway.test.js`, `gateway/test/fixtures/fake-playwright/cli.js`, `docker/runner/Dockerfile`, `installer/windows/PlaywrightBackend.psm1`, `installer/windows/run-backend.ps1`. The WebSocket library on both sides is `ws` ^8.18 (`gateway/package.json:16`); where a behaviour comes from `ws` defaults rather than repo code it is marked "(ws)".

Transport constants (`protocol.js`):

| Constant | Value | Where |
| --- | --- | --- |
| `AGENT_VERSION` | `gateway/package.json` `version` = `1.0.0` | protocol.js:6, package.json:3 |
| `NAME_RE` | `^[A-Za-z0-9._-]{1,128}$` | protocol.js:7 |
| `MAX_SLOTS` | 64 | protocol.js:8 |
| `MAX_PAYLOAD` | 256 MiB (268435456) | protocol.js:10 |
| `MANAGED_TOKEN_RE` | `^pwg[ac]_[A-Za-z0-9_-]{43}$` | protocol.js:20 |
| `DROP_HEADERS` | host, connection, upgrade, content-length, transfer-encoding, authorization, x-gateway-token, keep-alive, te, trailer | protocol.js:22-25 |

---

## 1. Endpoints

All agent traffic is outbound from the agent to the hub's single port (`HUB_PORT`, default 4500, config.js:27). Base URL comes from `--hub`/`HUB_URL`, must match `^wss?://[^/]+` (case-insensitive), trailing `/` characters are stripped (config.js:138-139).

### 1.1 URL forms

| Purpose | URL | Hub match |
| --- | --- | --- |
| Control channel | `<hub>/agent` | exact `pathname === '/agent'` (hub.js:163); `/agent/` is **not** matched and gets HTTP 404 (hub.js:171) |
| Data channel | `<hub>/agent/data/<id>` | `^/agent/data/([0-9a-f-]{36})$` (hub.js:164): exactly 36 chars from lowercase hex and `-`. The id is the `id` of the `open` frame, copied byte for byte |

The reference agent builds them as `${config.hub}/agent` (agent.js:237) and `${config.hub}/agent/data/${id}` (agent.js:386).

### 1.2 Authorization header

Both channels send `Authorization: Bearer <token>` when a token is configured, and no header at all otherwise (agent.js:238, 387). Hub parses with `^Bearer\s+(.+)$` case-insensitively and trims (protocol.js:116-119). Both channels use `perMessageDeflate: false` and `handshakeTimeout: 10000` ms (agent.js:239-240, 388); the data channel also sets receive `maxPayload: MAX_PAYLOAD` (agent.js:388).

### 1.3 What the hub checks on the control upgrade (`/agent`)

Order in `onUpgrade` (hub.js:153-172) and `acceptAgent` (hub.js:181-202):

1. `stopping` → HTTP 503 `hub is shutting down` (hub.js:155).
2. Unparsable URL → HTTP 400 `bad request` (hub.js:160).
3. Token verdict via `tokens.check('agent', token)` (hub.js:185; tokens.js:103-108):
   - **env bootstrap token**: if `GATEWAY_AGENT_TOKEN` is set on the hub and the presented token equals it (SHA-256 timing-safe compare, protocol.js:109-114) → `via: 'env'`, `tokenId: null`, `nodeName: null` (tokens.js:105).
   - **managed token**: anything that does not equal the env token and does not match `MANAGED_TOKEN_RE` is `unknown token` without a DB lookup (tokens.js:106). A `pwga_…` token is looked up by SHA-256 hex of the exact string (protocol.js:122-124; tokens.js:107, 118). Row must have `kind === 'agent'`, not be revoked, not be expired (tokens.js:14-20), else refused with `problem` ∈ {`unknown`, `not an agent token`, `revoked`, `expired`}. A valid row gives `via: 'managed'`, `tokenId`, `nodeName` (null or the node the token is bound to), `label` (tokens.js:127).
   - **no/unmatched token**: allowed as `via: 'open'` only while enforcement is off for agents (no env token and no managed agent token ever created, or `GATEWAY_ALLOW_OPEN_AGENTS` with every token revoked) (tokens.js:133-144, 69-86). Otherwise refused with message `missing agent token` / `unknown token` and no `problem`.
   - DB failure during lookup → `unavailable: true` (tokens.js:114, 120, 139).
4. After the verdict, `stopping` again → HTTP 503 (hub.js:186).
5. **The WebSocket upgrade (101) is completed even for a refused token** (hub.js:184, 187), so the refusal arrives as a JSON `error` frame followed by a close code (see §3): `unavailable` → `error` + close 1013 reason `token check unavailable` (hub.js:189-192); not ok → `error` `agent token rejected: <message>` + close 4401 reason `token <problem>` or `unauthorized` (hub.js:194-196).
6. An exception in the token check destroys the socket without any HTTP answer (hub.js:198-201).

The control-channel server uses `maxPayload: 1 MiB` (hub.js:40): a control frame larger than that is closed by `ws` with 1009 (ws).

Node binding: a managed token row may carry `nodeName`. It is **not** checked at upgrade; it is checked in `register` (§2.2) and in the periodic re-check (§3).

### 1.4 What the hub checks on the data upgrade (`/agent/data/<id>`)

`acceptData` (hub.js:778-801), answered with plain HTTP on failure (`reject`, hub.js:145-151: status line, `Connection: close`, `text/plain`, body `<message>\n`, socket destroyed 1 s later):

| Condition | HTTP answer | Where |
| --- | --- | --- |
| `id` not in `pending` (unknown, already attached, timed out, client left, agent disconnected) | 404 `no pending session with that id` | hub.js:780 |
| Control connection authenticated via **env**: data token must equal `config.agentToken` exactly (not merely any valid token) | 401 `bad agent token` | hub.js:783-785 |
| Control via **managed**: data token must hash-equal the control token's hash; on success `lastUsedAt` is touched | 401 `bad agent token` | hub.js:787-791 |
| Control via **open**: full `tokens.check('agent', token)` is run (enforcement may have been switched on since) | 503 `token check unavailable` or 401 `bad agent token`; 404 if the pending entry changed meanwhile | hub.js:793-796 |
| token check throws | socket destroyed | hub.js:797-800 |
| held `/connect` client already gone | 410 `client went away` (and the client is logged as left) | hub.js:806-809 |
| `handleUpgrade` fails (invalid WS handshake) | `ws` answers the socket; hub counts it as a 502 connect failure on the slot: `node <name> sent a bad data channel handshake` | hub.js:812-817 |
| client no longer in `opening` state after upgrade | data WS closed 1001 `client went away` | hub.js:823 |
| client-side upgrade fails | data WS closed 1001 `client went away`, slot released | hub.js:843-846 |

The data WSS uses `maxPayload: MAX_PAYLOAD` (hub.js:41). After attach, the hub pipes the data WS to the client WS transparently (hub.js:838); **no JSON control frames are ever sent on a data channel**.

### 1.5 HTTP endpoints an agent may hit by mistake

`GET /healthz` → `{ok, db, nodes}`; `GET /api/nodes` → `{nodes:[describe(agent)…]}` with `name, slots, healthySlots, playwrightVersion, agentVersion, platform, browsers, ephemeral, remoteAddress, connectedAt, lastSeen` (hub.js:119-120, 128-142); any other method → 405; other path → 404 (hub.js:118-121).

---

## 2. JSON frames

Every control frame is a **text** WebSocket frame containing one JSON object with a string `type` (protocol.js:99-107). The hub parses with `parseMessage`: a binary frame, invalid JSON, non-object, or missing/non-string `type` is "malformed" (protocol.js:100-106). The hub serialises with `JSON.stringify` and terminates the socket if the send fails (protocol.js:93-97).

### 2.1 Ordering and malformed-frame rules on the hub (hub.js:204-248)

| Situation | Hub reaction | Where |
| --- | --- | --- |
| Any frame updates `agent.alive` (liveness) before parsing | — | hub.js:217 |
| Malformed frame at any time | `error` `expected a JSON control frame` + close 4400 | hub.js:218-219, 209-212 |
| No `register` within `REGISTER_TIMEOUT_MS` = 10000 ms of the 101 | `error` `no register frame` + close 4400 | hub.js:21, 207 |
| First frame not `register` | `error` `first frame must be register` + 4400 | hub.js:223 |
| While `register` is being processed (the async name-holder lookup, hub.js:267): a second `register` | `already registered` + 4400 | hub.js:222 |
| While `register` is being processed: any other frame | **silently dropped** | hub.js:222 |
| `register` throws internally | `error` `register failed` + 4400 | hub.js:226-229 |
| After registration: `register` | `already registered` + 4400 | hub.js:233 |
| After registration: unknown `type` | warning logged, ignored | hub.js:234 |

`fail(message)` always sends `{type:'error', message}` and then closes with 4400 and the same message as the close reason (hub.js:209-212).

### 2.2 Agent → hub: `register`

Sent immediately on `open` of the control socket (agent.js:243-256).

| Field | Type | Hub validation | Where |
| --- | --- | --- | --- |
| `type` | `"register"` | required first frame | hub.js:223 |
| `name` | string | must match `NAME_RE` (`^[A-Za-z0-9._-]{1,128}$`), else 4400 `node name must be 1-128 characters of A-Z a-z 0-9 . _ -`. Canonicalised to **lower-case** (`canonicalName`) | hub.js:252, 256; protocol.js:27-35 |
| `slots` | array of `{index, healthy}` | `normalizeSlots`: array, length 1..64, every entry truthy with `index === i+1` (number, strict), else 4400 `slots must be a list of 1-64 entries indexed from 1`. `healthy` is coerced with `!!` (any truthy value); extra properties ignored | hub.js:251, 253; protocol.js:137-141 |
| `playwrightVersion` | string | `str(v,32)`: non-empty string → first 32 chars, anything else → `null` | hub.js:254, 278 |
| `agentVersion` | string | `str(v,32)` | hub.js:279 |
| `platform` | string | `str(v,64)` | hub.js:280 |
| `browsers` | string[] | if array: non-strings dropped, each cut to 64 chars; not an array → `[]`. Compared case-insensitively by routing; a node that lists **none** is treated as offering **every** browser (routing.js:47-53). `chrome`/`msedge`/`chromium-*` entries also satisfy `chromium` | hub.js:281; routing.js:12, 47-53 |
| `ephemeral` | boolean | `=== true` only; anything else is false | hub.js:282 |

Processing order after validation (hub.js:257-301):

1. Managed token bound to a node (`auth.nodeName`) and it differs from the canonical name → `error` `token not valid for this node name (<name>)` + close **4403** reason `token not valid for this node name` (hub.js:258-262).
2. Token not bound → `nameHolder(name)` (hub.js:266-274, 305-315): if another connected agent holds that name with a token bound to it, or the DB says a token is bound to it → `error` `node name <name> is reserved for the agent token bound to it` + **4403** reason `node name reserved for its bound token`. If the hub is stopping or the socket closed during the lookup, nothing is sent (hub.js:268).
3. Agent is recorded (`name`, `slots`, `info`, `connectedAt = lastSeen = now`); a pending ephemeral reap timer for the name is cancelled (hub.js:275-287).
4. If another connection holds the same name: it receives `error` `replaced by a newer connection with the same name` + **4409** reason `replaced`; the newest connection wins (hub.js:290-296).
5. Hub sends `{type:'registered', name:<canonical lower-case name>}` (hub.js:298), then writes the node to the registry asynchronously; the node becomes routable only once that write lands (`agent.synced`, hub.js:317-324). Tests: `register` answer is exactly `{type:'registered', name:'twin'}` (gateway.test.js:183) and `Runner-A` → `runner-a` (gateway.test.js:335).

Reference agent values: `name: config.name` (already lower-case, config.js:140), `slots: slotHealth()` (`[{index, healthy}]`, agent.js:226-228), `playwrightVersion` read from `package.json` next to the cli (`version` or `unknown`, agent.js:31-37), `agentVersion: '1.0.0'`, `platform: process.platform` (`win32`/`linux`/`darwin`; tests assert equality with the test process's platform, gateway.test.js:32), `browsers: config.browsers`, `ephemeral: !!config.ephemeral` (agent.js:246-255).

### 2.3 Agent → hub: `heartbeat`

| Field | Type | Hub handling | Where |
| --- | --- | --- | --- |
| `type` | `"heartbeat"` | | hub.js:231 |
| `slots` | `[{index, healthy}]` | normalised as in register; applied **only if valid and the same length as at register** (a length change is ignored, not an error). If some slot went unhealthy→healthy the hub re-runs routing at once | hub.js:327-332 |

Always sets `lastSeen = now` (hub.js:333). If the node is not the current holder of its name the frame does nothing else (hub.js:334). If the register write failed earlier, the heartbeat repairs it; if `registry.heartbeat` returns false (row wiped) the node is re-registered (hub.js:335-343). The agent sends it every `heartbeatMs` once registered (agent.js:324) and immediately on any slot health change ("urgent", agent.js:220-224, 175). Sending it before `registered` is harmless (silently dropped during registration, hub.js:222).

### 2.4 Agent → hub: `open-failed`

| Field | Type | Hub handling | Where |
| --- | --- | --- | --- |
| `type` | `"open-failed"` | | hub.js:232 |
| `id` | string | must be a pending open **owned by this control connection**, else ignored | hub.js:767-768 |
| `message` | string | `String(msg.message \|\| 'open failed').slice(0, 4000)`; first 500 chars are shown to the client | hub.js:769, 774 |
| `status` | integer or absent | if `Number.isInteger` it is used; otherwise the hub parses `answered HTTP (\d{3})\b` out of `message`; otherwise `null` | hub.js:771 |

Effect: `status === 428` → the client gets HTTP 428 `Playwright version mismatch: …` built by `versionRefusal`, which regexes `server version: v?(\d+\.\d+)` and `client version: v?(\d+\.\d+)` out of `message` (hub.js:31-36, 773) — so the run-server's response body must be included in `message` for the versions to be named; it counts **no** failure against the slot and is final for the client. Any other status → the slot gets a connect failure (`failCount+1`), the client is retried on another slot up to `GATEWAY_CONNECT_ATTEMPTS` (default 3), and finally gets HTTP 502 `node <name> slot <n>: <message>` (hub.js:774, 744-764).

### 2.5 Hub → agent: `registered`

`{type:'registered', name:<string>}` (hub.js:298). Reference agent: sets `registered=true`, clears `lastError`, resets reconnect backoff to `reconnectBaseMs`, logs the name (agent.js:264-270). The agent does **not** acknowledge it.

### 2.6 Hub → agent: `open`

Sent only to the agent currently registered under the chosen node name, once the hub has claimed the slot (hub.js:662-674):

| Field | Type | Value |
| --- | --- | --- |
| `type` | `"open"` | |
| `id` | string | `crypto.randomUUID()` — 36 lowercase hex/dash chars (hub.js:665) |
| `slot` | integer | 1-based slot index (`c.slot` = registry `PortNumber`) (hub.js:673, 628) |
| `path` | string | always `"/"` at this commit (hub.js:673) |
| `query` | string | the client's raw query string **without** a leading `?`, with the `gateway_token` and `node` pairs removed byte-for-byte otherwise (hub.js:451; protocol.js:53-65). May be `""` |
| `headers` | object | `forwardHeaders(req.headers)` of the client's upgrade request: keys lower-cased, `DROP_HEADERS`, `sec-websocket-*` and `proxy-*` removed (hub.js:454; protocol.js:43-51). Values are strings or arrays of strings (Node's `IncomingHttpHeaders`) |

Several opens may be outstanding at once (one per waiting client); each must be handled independently. The hub starts a `config.openTimeoutMs` timer (default `OPEN_TIMEOUT_S`=15 s, config.js:45) when it sends the frame (hub.js:668).

### 2.7 Hub → agent: `error`

`{type:'error', message:<string>}`, purely informational, always followed by a close (hub.js:191, 195, 210, 260, 271, 294, 399). Reference agent stores `lastError = String(msg.message)` and logs it (agent.js:273-276). A malformed frame from the hub is logged and ignored by the agent (agent.js:263); unknown types are ignored.

---

## 3. Close codes and the agent's reaction

| Code | Constant | Hub sends when | Reason text | Agent reaction (agent.js:282-311) |
| --- | --- | --- | --- | --- |
| 1001 | — | hub `stop()`: every agent and both session legs | `hub is shutting down` (hub.js:1038-1041); sockets terminated 200 ms later (hub.js:1047-1048) | normal reconnect backoff |
| 1006 | — | hub `terminate()`s the socket: missed heartbeats (hub.js:944), send error (protocol.js:95), stop after 200 ms | none | normal backoff; `lastError` keeps the last `error` event text if there was one (agent.js:287) |
| 1009 | — | control frame > 1 MiB (ws, hub.js:40) | ws default | normal backoff |
| 1013 | `TRY_AGAIN_LATER` | token DB unavailable at upgrade (hub.js:192) | `token check unavailable` | warns, normal backoff (agent.js:300-301) |
| 4400 | `BAD_REQUEST` | any `fail()` in §2.1/§2.2 | the error message | normal backoff (falls into the generic branch, agent.js:302-303) |
| 4401 | `UNAUTHORIZED` | token refused at upgrade (hub.js:196) or in the 15 s re-check (hub.js:400) | `token <problem>` (`unknown`/`revoked`/`expired`/`not an agent token`), `unauthorized`, or `agent token required` | logs error, keeps retrying with normal backoff (agent.js:294-296). Test expects `lastError` to contain `4401` (gateway.test.js:152) |
| 4403 | `FORBIDDEN` | token bound to another node, or name reserved (hub.js:261, 272); re-check finds the binding changed (hub.js:403, no `error` frame first) | `token not valid for this node name` / `node name reserved for its bound token` | logs error, normal backoff (agent.js:297-299) |
| 4409 | `REPLACED` | a newer connection registered the same name (hub.js:295) | `replaced` | delay = `max(normal, replacedDelayMs=30000)` (agent.js:291-293) |

Backoff (agent.js:289-290, DEFAULT_TIMINGS agent.js:18-29): `delay = reconnectDelay + floor(random() * reconnectDelay * 0.3)`; then `reconnectDelay = min(reconnectDelay*2, reconnectMaxMs=30000)`; starts at `reconnectBaseMs=1000`; reset to base on `registered` (agent.js:267). On close the agent sets `connected=false`, `registered=false`, sets `lastError = "hub closed the connection (<code>[ <reason>])"` unless code is 1006 and a socket error was already recorded (agent.js:284-287), writes status, and schedules `connect()` (agent.js:305-310). When the agent is stopping it writes status and does not reconnect (agent.js:288). A `handshakeTimeout` or TCP error fires `error` then `close` (ws), so the same path applies to "could not reach the hub".

`sendableCloseCode` (protocol.js:72-75) applies to everything the hub or agent puts on the wire: 1000–1014 except 1004/1005/1006, and 3000–4999 pass through; 1005 → 1000; anything else (1006, 1004, 1015, 0, 2xxx, ≥5000) → 1011. `closeReason` (protocol.js:77-81): UTF-8, truncated to ≤ 123 bytes by removing trailing characters.

---

## 4. Liveness

Hub side (control): `pingInterval = clamp(floor(heartbeatTimeoutMs/3), 1000, 10000)` → 10 s at the default `HEARTBEAT_TIMEOUT_S=30` (hub.js:78; config.js:44). Every interval `sweep()` runs: an agent whose `alive` is older than `heartbeatTimeoutMs` is `terminate()`d (no close frame), otherwise it gets a WS ping (hub.js:939-948). `alive` is refreshed by **any received frame** (hub.js:217) or a **pong** (hub.js:214). The hub never listens for agent pings; `ws` auto-answers them with pongs (ws). So an agent must either answer the hub's pings (ws clients do automatically) or send a frame at least every `heartbeatTimeoutMs`.

Agent side (control, agent.js:314-325): every `heartbeatMs=10000` `tick()` runs: if the socket is open and `now - lastHubActivity > hubSilenceMs=45000` → `lastError='hub stopped answering'`, `terminate()` (→ close 1006 → reconnect); else send a WS ping and, if registered, a `heartbeat`. `lastHubActivity` is set on open, ping, pong, message (agent.js:245, 258-261).

Data channels: hub pings each session leg every `pingInterval` and records `dataAlive` on pong **or message**; if a leg is silent for `sessionTimeoutMs` (default `max(heartbeatTimeoutMs, 60000)` = 60 s, hub.js:80, overridable in tests) both legs are `terminate()`d (hub.js:831-837, 949-958). Agent: each tick, for every session whose data socket is OPEN, if `now - lastActivity > hubSilenceMs` → terminate both data and upstream, else ping the data socket (agent.js:330-342); `lastActivity` set on open/ping/pong/message of the data socket (agent.js:395-396). Test coverage: gateway.test.js:374-408.

Open timeout: the hub's `open` timer fires after `openTimeoutMs` (15 s default; 800 ms–10 s in tests) → `failOpen(id, 504, 'node <n> slot <s> did not answer within <t>s')` (hub.js:668), which removes the pending entry (hub.js:717-725), so a late data channel gets HTTP 404 (hub.js:780; test gateway.test.js:115-120, 136). The agent therefore has ≤ `openTimeoutMs` from receiving `open` to completing the data handshake, including the ≤10 s run-server handshake. If the control connection closes, every pending open of that agent fails with 502 `node <name> disconnected` without counting against the slot (hub.js:240).

---

## 5. Session open flow

1. Hub sends `open` (§2.6) after claiming the slot in the DB (hub.js:627-660, 673).
2. Agent validates (agent.js:344-361): `id` must match `^[0-9a-f-]{36}$` else the frame is ignored with a warning (no open-failed can be sent without an id); `slot` indexes `slots[slot-1]`, missing → `open-failed` `no slot <slot>`; `path` used only if a string starting with `/`, else `/`; `query` prefixed with `?` only if a non-empty string; `headers` re-filtered through `forwardHeaders` (so `host`, `authorization`, `x-gateway-token`, `sec-websocket-*`, `proxy-*`, hop-by-hop headers can never reach run-server even if the hub sent them) and only string / all-string-array values kept. `x-playwright-browser`, `x-playwright-launch-options`, `x-playwright-proxy`, `user-agent` etc. pass through unchanged — run-server reads browser name, launch options and the client version (`Playwright/<x.y.z> …` in `user-agent`) from them (protocol.js:41-42; routing.js:14-16, 34-38). Test: gateway.test.js:52-95 asserts the forwarded set and that `host` is the slot's `127.0.0.1:<port>`.
3. Agent dials `ws://127.0.0.1:<basePort+slot-1><path><?query>` with those headers, `perMessageDeflate:false`, `handshakeTimeout:10000`, `maxPayload: MAX_PAYLOAD` (agent.js:364-366).
4. HTTP refusal by run-server (non-101): read the body (first 2000 chars), send `open-failed {id, message:"run-server on port <p> answered HTTP <status>[: <trimmed body>]", status:<status>}`, terminate the upstream (agent.js:368-378). 428 = Playwright major.minor mismatch (final for the client, no slot failure counted); 400/403 = run-server refused the path/host (counted as slot failure) (hub.js:29-36, 766-775; fake server fixture cli.js:7-9, 25-30; test gateway.test.js:108-113).
5. Dial error (ECONNREFUSED, handshake timeout, …): `open-failed {id, message:"run-server on port <p>: <err>"}` with no `status` (agent.js:379-383); the hub turns it into 502 and counts a slot failure.
6. On upstream `open`: dial `<hub>/agent/data/<id>` with the **same** `Authorization` as the control channel, `perMessageDeflate:false`, `handshakeTimeout:10000`, `maxPayload` (agent.js:384-389). If the data socket errors before reaching OPEN (HTTP 401/404/410/503 from §1.4, or network), send `open-failed {id, message:"data channel: <err>"}` (agent.js:390-393); the upstream is then closed by the pipe's close mirroring.
7. Pipe upstream ↔ data (agent.js:398) with `pipe()` (pipe.js:13-52): every message frame is forwarded with its binary/text flag preserved (pipe.js:29-30, 36-37); frames arriving while the other side is still CONNECTING are queued and flushed on `open` (pipe.js:38-42); when either side closes, the other is closed with `sendableCloseCode(code)` and `closeReason(reason)` (pipe.js:43-46) and the done callback runs once (pipe.js:15-19). Backpressure: when the destination's unsent bytes exceed `HIGH_WATER` = 4 MiB the source stops being read; reading resumes once the destination drains to ≤ `LOW_WATER` = 1 MiB (pipe.js:8-9, 23-34). `error` events are swallowed because `close` always follows (pipe.js:48). The hub does the same between client and data socket (hub.js:838), so the test suite verifies end to end: text and binary preserved, a 5 MiB frame (gateway.test.js:64-78), client 4123 close reaches run-server as 4123 with reason, run-server 4321 reaches the client, client vanishing (1006) reaches run-server as 1011, close-without-code (1005) reaches run-server as 1000 (gateway.test.js:248-281), 48 MiB burst with a stalled reader keeps hub buffering < 16 MiB (gateway.test.js:410-435).
8. On agent `stop()`, each session's data and upstream sockets are closed 1001 `agent is shutting down` (agent.js:444-447); the hub mirrors 1001 to the client (test gateway.test.js:283-292).

---

## 6. Slot management the hub assumes

- Slots are numbered 1..N and reported in order; slot *i* listens on `127.0.0.1:<basePort+i-1>` (agent.js:87; config.js:86). The hub routes `open.slot = i` and the registry stores one row per index; the number of slots is fixed for the life of a registration (heartbeats with a different length are ignored, hub.js:328).
- `healthy` is the only state the hub uses to route: a slot whose live `healthy` is false is "unhealthy" and never chosen; a node not connected is "offline" (routing.js:88-100). The agent must report `healthy:false` for a slot whose run-server is down.
- "Healthy" in the reference agent = child process alive (`exitCode === null && signalCode === null`) **and** a TCP connect to `127.0.0.1:<port>` succeeds within 2000 ms (agent.js:39-50, 163-169). Checked every `healthMs=5000` (agent.js:213) and every 250 ms for the first 30 s after a start until it is healthy (agent.js:157-160). The hub does not know about browsers being present (README.md:149).
- Any health transition logs and sends an **urgent** heartbeat immediately when registered (agent.js:171-176, 220-224); the hub re-routes waiting clients as soon as a slot recovers (hub.js:329-331).
- Process: `node <cli.js> run-server --port <port> --host 127.0.0.1` (agent.js:111), `windowsHide: true`. Output goes to `<logDir>/slot-<i>.log` (previous file renamed to `.log.prev`, directory created) when `--log-dir` is set, else to the agent's stdout prefixed `[slot i]` (agent.js:100-127).
- Restart backoff: on exit, `healthy=false`; if the process lived ≥ `stableMs=60000` the backoff resets to `restartBaseMs=5000`; the restart is scheduled after the current backoff, which then doubles up to `restartMaxMs=300000`; `restarts++` (agent.js:134-152). No restart while stopping.
- Stop: cancel a pending restart, `killTree(child)`: on Windows `taskkill /pid <pid> /T /F` (falls back to `child.kill()` if taskkill cannot be spawned), elsewhere SIGTERM; SIGKILL after 5 s; give up waiting after 8 s (agent.js:64-82, 178-181).

---

## 7. Status file (`runtime.json` / `STATUS_FILE`)

Written atomically: `<file>.<pid>.tmp` then rename over the target; directory created; a failed rename is logged once and retried next time (agent.js:421-437). Written every `statusMs=5000` (agent.js:215), on every state change (agent.js:221) and on stop (agent.js:452). Pretty-printed with 2-space indent plus trailing newline (agent.js:428). Shape (agent.js:406-419; key order asserted by gateway.test.js:212, 221):

| Key | Type | Value |
| --- | --- | --- |
| `name` | string | node name (lower-case) |
| `hub` | string | hub base URL |
| `agentVersion` | string | `1.0.0` |
| `playwrightVersion` | string | from cli's package.json, or `unknown` |
| `pid` | int | agent process id |
| `connected` | bool | control socket open |
| `registered` | bool | `registered` frame received and not since closed |
| `lastError` | string\|null | last socket error / hub `error` message / close description; `null` after `registered` |
| `updated` | string | ISO-8601 UTC with ms, `Z` suffix |
| `slots[]` | array | one per slot: `index` int, `port` int, `pid` int\|null (null when the child is not running), `healthy` bool, `restarts` int, `since` ISO string\|null (start time of the current child) |

Consumers:
- `installer/windows/PlaywrightBackend.psm1`: `Read-BackendRuntime` reads `<InstallDir>\runtime.json`, retrying 3× on parse errors (mid-rename) (psm1:268-277); `Test-AgentAlive` requires `pid` to be a running process and `updated` ≤ 30 s old (psm1:279-286); `Get-AgentProblem` reads `connected`, `lastError`, `registered`, `slots.Count` vs configured slots, and `slots[].healthy`/`index` (psm1:288-299); `New-BackendReport` reads `slots[].index/port/healthy/restarts/pid`, `name`, `agentVersion`, `connected`, `registered`, `lastError` (psm1:312-342). The installer deletes a stale `runtime.json` before starting the agent (install-backend.ps1:303); the supervisor passes `--status-file "<InstallDir>\runtime.json"` and `--log-dir "<InstallDir>\logs"` (run-backend.ps1:64-65).
- `docker/runner/Dockerfile:32-33` HEALTHCHECK: `registered && Date.now() - Date.parse(updated) < 30000`.

---

## 8. Environment inherited and passed to run-server

- The child gets the agent's entire environment (`...process.env`, agent.js:58). That is how `PLAYWRIGHT_BROWSERS_PATH` reaches run-server: set by `run-backend.ps1:67` (`$config.browsersPath`), `install-backend.ps1:365` for the browser download, and `docker/runner/Dockerfile:11` (`/ms-playwright`); README.md:290 documents the pass-through.
- `NODE_OPTIONS` is rewritten to `"<original> --require \"<agent dir>/lib/parent-watchdog.js\""` (JSON-quoted path, trimmed) and the original is stashed in `GATEWAY_AGENT_NODE_OPTIONS` (agent.js:52-62). `parent-watchdog.js` runs inside run-server before anything else: restores `NODE_OPTIONS` to the original (or deletes it when empty) and deletes the stash so browsers and other grandchildren do not inherit the preload (watchdog:7-11); then every 2 s (unref'd timer) does `process.kill(ppid, 0)` and exits with 0 on `ESRCH` (watchdog:12-18). Purpose: when the agent dies without cleanup (Task Manager, OOM, SIGKILL) the run-servers exit instead of holding the slot ports as orphans, and Playwright's exit hooks close their browsers (watchdog:3-5). Test gateway.test.js:294-323 SIGKILLs the agent and waits ≤10 s for every slot pid to disappear. The trick needs only the `.js` file on disk and the `NODE_OPTIONS`/`GATEWAY_AGENT_NODE_OPTIONS` pair; it is independent of the agent's language. (On Windows the supervisor additionally puts itself in a kill-on-close Job Object that the agent and children inherit, run-backend.ps1:29-53.)
- `killTree` on Windows: `taskkill /pid <pid> /T /F` with `windowsHide`, because run-server's browser children outlive a plain kill (agent.js:74-77).
- The agent's own config comes from flags (`--hub --token --name --slots --base-port --playwright-cli --browsers --status-file --log-dir --ephemeral`, forms `--k v` and `--k=v`, `--ephemeral` alone = true) overriding env (`HUB_URL GATEWAY_AGENT_TOKEN NODE_NAME SLOTS BASE_PORT PLAYWRIGHT_CLI BROWSERS STATUS_FILE LOG_DIR EPHEMERAL`) (config.js:65-115, 133-150). Unknown flag or bad value → message on stderr and **exit 2**; `-h/--help` prints usage and exits 0 (agent.js:459-469); the supervisor treats a fast exit 2 as a crash loop (run-backend.ps1:94-98). Validation: slots 1..64 default 4; base-port 1..(65535-slots+1) default 3100; browsers comma-split, trimmed, empty dropped, default `chromium`; status-file and log-dir resolved against cwd; ephemeral accepts `1/true/yes/on` and `0/false/no/off` (empty = false, anything else = error) (config.js:15-21, 142-148). The Windows supervisor removes `HUB_URL NODE_NAME SLOTS BASE_PORT PLAYWRIGHT_CLI STATUS_FILE LOG_DIR` from the environment and passes everything but the token on the command line (run-backend.ps1:56-72).
- Signals handled: SIGINT, SIGTERM, SIGHUP, SIGBREAK → `stop()` then exit 0, hard exit after 10 s (agent.js:473-484).

---

## 9. Other things a reimplementation can get wrong

1. **Name canonicalisation**: the agent lower-cases its name before use (`canonicalName`, config.js:140) and the hub lower-cases again and answers with the lower-case form (hub.js:256, 298); routing by `?node=` is case-insensitive and two spellings of one name replace each other with 4409 (gateway.test.js:332-351).
2. **Default name**: `sanitizeName(os.hostname())` replaces every char outside `[A-Za-z0-9._-]` with `-`, truncates to 128, falls back to `node` (protocol.js:37-39; config.js:140); then validated with `NAME_RE` (config.js:141).
3. **Token never logged**: the start-up log line names everything but the token (agent.js:210); only "no GATEWAY_AGENT_TOKEN set" is logged (agent.js:211). The hub likewise keeps only a hash (hub.js:174-179) and labels tokens by their 12-char prefix (protocol.js:131-135). The installer passes the token through the environment, never argv (run-backend.ps1:68-70).
4. **Same token on both channels**: a data channel must carry the token its control connection authenticated with (§1.4); for env-token agents it must equal the hub's env token exactly.
5. **Reconnect jitter** of 0–30 % and the 30 s floor after 4409 (`replacedDelayMs`) exist so two agents with one name do not kick each other in lock-step (agent.js:27, 289-293); the hub-side comment at run-backend.ps1:24-25 relies on it.
6. **Registered before DB sync**: `registered` arrives before the node is routable; do not treat the first `open` as guaranteed to follow quickly.
7. **Silent window during registration**: frames sent between `register` and `registered` are dropped (hub.js:222); only send `heartbeat` once registered (agent.js:223, 324). Pings are fine at any time.
8. **Heartbeat length**: a heartbeat whose `slots` length differs from the registered count is ignored (hub.js:328); the slot count can only change by reconnecting and re-registering.
9. **Control frames are text**, never binary (protocol.js:100); binary on the control channel closes it with 4400.
10. **Close reasons** must be ≤ 123 UTF-8 bytes and codes must be sendable (§3); `ws` reports a missing close code as 1005 and an abrupt loss as 1006, which the pipe maps to 1000 and 1011 respectively (protocol.js:72-75, test gateway.test.js:267-280).
11. **Ping handling**: the hub counts only pongs and frames as agent liveness, not received pings (hub.js:214-217); make sure the client library answers pings automatically or send a frame within `heartbeatTimeoutMs`.
12. **`open.headers` values may be arrays** (repeated headers); keep arrays of strings, drop anything else (agent.js:360).
13. **Open frame `slot` type**: a non-numeric `slot` yields `no slot <value>` open-failed (agent.js:354-355); never crash on a bad frame.
14. **Data-channel error after OPEN** is not an open failure: only errors while `readyState !== OPEN` produce `open-failed` (agent.js:391-393); after that the pipe's close mirroring ends the session.
15. **Hub-side 1 MiB control payload cap** (hub.js:40): keep `register` small (browsers ≤ 64 chars each, versions ≤ 32, platform ≤ 64; everything beyond is truncated anyway, hub.js:278-281).
16. **Status file atomicity**: write-then-rename, `<file>.<pid>.tmp` sibling, and tolerate a rename refused by a reader on Windows (agent.js:421-437; the PowerShell reader retries 3×, psm1:272-276).
17. **Windows process discovery**: `Get-BackendProcesses` recognises backend processes by `ExecutablePath` under the install dir, or `node.exe` whose command line contains `<InstallDir>\app\`, or the supervisor script (psm1:221-233). A non-Node agent binary must live under the install directory to be stopped by `Stop-BackendProcesses`; run-servers spawned as `node.exe` are found by the `app\` path in their command line only if the cli path is under `<InstallDir>\app\` (run-backend.ps1:63).
18. **Ephemeral nodes**: with `ephemeral:true`, the hub deletes the node and its free slots `EPHEMERAL_GRACE_S` (120 s) after the control connection drops unless the same name re-registers first (hub.js:245, 346-368; Dockerfile:2-4, 29). Container runners register under the container-ID hostname.
19. **Versions**: the hub compares the client's `Playwright/x.y.z` user-agent major.minor with the node's `playwrightVersion` major.minor before routing (routing.js:34-44, 126); report the real playwright-core version string (`x.y.z`) or `unknown` (which disables the check for that node).
20. **Test harness expectations** (for keeping `gateway/test/gateway.test.js` green with a replacement): hub started in-process with `db:'memory'` / `MemoryRegistry`, port 0, `heartbeatTimeoutMs:30000`, `openTimeoutMs:5000` (helpers.js:54-66); agents started with the fake cli `test/fixtures/fake-playwright/cli.js` (an echo run-server honouring `info`, `lastclose`, `close:<code>:<reason>`, and the `x-fake-reject` header; package.json version `0.0.0-fake`), `FAST` timings (helpers.js:14-17: heartbeat 300 ms, health 100 ms, status 200 ms, restart 200→1000 ms, reconnect 100→400 ms, hubSilence 5 s), random free `basePort` in 20000–50000, a status file in a temp dir (helpers.js:74-85). Tests inspect `agent.registered`, `agent.connected`, `agent.lastError`, `agent.slots[i].healthy/restarts/child.pid`, `agent.sessions` (Set of `{id, upstream, data}`), `agent.config.statusFile/basePort`, and events `registered`/`disconnected(code)`/`status` (gateway.test.js:17, 152, 179-185, 226-235, 386, 399-407). The CLI test spawns `node agent.js --hub … --name cli-node --slots 2 --base-port … --playwright-cli … --status-file …` and asserts `status.pid === child.pid` and orphan cleanup after SIGKILL (gateway.test.js:294-323); `/api/nodes` must show `agentVersion '1.0.0'` and `platform === process.platform` of the test runner (gateway.test.js:31-32).

<!-- end verbatim protocol specification -->

Two notes on the verbatim text for the Go implementation: (a) `agentVersion` is `main.version` (decision 10), not `1.0.0`, and `gateway/test/gateway.test.js` keeps running against `agent.js` unchanged — the Go agent is tested by the new contract suite; (b) item 17's "a non-Node agent binary must live under the install directory" is satisfied by `<InstallDir>\pwrunner.exe`, and run-servers are discoverable by image path (`runtime\node\node.exe`) rather than command line.

---

## 7. Component specifications

Conventions for all packages: no global mutable state; every blocking call takes a `context.Context`; errors are returned, never logged and swallowed, except where the spec says "logged and ignored"; log lines never contain the token (the `config.Settings.Token` field has a `String()` that prints `<set>`/`<unset>` and the logger refuses to format any string equal to the token — a `logging.Redactor` installed at start-up with the token's value).

### 7.1 `internal/cli`

Responsibilities: parse `os.Args` into subcommand + flags in the forms `--k v` and `--k=v` (and `-h`/`--help`), produce usage text, map errors to exit codes.

```go
package cli

type FlagSpec struct {
    Name    string // "hub", "slots", ...
    Bool    bool   // flag may appear without a value (--ephemeral)
    Hidden  bool   // omitted from usage (--timings)
    Repeat  bool   // may appear more than once (none today)
    Usage   string
}
type Parsed struct {
    Command string
    Values  map[string][]string // raw values in order of appearance
    Rest    []string            // after "--" (pwrunner node -- ...)
}
func Parse(args []string, specs []FlagSpec) (Parsed, error)   // unknown flag -> error "unknown argument --x"
func Usage(command string, specs []FlagSpec) string
type ExitError struct{ Code int; Msg string }                 // Error() == Msg
func Exit(err error)                                          // prints Msg to stderr, os.Exit(Code); plain error -> code 1
```

Key behaviour: unknown flag, missing value, or a value failing validation → `ExitError{2, msg}` (the SCM and the old supervisor treat a fast exit 2 as "bad config"); `--help` → usage on stdout, exit 0; `--k=` (empty value) is a value, not a bool.

### 7.2 `internal/config`

Responsibilities: the agent's `Settings` (same names, defaults and validation as `gateway/lib/config.js:65-150`), the `backend.json` model, and the precedence `flags > environment > backend.json (--config)`.

```go
package config

type Timings struct {
    HeartbeatMs, HealthMs, StatusMs      int
    RestartBaseMs, RestartMaxMs, StableMs int
    ReconnectBaseMs, ReconnectMaxMs      int
    HubSilenceMs, ReplacedDelayMs        int
}
var DefaultTimings = Timings{10000, 5000, 5000, 5000, 300000, 60000, 1000, 30000, 45000, 30000}

type Settings struct {
    Hub           string   // ^wss?://[^/]+ (case-insensitive), trailing "/" stripped
    Token         Secret   // from --token, GATEWAY_AGENT_TOKEN, or agent.token in service mode
    Name          string   // canonical lower-case; default sanitizeName(hostname)
    Slots         int      // 1..64, default 4
    BasePort      int      // 1..(65535-Slots+1), default 3100
    PlaywrightCLI string   // path to cli.js; required
    NodeExe       string   // path to node binary; default runtime\node\node.exe next to the exe, else "node" on PATH
    Browsers      []string // comma-split, trimmed, empties dropped, default ["chromium"]
    StatusFile    string   // resolved against cwd; "" = not written
    LogDir        string   // resolved against cwd; "" = slot output to stderr prefixed "[slot i]"
    Ephemeral     bool     // 1/true/yes/on | 0/false/no/off | "" = false; anything else is an error
    Timings       Timings  // hidden --timings '{"heartbeatMs":300,...}' (Go only)
}
type Secret string
func (s Secret) String() string          // "<set>" or "<unset>"; never the value
func (s Secret) Value() string

func FromArgs(args []string, env func(string) string, hostname string) (Settings, error)
//   error messages are the exact strings of config.js (e.g. "slots must be 1-64", "unknown argument --x");
//   caller maps error -> ExitError{2,...}

type Backend struct {                       // backend.json; field order = file order
    HubURL            string   `json:"hubUrl"`
    NodeName          string   `json:"nodeName"`
    Slots             int      `json:"slots"`
    BasePort          int      `json:"basePort"`
    PlaywrightVersion string   `json:"playwrightVersion"`
    Browsers          []string `json:"browsers"`
    NodeExe           string   `json:"nodeExe"`
    NodeVersion       string   `json:"nodeVersion"`
    PlaywrightCLI     string   `json:"playwrightCli"`
    BrowsersPath      string   `json:"browsersPath"`
    TaskName          string   `json:"taskName,omitempty"` // read only (legacy scheduled task name); never written (decision 7)
    ServiceName       string   `json:"serviceName"`
    ServiceAccount    string   `json:"serviceAccount"`    // "LocalSystem" only in this release
    ReportURL         *string  `json:"reportUrl"`
    InstallDir        string   `json:"installDir"`
    DownloadHost      *string  `json:"downloadHost"`
    Ephemeral         bool     `json:"ephemeral"`
    LogMaxBytes       int64    `json:"logMaxBytes"`       // default 10485760
    LogKeep           int      `json:"logKeep"`           // default 5
    RunnerVersion     string   `json:"runnerVersion"`
    InstalledAt       string   `json:"installedAt"`       // ISO-8601 UTC
    LegacyPortCount   *int     `json:"portCount,omitempty"` // read only; never written (omit on write)
}
func ReadBackend(path string) (Backend, bool, error)       // (cfg, exists, err)
func WriteBackend(path string, b Backend) error            // ASCII, 2-space indent, trailing newline, atomic
func (b Backend) AgentSettings() Settings                  // what the service runs with (token loaded separately)
```

Validation is a port of `config.js:15-21, 65-115, 133-150`: port tests in `config_test.go` enumerate every message. `--config <backend.json>` (service mode) fills defaults from the file, then env, then flags.

### 7.3 `internal/protocol`

```go
package protocol

const (
    MaxSlots        = 64
    MaxPayload      = 256 << 20
    ControlMaxFrame = 1 << 20
    HighWater       = 4 << 20   // documentary; see pipe
    LowWater        = 1 << 20
)
var NameRE = regexp.MustCompile(`^[A-Za-z0-9._-]{1,128}$`)
var SessionIDRE = regexp.MustCompile(`^[0-9a-f-]{36}$`)
var DropHeaders = map[string]bool{"host":true,"connection":true,"upgrade":true,"content-length":true,
    "transfer-encoding":true,"authorization":true,"x-gateway-token":true,"keep-alive":true,"te":true,"trailer":true}

func CanonicalName(s string) string                  // lower-case; "" if !NameRE
func SanitizeName(hostname string) string            // [^A-Za-z0-9._-] -> "-", cut 128, "" -> "node"
func ForwardHeaders(h map[string][]string) http.Header // keys added with http.Header.Add (canonical form); drop DropHeaders, sec-websocket-*, proxy-*; keep string / []string only
//   Canonical keys matter: Go's net/http only recognises `User-Agent` in canonical form. A lower-case `user-agent` key
//   would be written in addition to Go's default `User-Agent: Go-http-client/1.1`, and Node keeps the first one, so
//   run-server would never see the client's `Playwright/x.y.z` and its 428 check would be disabled. When the forwarded
//   headers carry no user-agent, set `User-Agent` to "" so net/http adds no default. Test: exactly one user-agent
//   reaches the fake run-server and it equals the client's.
func SendableCloseCode(code int) int                 // 1000-1014 minus 1004/1005/1006 and 3000-4999 pass; 1005->1000; else 1011
func CloseReason(s string) string                    // <= 123 UTF-8 bytes, cut on rune boundary
func TokenLabel(token string) string                 // first 12 chars (as protocol.js:131-135), for `token show-prefix`
func TokenHash(token string) string                  // sha256 hex of the exact string

type Slot struct { Index int `json:"index"`; Healthy bool `json:"healthy"` }
type Register struct {
    Type string `json:"type"`; Name string `json:"name"`; Slots []Slot `json:"slots"`
    PlaywrightVersion string `json:"playwrightVersion"`; AgentVersion string `json:"agentVersion"`
    Platform string `json:"platform"`; Browsers []string `json:"browsers"`; Ephemeral bool `json:"ephemeral"`
}
type Heartbeat struct { Type string `json:"type"`; Slots []Slot `json:"slots"` }
type OpenFailed struct { Type string `json:"type"`; ID string `json:"id"`; Message string `json:"message"`; Status *int `json:"status,omitempty"` }
type Open struct { ID string; Slot json.RawMessage; Path, Query json.RawMessage; Headers map[string]json.RawMessage } // raw: validated by session
type Inbound struct { Type string; Raw json.RawMessage }
func ParseInbound(data []byte) (Inbound, error)      // object with string "type", else error
func Platform() string                               // runtime.GOOS: windows->"win32", linux->"linux", darwin->"darwin"
```

Frame field order in `Register`/`Heartbeat`/`OpenFailed` matches `agent.js` so logs and tests comparing JSON text are stable. `Message` in `OpenFailed` is cut to 4000 bytes before sending.

### 7.4 `internal/hubconn`

Responsibilities: one control connection to `<hub>/agent`, the register/registered handshake, periodic and urgent heartbeats, pings and the hub-silence watchdog, reconnect backoff with jitter, dispatch of `open` frames to a handler, and the `lastError`/`connected`/`registered` observables.

```go
package hubconn

type Options struct {
    HubURL       string
    Token        config.Secret
    Register     func() protocol.Register    // built fresh at every connect (current slot health)
    SlotHealth   func() []protocol.Slot
    OnOpen       func(ctx context.Context, frame protocol.Open)   // runs in its own goroutine per frame; ctx is the AGENT's lifetime ctx (the one passed to Run), never the control connection's: sessions are independent sockets and survive control-channel drops and reconnects, exactly as agent.js sessions do (hub.js:236-247 fails only pending opens)
    OnState      func(State)                 // every change: connected/registered/lastError
    Timings      config.Timings
    Logger       *logging.Logger
    Rand         func() float64              // injectable for tests
}
type State struct { Connected, Registered bool; LastError *string; Name string }
type Conn struct{ /* unexported */ }
func New(o Options) *Conn
func (c *Conn) Run(ctx context.Context) error     // blocks until ctx is done; reconnect loop inside
func (c *Conn) SendHeartbeat(urgent bool)         // no-op unless registered
func (c *Conn) SendOpenFailed(f protocol.OpenFailed)
func (c *Conn) State() State
```

Key behaviours (each is a test case in `hubconn_test.go` against a fake hub built with `httptest.Server` + `websocket.Accept`):

- Dial: `websocket.Dial(ctx10s, hub+"/agent", &DialOptions{HTTPHeader: {"Authorization": "Bearer "+token} (only if set), HTTPClient: &http.Client{Transport: &http.Transport{Proxy: nil}}, CompressionMode: CompressionDisabled})`; `conn.SetReadLimit(ControlMaxFrame)`; on success `Connected=true`, send `Register` immediately as a **text** frame.
- Reader goroutine always in `conn.Read` (control frames are only serviced while reading); `OnPongReceived` and every message set `lastHubActivity`. Inbound dispatch: `registered` → `Registered=true`, `LastError=nil`, backoff reset, log `registered with the hub as <name>`; `open` → `OnOpen` in a goroutine; `error` → `LastError=message`, log; malformed → log and ignore; unknown type → ignore.
- Writer: one mutex around `conn.Write(ctx, MessageText, json)`; a failed write closes the connection (`CloseNow`).
- Tick every `HeartbeatMs`: if `now - lastHubActivity > HubSilenceMs` → `LastError="hub stopped answering"`, `CloseNow()`; else `conn.Ping(ctx)` (async, result sets `lastHubActivity`) and, if registered, `Heartbeat{slots}`.
- Close handling: `websocket.CloseStatus(err)` gives the code (−1 → 1006, no reason). `LastError = "hub closed the connection (<code>[ <reason>])"` unless code is 1006 and a socket error was already recorded (then keep the error text). Then `Connected=false`, `Registered=false`, `OnState`, and sleep `delay` where `delay = d + floor(rand*d*0.3)`, `d = min(2d, ReconnectMaxMs)` starting at `ReconnectBaseMs`; for 4409 `delay = max(delay, ReplacedDelayMs)`. Codes 4401/4403 log at ERROR, 1013 at WARN, everything else INFO; none stops the loop. Dial errors (refused, handshake timeout, non-101) set `LastError = "<err>"` and take the same backoff.
- Stop: when ctx is cancelled, close with `conn.Close(1001, "agent is shutting down")` (bounded by 2 s, then `CloseNow`), no reconnect.
- Never sends `heartbeat` before `registered`; pings are allowed any time.

### 7.5 `internal/session` and `internal/pipe`

```go
package session

type Options struct {
    HubURL   string
    Token    config.Secret
    SlotAddr func(index int) (addr string, ok bool)  // "127.0.0.1:<port>" for 1..N
    Report   func(protocol.OpenFailed)
    Timings  config.Timings
    Logger   *logging.Logger
}
type Manager struct{ /* unexported */ }
func NewManager(o Options) *Manager
func (m *Manager) HandleOpen(ctx context.Context, f protocol.Open)  // the whole §5 flow; returns when the session ends
func (m *Manager) Count() int
func (m *Manager) Tick(now time.Time)                               // data-channel pings and the HubSilenceMs check (flow step 7)
func (m *Manager) CloseAll(code int, reason string)                 // on stop: 1001 "agent is shutting down"
```

Flow of `HandleOpen` (each numbered item is a test):

1. `ID` must match `SessionIDRE` else log WARN and return. `Slot` must decode to an integer `1..N` else `Report{ID, "no slot <raw>"}`. `Path`: string starting with `/`, else `/`. `Query`: non-empty string → `?`+query, else "". `Headers`: `ForwardHeaders` applied again; values must be strings or arrays of strings; others dropped.
2. Open deadline: `dialCtx, cancel := context.WithTimeout(ctx, 14*time.Second)` (inside the hub's 15 s open timeout) bounds **only the two dials** (this step and step 5); `cancel()` is called as soon as both are open. The piped session (step 6) runs under the agent's lifetime `ctx` with no deadline and lives until either side closes or the agent stops. Dial upstream `ws://127.0.0.1:<port><path><?query>` with the headers, `CompressionDisabled`, `SetReadLimit(MaxPayload)`, handshake timeout 10 s via `dialCtx`, `Proxy: nil`. The `Host` header is left to the HTTP client (`127.0.0.1:<port>`), as `ws` does.
3. Non-101: `status := resp.StatusCode`; body = whatever `resp.Body` holds (bounded prefix, decision 16), trimmed, cut to 2000 chars; `Report{ID, "run-server on port <p> answered HTTP <status>[: <body>]", &status}`; return.
4. Dial error without a response: `Report{ID, "run-server on port <p>: <err>"}` with no status; return.
5. Dial `<hub>/agent/data/<id>` with the same `Authorization`, `CompressionDisabled`, `SetReadLimit(MaxPayload)`, 10 s handshake. Failure (HTTP 401/404/410/503 or network): `Report{ID, "data channel: <err>"}`, close upstream with 1011, return.
6. `pipe.Run(ctx, upstream, data)` with the agent's lifetime `ctx`; when it returns, log the session end with both close codes.
7. Data-channel liveness (port of `agent.js:327-342`): the manager records `lastActivity` for every session on any frame, ping or pong received on the data socket (`OnPingReceived`/`OnPongReceived` in the dial options, plus every `Reader` return). Every `HeartbeatMs` tick (`Manager.Tick()`, called by `internal/agent` from the same ticker as the control heartbeat) it pings every open data socket (`conn.Ping` with a 5 s context, asynchronously) and, when `now - lastActivity > HubSilenceMs`, logs `session <id>: no word from the hub on its data channel for 45s; closing it` and `CloseNow()`s **both** legs, so run-server closes the browser. Integration test 15 covers a hub host that vanishes without FIN.

```go
package pipe

// Run copies frames in both directions until either side closes, mirroring the close to the other side.
// Frame type (text/binary) is preserved. Frames are streamed with Reader/Writer, never fully buffered.
// Backpressure: a direction reads the next frame only after the previous frame was fully written to the
// destination, so at most one in-flight frame per direction is buffered by the kernel/socket.
func Run(ctx context.Context, a, b *websocket.Conn) (closeA, closeB Result)
type Result struct { Code int; Reason string; Err error }
func mirror(err error) (code int, reason string) // CloseStatus(err): -1/1006 -> 1011 ""; 1005 -> 1000; else SendableCloseCode(code), CloseReason(reason)
```

Two goroutines per session (`a→b`, `b→a`): `for { typ, r, err := src.Reader(ctx); if err → mirror to dst, return; w, _ := dst.Writer(ctx, typ); io.Copy(w, r); w.Close() }`. Writes use a per-connection mutex (coder/websocket allows one concurrent writer). Tests: 5 MiB binary frame, text/binary preservation, close mapping (4123/4321 pass with reason, missing code → 1000, abrupt loss → 1011, reason > 123 bytes truncated), 48 MiB burst with a stalled reader (the writer side must block, the reader's socket buffer must not grow beyond a few MiB; measured with `conn`'s underlying `net.Conn` wrapped in a counting reader), and "both sides closed exactly once".

### 7.6 `internal/slot`

```go
package slot

type Options struct {
    Index     int
    Port      int
    NodeExe   string
    CLI       string            // cli.js
    Env       []string          // allow-listed env (decision 14) incl. PLAYWRIGHT_BROWSERS_PATH
    WatchdogJS string           // path to parent-watchdog.js; never "" in practice: `run` and `service` always resolve one (see below)
    LogDir    string            // "" -> prefixed stderr
    Timings   config.Timings
    OnHealth  func(index int, healthy bool)
    Logger    *logging.Logger
    Job       proc.Job          // Windows: the agent's own kill-on-close job; nil elsewhere
}
type Slot struct{ /* unexported */ }
type Status struct { Index, Port int; PID *int; Healthy bool; Restarts int; Since *time.Time }
func New(o Options) *Slot
func (s *Slot) Run(ctx context.Context)   // start, probe, restart loop; returns after Stop completes
func (s *Slot) Status() Status
func (s *Slot) Stop(ctx context.Context)  // cancel pending restart, graceful then hard kill (see below)
```

Behaviours (port of `agent.js:39-181`):

- Spawn: `exec.CommandContext(nil, NodeExe, CLI, "run-server", "--port", port, "--host", "127.0.0.1")`, `Dir` = directory of `CLI`, stdin = `nil` (as `agent.js:113` `'ignore'`), stdout+stderr → `slot-<i>.log` (previous renamed to `.log.prev`; directory created) or prefixed stderr; Windows `SysProcAttr{HideWindow: true, CreationFlags: CREATE_NO_WINDOW}`; child inherits the agent's job (no per-slot job in this release; `taskkill /T`-equivalent is `proc.KillTree`).
- `NODE_OPTIONS` = `strings.TrimSpace(orig + " --require " + strconv.Quote(WatchdogJS))` (JSON-quoted path as `agent.js:52-62`), `GATEWAY_AGENT_NODE_OPTIONS` = `orig` (decision 14 says where `orig` comes from). Watchdog path resolution, in order: `--watchdog` flag (`PWRUNNER_WATCHDOG`); in service mode `<D>\app\lib\parent-watchdog.js` (written at install from `assets/`); otherwise (`run` console mode, the integration harness, the contract suite) the embedded `assets.ParentWatchdogJS` is written at start to `<os.TempDir()>\pwrunner-<version>\parent-watchdog.js` (overwritten every start, 0644) and that path is used. The preload is therefore always on; the unit test for the `NODE_OPTIONS` string uses an explicit `--watchdog` path.
- Health: process alive **and** `net.DialTimeout("tcp", "127.0.0.1:<port>", 2s)` succeeds; checked every `HealthMs` and every 250 ms for the first 30 s after a start until healthy. Transitions call `OnHealth` (the agent sends an urgent heartbeat and writes status).
- Exit: `Healthy=false`; if lifetime ≥ `StableMs` backoff resets to `RestartBaseMs`; restart after the backoff; backoff doubles up to `RestartMaxMs`; `Restarts++`. No restart while stopping.
- Stop: cancel any pending restart, then `proc.KillTree(pid)` at once (run-server has no graceful stop, see §2; Windows: `taskkill /pid <pid> /T /F` hidden, fallback `TerminateProcess`; Linux: SIGTERM to the process group, SIGKILL after 5 s), give up waiting after 8 s, as `agent.js:64-82`.
- `playwrightVersion` for `register`: read `package.json` next to `CLI` (`version` or `unknown`), function `PlaywrightVersion(cli string) string` in this package.

### 7.7 `internal/status`

```go
package status

type SlotStatus struct {
    Index int `json:"index"`; Port int `json:"port"`; PID *int `json:"pid"`; Healthy bool `json:"healthy"`
    Restarts int `json:"restarts"`; Since *string `json:"since"`
}
type File struct {
    Name string `json:"name"`; Hub string `json:"hub"`; AgentVersion string `json:"agentVersion"`
    PlaywrightVersion string `json:"playwrightVersion"`; PID int `json:"pid"`; Connected bool `json:"connected"`
    Registered bool `json:"registered"`; LastError *string `json:"lastError"`; Updated string `json:"updated"`
    Slots []SlotStatus `json:"slots"`
}
type Writer struct{ /* path, pid, logger, warnedOnce */ }
func NewWriter(path string, logger *logging.Logger) *Writer
func (w *Writer) Write(f File) error   // MarshalIndent("", "  ") + "\n"; <path>.<pid>.tmp then os.Rename; mkdir -p; on Windows retry rename 5x over 250 ms on sharing violation; first failure logged once
func Now() string                      // time.Now().UTC().Format("2006-01-02T15:04:05.000Z")
```

Key order is fixed by the struct; a golden test compares against a fixture produced by `agent.js` (`gateway/test/gateway.test.js:212,221` asserts the same order). Cadence (`StatusMs`, every state change, on stop) is driven by `internal/agent`.

### 7.8 `internal/agent`

```go
package agent

type Agent struct{ /* unexported */ }
func New(s config.Settings, version string, logger *logging.Logger) (*Agent, error)
func (a *Agent) Run(ctx context.Context) error   // starts slots, hubconn, status ticker; blocks; graceful stop on ctx cancel (bounded 10 s)
func (a *Agent) Snapshot() status.File           // for tests and the service's health reporting
```

Wiring: `slot.Options.OnHealth` → `hubconn.SendHeartbeat(true)` + status write; `hubconn.Options.OnOpen` → `session.HandleOpen` (with the agent's lifetime ctx); one `HeartbeatMs` ticker drives both `hubconn`'s tick and `session.Manager.Tick` (data-channel pings and silence check); `hubconn.Options.OnState` → status write; `Register()` built from `slot.Status()`, `PlaywrightVersion(cli)`, `version`, `protocol.Platform()`, `Browsers`, `Ephemeral`. Start-up log line: `agent <version> node <name> hub <hub> slots <n> base-port <p> cli <path> node <nodeExe> browsers <a,b> token <set|unset>`. Stop order: sessions `CloseAll(1001,"agent is shutting down")` → control close → slots stop (parallel) → final status write.

### 7.9 `internal/runtime` (Node + playwright-core acquisition)

```go
package runtime

type Pin struct { URL, SHA256 string; Size int64 }   // Size 0 = unknown
var NodeVersion = "22.23.4"
var NodePins = map[string]Pin{ // key "<goos>/<goarch>"
    "windows/amd64": {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-x64.zip",   "cbc74b5e0982c9dbb41525fd8fb5dcd32fbee237863f7829072f4639a1ce5f20", 35604229},
    "windows/arm64": {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-win-arm64.zip", "a8b8cd79cbbb3062435f449591d98dd63730bbbf30ce2d7cc3a6376c236cb111", 0},
    "linux/amd64":   {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-x64.tar.gz",   "<from SHASUMS256.txt at pin time>", 0},
    "linux/arm64":   {"https://nodejs.org/dist/v22.23.4/node-v22.23.4-linux-arm64.tar.gz", "<from SHASUMS256.txt at pin time>", 0},
}
var PlaywrightVersion = "1.64.1"
var PlaywrightPins = map[string]string{ // version -> SRI integrity
    "1.64.1": "sha512-BC4s8y6SEVA6Q1rSxZTto8hI1KUI3xXlHGZgvGqP4IqPdI/a6FX9H29IDNz7vKo9l21b6LfYosScPL6M3xxyDQ==",
}
var NodeEOL = map[string]string{"22": "2027-04-30", "24": "2028-04-30"}   // selfcheck warning

type Source struct { OfflineDir, NodeMirror, NPMRegistry string; NodeSHA256, PlaywrightIntegrity string /* overrides for unpinned versions */ }
func Embedded() (nodeZip, pwTgz []byte, ok bool)                            // build-tagged
func FetchNode(ctx, cacheDir, version, goos, goarch string, src Source) (path string, err error)   // embedded > offline dir > download; verify sha256 while streaming; ".part" then rename
func FetchPlaywright(ctx, cacheDir, version string, src Source) (path string, err error)          // same with sha512 SRI
func ExtractNode(archive, destDir string) error         // zip: only "node-v*/node.exe" and LICENSE; tar.gz: bin/node and LICENSE; reject "..", absolute paths, symlinks
func ExtractPlaywright(tgz, destDir string) error       // only "package/*" -> destDir; zip-slip safe
type Manifest struct { NodeVersion, NodeSHA256, PlaywrightVersion, PlaywrightIntegrity, RunnerVersion, Source, Fetched string }
func ReadManifest(installDir string) (Manifest, bool, error)
func WriteManifest(installDir string, m Manifest) error
func LayDown(ctx, installDir string, want Manifest, src Source, log *logging.Logger) (changed bool, err error)
//   if manifest matches want -> log "already installed, skipping npm" and return false
//   else build <installDir>\app.new\ (node_modules\playwright-core from tgz, lib\parent-watchdog.js from assets)
//   and <installDir>\runtime.new\node\ ; verify `node.exe --version` == "v"+NodeVersion ; then swap:
//   app -> app.prev (remove older app.prev first), app.new -> app ; runtime -> runtime.prev, runtime.new -> runtime ;
//   write manifest. Rollback(installDir) reverses the swap if a later step fails.
func Rollback(installDir string) error
func InstallBrowsers(ctx, nodeExe, cli, browsersPath string, browsers []string, downloadHost string, log *logging.Logger) error
//   env: allow-list of decision 14 (proxy variables included) + PLAYWRIGHT_BROWSERS_PATH=browsersPath (+PLAYWRIGHT_DOWNLOAD_HOST); argv: cli install <browsers...>
//   stdout/stderr -> install.log ; non-zero exit -> error
```

Rules: a requested Node or Playwright version not in the pins table requires `--node-sha256` / `--playwright-integrity` (else exit 2 with a message naming the flag); in the embedded build a version different from the embedded one takes the download path; downloads honour `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` via `http.ProxyFromEnvironment`, 10 min timeout; hash mismatch deletes the file and fails the step `install node packages`. The Linux Node tarball contains `bin/node` — the agent's `NodeExe` default on Linux is `node` on PATH (Docker image), so `FetchNode` on Linux is used only by Phase 6.

`build/manifest.xml` (longPathAware, `requestedExecutionLevel level="asInvoker"`) is embedded as a Windows resource with `go install github.com/tc-hib/go-winres@v0.3.3` + `go generate` (`//go:generate go-winres make --in build/winres.json`); `install` and `uninstall` check elevation themselves (§7.12) rather than requesting it from the manifest, so `pwrunner status` and `version` run unelevated.

### 7.10 `internal/service`

```go
package service

type Config struct { Name, DisplayName, Description, ExePath, InstallDir string; Account string /* "LocalSystem" */ }
func Install(c Config) error        // mgr.Connect; CreateService(name, exe, mgr.Config{StartType: StartAutomatic, ServiceStartName: "", DisplayName, Description}, "service", "--install-dir", dir) or update existing config;
                                    // s.SetRecoveryActions([{ServiceRestart,5s},{ServiceRestart,60s},{ServiceRestart,300s}], 86400); s.SetRecoveryActionsOnNonCrashFailures(true); eventlog.InstallAsEventCreate(name, Error|Warning|Info)
                                    // Install is re-runnable: an existing service gets UpdateConfig, and an InstallAsEventCreate error whose text contains "registry key already exists" is treated as success (x/sys returns it when the source exists).
func Remove(name string) error      // stop, Delete, eventlog.Remove
func Start(name string, timeout time.Duration) error
func Stop(name string, timeout time.Duration) error   // Control(svc.Stop), poll until Stopped
func Query(name string) (State, error)                // NotInstalled | Stopped | StartPending | Running | StopPending
func IsWindowsService() bool
func RunAsService(name string, run func(ctx context.Context) error) error   // svc.Run with a Handler: StartPending -> Running immediately; Stop/Shutdown -> cancel ctx, StopPending, wait <= 25 s, Stopped; agent error exit 2 -> svcSpecificEC=true, exitCode=2
func RunConsole(run func(ctx context.Context) error) error                  // Ctrl-C/SIGTERM/SIGBREAK -> cancel; hard exit after 10 s
```

The service process: reads `backend.json`, reads `agent.token` into a `config.Secret`, builds `Settings` with `StatusFile=<dir>\runtime.json`, `LogDir=<dir>\logs`, `NodeExe=<dir>\runtime\node\node.exe`, `PlaywrightCLI=<dir>\app\node_modules\playwright-core\cli.js`, creates the kill-on-close Job Object and assigns itself (`winsys.NewKillOnCloseJob`, `AssignCurrent`), kills leftover processes whose image is under `<dir>\runtime\node` (as `run-backend.ps1:24-27` does for a previous supervisor's leftovers), deletes nothing else, then `agent.Run`. Event Log: Info on start/stop, Warning when a slot's restart delay exceeds 60 s, Error on exit 2. Non-Windows: `RunConsole` only.

### 7.11 `internal/install`

Install-dir layout (Windows, `<D>` = install dir):

```
<D>\pwrunner.exe                      installed copy (service BinaryPathName); owner Administrators
<D>\backend.json                      config (§7.2 Backend)
<D>\agent.token                       token; DACL SYSTEM FA + Administrators FA only; UTF-8 no BOM, no newline
<D>\runtime.json                      live status (agent); deleted before start by install
<D>\status.json                       last install/status report
<D>\manifest.json                     runtime.Manifest
<D>\runtime\node\node.exe, LICENSE    private Node
<D>\app\node_modules\playwright-core\ package/ from the tgz (cli.js, package.json, lib\coreBundle.js, ...)
<D>\app\lib\parent-watchdog.js        from assets/
<D>\app.prev\, <D>\runtime.prev\      previous generation (rollback source), replaced on the next successful install
<D>\browsers\                         PLAYWRIGHT_BROWSERS_PATH
<D>\logs\install.log, service.log, agent.log, slot-<i>.log[.prev]   (+ .1..5 rotated generations)
<D>\cache\                            downloaded archives (slim build); safe to delete
```

```go
package install

type Params struct {
    InstallDir, HubURL, Name string; Slots, BasePort int; Browsers []string
    Token *string /* nil = keep, "" = delete */; TokenSource string /* "argv"|"file"|"stdin" for the warning */
    PlaywrightVersion, NodeVersion string; ReportURL *string; HealthTimeout time.Duration
    ServiceName string; NoService bool; Source runtime.Source; BrowsersFrom string; SkipBrowsers, ForceReset, JSON bool
    Bound map[string]bool /* which flags were passed explicitly: re-run keep rules */
}
type StepName string
const (
    Preflight     StepName = "preflight"
    StopExisting  StepName = "stop existing backend"
    NodePackages  StepName = "install node packages"
    InstallAgent  StepName = "install agent"
    Browsers      StepName = "install browsers"
    WriteConfig   StepName = "write config"
    Firewall      StepName = "remove legacy firewall rule"
    StartBackend  StepName = "start backend"
)
func Run(ctx context.Context, p Params, version string) (report.Report, int /* exit code */)
func Uninstall(ctx context.Context, installDir string, removeFiles, yes bool) error
```

Step contents (the implementer ports the referenced PowerShell behaviour; `INS:<lines>` means lines of `installer/windows/install-backend.ps1`, `PSM:<lines>` lines of `installer/windows/PlaywrightBackend.psm1`):

| Step | Does | Fails when |
|---|---|---|
| `preflight` | elevation required unless `--no-service` (`winsys.IsElevated`); non-elevated against a locked folder → error text of INS:200-202; `len(installDir) > 150` → WARN with the MAX_PATH text of INS:245-248; read old `backend.json`; merge (keep `hubUrl, nodeName, slots, basePort, browsers, reportUrl, serviceName` unless in `Bound`; `playwrightVersion`/`nodeVersion`/`healthTimeout`/`noService` never kept; legacy `portCount` > 64 clamped with WARN; log `Changing X from 'a' to 'b'` and `Keeping the installed settings: …`); validate (`NameRE`, slots 1..64, base-port range, hub URL); `GET <hub as http(s)>/healthz` with 5 s timeout, WARN only; elevated + dir missing → `winsys.CreateLockedDir` **before the log file is opened** | missing hub URL on first run; invalid values; not elevated |
| `stop existing backend` | record `previous{serviceExisted, wasRunning, legacyTaskName, legacySupervisorPIDs}`; `service.Stop` if installed; legacy task name = old `backend.json.taskName`, else `PlaywrightBackend` (INS:287): `schtasks /End /TN <name>` + stop `powershell.exe` processes whose command line contains `<D>\run-backend.ps1`; `winsys.KillByImagePrefix(<D>)` (supervisors first, wait 15 s); `--no-service` with an existing task/service → unregister elevated else WARN; delete stale `runtime.json`; elevated: `winsys.PathProblem(<D>)` → `ResetUntrusted` (logs `from scratch`, keeps `logs`, `backend.json`, `*.ps1`, `*.psm1`; token content kept and file re-locked; `previous.Wiped=true`) else `Protect(<D>, usersCanRead=true)` | stop/kill failures |
| `install node packages` | `runtime.LayDown` (skip logs `already installed, skipping npm`) | fetch/verify/extract failure (→ rollback not needed, nothing swapped yet) |
| `install agent` | copy the running exe to `<D>\pwrunner.exe` (if the running exe *is* that file: rename to `pwrunner.old.exe`, copy, delete `.old` at next start); run `<D>\pwrunner.exe version` as a smoke test; `Protect` the file. **Legacy files**: move `<D>\install-backend.ps1`, `run-backend.ps1`, `backend-status.ps1`, `uninstall-backend.ps1`, `PlaywrightBackend.psm1`, `app\gateway\` and `logs\supervisor.log`, `logs\agent.err.log` into `<D>\legacy\` and write `<D>\legacy\README.txt` ("replaced by pwrunner.exe on <date>; use pwrunner.exe status / uninstall"), so the installed copy of `install-backend.ps1` can no longer re-register the scheduled task and start a second agent with the same node name (4409 every 30 s). Log `Moved the PowerShell installer files to legacy\` | copy or smoke failure (→ `runtime.Rollback`) |
| `install browsers` | unless `--skip-browsers`: `--browsers-from <dir>` copies the tree, else `runtime.InstallBrowsers` | non-zero exit (→ rollback) |
| `write config` | token: `--token ""` → delete file; token given → `winsys.WriteTokenFile` (delete-first, `CreateFile` with `CREATE_NEW` + SDDL `D:P(A;;FA;;;SY)(A;;FA;;;BA)`, no BOM, no newline); none and no file → WARN `no agent token: the hub will reject this node if it sets GATEWAY_AGENT_TOKEN`; write `backend.json` and `manifest.json` | write failure (→ rollback) |
| `remove legacy firewall rule` | `netsh advfirewall firewall delete rule name="Playwright Backend (<serviceName>)"`; not found or not elevated → WARN | never fatal |
| `start backend` | `previous.Replaced=true`; legacy task present → `schtasks /Delete /TN <name> /F` (elevated) else WARN; `--no-service` → spawn detached `<D>\pwrunner.exe service --install-dir <D> --console` and write `<D>\pwrunner.pid`; else `service.Install` + `service.Start`; then `report.WaitHealthy(<D>, slots, healthTimeout)` | service start failure (→ rollback + restore previous) |

After the steps: `result = success (0) | degraded (2, health problem) | failed (1, exception)`; on failure before `previous.Replaced` and unless `previous.Wiped`, restart the previous service/task/supervisor and set `restartedPrevious`; on failure after the runtime swap, `runtime.Rollback`. Write `status.json` (best effort), POST to the report URL (15 s, WARN on failure), print the console summary (`Result: <result> (<h>/<t> slots healthy, <n>s)`, per-step seconds, slot table, `Connect with <hub>/connect?node=<name>`).

Uninstall: refuse without elevation when a service or task exists; `service.Stop` + `service.Remove`; legacy task + firewall rule removal; `--remove-files`: zero-fill `agent.token` then delete, `os.Chdir(%TEMP%)`, `os.RemoveAll` with `\\?\`-prefixed path (Go handles long paths), browsers included; without `--remove-files` keep `browsers\` (fast reinstall). Prompts for confirmation unless `--yes` (stdin not a terminal → requires `--yes`).

### 7.12 `internal/winsys` (Windows only; stubs elsewhere)

```go
package winsys

const (
    SDDLInstallDir = "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;0x1200a9;;;BU)"   // Users RX, protected, owner Administrators
    SDDLTokenFile  = "O:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)"
    SIDSystem = "S-1-5-18"; SIDAdministrators = "S-1-5-32-544"; SIDUsers = "S-1-5-32-545"
    UntrustedWriteMask = 0x500D0156   // GENERIC_ALL/WRITE, WRITE_OWNER, WRITE_DAC, DELETE, write/append/delete-child
)
func IsElevated() (bool, error)                                   // token membership of Administrators
func EnablePrivileges() error                                     // SeTakeOwnershipPrivilege, SeRestorePrivilege
func CreateLockedDir(path string, sddl string, currentUserFull bool) error   // CreateDirectory with SecurityAttributes; non-elevated adds the current user's SID FullControl
func Protect(path string, usersCanRead bool) error                // SetNamedSecurityInfo OWNER -> BA, then DACL from SDDL (protected)
func PathProblem(path string) (string, error)                     // "" = trusted: owner in {SY,BA}, DACL protected, no Allow ACE for a non-trusted SID with any bit of UntrustedWriteMask
func ResetUntrusted(installDir string, keep []string, log *logging.Logger) (tokenContent string, err error)   // port of Reset-UntrustedInstall: take ownership of root, lock, delete reparse points without following, agent.token locked and read, others: SetOwner(BA), unprotected empty DACL, recurse with \\?\, delete unless kept; "rd /s /q" replaced by os.RemoveAll on \\?\ path
func WriteTokenFile(path, token string) error                     // delete first; CreateFile CREATE_NEW with SDDLTokenFile; write UTF-8 bytes; no newline
func ReadTokenFile(path string) (string, bool, error)             // TrimSpace; "" -> not present
type Job struct{ h windows.Handle }
func NewKillOnCloseJob() (*Job, error)                            // CreateJobObject + SetInformationJobObject(JobObjectExtendedLimitInformation, KILL_ON_JOB_CLOSE)
func (j *Job) AssignCurrent() error
func (j *Job) Assign(pid int) error
func (j *Job) Terminate(exit uint32) error
func (j *Job) Close() error
type ProcInfo struct { PID, ParentPID int; Image string }
func ListProcesses() ([]ProcInfo, error)                          // Toolhelp32 snapshot + QueryFullProcessImageName (empty Image when access is denied)
func KillByImagePrefix(prefix string, wait time.Duration) (killed []int, err error)   // excludes own PID; TerminateProcess; WaitForSingleObject
func ImagePathOf(pid int) (string, error)
func CommandLinesMatching(substr string) ([]int, error)           // via powershell.exe -NoProfile -NonInteractive -Command "Get-CimInstance Win32_Process | ..." ; migration only
```

Unit tests on Windows: `CreateLockedDir` then `PathProblem` == ""; plant an Allow ACE for Users with `FILE_GENERIC_WRITE` → non-empty problem naming the SID; junction inside the dir is deleted by `ResetUntrusted` and its target untouched; `WriteTokenFile` DACL has exactly two ACEs (SY, BA); `NewKillOnCloseJob` + `cmd /c timeout 60` child → child gone within 2 s of `Close()`.

### 7.13 `internal/report` and the `status` command

```go
package report

type Slot struct { Index, Port int; Healthy bool; Restarts int; PID *int }
type Report struct {   // key order = status.json order (New-BackendReport, psm1:312-342)
    Node string `json:"node"`; HubURL string `json:"hubUrl"`; Result string `json:"result"`
    PlaywrightVersion string `json:"playwrightVersion"`; AgentVersion string `json:"agentVersion"`
    Connected bool `json:"connected"`; Registered bool `json:"registered"`; Problem *string `json:"problem"`
    LastError *string `json:"lastError"`; HealthySlots int `json:"healthySlots"`; TotalSlots int `json:"totalSlots"`
    Slots []Slot `json:"slots"`; FailedStep *string `json:"failedStep"`; Error *string `json:"error"`
    DurationSec float64 `json:"durationSec"`; Steps map[string]float64 `json:"steps,omitempty"`
    Timestamp string `json:"timestamp"`; RestartedPrevious *bool `json:"restartedPrevious,omitempty"`
    RunnerVersion string `json:"runnerVersion"`; NodeVersion string `json:"nodeVersion"`
}
func ReadRuntime(path string) (status.File, bool, error)        // 3 attempts 100 ms apart on parse error
func AgentAlive(f status.File, installDir string) bool          // pid running AND image path == <installDir>\pwrunner.exe (Windows) AND updated <= 30 s old
func Problem(f *status.File, alive bool, expectedSlots int) *string
//   nil file     -> "the agent has not written runtime.json yet"
//   !alive       -> "the agent (pid N) is not running or has stopped updating runtime.json"
//   !connected   -> "not connected to the hub: <lastError>"
//   !registered  -> "connected but not registered with the hub yet"
//   len != want  -> "the agent runs X slots, expected Y"
//   unhealthy    -> "unhealthy slots: i, j"
func WaitHealthy(ctx, installDir string, slots int, timeout time.Duration) (status.File, *string)   // poll 500 ms
func Build(cfg config.Backend, f *status.File, alive bool, problem *string, extra ...) Report
func WriteStatusJSON(path string, r Report) error
func Post(ctx, url string, r Report) error                      // POST application/json, 15 s timeout
func PrintHuman(w io.Writer, r Report)                          // "<node>: <RESULT> (<h>/<t> slots healthy, hub <H>, Playwright <V>)", connected/registered, problem, slot table
```

`pwrunner status`: exit 0 healthy, 2 problem, 1 no install (`backend.json` missing or without `hubUrl`); `--health-timeout N` polls while unhealthy; `--json` prints the report; `--report-url` (flag or `backend.json.reportUrl`) POSTs it. `connected`/`registered`/`slots[].healthy` are ANDed with `alive`.

### 7.14 `internal/proc`, `internal/logging`, `assets`

- `proc.Spawn(cmd *exec.Cmd, hidden bool)`; `proc.KillTree(pid int) error` (Windows: `taskkill /pid <pid> /T /F` with `HideWindow`, fallback `TerminateProcess`; Unix: `syscall.Kill(-pgid, SIGTERM)`, `SIGKILL` after 5 s); `proc.Alive(pid int) bool`.
- `logging.New(w io.Writer, redact []string) *Logger`; `logging.OpenRotating(path string, maxBytes int64, keep int) (io.WriteCloser, error)`; levels `INFO WARN ERROR DEBUG`; `[2006-01-02 15:04:05] LEVEL msg`; `Logger.Redactor` replaces any occurrence of a redacted value with `<redacted>` (defence in depth; the token must never reach a log line in the first place).
- `assets.ParentWatchdogJS []byte` via `//go:embed parent-watchdog.js`; test compares with `../../gateway/lib/parent-watchdog.js` and fails with "update runner/assets/parent-watchdog.js from gateway/lib" on drift.

---

## 8. CLI reference

Flag forms: `--name value` and `--name=value`; `--flag` alone for booleans. Exit code 2 for any usage or value error (message on stderr), 0 for `--help`. Global flags: `--install-dir <path>` (default: the exe's own folder if it contains `backend.json`, else `C:\PlaywrightBackend`; Linux: `/opt/pwrunner`), `--log-level info|debug`.

| Subcommand | Flags / env | Exit codes | Example |
|---|---|---|---|
| `run` | `--hub` (`HUB_URL`), `--token` (`GATEWAY_AGENT_TOKEN`; env preferred), `--name` (`NODE_NAME`), `--slots` (`SLOTS`), `--base-port` (`BASE_PORT`), `--playwright-cli` (`PLAYWRIGHT_CLI`), `--node` (`PWRUNNER_NODE`), `--browsers` (`BROWSERS`), `--status-file` (`STATUS_FILE`), `--log-dir` (`LOG_DIR`), `--watchdog <path>` (`PWRUNNER_WATCHDOG`; default: the embedded watchdog written to the temp dir, §7.6), `--ephemeral[=bool]` (`EPHEMERAL`), `--config <backend.json>`, hidden `--timings <json>` | 0 stopped by signal; 1 runtime failure; 2 bad config | `pwrunner run --hub ws://127.0.0.1:14500 --name dev-01 --slots 2 --base-port 3100 --playwright-cli C:\PlaywrightBackend\app\node_modules\playwright-core\cli.js` |
| `install` | `--hub-url`, `--token` (WARN: prefer `--token-file`/`--token-stdin`), `--token-file <path>`, `--token-stdin`, `--name`, `--slots`, `--base-port`, `--playwright-version`, `--playwright-integrity sha512-…`, `--node-version`, `--node-sha256`, `--browsers a,b`, `--report-url`, `--health-timeout <sec, 60>`, `--service-name` (`PlaywrightBackend`), `--no-service`, `--offline-dir <dir>`, `--node-mirror <url>`, `--npm-registry <url>`, `--download-host <url>`, `--browsers-from <dir>`, `--skip-browsers`, `--force-reset`, `--json`; ignored with WARN: `--bind-host --advertise-host --no-firewall --install-node`; aliases accepted: `--port-count`=`--slots`, `--port-start`=`--base-port` | 0 success; 2 degraded (installed but not healthy within the timeout) or usage; 1 failed | `Get-Content .\token.txt \| .\pwrunner.exe install --hub-url wss://hub.example:4500 --token-stdin --name win-07 --slots 4` |
| `uninstall` | `--remove-files`, `--yes` | 0; 1 failure; 2 not elevated / refused | `pwrunner uninstall --remove-files --yes` |
| `status` | `--json`, `--report-url`, `--health-timeout <sec, 0>` | 0 healthy; 2 problem; 1 not installed | `pwrunner status --json` |
| `start` / `stop` / `restart` | `--timeout <sec, 30>` (SCM; with a `--no-service` install uses `pwrunner.pid`) | 0; 1 | `pwrunner restart` |
| `service` | `--install-dir`, `--console` (run the service body in the foreground) — the SCM entry point; auto-selected when started by the SCM | as `run` | (internal) |
| `token` | `set --stdin` / `set --file <p>` / `clear` / `show-prefix` (prints the 12-char label only); `set` and `clear` restart the service | 0; 1; 2 | `Get-Content t.txt \| pwrunner token set --stdin` |
| `selfcheck` | `--quick` (no network) | 0 ok; 2 problems listed | `pwrunner selfcheck` — checks: elevation, path length, `PathProblem`, token file DACL, `node.exe --version`, `cli.js --version`, slot ports bindable (when the service is stopped) or run-servers listening (when running), hub `/healthz`, Node EOL date, legacy task absent |
| `node -- <args>` | runs the private `node.exe` with the service's environment | child's exit code | `pwrunner node -- app\node_modules\playwright-core\cli.js install --list` |
| `fetch` | `--playwright-version`, `--node-version`, `--offline-dir`, mirrors — download + verify into `<D>\cache` only | 0; 1 | `pwrunner fetch` |
| `version` | — | 0 | prints `pwrunner <ver> (go1.27.x, windows/amd64) node v22.23.4 playwright-core 1.64.1 embedded=yes` |

Environment variables read by `run` (lowest precedence after flags): `HUB_URL GATEWAY_AGENT_TOKEN NODE_NAME SLOTS BASE_PORT PLAYWRIGHT_CLI PWRUNNER_NODE BROWSERS STATUS_FILE LOG_DIR PWRUNNER_WATCHDOG EPHEMERAL`, plus `PLAYWRIGHT_BROWSERS_PATH` and the proxy variables, which are passed through to the children (decision 14). `install`, `status`, `service` read none of them (service mode uses `backend.json` + `agent.token` only). Download code honours `HTTPS_PROXY`/`NO_PROXY`.

---

## 9. Security requirements checklist

The implementer ticks every item; the Windows e2e job asserts the ones marked (CI).

- [ ] Install dir created **already locked** (`CreateLockedDir` with `SDDLInstallDir`) before any log file is opened when elevated. Owner `S-1-5-32-544`, protected DACL, ACEs: `S-1-5-18` FullControl, `S-1-5-32-544` FullControl, `S-1-5-32-545` ReadAndExecute, all `OICI`. (CI: owner and no-writer checks on `<D>`, `pwrunner.exe`, `app\node_modules\playwright-core\cli.js`, `runtime\node\node.exe`.)
- [ ] `agent.token`: deleted before rewrite; created `CREATE_NEW` with `SDDLTokenFile` (exactly `S-1-5-18` and `S-1-5-32-544`, no inheritance); UTF-8, no BOM, no trailing newline. (CI: DACL protected and only those two SIDs.)
- [ ] The token is never in argv of any process (CI: no process command line contains it), never in `backend.json`, `status.json`, `runtime.json`, `manifest.json`, any log or the Event Log (CI: grep of `logs\` and `*.json`). `install --token` logs the WARN `token passed on the command line; prefer --token-file or --token-stdin`.
- [ ] Run-server children never receive `GATEWAY_AGENT_TOKEN` (allow-listed env, decision 14). Unit test asserts the child env.
- [ ] `Logger.Redactor` installed with the token value as soon as it is known.
- [ ] Untrusted-folder reset (`PathProblem` ≠ "") when elevated: take ownership, lock root, delete reparse points without following them at every depth, keep only `logs`, `backend.json`, `*.ps1`, `*.psm1` (moved to `legacy\` by the `install agent` step afterwards); `agent.token` content read and the file re-created locked; everything else (including a previous `pwrunner.exe`) removed; log line contains `from scratch`. (CI step 7-8: files planted after the legacy install, Users-owned tree → all gone, string logged by pwrunner.)
- [ ] `--force-reset` runs the same reset on a trusted folder.
- [ ] Not elevated and not `--no-service` → exit 2 with the INS:194-196 text. Not elevated against a locked folder → exit 2 with the INS:200-202 text. `--no-service` (unelevated) adds the current user's SID FullControl to the dir DACL (as today) and warns that a later elevated install will reset the folder.
- [ ] `uninstall` refuses (exit 2) without elevation when a service or scheduled task exists; `--remove-files` zero-fills `agent.token` before deletion.
- [ ] Service runs as LocalSystem (same privilege as today's SYSTEM task). `--service-account` is **not** accepted in this release (exit 2 "not supported"); when added, the account's SID needs: Modify on `<D>\logs`, `<D>\browsers`, write on `<D>\runtime.json` and `<D>\status.json` (or a separate state dir), Read on `agent.token`, ReadAndExecute on the rest; `backend.json.serviceAccount` records it; the e2e ACL assertions must list the extra SID as trusted.
- [ ] Artefacts verified by pinned hash before extraction, embedded bytes included; extraction rejects `..`, absolute paths and symlinks; only `node.exe`/`LICENSE` and `package/*` are written.
- [ ] Process discovery by image path under `<D>`; kill never targets processes outside `<D>` (unit test with a decoy process).
- [ ] Control channel read limit 1 MiB, data/upstream 256 MiB, compression disabled.
- [ ] Headers forwarded to run-server pass `ForwardHeaders` again inside the agent (drops `host`, `authorization`, `x-gateway-token`, `sec-websocket-*`, `proxy-*`, hop-by-hop).
- [ ] The exe never reads `HUB_URL`/`NODE_NAME`/… in service mode (only `backend.json`), so machine-wide variables cannot redirect a node.

---

## 10. Test plan

### 10.1 Go unit tests (per package; `go test ./...` on windows and ubuntu; `-race` only on the ubuntu CI job, because the race detector needs cgo and the dev machine has no C compiler)

| Package | Must cover |
|---|---|
| `cli` | both flag forms, bool flags, `--k=`, unknown flag → exit 2, `--help` → 0, `--` rest |
| `config` | every `config.js` error string; precedence flag > env > file; hostname sanitising; ephemeral parsing table; `Secret.String()` never leaks |
| `protocol` | port of `gateway/test/protocol.test.js` (canonical/sanitize names, forwardHeaders incl. array values, stripQuery not needed agent-side, sendableCloseCode table, closeReason at 123 bytes with multibyte runes, parseInbound) |
| `hubconn` | fake hub: `registered` round trip; heartbeat only after registered; urgent heartbeat; ping answers (fake hub pings, asserts pong); silence > `HubSilenceMs` → close + `hub stopped answering`; close codes 1001/1006/1013/4400/4401/4403/4409 → `lastError` text and delay (4409 ≥ `ReplacedDelayMs`); backoff doubling and jitter bounds with injected `Rand`; no reconnect after ctx cancel; 1001 `agent is shutting down` sent on stop |
| `session` | id regex; bad slot → `no slot <raw>`; path/query rules; header re-filter; exactly one canonical `User-Agent` reaches the upstream; non-101 428 with body → `open-failed` with status and the two version lines present; dial refused → message without status; data-channel 401/404 → `data channel: …`; the 14 s bound applies to the dials only (a session held open for 60 s with injected timings is not cancelled); a session survives the control connection closing; data-channel silence > `HubSilenceMs` closes both legs |
| `pipe` | §7.5 list incl. the 48 MiB burst with stalled reader (buffered < 16 MiB) |
| `slot` | with the fake cli and the system node: healthy within 30 s; kill child → `healthy=false`, restart after backoff, `restarts=1`; stop kills the tree and the port is bindable again within 8 s; `NODE_OPTIONS` string exact (explicit `--watchdog` path); log `.prev` rotation; Windows: hidden window flag set |
| `status` | golden key order; ISO ms `Z`; tmp+rename; Windows rename retry |
| `runtime` | synthetic zip/tgz: only allowed entries extracted; zip-slip rejected; hash mismatch deletes and errors; manifest match → skip + `already installed, skipping npm` logged; swap and rollback leave the expected dirs |
| `install` | step order and names; re-run keep rules (INS:207-235: `hubUrl, nodeName, slots, basePort, browsers, reportUrl, serviceName` kept unless passed; `playwrightVersion`/`nodeVersion` never kept); legacy files moved to `legacy\`; `restartedPrevious`; `failedStep` on each failing step (with injected failures); console summary text |
| `report` | `Problem` message table; `AgentAlive` false on stale `updated` and on wrong image path; status.json key order; POST body |
| `winsys` (windows) | §7.12 list |
| `logging` | rotation at `maxBytes`, `keep` generations; redactor |
| `assets` | byte-identical to `gateway/lib/parent-watchdog.js` |

### 10.2 Integration harness (`runner/test/integration`, `//go:build integration`)

`harness.go` starts `node <repo>/gateway/hub.js` with env `HUB_DB=memory HUB_PORT=0 HUB_HOST=127.0.0.1 GATEWAY_AGENT_TOKEN=<random> HEARTBEAT_TIMEOUT_S=6 OPEN_TIMEOUT_S=5`, parses `hub listening on 127.0.0.1:(\d+)` from its stdout, and kills it on cleanup. The fake run-server is `gateway/test/fixtures/fake-playwright/cli.js` (needs `gateway/node_modules/ws`: the harness fails fast with "run npm ci in gateway/" if missing). The agent under test is `pwrunner run` spawned as a child (built by `go build -o $TMP/pwrunner(.exe) ./cmd/pwrunner` in `TestMain`) with `--node <system node> --playwright-cli <fake cli> --slots 2 --base-port <free>`, `--status-file`, `--log-dir`, and the token in its environment.

Assertions (one test each, via `/api/nodes`, a Go WebSocket client on `/connect?node=…`, and `runtime.json`):

1. register: node listed with `slots 2`, `healthySlots 2`, `agentVersion` = build version, `platform` = `win32`/`linux`, `playwrightVersion 0.0.0-fake`, name lower-cased (`--name Runner-A` → `runner-a`).
2. heartbeat/liveness: with `HEARTBEAT_TIMEOUT_S=6` the node stays listed for 30 s (pongs + heartbeats).
3. open + header forwarding: client sends `info` with custom headers; echo shows `x-playwright-browser` and `user-agent` forwarded, exactly one `user-agent` equal to the client's (no `Go-http-client`), `host` = `127.0.0.1:<port>`, no `authorization`/`x-gateway-token`/`sec-websocket-*`.
4. pipe: text and binary preserved; 5 MiB binary frame echoed.
5. close mirroring: client close 4123 → fixture `lastclose` 4123 + reason; fixture `close:4321:bye` → client 4321; client socket destroyed → fixture sees 1011; close without code → 1000.
6. 428: `x-fake-reject: 428` → client HTTP 428 with `server version` and `client version` named; node's slot not marked failing (a second connect succeeds).
7. 400: `x-fake-reject: 400` → client HTTP 502 after `GATEWAY_CONNECT_ATTEMPTS`; hub log shows `open-failed` message `run-server on port <p> answered HTTP 400…`.
8. backpressure: 48 MiB burst from the fixture toward a client that stops reading; the agent's process RSS stays < 100 MiB and the hub buffering < 16 MiB (hub-side `bufferedAmount` is not reachable from Go; measure RSS of the agent and completion after the client resumes).
9. 4409: second agent with the same name → first agent's `runtime.json.lastError` contains `4409` and it does not reconnect for ≥ 30 s; second agent serves sessions.
10. token refusal: wrong token → `lastError` contains `4401`; agent keeps retrying (connect attempts observed in the hub log) and recovers when the hub is restarted with the agent's token.
11. slot restart: kill the run-server pid from `runtime.json` → `healthy:false` then `true`, `restarts 1`, node `healthySlots` back to 2 within 20 s.
12. orphan cleanup: kill the agent with `taskkill /F` (Windows) / `SIGKILL` → every slot pid gone within 10 s (watchdog), `runtime.json.pid` stale.
13. graceful stop: SIGTERM / Ctrl-Break → open client sessions see close 1001 `agent is shutting down`; exit code 0 within 10 s.
14. open timeout: agent started with a `--timings` that delays the data dial (hidden test hook `openDelayMs`) → hub 504, late data channel gets 404 (hub log).
15. data-channel silence: with `--timings {"hubSilenceMs":3000}` and a session open through a freezable TCP proxy in front of the hub (as `gateway/test/gateway.test.js:374-408` does), freeze the proxy → within 6 s the agent logs `no word from the hub on its data channel` and the fixture's `lastclose` shows the session closed; a session that stays open for 60 s with the proxy running is not closed.
16. control drop keeps sessions: open a session, then freeze only the control connection's proxy → the agent reconnects and re-registers while the client's session keeps echoing.

### 10.3 Black-box contract suite `gateway/test/agent-contract.test.js` (Node)

Uses `gateway/test/helpers.js` to start the hub in-process (`db:'memory'`, `openTimeoutMs:5000`, `heartbeatTimeoutMs:6000`) and spawns the agent from `process.env.AGENT_CMD` (default `node agent.js`) with `--playwright-cli test/fixtures/fake-playwright/cli.js`, a temp `--status-file`, `--slots 2`, a free `--base-port`, token in env. Same assertion list as 10.2 items 1, 3–7, 9, 11, 12, 13 (12 uses `process.kill(pid,'SIGKILL')` on Linux and `taskkill /F /PID` on Windows). Default timings on both implementations (decision 15); timeouts: 15 s register, 20 s restart, 45 s for the 4409 wait. Run: `node --test test/agent-contract.test.js`. `gateway/package.json` is **not** modified (the file is run directly); `npm test` keeps running the existing suite only.

### 10.4 Windows end-to-end (`.github/workflows/windows-backend-installer.yml`, rewritten in Phase 4)

`windows-latest`, `shell: powershell`, 25 min, `INSTALL_DIR=C:\PlaywrightBackend`, `HUB_HTTP=http://127.0.0.1:4500` (GitHub runner, no WSL conflict). Triggers on `runner/**`, `gateway/**`, the workflow.

| # | Step | Asserts |
|---|---|---|
| 1 | checkout; `actions/setup-node@v4` (20, cache on `gateway/package-lock.json`); `actions/setup-go@v5` (`go-version-file: runner/go.mod`) | — |
| 2 | `cd runner; go vet ./...; staticcheck ./...; go test ./...` | green |
| 3 | restore cache `runner\internal\runtime\files` keyed on `hashFiles('runner/internal/runtime/pins.go')`; `cd runner; go run ./tools/fetchbundle --out .\internal\runtime\files` (writes the pin basenames: `node-v22.23.4-win-x64.zip`, `playwright-core-1.64.1.tgz`; `go:embed` can only see that folder); `go build -tags embedruntime -ldflags "-s -w -X main.version=ci" -o C:\pwbuild\pwrunner.exe ./cmd/pwrunner` | exe exists; `pwrunner.exe version` prints `embedded=yes` |
| 4 | `cd gateway; npm ci; npm test; node --test test/agent-contract.test.js` with `AGENT_CMD="node agent.js"`, then with `AGENT_CMD="C:\pwbuild\pwrunner.exe run"` | both green |
| 5 | start hub `node hub.js` with `HUB_DB=memory HUB_PORT=4500 GATEWAY_AGENT_TOKEN=ci-token` exactly as the current workflow does: `Start-Process cmd -ArgumentList '/c', "node hub.js > $env:RUNNER_TEMP\hub.log 2> $env:RUNNER_TEMP\hub.err.log" -WindowStyle Hidden` (ShellExecute, no inherited handles; a child that inherits the step's output pipe is killed when the step ends, so `start /b` must not be used) | `/healthz` `.ok` within 30 s, and again at the start of every later step |
| 6 | **legacy migration**: `installer\windows\install-backend.ps1 -InstallDir … -HubUrl ws://127.0.0.1:4500 -AgentToken ci-token -NodeName ci-node-07 -Slots 2` (old path; needs setup-node) | exit 0 or 2; scheduled task `PlaywrightBackend` exists |
| 7 | plant files **after** the legacy install (so the reset below is pwrunner's, not the old installer's): `app\node_modules\planted\index.js`, `planted.cmd`; `icacls C:\PlaywrightBackend /setowner *S-1-5-32-545 /T` (the legacy `agent.token` stays, its content is carried over) | — |
| 8 | `Set-Content ci-token.txt ci-token -NoNewline`; `C:\pwbuild\pwrunner.exe install --install-dir C:\PlaywrightBackend --hub-url ws://127.0.0.1:4500 --token-file ci-token.txt --name ci-node-07 --slots 2 --offline-dir runner\internal\runtime\files` | exit 0; `status.json.result == 'success'`, `.registered`, `.healthySlots == 2`; `/api/nodes` shows `ci-node-07` with `slots 2`, `healthySlots 2`, `agentVersion 'ci'`; planted files gone; `install.log` contains `from scratch` and `Moved the PowerShell installer files to legacy\`; `C:\PlaywrightBackend\install-backend.ps1` absent and `legacy\install-backend.ps1` present; `Get-ScheduledTask PlaywrightBackend` absent; `Get-Service PlaywrightBackend` Running; no `powershell.exe` with `run-backend.ps1` on its command line |
| 9 | token/ACL: `agent.token` DACL protected, ACEs only `S-1-5-18`/`S-1-5-32-544`; owner ∈ {SYSTEM, Administrators} for `<D>`, `agent.token`, `pwrunner.exe`, `app\node_modules\playwright-core\cli.js`, `runtime\node\node.exe`; no non-trusted SID with `0x40000116` on `<D>`, `pwrunner.exe`, `cli.js`; no process command line contains `ci-token`; no file under `logs\` or `*.json` contains `ci-token` | all true |
| 10 | re-run installed copy: `C:\PlaywrightBackend\pwrunner.exe install` (no args) | exit 0; `install.log` contains `already installed, skipping npm`; `backend.json.nodeName == 'ci-node-07'`, `.slots == 2` |
| 11 | failed upgrade: `pwrunner.exe install --playwright-version 0.0.0-does-not-exist --playwright-integrity sha512-AAAA` | exit 1; `status.json.failedStep == 'install node packages'`; `.restartedPrevious == true`; `pwrunner.exe status --health-timeout 90` exit 0; `app\` unchanged (`manifest.json.playwrightVersion == '1.64.1'`) |
| 12 | two concurrent Node clients `chromium.connect('ws://127.0.0.1:4500/connect?node=ci-node-07')` with `setContent`/`textContent` | both OK; `pwrunner.exe status` exit 0 |
| 13 | `Stop-Process -Id (runtime.json.pid) -Force` | `pwrunner.exe status --health-timeout 90` exit 0; new pid ≠ old (SCM recovery) |
| 14 | `Stop-Service PlaywrightBackend`; wait 5 s | no `node.exe` whose image path starts with `<D>\runtime`; `/api/nodes` no longer lists the node; `pwrunner.exe status --json` exit 2 |
| 15 | `pwrunner.exe uninstall --remove-files --yes` | exit 0; `<D>` gone; service gone |
| 16 | upload `logs\`, `*.json`, hub output on failure | — |

### 10.5 Manual verification against the live hub (optional, only on the user's say-so)

Not part of any phase's acceptance. If and only if the user explicitly asks: on a Windows host **other than the dev machine's WSL grid**, run `pwrunner install --hub-url ws://<live hub>:4500 --token-file <managed agent token bound to that node name> --name <existing node name> --slots N`; watch the Grid UI for the node; run one check through `/connect?node=<name>`; roll back with `pwrunner uninstall` followed by the old `install-backend.ps1`. The implementer never does this unprompted.

---

## 11. Delivery phases

Each phase is one PR (§12). "Must not touch" applies to the whole phase.

### Phase 0 — scaffolding and CI

Tasks:
- [ ] `runner/go.mod` (`go 1.26`), `cmd/pwrunner/main.go` with `version` subcommand, `internal/cli`, `internal/logging`, `assets/` with the watchdog copy + equality test.
- [ ] `tools/fetchbundle`, `tools/updatepins`, `internal/runtime/pins.go` with the four Node pins (fill the two Linux hashes from `SHASUMS256.txt`) and the Playwright pin; `files/.gitignore`.
- [ ] `build/build.ps1` (`CGO_ENABLED=0`, matrix windows/amd64, windows/arm64, linux/amd64, linux/arm64, `-ldflags "-s -w -X main.version=$v"`, `-tags embedruntime` for the two Windows embedded artefacts).
- [ ] `.github/workflows/runner-go.yml`: `gofmt -l`, `go vet`, `staticcheck`, `govulncheck`, `go test ./...` on ubuntu + windows (`go test -race ./...` on ubuntu only); build matrix; artefacts uploaded.
- [ ] `gateway/test/agent-contract.test.js` with the full assertion list, passing against `node agent.js` (baseline).

Files touched: `runner/**` (new), `gateway/test/agent-contract.test.js` (new), `.github/workflows/runner-go.yml` (new), `.gitignore`.

Acceptance:
```powershell
cd C:\Users\sam\source\repos\playwright-remote-orchestrator\runner
gofmt -l . ; go vet ./... ; staticcheck ./... ; go test ./...              # no output from gofmt, all pass (-race needs cgo; CI runs it on ubuntu)
go build -ldflags "-s -w -X main.version=0.0.1-dev" -o ..\dist\pwrunner.exe .\cmd\pwrunner ; ..\dist\pwrunner.exe version
go run .\tools\fetchbundle --out .\internal\runtime\files ; go build -tags embedruntime -o ..\dist\pwrunner-embedded.exe .\cmd\pwrunner
(Get-Item ..\dist\pwrunner-embedded.exe).Length -gt 40MB
cd ..\gateway ; npm ci ; npm test ; node --test test\agent-contract.test.js   # AGENT_CMD unset -> node agent.js; all pass
git status --porcelain -- gateway\agent.js gateway\hub.js gateway\lib docker installer   # empty
```
Must not touch: `gateway/agent.js`, `gateway/hub.js`, `gateway/lib/**`, `gateway/package.json`, `docker/**`, `installer/**`, the existing workflow.

### Phase 1 — Go agent against a local hub (console mode) with the fake run-server

Tasks:
- [ ] `internal/config`, `internal/protocol`, `internal/hubconn`, `internal/session`, `internal/pipe`, `internal/slot`, `internal/status`, `internal/agent`, `internal/proc`, `pwrunner run` (console; Ctrl-C/SIGTERM stop).
- [ ] Unit tests of §10.1 for those packages; integration harness and tests of §10.2.
- [ ] Windows: hidden child windows; `taskkill /T /F` kill tree; Linux: process groups.

Files touched: `runner/**` only.

Acceptance:
```powershell
cd C:\Users\sam\source\repos\playwright-remote-orchestrator\runner
go test ./... ; go test -tags integration ./test/integration/...                  # all pass (uses system node + fake cli)
go build -o ..\dist\pwrunner.exe .\cmd\pwrunner
cd ..\gateway ; $env:AGENT_CMD = "$PWD\..\dist\pwrunner.exe run" ; node --test test\agent-contract.test.js ; Remove-Item Env:AGENT_CMD
npm test                                                                          # existing suite still green
# manual smoke (hub on 14500 as in §4), then:
..\dist\pwrunner.exe run --hub ws://127.0.0.1:14500 --name dev-01 --slots 2 --base-port 20100 --playwright-cli .\test\fixtures\fake-playwright\cli.js --status-file $env:TEMP\rt.json
Invoke-RestMethod http://127.0.0.1:14500/api/nodes | ConvertTo-Json -Depth 5     # dev-01, healthySlots 2
```
Must not touch: `gateway/**` except the contract test, `docker/**`, `installer/**`.

### Phase 2 — real run-servers with the private Node runtime

Tasks:
- [ ] `internal/runtime` complete (fetch, verify, extract, lay down, swap/rollback, browsers, embedded variants); `pwrunner fetch`, `pwrunner node`, `pwrunner selfcheck --quick`.
- [ ] `run` defaults `--node` to `<exe dir>\runtime\node\node.exe` when present.
- [ ] Verify end to end with real Playwright: lay down into a scratch dir, install chromium, run 2 slots, connect with a Playwright client of the same major.minor through the local hub.

Files touched: `runner/**` only.

Acceptance:
```powershell
cd C:\Users\sam\source\repos\playwright-remote-orchestrator\runner ; go test ./...
$D = "$env:TEMP\pwtest" ; go build -tags embedruntime -o ..\dist\pwrunner.exe .\cmd\pwrunner
..\dist\pwrunner.exe fetch --install-dir $D                                   # verifies pins, fills $D\cache (embedded: no network)
# lay-down is exercised by `install --no-service` in Phase 3; here use the Go test that calls runtime.LayDown on $D:
go test -run TestLayDownReal -tags realruntime ./internal/runtime/ -args -dir $D
& "$D\runtime\node\node.exe" --version                                        # v22.23.4
& "$D\runtime\node\node.exe" "$D\app\node_modules\playwright-core\cli.js" --version   # Version 1.64.1
$env:PLAYWRIGHT_BROWSERS_PATH = "$D\browsers" ; ..\dist\pwrunner.exe node --install-dir $D -- "$D\app\node_modules\playwright-core\cli.js" install chromium
..\dist\pwrunner.exe run --hub ws://127.0.0.1:14500 --name dev-02 --slots 2 --base-port 3100 --playwright-cli "$D\app\node_modules\playwright-core\cli.js" --node "$D\runtime\node\node.exe"
# in gateway/: node -e "const {chromium}=require('playwright-core');(async()=>{const b=await chromium.connect('ws://127.0.0.1:14500/connect?node=dev-02');const p=await b.newPage();await p.setContent('<h1>ok</h1>');console.log(await p.textContent('h1'));await b.close()})()"
#   requires `npm install --no-save playwright-core@1.64.1` in a scratch dir, not in gateway/ -> prints ok
```
Must not touch: `gateway/**`, `docker/**`, `installer/**`.

### Phase 3 — Windows service, install/uninstall/status, ACLs, migration

Tasks:
- [ ] `internal/winsys`, `internal/service`, `internal/install`, `internal/report`; subcommands `install`, `uninstall`, `status`, `start/stop/restart`, `service`, `token`, full `selfcheck`.
- [ ] Legacy detection and migration: scheduled task `backend.json.taskName` else `PlaywrightBackend`, `run-backend.ps1` supervisors, firewall rule `Playwright Backend (<task name>)`, and the move of the PowerShell files and `app\gateway\` into `<D>\legacy\` (§7.11 `install agent`).
- [ ] Event Log source, recovery actions, kill-on-close job in the service process.
- [ ] Unit tests of §10.1 for these packages (Windows-only tests skip elsewhere).

Files touched: `runner/**` only.

Acceptance (elevated PowerShell on the dev machine; uses a scratch install dir and the local 14500 hub; nothing touches WSL):
```powershell
$D = 'C:\PwRunnerTest' ; cd C:\Users\sam\source\repos\playwright-remote-orchestrator\runner ; go test ./...
go build -tags embedruntime -ldflags "-X main.version=0.0.1-dev" -o ..\dist\pwrunner.exe .\cmd\pwrunner
Set-Content "$env:TEMP\t.txt" dev-agent-token -NoNewline
..\dist\pwrunner.exe install --install-dir $D --hub-url ws://127.0.0.1:14500 --token-file "$env:TEMP\t.txt" --name dev-03 --slots 2 --service-name PwRunnerTest ; $LASTEXITCODE   # 0
Get-Service PwRunnerTest                                                   # Running
(Get-Acl "$D\agent.token").Access | Select IdentityReference,FileSystemRights   # exactly SYSTEM + BUILTIN\Administrators FullControl
(Get-Acl $D).Owner                                                         # BUILTIN\Administrators
Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*dev-agent-token*' } | Measure-Object | Select Count   # 0
Select-String -Path "$D\logs\*.log","$D\*.json" -Pattern dev-agent-token  # nothing
..\dist\pwrunner.exe status --install-dir $D ; $LASTEXITCODE               # 0
& "$D\pwrunner.exe" install ; $LASTEXITCODE                                # 0, log has "already installed, skipping npm"
Stop-Process -Id (Get-Content "$D\runtime.json" | ConvertFrom-Json).pid -Force ; ..\dist\pwrunner.exe status --install-dir $D --health-timeout 90 ; $LASTEXITCODE   # 0
Stop-Service PwRunnerTest ; Get-Process node -ErrorAction SilentlyContinue | ? Path -like "$D\*" | Measure-Object | Select Count   # 0
..\dist\pwrunner.exe uninstall --install-dir $D --remove-files --yes ; Test-Path $D ; Get-Service PwRunnerTest -ErrorAction SilentlyContinue   # False, nothing
```
Must not touch: `gateway/**`, `docker/**`, `installer/**`, `README.md`.

### Phase 4 — CI end-to-end on windows-latest and release artefacts

Tasks:
- [ ] Rewrite `.github/workflows/windows-backend-installer.yml` to §10.4 (keep the file name and job name so branch protection keeps matching; the legacy step 7 uses the old scripts, which still exist).
- [ ] Release job in `runner-go.yml` on tags `pwrunner/v*`: build matrix, `SHA256SUMS.txt`, `pwrunner-runtime-<ver>-windows-amd64.zip`, GitHub release upload.
- [ ] Cache for `C:\pwcache` and browsers.

Files touched: `.github/workflows/windows-backend-installer.yml`, `.github/workflows/runner-go.yml`, `runner/build/**`.

Acceptance: push the branch; `gh run list --branch <branch>`; both workflows green; `gh run download` of the Windows artefacts. The release job is tested **without a tag**: it also runs on `workflow_dispatch` with an input `dry_run=true` that builds the full matrix and uploads the seven artefact names of decision 12 plus `SHA256SUMS.txt` as workflow artifacts instead of creating a release; `gh workflow run runner-go.yml -f dry_run=true --ref <branch>` then `gh run download` shows all eight files. No tag and no GitHub release are created in any phase; the first real tag is cut by the user after Phase 5 merges.
Must not touch: `gateway/**`, `docker/**`, `installer/**`.

### Phase 5 — docs and installer deprecation

Tasks:
- [ ] `README.md` "Adding a Windows host" rewritten for `pwrunner.exe` (download, `install` example with `--token-stdin`, re-run semantics, `status`, `uninstall`, logs, service name, rollback to the PowerShell installer); "Agent" configuration section lists the Go flags; "Tests" section mentions the contract suite and `go test`.
- [ ] `installer/windows/DEPRECATED.md`; `backend-status.ps1` and `uninstall-backend.ps1` become 3-line shims that call `<InstallDir>\pwrunner.exe status/uninstall` when the exe exists and fall back to the old module otherwise; `install-backend.ps1` prints a deprecation WARN at start (one line, no behaviour change).
- [ ] `runner/README.md` build/test/release commands.

Files touched: `README.md`, `installer/windows/{backend-status.ps1,uninstall-backend.ps1,install-backend.ps1,DEPRECATED.md}`, `runner/README.md`.

Acceptance: `npm test` and the Windows e2e workflow still green (the legacy step still runs `install-backend.ps1`); `powershell -NoProfile -Command "[ScriptBlock]::Create((Get-Content -Raw installer/windows/backend-status.ps1))"` parses; README renders (`git diff --stat` shows only the listed files).
Must not touch: `gateway/**`, `docker/**`, `runner/internal/**` (except doc comments).

### Phase 6 — optional follow-ups (not started without the user's go-ahead)

**6a. The same binary as the Linux/Docker runner agent.** `docker/runner/Dockerfile` would `COPY` the `linux/amd64` build and set `CMD ["pwrunner","run"]` with the same env (`HUB_URL`, `SLOTS`, `BASE_PORT`, `BROWSERS`, `PLAYWRIGHT_CLI`, `STATUS_FILE`, `EPHEMERAL`, `PWRUNNER_NODE=node`), keeping the HEALTHCHECK (it reads `runtime.json`). Acceptance: contract suite green on ubuntu with `AGENT_CMD=./pwrunner run`; `docker compose build runner` and a compose smoke test on a **separate** compose project (never the live one). Only after that could `gateway/agent.js` be retired (the hub keeps `lib/protocol.js`). Estimated 2–3 days.

**6b. Hub in Go — what it would take.** Port of `hub.js` (1,104 lines), `registry.js` (634, MySQL + memory), `routing.js` (206), `tokens.js` (159) and their tests (~2,650 lines) with byte-identical refusal texts (`main.py` parses them) and the same SQL; dual-hub soak on the live MySQL for a week; cut-over of port 4500 with the Node hub kept as a compose profile for rollback. Estimated 25–35 engineering days; the only component every runner, check and the Grid UI depends on; recommended **not** to do unless a concrete need appears (hub on Windows, dropping the Node image). No code in this plan precludes it: `internal/protocol` and `internal/pipe` would be reused.

---

## 12. Branch and PR plan

- Branches: `go-agent-phase-0` … `go-agent-phase-5` off `master`, one PR each, merged in order (each phase builds on the previous merge; rebase on `master` before opening the PR).
- Commit granularity: one commit per package or per coherent step (e.g. `Add runner module skeleton and version command`, `Add protocol helpers ported from lib/protocol.js`, `Add control channel with reconnect backoff`, `Add session piping with close-code mirroring`, `Add run-server slot supervision`, `Add runtime lay-down with pinned Node and playwright-core`, `Add Windows service, install and uninstall`, `Rewrite Windows e2e workflow for pwrunner`, `Document pwrunner and deprecate the PowerShell installer`). Subject imperative, ≤ 72 chars; body explains what and why, references file paths and the spec section, no attribution lines of any kind.
- PR title: `Go runner agent for Windows, phase N: <scope>` (e.g. `Go runner agent for Windows, phase 1: agent core against the Node hub`).
- PR body outline:
  1. **What** — two or three sentences; link to `docs/plans/go-agent.md` §11 phase N.
  2. **Why** — the gaps this phase closes (the list in §1 "Goal", with the `installer/windows` file:line each gap comes from).
  3. **Changes** — bullet list by package/file.
  4. **Not changed** — explicit: hub, protocol, Docker runner, installer (per the phase's must-not-touch list) with `git diff --stat master -- gateway/agent.js gateway/hub.js gateway/lib docker installer` output pasted (expected empty through Phase 4).
  5. **Testing** — every acceptance command of the phase with its observed output (trimmed), the CI run links, and the statement that nothing was run against the WSL grid.
  6. **Follow-ups** — anything deferred, pointing at §14.
- No AI/assistant attribution anywhere in commits, comments, PR titles or bodies.

---

## 13. Open questions for the user (assume the default and continue)

| # | Question | Default to assume |
|---|---|---|
| 1 | Code-signing certificate for `pwrunner.exe`? | None available; publish unsigned with `SHA256SUMS.txt`; leave a `signtool` step commented in the release job. |
| 2 | Published Windows artefact: embedded (≈50 MB) or slim (≈10 MB)? | Embedded is the default download; slim is also published. |
| 3 | `agentVersion` shown in `/api/nodes` for Go nodes: the pwrunner version (`1.1.0`) or keep `1.0.0`? | pwrunner version. |
| 4 | Keep the 3-line `.ps1` shims for one release? | Yes (Phase 5). |
| 5 | Service account other than LocalSystem (NetworkService / virtual account) in this series? | No; LocalSystem only; ACL additions documented in §9 for a follow-up. |
| 6 | Swap the Docker runner to the Go binary (Phase 6a)? | No. |
| 7 | Node line: 22 (Maintenance LTS, EOL 2027-04-30) or 24 (Active LTS)? | 22.23.4 as pinned; bumping is a one-line change via `tools/updatepins`. |
| 8 | Should `install` require `--token-file`/`--token-stdin` and reject `--token`? | Accept `--token` with a WARN (parity with `-AgentToken`). |

---

## 14. Appendix — improvement opportunities after PR #5 and how this plan treats them

| P | Item (from the review of `a58ea1f`) | This plan |
|---|---|---|
| P1 | Client token = code execution on every runner (launch options forwarded verbatim; `executablePath`) | **Follow-up.** Hub-side whitelist is out of scope (hub unchanged). The Go `session` package keeps a single `filterLaunchOptions` hook point (currently pass-through) so the agent-side defence-in-depth filter can be added without touching the pipe. |
| P1 | Agent and browsers run as SYSTEM | **Partially absorbed.** Real service with LocalSystem now; `--service-account` with the documented ACL additions (§9) is the follow-up. |
| P1 | No node drain | **Follow-up.** `hubconn.SendHeartbeat` already takes the slot vector from a function; a `drain` subcommand = report all slots unhealthy, wait for `session.Manager.Count()==0`, stop. Estimated 1 day on top of Phase 3. |
| P1 | No hub drain / hub SPOF | Follow-up (hub work). Multi-hub dial in the agent is not in scope. |
| P1 | Black-hole node (browser missing but TCP probe passes) | **Partially absorbed.** `selfcheck` verifies each declared browser's executable exists under `browsers\` (via `cli.js install --dry-run` output) and warns; the runtime health probe stays TCP-only in this series (hub-side fix needed for the full solution). |
| P1 | CI runs only the Windows installer job | **Partially absorbed.** `runner-go.yml` adds ubuntu + windows Go jobs and the contract suite; Python/UI tests and MySQL-backed tests remain a separate `ci.yml` follow-up. |
| P2 | Bootstrap token rotation needs a hub restart | Follow-up (hub). |
| P2 | No rate limiting on the hub port | Follow-up (hub). |
| P2 | `/api/nodes`, `/healthz` unauthenticated | Follow-up (hub). |
| P2 | Per-client-token limits / fair queue | Follow-up (hub). |
| P2 | OAuth / proxy-aware rate limiting in the UI | Follow-up (UI). |
| P2 | `/healthz` is not readiness | Follow-up (hub). |
| P2 | No metrics / structured logs | Follow-up. The Go logger has a single `Logger` type; a `LOG_FORMAT=json` variant is a small later change. |
| P2 | No agent self-update | **Partially absorbed.** Upgrade = run the new exe's `install` (no checkout, hashes verified, transactional swap with rollback). Automatic self-update and the UI "outdated" badge are follow-ups. |
| P2 | UI snapshot performance | Follow-up (UI). |
| P2 | Heartbeat = 3 statements per node | Follow-up (hub). |
| P2 | Token expiry judged on two clocks | Follow-up (hub). |
| P2 | `.env.example` / README runbooks | Phase 5 rewrites the Windows section only; the rest is a follow-up. |
| P2 | Dev setup (`Makefile`, linters) | `runner/build/build.ps1` + Go linters absorbed for the Go side; repo-wide dev setup is a follow-up. |
| P3 | Agent log retention | **Absorbed** (decision 11: size-rotated `agent.log`/`service.log`/`install.log`; slot logs keep `.prev`). |
| P3 | Compose healthchecks for UI/checks | Follow-up (compose). |
| P3 | 256 MiB frames buffered by the hub; agent queues frames while CONNECTING | **Partially absorbed** on the agent side: Go streams frames (no whole-frame buffering) and never queues before the data channel is open (the upstream is dialled first and read only after the data channel is OPEN). Hub side is a follow-up. |
| P3 | Checks image runs as root | Follow-up (docker/python). |
| P3 | Stale comment in `ui/store.py:57` | Follow-up (one line, UI). |
| Bug 1 | Broken node becomes a routing black hole | See P1 black-hole row. |
| Bug 2 | Second hub marks the first hub's nodes offline | Follow-up (hub/registry). |
| Bug 3 | Token expiry clocks | Follow-up (hub). |
| Bug 4 | Hub restart leaves slots busy up to 90 s | Follow-up (hub). |
| Bug 5 | Token cache flood | Follow-up (hub). |
