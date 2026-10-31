<#
.SYNOPSIS
    Reports the health of the Playwright Windows backend on this host.

.DESCRIPTION
    Probes every configured port and prints a table, or JSON with -Json. Use -ReportUrl to POST the
    same JSON to the orchestrator (for example from a scheduled task).
    Exit code: 0 = all ports healthy, 2 = some ports unhealthy, 1 = backend not installed.
#>
[CmdletBinding()]
param(
    # Defaults to the folder this script is in when run from an install, otherwise the installer's default.
    [string]$InstallDir = $(if (Test-Path (Join-Path $PSScriptRoot 'backend.json')) { $PSScriptRoot } else { 'C:\PlaywrightBackend' }),
    [switch]$Json,
    [string]$ReportUrl
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'PlaywrightBackend.psm1') -Force

$config = Get-BackendConfig -InstallDir $InstallDir
if (-not $config) {
    Write-Error "No backend.json in $InstallDir; the backend is not installed there."
    exit 1
}

$started = Get-Date
$probeHost = Get-ProbeHost $config.bindHost
$state = [ordered]@{}
foreach ($p in (Get-BackendPorts $config)) { $state[$p] = Test-BackendPort -ProbeHost $probeHost -Port $p }

$down = @($state.Keys | Where-Object { -not $state[$_] })
$result = if ($down.Count -eq 0) { 'success' } else { 'degraded' }
$report = New-BackendReport -Config $config -PortState $state -Result $result -DurationSec ((Get-Date) - $started).TotalSeconds

# Add the supervisor's restart counts when it is running.
$runtimePath = Join-Path $InstallDir 'runtime.json'
if (Test-Path $runtimePath) {
    try { $report['runtime'] = Get-Content $runtimePath -Raw | ConvertFrom-Json } catch { }
}

if ($ReportUrl -or $config.reportUrl) {
    Send-BackendReport -Url $(if ($ReportUrl) { $ReportUrl } else { $config.reportUrl }) -Report $report
}

if ($Json) {
    $report | ConvertTo-Json -Depth 6
} else {
    Write-Host ("{0}: {1} ({2}/{3} ports healthy, Playwright {4})" -f $report.host, $result.ToUpper(), $report.healthyPorts, $report.totalPorts, $config.playwrightVersion)
    $report.ports | ForEach-Object { [pscustomobject]$_ } | Format-Table -AutoSize | Out-Host
}

if ($down.Count -eq 0) { exit 0 } else { exit 2 }
