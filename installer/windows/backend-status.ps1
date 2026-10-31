<#
.SYNOPSIS
    Reports the health of the Playwright runner node on this host.

.DESCRIPTION
    Reads the agent's status file (runtime.json) and prints the node name, hub, connection state and
    per-slot health, or JSON with -Json. The node is healthy when the agent is running, connected and
    registered with the hub and every slot's run-server answers. Use -ReportUrl to POST the same JSON
    to the orchestrator (for example from a scheduled task).
    Exit code: 0 = healthy, 2 = not registered or some slots unhealthy, 1 = backend not installed.
#>
[CmdletBinding()]
param(
    # Defaults to the folder this script is in when run from an install, otherwise the installer's default.
    [string]$InstallDir,
    [switch]$Json,
    [string]$ReportUrl,
    # Keep re-reading for up to this long while the node is not healthy yet (0 = check once).
    [int]$HealthTimeoutSec = 0
)

$ErrorActionPreference = 'Stop'
# Not a param default: Windows PowerShell 5.1 leaves $PSScriptRoot empty there when run with -File.
if (-not $InstallDir) { $InstallDir = if (Test-Path (Join-Path $PSScriptRoot 'backend.json')) { $PSScriptRoot } else { 'C:\PlaywrightBackend' } }
Import-Module (Join-Path $PSScriptRoot 'PlaywrightBackend.psm1') -Force

$config = Get-BackendConfig -InstallDir $InstallDir
if (-not $config -or -not $config.hubUrl) {
    Write-Error "No agent install (backend.json with a hubUrl) in $InstallDir."
    exit 1
}

$started = Get-Date
$health = Wait-BackendAgent -InstallDir $InstallDir -Slots $config.slots -TimeoutSec $HealthTimeoutSec
$result = if ($health.Problem) { 'degraded' } else { 'success' }
$report = New-BackendReport -Config $config -Runtime $health.Runtime -Problem $health.Problem -Result $result `
    -DurationSec ((Get-Date) - $started).TotalSeconds

if ($ReportUrl -or $config.reportUrl) {
    Send-BackendReport -Url $(if ($ReportUrl) { $ReportUrl } else { $config.reportUrl }) -Report $report
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
} else {
    Write-Host ("{0}: {1} ({2}/{3} slots healthy, hub {4}, Playwright {5})" -f $report.node, $result.ToUpper(), $report.healthySlots, $report.totalSlots, $report.hubUrl, $config.playwrightVersion)
    Write-Host ("  connected: {0}  registered: {1}" -f $report.connected, $report.registered)
    if ($health.Problem) { Write-Host "  problem: $($health.Problem)" -ForegroundColor Yellow }
    if ($report.slots.Count) { Write-BackendSlots $report }
}

if ($health.Problem) { exit 2 } else { exit 0 }
