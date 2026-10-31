<#
.SYNOPSIS
    Installs (or upgrades) a Playwright runner node on a Windows host.

.DESCRIPTION
    Installs playwright-core, the gateway agent and only the browsers you ask for into one folder, then
    runs the agent under a supervisor. The agent dials OUT to the hub (-HubUrl), registers this node by
    name and runs one `playwright run-server` per slot on 127.0.0.1, so the host opens no inbound ports
    and the orchestrator needs no database.yaml entry. The supervisor runs as a SYSTEM scheduled task so
    it survives logoff and reboots (or as a background process for this session with -NoService).

    Re-running the script is safe: it keeps the installed settings (hub, token, node name, slots, ports,
    browsers, report URL, task name) unless you pass them again, skips the npm install when the requested
    versions are already present, lets Playwright skip browsers that are already downloaded, and replaces
    the task in place. If a re-run fails before the new backend starts, the previous one is restarted.
    Upgrading from the older one-port-per-server install removes its inbound firewall rule.

    Elevated, it makes the install folder writable only by SYSTEM and Administrators (the task runs its
    files as SYSTEM). A folder that other users could change, for example one made by the older installer
    or by a non-elevated install, gets a fresh copy of app\ and browsers\.

    When it finishes it waits for the node to register and reports the outcome:
      - a summary on the console
      - <InstallDir>\status.json with per-slot health, timings and any error
      - an optional JSON POST to -ReportUrl
      - the exit code: 0 = registered and all slots healthy, 2 = installed but not registered or some
        slots unhealthy, 1 = failed

.EXAMPLE
    .\install-backend.ps1 -HubUrl ws://orchestrator:4500 -AgentToken $token -Slots 4

.EXAMPLE
    .\install-backend.ps1 -HubUrl wss://grid.example.com -NodeName build-07 -Browsers chromium,firefox -NoService
#>
[CmdletBinding()]
param(
    # ws:// or wss:// address of the gateway hub. Optional on a re-run: the installed value is kept.
    [string]$HubUrl,
    # Must match the hub's GATEWAY_AGENT_TOKEN. Optional on a re-run: the installed token is kept.
    [string]$AgentToken,
    # Defaults to the machine's DNS host name with anything outside [A-Za-z0-9._-] replaced by '-'.
    [string]$NodeName,
    [Alias('PortCount')][ValidateRange(1, 64)][int]$Slots = 4,
    # Slot i listens on 127.0.0.1:(BasePort+i-1). Loopback only; nothing is exposed.
    [Alias('PortStart')][ValidateRange(1, 65535)][int]$BasePort = 3100,
    # Defaults to the folder this script is in when run from an install, otherwise C:\PlaywrightBackend.
    [string]$InstallDir,
    # Must match the major.minor of the Playwright client the orchestrator's checks use.
    [string]$PlaywrightVersion = '1.64.1',
    [string[]]$Browsers = @('chromium'),
    [string]$ReportUrl,
    [int]$HealthTimeoutSec = 60,
    [string]$TaskName = 'PlaywrightBackend',
    # Run the supervisor in the background for this session instead of as a scheduled task (no elevation needed).
    [switch]$NoService,
    # Install Node.js LTS with winget if Node 20+ is missing.
    [switch]$InstallNode,
    # Deprecated: run-servers listen on loopback and the agent dials out, so these do nothing.
    [string]$BindHost,
    [string]$AdvertiseHost,
    [switch]$NoFirewall
)

$ErrorActionPreference = 'Stop'
# Not a param default: Windows PowerShell 5.1 leaves $PSScriptRoot empty there when run with -File.
if (-not $InstallDir) { $InstallDir = if (Test-Path (Join-Path $PSScriptRoot 'backend.json')) { $PSScriptRoot } else { 'C:\PlaywrightBackend' } }
$ProgressPreference = 'SilentlyContinue'   # Progress bars slow Invoke-WebRequest and friends a lot on 5.1.

Import-Module (Join-Path $PSScriptRoot 'PlaywrightBackend.psm1') -Force

