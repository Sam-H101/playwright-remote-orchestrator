# The PowerShell installer is deprecated

`pwrunner.exe` replaces these scripts on Windows runner hosts (see "Adding a Windows host" in the
[README](../../README.md) and [runner/README.md](../../runner/README.md)). It is one executable with a
private, hash-pinned Node and `playwright-core`, runs the agent as the Windows service `PlaywrightBackend`,
and needs no Node, npm or repository checkout on the host.

| Script | Replacement |
| --- | --- |
| `install-backend.ps1 -HubUrl ... -AgentToken ...` | `pwrunner.exe install --hub-url ... --token-stdin` (`-PortCount`/`-PortStart` are accepted as `--port-count`/`--port-start`) |
| `backend-status.ps1 [-Json] [-ReportUrl] [-HealthTimeoutSec]` | `pwrunner.exe status [--json] [--report-url] [--health-timeout]` |
| `uninstall-backend.ps1 [-RemoveFiles]` | `pwrunner.exe uninstall [--remove-files] --yes` |
| `run-backend.ps1` (scheduled task supervisor) | the `PlaywrightBackend` service (`pwrunner.exe service`) |

For one release the scripts stay:

- `backend-status.ps1` and `uninstall-backend.ps1` hand over to `<InstallDir>\pwrunner.exe` when it exists
  and keep working on a PowerShell install otherwise.
- `install-backend.ps1` still installs the PowerShell way and logs a deprecation warning.
- `pwrunner.exe install` on a PowerShell install migrates it: it stops and deletes the scheduled task and
  its supervisor, keeps the settings and the token, and moves these scripts to `<InstallDir>\legacy\`.

To roll back from `pwrunner.exe` to the PowerShell installer: `pwrunner.exe uninstall`, then run
`install-backend.ps1` from a checkout as before (it needs Node.js 20+ or `-InstallNode`).
