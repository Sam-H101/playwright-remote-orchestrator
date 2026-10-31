<#
.SYNOPSIS
    Installs (or upgrades) a Playwright remote backend on a Windows host.

.DESCRIPTION
    Installs playwright-core and only the browsers you ask for into one shared folder, then runs
    one `playwright run-server` per port under a supervisor that restarts crashed servers.
    The supervisor runs as a SYSTEM scheduled task so it survives logoff and reboots.

    Re-running the script is safe: it skips the npm install when the requested version is already
    present, lets Playwright skip browsers that are already downloaded, and replaces the task and
    firewall rule in place.

    When it finishes it health-checks every port and reports the outcome:
      - a summary on the console, including the host entry for src/database.yaml
      - <InstallDir>\status.json with per-port health, timings and any error
      - an optional JSON POST to -ReportUrl
      - the exit code: 0 = all ports healthy, 2 = installed but some ports unhealthy, 1 = failed

.EXAMPLE
    .\install-backend.ps1 -PortStart 3000 -PortCount 4

.EXAMPLE
    .\install-backend.ps1 -PlaywrightVersion 1.64.1 -Browsers chromium,firefox -ReportUrl http://orchestrator:8080/api/nodes
#>
[CmdletBinding()]
param(
    [string]$InstallDir = 'C:\PlaywrightBackend',
    # Must match the major.minor of the Playwright client the orchestrator's checks use.
    [string]$PlaywrightVersion = '1.64.1',
    [string[]]$Browsers = @('chromium'),
    [int]$PortStart = 3000,
    [ValidateRange(1, 1000)][int]$PortCount = 4,
    [string]$BindHost = '0.0.0.0',
    # Name the orchestrator should use to reach this host. Defaults to the machine's DNS name.
    [string]$AdvertiseHost,
    [string]$ReportUrl,
    [int]$HealthTimeoutSec = 60,
    [string]$TaskName = 'PlaywrightBackend',
    # Run the supervisor in the background for this session instead of as a scheduled task.
    [switch]$NoService,
    [switch]$NoFirewall,
    # Install Node.js LTS with winget if Node 20+ is missing.
    [switch]$InstallNode
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Progress bars slow Invoke-WebRequest and friends a lot on 5.1.

Import-Module (Join-Path $PSScriptRoot 'PlaywrightBackend.psm1') -Force

$started = Get-Date
$steps = [ordered]@{}
$currentStep = $null
$config = $null
$portState = $null

Set-BackendLogFile (Join-Path $InstallDir 'logs\install.log')

function Invoke-Step {
    param([string]$Name, [scriptblock]$Body)
    $script:currentStep = $Name
    Write-BackendLog "==> $Name"
    $t = Get-Date
    & $Body
    $script:steps[$Name] = [math]::Round(((Get-Date) - $t).TotalSeconds, 1)
}

function Invoke-Native {
    param([string]$File, [string[]]$Arguments, [string]$What)
    $log = Join-Path $InstallDir 'logs\install.log'
    # Merge stderr into the log: npm and Playwright write progress there, which is not an error.
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $File @Arguments 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $log -Encoding ASCII
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($code -ne 0) { throw "$What failed with exit code $code (see $log)" }
}

function Update-PathFromRegistry {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Get-NodeExe {
    $cmd = Get-Command node.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $ver = (& $cmd.Source --version) -replace '^v', ''
    if ([int]($ver.Split('.')[0]) -lt 20) {
        Write-BackendLog "Found Node $ver at $($cmd.Source), but Playwright needs Node 20 or newer." 'WARN'
        return $null
    }
    Write-BackendLog "Using Node $ver at $($cmd.Source)"
    return $cmd.Source
}

try {
    Invoke-Step 'preflight' {
        if ((-not $NoService -or -not $NoFirewall) -and -not (Test-IsAdmin)) {
            throw 'Run this from an elevated PowerShell, or pass -NoService -NoFirewall.'
        }
        if (-not $AdvertiseHost) {
            try { $script:AdvertiseHost = [System.Net.Dns]::GetHostEntry('').HostName } catch { $script:AdvertiseHost = $env:COMPUTERNAME }
        }
        $script:nodeExe = Get-NodeExe
        if (-not $script:nodeExe) {
            if (-not $InstallNode) { throw 'Node.js 20+ was not found on PATH. Install it, or re-run with -InstallNode.' }
            if (-not (Get-Command winget.exe -ErrorAction SilentlyContinue)) { throw '-InstallNode needs winget, which is not available on this host.' }
            Invoke-Native winget.exe @('install', '--id', 'OpenJS.NodeJS.LTS', '-e', '--silent', '--accept-package-agreements', '--accept-source-agreements') 'winget install Node.js'
            Update-PathFromRegistry
            $script:nodeExe = Get-NodeExe
            if (-not $script:nodeExe) { throw 'Node.js was installed but node.exe is still not usable.' }
        }
        $script:npmCmd = Join-Path (Split-Path -Parent $script:nodeExe) 'npm.cmd'
        if (-not (Test-Path $script:npmCmd)) { throw "npm.cmd was not found next to $($script:nodeExe)." }
    }

    $appDir = Join-Path $InstallDir 'app'
    $browsersDir = Join-Path $InstallDir 'browsers'
    $cli = Join-Path $appDir 'node_modules\playwright-core\cli.js'

    # Servers lock files under node_modules and the browsers folder, so stop them before touching either.
    Invoke-Step 'stop existing backend' {
        Stop-Backend -InstallDir $InstallDir -TaskName $TaskName
    }

    Invoke-Step 'install playwright-core' {
        New-Item -ItemType Directory -Path $appDir, $browsersDir -Force | Out-Null
        $pkgJson = Join-Path $appDir 'node_modules\playwright-core\package.json'
        $installed = $null
        if (Test-Path $pkgJson) { $installed = (Get-Content $pkgJson -Raw | ConvertFrom-Json).version }
        if ($installed -eq $PlaywrightVersion) {
            Write-BackendLog "playwright-core $installed already installed, skipping npm."
            return
        }
        Write-BackendLog "Installing playwright-core $PlaywrightVersion (was: $(if ($installed) { $installed } else { 'none' }))"
        # playwright-core has the run-server CLI without the test runner, so it is the smallest install.
        $pkg = [ordered]@{ name = 'playwright-backend'; private = $true; dependencies = [ordered]@{ 'playwright-core' = $PlaywrightVersion } }
        $pkg | ConvertTo-Json | Set-Content -Path (Join-Path $appDir 'package.json') -Encoding ASCII
        Push-Location $appDir
        try {
            Invoke-Native $npmCmd @('install', '--no-audit', '--no-fund', '--omit=dev', '--loglevel=error') 'npm install'
        } finally {
            Pop-Location
        }
    }

    Invoke-Step 'install browsers' {
        # A fixed, machine-wide browsers path so the SYSTEM task finds the same browsers this session downloads.
        $env:PLAYWRIGHT_BROWSERS_PATH = $browsersDir
        Write-BackendLog "Ensuring browsers: $($Browsers -join ', ') (already-downloaded ones are skipped)"
        Invoke-Native $nodeExe (@($cli, 'install') + $Browsers) 'playwright install'
    }

    Invoke-Step 'write config' {
        $sameDir = (Resolve-Path $PSScriptRoot).Path.TrimEnd('\') -eq (Resolve-Path $InstallDir).Path.TrimEnd('\')
        if (-not $sameDir) {
            foreach ($f in 'PlaywrightBackend.psm1', 'run-backend.ps1', 'backend-status.ps1', 'uninstall-backend.ps1', 'install-backend.ps1') {
                Copy-Item -Path (Join-Path $PSScriptRoot $f) -Destination $InstallDir -Force
            }
        }
        $script:config = [pscustomobject][ordered]@{
            playwrightVersion = $PlaywrightVersion
            browsers          = $Browsers
            portStart         = $PortStart
            portCount         = $PortCount
            bindHost          = $BindHost
            advertiseHost     = $AdvertiseHost
            nodeExe           = $nodeExe
            browsersPath      = $browsersDir
            taskName          = $TaskName
            reportUrl         = $ReportUrl
        }
        $config | ConvertTo-Json | Set-Content -Path (Join-Path $InstallDir 'backend.json') -Encoding ASCII
    }

    if (-not $NoFirewall) {
        Invoke-Step 'firewall' {
            $ruleName = "Playwright Backend ($TaskName)"
            Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
            $range = if ($PortCount -eq 1) { "$PortStart" } else { "$PortStart-$($PortStart + $PortCount - 1)" }
            New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $range -Action Allow | Out-Null
            Write-BackendLog "Opened inbound TCP $range"
        }
    }

    Invoke-Step 'start backend' {
        $runner = Join-Path $InstallDir 'run-backend.ps1'
        $psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`""
        if ($NoService) {
            Start-Process -FilePath 'powershell.exe' -ArgumentList $psArgs -WindowStyle Hidden | Out-Null
            Write-BackendLog 'Started supervisor in the background (no scheduled task; it stops at logoff).'
        } else {
            $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs -WorkingDirectory $InstallDir
            $trigger = New-ScheduledTaskTrigger -AtStartup
            $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
                -Description "Playwright run-server on ports $PortStart..$($PortStart + $PortCount - 1)" -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Write-BackendLog "Registered and started scheduled task '$TaskName'."
        }
    }

    Invoke-Step 'health check' {
        $ports = Get-BackendPorts $config
        $script:portState = Wait-BackendPorts -ProbeHost (Get-ProbeHost $BindHost) -Ports $ports -TimeoutSec $HealthTimeoutSec
    }

    $down = @($portState.Keys | Where-Object { -not $portState[$_] })
    $result = if ($down.Count -eq 0) { 'success' } else { 'degraded' }
    $exitCode = if ($down.Count -eq 0) { 0 } else { 2 }
    $report = New-BackendReport -Config $config -PortState $portState -Result $result -DurationSec ((Get-Date) - $started).TotalSeconds -Steps $steps

    if ($down.Count -gt 0) {
        Write-BackendLog "$($down.Count) of $($portState.Count) ports did not come up: $($down -join ', '). See $InstallDir\logs\server-<port>.err.log" 'WARN'
    }
} catch {
    $msg = $_.Exception.Message
    Write-BackendLog "Step '$currentStep' failed: $msg" 'ERROR'
    $result = 'failed'
    $exitCode = 1
    $report = New-BackendReport -Config $config -PortState $portState -Result $result -FailedStep $currentStep -ErrorMessage $msg `
        -DurationSec ((Get-Date) - $started).TotalSeconds -Steps $steps
}

New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
$report | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $InstallDir 'status.json') -Encoding ASCII
Send-BackendReport -Url $ReportUrl -Report $report

Write-Host ''
Write-Host ("Result: {0}  ({1}/{2} ports healthy, {3}s)" -f $report.result.ToUpper(), $report.healthyPorts, $report.totalPorts, $report.durationSec)
foreach ($k in $steps.Keys) { Write-Host ("  {0,-24} {1,6}s" -f $k, $steps[$k]) }
if ($result -ne 'failed') {
    Write-Host ''
    Write-Host 'Add this host to src/database.yaml on the orchestrator:'
    Write-Host (Format-OrchestratorHostEntry $config)
}
Write-Host ''
Write-Host "Status written to $(Join-Path $InstallDir 'status.json')"
exit $exitCode