$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
# Inside the Invoke-Step script blocks $PSBoundParameters is the block's own (empty) one.
$bound = $PSBoundParameters
$started = Get-Date
$steps = [ordered]@{}
$currentStep = $null
$config = $null
$oldConfig = $null
$keptToken = $null
$health = $null
$elevated = Test-IsAdmin
# Running the copy of this script that an earlier install put into InstallDir.
$sameDir = (Resolve-Path $PSScriptRoot).Path.TrimEnd('\') -eq $InstallDir
# What this run stopped, so a failure before the new backend starts can bring the old one back.
$previous = @{ Task = $null; Supervisor = $false; Wiped = $false; Replaced = $false }

# Folders under C:\ inherit Modify for Authenticated Users, and the task runs these files as SYSTEM, so
# an elevated install creates its folder already locked.
if ($elevated -and -not (Test-Path -LiteralPath $InstallDir)) { New-BackendDirectory $InstallDir }

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

function Get-InstalledVersion {
    param([string]$Package)
    $pkgJson = Join-Path $appDir "node_modules\$Package\package.json"
    if (Test-Path $pkgJson) { return (Get-Content $pkgJson -Raw | ConvertFrom-Json).version }
    return $null
}

# For an install folder other users could change: lock it, take ownership of everything in it, and delete
# all but the logs, settings and scripts (which are rewritten), so nothing they planted runs as SYSTEM.
function Reset-UntrustedInstall {
    # Even half done, this leaves nothing worth restarting after a failure.
    $script:previous.Wiped = $true
    Protect-BackendPath $InstallDir -UsersCanRead
    $keep = @('logs', 'backend.json', 'PlaywrightBackend.psm1', 'run-backend.ps1', 'backend-status.ps1', 'uninstall-backend.ps1', 'install-backend.ps1')
    foreach ($item in Get-ChildItem -LiteralPath $InstallDir -Force) {
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName) } else { [IO.File]::Delete($item.FullName) }
        } elseif ($item.Name -eq 'agent.token' -and -not $item.PSIsContainer) {
            # Locked rather than reset, so it is never readable by Users; write config re-creates it.
            Protect-BackendPath $item.FullName
            $script:keptToken = Get-BackendToken -Path $item.FullName
        } else {
            [BackendAcl]::SetOwner($item.FullName, 'S-1-5-32-544')
            [BackendAcl]::InheritOnly($item.FullName)
            if ($item.PSIsContainer) { Reset-BackendTree $item.FullName }
            if ($keep -notcontains $item.Name) {
                # rd copes with paths over MAX_PATH, which Remove-Item on 5.1 does not.
                if ($item.PSIsContainer) { cmd /c rd /s /q "`"$($item.FullName)`"" } else { Remove-Item -LiteralPath $item.FullName -Force }
                if (Test-Path -LiteralPath $item.FullName) { throw "Could not delete $($item.FullName)" }
            }
        }
    }
    Set-BackendLogFile (Join-Path $InstallDir 'logs\install.log')
}

# After a failure that left the previous backend stopped, start it again so the node keeps serving.
function Restore-PreviousBackend {
    if ($previous.Replaced -or $previous.Wiped) { return $false }
    try {
        if ($previous.Task -and (Get-ScheduledTask -TaskName $previous.Task -ErrorAction SilentlyContinue)) {
            Start-ScheduledTask -TaskName $previous.Task
            Write-BackendLog "Restarted scheduled task '$($previous.Task)' so the previous install keeps serving." 'WARN'
            return $true
        }
        $runner = Join-Path $InstallDir 'run-backend.ps1'
        if ($previous.Supervisor -and (Test-Path $runner)) {
            Start-Process -FilePath 'powershell.exe' -ArgumentList "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`"" `
                -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
            Write-BackendLog 'Restarted the previous supervisor in the background so the previous install keeps serving.' 'WARN'
            return $true
        }
    } catch {
        Write-BackendLog "Could not restart the previous install: $($_.Exception.Message)" 'WARN'
    }
    return $false
}

try {
    Invoke-Step 'preflight' {
        if (-not $NoService -and -not (Test-IsAdmin)) {
            throw 'Registering the scheduled task needs an elevated PowerShell. Run elevated, or pass -NoService.'
        }
        foreach ($p in 'BindHost', 'AdvertiseHost', 'NoFirewall') {
            if ($bound.ContainsKey($p)) { Write-BackendLog "-$p is deprecated and ignored: the node dials out to the hub and opens no inbound ports." 'WARN' }
        }
        if (-not $elevated -and (Test-Path -LiteralPath $InstallDir) -and -not (Get-BackendPathProblem $InstallDir)) {
            throw "$InstallDir was installed from an elevated PowerShell and only Administrators can change it. Re-run elevated."
        }
        $old = Get-BackendConfig -InstallDir $InstallDir
        $script:oldConfig = $old
        if (-not $HubUrl -and $old -and $old.hubUrl) { $script:HubUrl = $old.hubUrl }
        if (-not $HubUrl) { throw '-HubUrl is required, e.g. -HubUrl ws://orchestrator:4500' }
        if ($old) {
            # Settings that are not passed again keep their installed value (portCount: the pre-agent installer).
            $saved = [ordered]@{
                NodeName  = $old.nodeName
                Slots     = $(if ($old.slots) { $old.slots } else { $old.portCount })
                BasePort  = $old.basePort
                Browsers  = $old.browsers
                ReportUrl = $old.reportUrl
                TaskName  = $old.taskName
            }
            $kept = @()
            foreach ($k in $saved.Keys) {
                $v = $saved[$k]
                if ($null -eq $v -or "$v" -eq '') { continue }
                $was = @($v) -join ','
                $now = @(Get-Variable -Name $k -Scope Script -ValueOnly) -join ','
                if ($bound.ContainsKey($k)) {
                    if ($was -ne $now) { Write-BackendLog "Changing $k from '$was' to '$now'" }
                    continue
                }
                if ($k -eq 'Slots' -and [int]$v -gt 64) {
                    Write-BackendLog "The old install ran $v servers; a node has at most 64 slots, so it gets 64." 'WARN'
                    $v = 64
                }
                Set-Variable -Name $k -Scope Script -Value $(if ($k -eq 'Browsers') { [string[]]@($v) } else { $v })
                $kept += "$k=$(@($v) -join ',')"
            }
            if ($kept.Count) { Write-BackendLog "Keeping the installed settings: $($kept -join ' ')" }
        }
        $script:HubUrl = $HubUrl.TrimEnd('/')
        if ($HubUrl -notmatch '^wss?://[^/\s]+') { throw "-HubUrl must be a ws:// or wss:// URL, got '$HubUrl'" }
        if (-not $NodeName) {
            try { $host_ = [System.Net.Dns]::GetHostEntry('').HostName } catch { $host_ = $env:COMPUTERNAME }
            $script:NodeName = ConvertTo-NodeName $host_
        } elseif ($NodeName -notmatch '^[A-Za-z0-9._-]{1,128}$') {
            throw "-NodeName may only use A-Z a-z 0-9 . _ - (1-128 chars), got '$NodeName'"
        }
        if ($BasePort + $Slots - 1 -gt 65535) { throw "-BasePort $BasePort leaves no room for $Slots slots" }
        # browsers\chromium_headless_shell-NNNN\chrome-headless-shell-win64\chrome-headless-shell.exe adds about 95
        # characters, and Windows can't start an exe whose path is over MAX_PATH (260): run-server would
        # look healthy but every session would fail with "spawn ... ENOENT".
        if ($InstallDir.Length -gt 150) { Write-BackendLog "-InstallDir is $($InstallDir.Length) characters long; browser paths under it may exceed 260 characters and fail to launch. Use a shorter path." 'WARN' }

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

        # The agent ships with this repo; a copy of the installer inside InstallDir reuses the installed agent.
        # (For C:\PlaywrightBackend the folder two levels up is '', which Join-Path rejects.)
        $script:gatewaySrc = $null
        $repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
        if (-not $sameDir -and $repoRoot -and (Test-Path (Join-Path $repoRoot 'gateway\agent.js'))) {
            $script:gatewaySrc = Join-Path $repoRoot 'gateway'
        }
        $script:wsVersion = '^8.18.0'
        if ($gatewaySrc) {
            # Pin ws to what the gateway's lockfile tested. (ConvertFrom-Json on 5.1 can't read package-lock's "" key.)
            $lock = Get-Content (Join-Path $gatewaySrc 'package-lock.json') -Raw -ErrorAction SilentlyContinue
            if ($lock -match '"node_modules/ws":\s*\{\s*"version":\s*"([^"]+)"') { $script:wsVersion = $Matches[1] }
        }
        Write-BackendLog "Node '$NodeName', $Slots slots on 127.0.0.1:$BasePort-$($BasePort + $Slots - 1), hub $HubUrl"
    }

    $appDir = Join-Path $InstallDir 'app'
    $agentDir = Join-Path $appDir 'gateway'
    $browsersDir = Join-Path $InstallDir 'browsers'
    $cli = Join-Path $appDir 'node_modules\playwright-core\cli.js'
    $tokenFile = Join-Path $InstallDir 'agent.token'

    # Servers lock files under node_modules and the browsers folder, so stop them before touching either.
    # This also stops run-servers left by the pre-agent supervisor, which would hold their old ports.
    Invoke-Step 'stop existing backend' {
        $oldTask = if ($oldConfig -and $oldConfig.taskName) { $oldConfig.taskName } else { $TaskName }
        if (Get-ScheduledTask -TaskName $oldTask -ErrorAction SilentlyContinue) { $script:previous.Task = $oldTask }
        $script:previous.Supervisor = [bool](Get-BackendProcesses -InstallDir $InstallDir | Where-Object { $_.Name -match '^(powershell|pwsh)\.exe$' })
        # A renamed task is stopped here and unregistered when the new one is registered, so both never run.
        if ($oldTask -ne $TaskName) { Stop-Backend -InstallDir $InstallDir -TaskName $oldTask }
        Stop-Backend -InstallDir $InstallDir -TaskName $TaskName
        if ($NoService -and (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
            # Left in place it would start a second agent with the same name at the next boot.
            if (Test-IsAdmin) {
                Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
                Write-BackendLog "Removed scheduled task '$TaskName' (-NoService)"
            } else {
                Write-BackendLog "Scheduled task '$TaskName' from an earlier install still exists; remove it elevated (uninstall-backend.ps1) or it starts a second agent at boot." 'WARN'
            }
        }
        # A stale file would make the health check below pass before the new agent has registered.
        Remove-Item (Join-Path $InstallDir 'runtime.json') -Force -ErrorAction SilentlyContinue
        if ($elevated) {
            # Before npm and the browser download, so nothing can be added under the folder while they run.
            $problem = Get-BackendPathProblem $InstallDir
            if ($problem) {
                Write-BackendLog "$InstallDir $problem, so other users may have changed files the backend runs. Locking it and installing app\ and browsers\ from scratch." 'WARN'
                Reset-UntrustedInstall
            } else {
                Protect-BackendPath $InstallDir -UsersCanRead
            }
            Write-BackendLog "Restricted $InstallDir to read-only for non-administrators"
        }
    }

    Invoke-Step 'install node packages' {
        New-Item -ItemType Directory -Path $appDir, $browsersDir -Force | Out-Null
        $pw = Get-InstalledVersion 'playwright-core'
        $ws = Get-InstalledVersion 'ws'
        $wsOk = $ws -and ($ws -eq $wsVersion -or $wsVersion.StartsWith('^'))
        if ($pw -eq $PlaywrightVersion -and $wsOk) {
            Write-BackendLog "playwright-core $pw and ws $ws already installed, skipping npm."
            return
        }
        Write-BackendLog "Installing playwright-core $PlaywrightVersion (was: $(if ($pw) { $pw } else { 'none' })) and ws $wsVersion (was: $(if ($ws) { $ws } else { 'none' }))"
        # playwright-core has the run-server CLI without the test runner; ws is the agent's only dependency.
        $pkg = [ordered]@{ name = 'playwright-backend'; private = $true; dependencies = [ordered]@{ 'playwright-core' = $PlaywrightVersion; ws = $wsVersion } }
        $pkg | ConvertTo-Json | Set-Content -Path (Join-Path $appDir 'package.json') -Encoding ASCII
        Push-Location $appDir
        try {
            Invoke-Native $npmCmd @('install', '--no-audit', '--no-fund', '--omit=dev', '--loglevel=error') 'npm install'
        } finally {
            Pop-Location
        }
    }

    Invoke-Step 'install agent' {
        if ($gatewaySrc) {
            # Only what agent.js requires; lib\registry.js and mysql2 are hub-only.
            if (Test-Path $agentDir) { Remove-Item $agentDir -Recurse -Force }
            New-Item -ItemType Directory -Path (Join-Path $agentDir 'lib') -Force | Out-Null
            Copy-Item (Join-Path $gatewaySrc 'agent.js') $agentDir
            foreach ($f in 'config.js', 'log.js', 'pipe.js', 'protocol.js', 'parent-watchdog.js') {
                Copy-Item (Join-Path $gatewaySrc "lib\$f") (Join-Path $agentDir 'lib')
            }
            # lib\protocol.js reads the agent version from ../package.json.
            $src = Get-Content (Join-Path $gatewaySrc 'package.json') -Raw | ConvertFrom-Json
            [ordered]@{ name = $src.name; version = $src.version; private = $true } | ConvertTo-Json |
                Set-Content -Path (Join-Path $agentDir 'package.json') -Encoding ASCII
            Write-BackendLog "Installed agent $($src.version) from $gatewaySrc"
        } elseif ($sameDir -and (Test-Path (Join-Path $agentDir 'agent.js'))) {
            Write-BackendLog "Running the installed copy of this script; keeping the agent already in $agentDir"
        } elseif (Test-Path (Join-Path $agentDir 'agent.js')) {
            Write-BackendLog "No gateway\ checkout next to this script; keeping the agent already in $agentDir" 'WARN'
        } else {
            throw 'gateway\agent.js was not found. Run install-backend.ps1 from a checkout of the repository (installer\windows\).'
        }
        # Loading the agent resolves every require, so a missing file or package fails here, not in the task.
        Invoke-Native $nodeExe @((Join-Path $agentDir 'agent.js'), '--help') 'loading the agent'
    }

    Invoke-Step 'install browsers' {
        # A fixed, machine-wide browsers path so the SYSTEM task finds the same browsers this session downloads.
        $env:PLAYWRIGHT_BROWSERS_PATH = $browsersDir
        Write-BackendLog "Ensuring browsers: $($Browsers -join ', ') (already-downloaded ones are skipped)"
        Invoke-Native $nodeExe (@($cli, 'install') + $Browsers) 'playwright install'
    }

    Invoke-Step 'write config' {
        if (-not $sameDir) {
            foreach ($f in 'PlaywrightBackend.psm1', 'run-backend.ps1', 'backend-status.ps1', 'uninstall-backend.ps1', 'install-backend.ps1') {
                Copy-Item -Path (Join-Path $PSScriptRoot $f) -Destination $InstallDir -Force
            }
        }
        if ($bound.ContainsKey('AgentToken')) {
            if ($AgentToken) {
                Set-BackendToken -Path $tokenFile -Token $AgentToken
                Write-BackendLog "Stored the agent token in $tokenFile (readable by SYSTEM and Administrators only)"
            } else {
                Remove-Item $tokenFile -Force -ErrorAction SilentlyContinue
            }
        } elseif ($keptToken) {
            # Taken over from a folder other users could change: written to a new file nobody else can open.
            Set-BackendToken -Path $tokenFile -Token $keptToken
            Write-BackendLog 'Re-created the installed agent token in a new file. Other users could have read the old one, so consider rotating it.' 'WARN'
        }
        if (-not (Get-BackendToken -Path $tokenFile)) { Write-BackendLog 'No -AgentToken: the hub will reject this node if it sets GATEWAY_AGENT_TOKEN.' 'WARN' }
        $script:config = [pscustomobject][ordered]@{
            hubUrl            = $HubUrl
            nodeName          = $NodeName
            slots             = $Slots
            basePort          = $BasePort
            playwrightVersion = $PlaywrightVersion
            browsers          = $Browsers
            nodeExe           = $nodeExe
            browsersPath      = $browsersDir
            taskName          = $TaskName
            reportUrl         = $ReportUrl
        }
        $config | ConvertTo-Json | Set-Content -Path (Join-Path $InstallDir 'backend.json') -Encoding ASCII
    }

    Invoke-Step 'remove legacy firewall rule' {
        Remove-LegacyFirewallRule -TaskName $TaskName
    }

    Invoke-Step 'start backend' {
        # From here on the previous backend is being replaced, so a failure no longer restarts it.
        $script:previous.Replaced = $true
        if ($previous.Task -and $previous.Task -ne $TaskName -and (Get-ScheduledTask -TaskName $previous.Task -ErrorAction SilentlyContinue)) {
            # Both tasks would start run-backend.ps1 at boot, and each supervisor stops the other's agent.
            if ($elevated) {
                Unregister-ScheduledTask -TaskName $previous.Task -Confirm:$false
                Write-BackendLog "Removed scheduled task '$($previous.Task)' (renamed to '$TaskName')"
            } else {
                Write-BackendLog "Scheduled task '$($previous.Task)' from an earlier install still exists; remove it elevated or it starts a second agent at boot." 'WARN'
            }
        }
        $runner = Join-Path $InstallDir 'run-backend.ps1'
        $psArgs = "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runner`""
        if ($NoService) {
            Start-Process -FilePath 'powershell.exe' -ArgumentList $psArgs -WorkingDirectory $InstallDir -WindowStyle Hidden | Out-Null
            Write-BackendLog 'Started supervisor in the background (no scheduled task; it stops at logoff).'
        } else {
            $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $psArgs -WorkingDirectory $InstallDir
            $trigger = New-ScheduledTaskTrigger -AtStartup
            $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
            $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
                -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings `
                -Description "Playwright gateway agent: node $NodeName, $Slots slots, hub $HubUrl" -Force | Out-Null
            Start-ScheduledTask -TaskName $TaskName
            Write-BackendLog "Registered and started scheduled task '$TaskName'."
        }
    }

    Invoke-Step 'health check' {
        $script:health = Wait-BackendAgent -InstallDir $InstallDir -Slots $Slots -TimeoutSec $HealthTimeoutSec
    }

    $result = if ($health.Problem) { 'degraded' } else { 'success' }
    $exitCode = if ($health.Problem) { 2 } else { 0 }
    $report = New-BackendReport -Config $config -Runtime $health.Runtime -Problem $health.Problem -Result $result `
        -DurationSec ((Get-Date) - $started).TotalSeconds -Steps $steps
    if ($health.Problem) {
        Write-BackendLog "The node is installed but not serving after ${HealthTimeoutSec}s: $($health.Problem). See $InstallDir\logs\agent.log" 'WARN'
    }
} catch {
    $msg = $_.Exception.Message
    Write-BackendLog "Step '$currentStep' failed: $msg" 'ERROR'
    $result = 'failed'
    $exitCode = 1
    $restored = Restore-PreviousBackend
    $report = New-BackendReport -Config $config -Runtime $(if ($health) { $health.Runtime }) -Result $result -FailedStep $currentStep `
        -ErrorMessage $msg -DurationSec ((Get-Date) - $started).TotalSeconds -Steps $steps
    $report['restartedPrevious'] = $restored
}

$statusWritten = $false
try {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    $report | ConvertTo-Json -Depth 6 | Set-Content -Path (Join-Path $InstallDir 'status.json') -Encoding ASCII -ErrorAction Stop
    $statusWritten = $true
} catch {
    Write-BackendLog "Could not write status.json: $($_.Exception.Message)" 'WARN'
}
Send-BackendReport -Url $ReportUrl -Report $report

Write-Host ''
Write-Host ("Result: {0}  ({1}/{2} slots healthy, {3}s)" -f $report.result.ToUpper(), $report.healthySlots, $report.totalSlots, $report.durationSec)
foreach ($k in $steps.Keys) { Write-Host ("  {0,-28} {1,6}s" -f $k, $steps[$k]) }
if ($report.slots.Count) { Write-BackendSlots $report }
if ($result -ne 'failed') {
    Write-Host ''
    Write-Host "Node '$NodeName' registers itself with the hub at $HubUrl (and again after any disconnect);"
    Write-Host 'no database.yaml edit or inbound firewall port is needed. Clients reach slot N at'
    Write-Host "  $HubUrl/connect/$([uri]::EscapeDataString($NodeName))/N   (N = 1..$Slots)"
}
Write-Host ''
if ($statusWritten) { Write-Host "Status written to $(Join-Path $InstallDir 'status.json')" }
exit $exitCode
