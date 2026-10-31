<#
.SYNOPSIS
    Supervisor for the Playwright Windows backend. Started by the scheduled task that install-backend.ps1 registers.

.DESCRIPTION
    Starts one `playwright run-server` per configured port, restarts any that exit (with a short back-off
    for servers that crash repeatedly), and keeps <InstallDir>\runtime.json up to date with pid, restart
    count and health for each port so backend-status.ps1 and the orchestrator can see what is running.
#>
[CmdletBinding()]
param(
    [int]$CheckIntervalSec = 10
)

$ErrorActionPreference = 'Stop'
$InstallDir = $PSScriptRoot
Import-Module (Join-Path $InstallDir 'PlaywrightBackend.psm1') -Force

$config = Get-BackendConfig -InstallDir $InstallDir
if (-not $config) { throw "backend.json not found in $InstallDir. Run install-backend.ps1 first." }

$logDir = Join-Path $InstallDir 'logs'
Set-BackendLogFile (Join-Path $logDir 'supervisor.log')
$cli = Join-Path $InstallDir 'app\node_modules\playwright-core\cli.js'
$probeHost = Get-ProbeHost $config.bindHost
$env:PLAYWRIGHT_BROWSERS_PATH = $config.browsersPath

# Servers left behind by a previous supervisor would hold the ports and make every new start fail.
Stop-BackendServerProcesses -InstallDir $InstallDir

$servers = [ordered]@{}
foreach ($port in (Get-BackendPorts $config)) {
    $servers[$port] = [pscustomobject]@{ Port = $port; Process = $null; Restarts = 0; FastFails = 0; LastStart = $null; NextStart = [datetime]::MinValue }
}

function Start-Server {
    param($Server)
    $out = Join-Path $logDir "server-$($Server.Port).out.log"
    $err = Join-Path $logDir "server-$($Server.Port).err.log"
    # Keep the previous run's logs so a crash is still diagnosable after the restart.
    foreach ($f in $out, $err) { if (Test-Path $f) { Move-Item $f "$f.prev" -Force } }
    $argList = @("`"$cli`"", 'run-server', '--port', $Server.Port, '--host', $config.bindHost)
    $Server.Process = Start-Process -FilePath $config.nodeExe -ArgumentList $argList -WorkingDirectory $InstallDir `
        -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
    # Touch the handle so ExitCode is populated once the process exits (Windows PowerShell 5.1 quirk).
    $null = $Server.Process.Handle
    $Server.LastStart = Get-Date
    Write-BackendLog "Started server on port $($Server.Port) (pid $($Server.Process.Id))"
}

Write-BackendLog "Supervisor starting: Playwright $($config.playwrightVersion), ports $($servers.Keys -join ', ')"

while ($true) {
    $now = Get-Date
    foreach ($s in $servers.Values) {
        if ($s.Process -and -not $s.Process.HasExited) { continue }
        if ($s.Process) {
            $s.Restarts++
            # A server that dies within a minute of starting is crash-looping; back off up to 5 minutes.
            if (($now - $s.LastStart).TotalSeconds -lt 60) { $s.FastFails++ } else { $s.FastFails = 0 }
            $delay = 0
            if ($s.FastFails -gt 0) { $delay = [int][math]::Min(300, 5 * [math]::Pow(2, $s.FastFails - 1)) }
            $s.NextStart = $now.AddSeconds($delay)
            Write-BackendLog "Server on port $($s.Port) exited with code $($s.Process.ExitCode); restart #$($s.Restarts) in ${delay}s" 'WARN'
            $s.Process = $null
        }
        if ($now -ge $s.NextStart) {
            try { Start-Server $s } catch { Write-BackendLog "Could not start port $($s.Port): $($_.Exception.Message)" 'ERROR'; $s.NextStart = $now.AddSeconds(30) }
        }
    }

    $runtime = [ordered]@{
        supervisorPid     = $PID
        playwrightVersion = $config.playwrightVersion
        updated           = (Get-Date).ToUniversalTime().ToString('o')
        ports             = @(foreach ($s in $servers.Values) {
            [ordered]@{
                port     = $s.Port
                pid      = $(if ($s.Process) { $s.Process.Id } else { $null })
                healthy  = $(if ($s.Process) { Test-BackendPort -ProbeHost $probeHost -Port $s.Port -TimeoutMs 1000 } else { $false })
                restarts = $s.Restarts
                since    = $(if ($s.LastStart) { $s.LastStart.ToUniversalTime().ToString('o') } else { $null })
            }
        })
    }
    try {
        $runtime | ConvertTo-Json -Depth 4 | Set-Content -Path (Join-Path $InstallDir 'runtime.json') -Encoding ASCII
    } catch {
        # A reader may hold the file open for a moment; the next loop rewrites it.
    }

    Start-Sleep -Seconds $CheckIntervalSec
}
