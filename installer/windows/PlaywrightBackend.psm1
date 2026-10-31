# Shared helpers for the Playwright Windows backend scripts.
# Kept ASCII-only so Windows PowerShell 5.1 reads it correctly without a BOM.

$script:LogFile = $null

function Set-BackendLogFile {
    param([string]$Path)
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $script:LogFile = $Path
}

function Write-BackendLog {
    param(
        [string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO'
    )
    $line = '[{0}] {1,-5} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        default { Write-Host $line }
    }
    if ($script:LogFile) { Add-Content -Path $script:LogFile -Value $line -Encoding ASCII }
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-BackendConfig {
    param([string]$InstallDir)
    $path = Join-Path $InstallDir 'backend.json'
    if (-not (Test-Path $path)) { return $null }
    return Get-Content -Path $path -Raw | ConvertFrom-Json
}

function Get-BackendPorts {
    param($Config)
    $ports = @()
    for ($p = [int]$Config.portStart; $p -lt ([int]$Config.portStart + [int]$Config.portCount); $p++) { $ports += $p }
    return $ports
}

function Get-ProbeHost {
    param([string]$BindHost)
    if (-not $BindHost -or $BindHost -eq '0.0.0.0' -or $BindHost -eq '::') { return '127.0.0.1' }
    return $BindHost
}

# A Playwright run-server answers a plain HTTP GET on its endpoint with 200 "Running".
# That proves the server process is up and serving, not just that the port is bound.
function Test-BackendPort {
    param([string]$ProbeHost, [int]$Port, [int]$TimeoutMs = 2000)
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://${ProbeHost}:${Port}/")
        $req.Proxy = $null
        $req.Timeout = $TimeoutMs
        $req.ReadWriteTimeout = $TimeoutMs
        $resp = $req.GetResponse()
        try {
            return ([int]$resp.StatusCode -eq 200)
        } finally {
            $resp.Close()
        }
    } catch {
        return $false
    }
}

# Polls every port each round until all are healthy or the timeout expires.
# Returns an ordered hashtable of port -> $true/$false.
function Wait-BackendPorts {
    param([string]$ProbeHost, [int[]]$Ports, [int]$TimeoutSec = 60)
    $state = [ordered]@{}
    foreach ($p in $Ports) { $state[$p] = $false }
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        foreach ($p in $Ports) {
            if (-not $state[$p]) { $state[$p] = Test-BackendPort -ProbeHost $ProbeHost -Port $p }
        }
        if (-not ($state.Values -contains $false)) { break }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $state
}

# Node processes started by run-backend.ps1, identified by the playwright-core CLI path in their command line.
function Get-BackendServerProcesses {
    param([string]$InstallDir)
    $cli = Join-Path $InstallDir 'app\node_modules\playwright-core\cli.js'
    Get-CimInstance Win32_Process -Filter "Name = 'node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($cli, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
}

function Stop-BackendServerProcesses {
    param([string]$InstallDir)
    foreach ($proc in @(Get-BackendServerProcesses -InstallDir $InstallDir)) {
        Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue
    }
}

function Get-SupervisorProcesses {
    param([string]$InstallDir)
    $script = Join-Path $InstallDir 'run-backend.ps1'
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe' OR Name = 'pwsh.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0 }
}

# Stops the scheduled task (if any), any foreground supervisor and every server it started.
function Stop-Backend {
    param([string]$InstallDir, [string]$TaskName)
    if ($TaskName -and (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    foreach ($proc in @(Get-SupervisorProcesses -InstallDir $InstallDir)) {
        if ($proc.ProcessId -ne $PID) { Stop-Process -Id $proc.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    Stop-BackendServerProcesses -InstallDir $InstallDir
}

function New-BackendReport {
    param($Config, $PortState, [string]$Result, [string]$FailedStep, [string]$ErrorMessage, [double]$DurationSec, $Steps)
    $ports = @()
    $up = 0
    if ($PortState) {
        foreach ($k in $PortState.Keys) {
            $ports += [ordered]@{ port = [int]$k; healthy = [bool]$PortState[$k] }
            if ($PortState[$k]) { $up++ }
        }
    }
    $report = [ordered]@{
        host              = $(if ($Config) { $Config.advertiseHost } else { $env:COMPUTERNAME })
        result            = $Result
        playwrightVersion = $(if ($Config) { $Config.playwrightVersion } else { $null })
        portStart         = $(if ($Config) { [int]$Config.portStart } else { $null })
        portEnd           = $(if ($Config) { [int]$Config.portStart + [int]$Config.portCount } else { $null })
        healthyPorts      = $up
        totalPorts        = $ports.Count
        ports             = $ports
        failedStep        = $FailedStep
        error             = $ErrorMessage
        durationSec       = [math]::Round($DurationSec, 1)
        steps             = $Steps
        timestamp         = (Get-Date).ToUniversalTime().ToString('o')
    }
    return $report
}

# The orchestrator reads hosts from src/database.yaml; port_end is exclusive there.
function Format-OrchestratorHostEntry {
    param($Config)
    $end = [int]$Config.portStart + [int]$Config.portCount
    return @(
        "  - server: `"$($Config.advertiseHost)`""
        "    port_start: $($Config.portStart)"
        "    port_end:   $end"
    ) -join [Environment]::NewLine
}

function Send-BackendReport {
    param([string]$Url, $Report)
    if (-not $Url) { return }
    try {
        $body = $Report | ConvertTo-Json -Depth 6
        Invoke-RestMethod -Uri $Url -Method Post -Body $body -ContentType 'application/json' -TimeoutSec 15 | Out-Null
        Write-BackendLog "Reported status to $Url"
    } catch {
        Write-BackendLog "Could not report status to ${Url}: $($_.Exception.Message)" 'WARN'
    }
}

Export-ModuleMember -Function *
