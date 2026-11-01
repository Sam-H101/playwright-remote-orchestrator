# pwrunner

`pwrunner.exe` is the Playwright runner host for Windows: the gateway agent (a drop-in for
`gateway/agent.js` against the unchanged hub), its installer, Windows service, status tool and
uninstaller in one Go executable. Operator documentation is under "Adding a Windows host" in the
[README](../README.md); the design and delivery phases are in [docs/plans/go-agent.md](../docs/plans/go-agent.md).

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

## Build

Go 1.26 or newer. From this folder:

```powershell
go build -o ..\dist\pwrunner.exe .\cmd\pwrunner                     # slim build: downloads the runtime at install
go run .\tools\fetchbundle --out .\internal\runtime\files            # pinned Node + playwright-core, hash-verified
go build -tags embedruntime -o ..\dist\pwrunner.exe .\cmd\pwrunner   # runtime embedded (about 42 MB)
.\build\build.ps1 -Version 1.1.0                                     # every release artefact + SHA256SUMS.txt into dist\
```

The pinned versions and hashes live in `internal/runtime/pins.go`. `go run ./tools/updatepins --node <v>
--playwright <v>` prints a new block for it; update the file names in `internal/runtime/embed_windows_*.go`
to match.

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

Releases are cut from tags `pwrunner/v<MAJOR.MINOR.PATCH>`:

```powershell
git tag pwrunner/v1.1.0 ; git push origin pwrunner/v1.1.0
```

`.github/workflows/runner-go.yml` then builds the embedded amd64/arm64 exes, the slim exe, the two Linux
builds, `pwrunner-runtime-<v>-windows-amd64.zip` (for `install --offline-dir`) and `SHA256SUMS.txt`, and
publishes them as a GitHub release. `gh workflow run runner-go.yml -f dry_run=true` builds the same set
without releasing (artefacts on the run). The builds are not code-signed yet; operators verify the
download against `SHA256SUMS.txt`.
