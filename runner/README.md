# pwrunner

`pwrunner.exe` is the Playwright runner host for Windows: the gateway agent (a drop-in for
`gateway/agent.js` against the unchanged hub), its installer, Windows service, status tool and
uninstaller in one Go executable. The design and the delivery phases are in
[docs/plans/go-agent.md](../docs/plans/go-agent.md).

## Build

Go 1.26 or newer (`go version`). From this folder:

```powershell
go build -o ..\dist\pwrunner.exe .\cmd\pwrunner                     # slim build: downloads the runtime at install
go run .\tools\fetchbundle --out .\internal\runtime\files            # pinned Node + playwright-core, hash-verified
go build -tags embedruntime -o ..\dist\pwrunner.exe .\cmd\pwrunner   # runtime embedded (about 42 MB)
.\build\build.ps1 -Version 1.1.0                                     # every release artefact + SHA256SUMS.txt into dist\
```

The pinned versions and hashes live in `internal/runtime/pins.go`; `go run ./tools/updatepins --node <v>
--playwright <v>` prints a new block for it.

## Test

```powershell
cd ..\gateway ; npm ci ; cd ..\runner          # the fake run-server fixture needs gateway/node_modules
gofmt -l . ; go vet ./... ; staticcheck ./... ; go test ./...
cd ..\gateway ; node --test test\agent-contract.test.js    # black-box agent contract, against node agent.js by default
```
