<#
.SYNOPSIS
    Supervisor for the Playwright Windows backend. Started by the scheduled task that install-backend.ps1 registers.

.DESCRIPTION
    Runs the gateway agent (app\gateway\agent.js), which dials out to the hub, registers this node and
    supervises one `playwright run-server` per slot on 127.0.0.1. This script only keeps the agent itself
    alive: it restarts it with a back-off when it exits. The agent writes <InstallDir>\runtime.json, which
    backend-status.ps1 reads.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$InstallDir = $PSScriptRoot
Import-Module (Join-Path $InstallDir 'PlaywrightBackend.psm1') -Force

$config = Get-BackendConfig -InstallDir $InstallDir
if (-not $config -or -not $config.hubUrl) { throw "backend.json in $InstallDir has no hubUrl. Run install-backend.ps1 first." }

$logDir = Join-Path $InstallDir 'logs'
Set-BackendLogFile (Join-Path $logDir 'supervisor.log')

# Leftovers from an earlier supervisor would hold the slot ports, or register the same node name twice
# and make the hub kick one of them out every 30s.
$n = Stop-BackendProcesses -InstallDir $InstallDir
if ($n) { Write-BackendLog "Stopped $n process(es) left over from a previous run" 'WARN' }

# Put this process in a kill-on-close job: the agent, its run-servers and their browsers inherit it, so
# they all die with the supervisor (Stop-ScheduledTask and Task Manager only kill this process).
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class BackendJob {
    [StructLayout(LayoutKind.Sequential)] struct Basic { public long a, b; public uint LimitFlags; public UIntPtr c, d; public uint e; public UIntPtr f; public uint g, h; }
    [StructLayout(LayoutKind.Sequential)] struct Extended { public Basic Basic; public ulong i1, i2, i3, i4, i5, i6; public UIntPtr m1, m2, m3, m4; }
    [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateJobObject(IntPtr attrs, string name);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool SetInformationJobObject(IntPtr job, int cls, ref Extended info, int size);
    [DllImport("kernel32.dll", SetLastError = true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    static IntPtr job;
    public static int KillChildrenWithMe() {
        job = CreateJobObject(IntPtr.Zero, null);
        var info = new Extended();
        info.Basic.LimitFlags = 0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, ref info, Marshal.SizeOf(info)) ||
            !AssignProcessToJobObject(job, GetCurrentProcess())) return Marshal.GetLastWin32Error();
        return 0;
    }
}
'@
$jobError = [BackendJob]::KillChildrenWithMe()
if ($jobError) { Write-BackendLog "Could not create a kill-on-close job (Win32 error $jobError); the agent may outlive this supervisor" 'WARN' }

$app = Join-Path $InstallDir 'app'
$agentArgs = @(
    "`"$(Join-Path $app 'gateway\agent.js')`""
    '--hub', $config.hubUrl
    '--name', $config.nodeName
    '--slots', $config.slots
    '--base-port', $config.basePort
    '--browsers', ($config.browsers -join ',')
    '--playwright-cli', "`"$(Join-Path $app 'node_modules\playwright-core\cli.js')`""
    '--status-file', "`"$(Join-Path $InstallDir 'runtime.json')`""
    '--log-dir', "`"$logDir`""
)
$env:PLAYWRIGHT_BROWSERS_PATH = $config.browsersPath
# The token goes to the agent through its environment, never its command line, which any local user can read.
$token = Get-BackendToken -Path (Join-Path $InstallDir 'agent.token')
if ($token) { $env:GATEWAY_AGENT_TOKEN = $token } else { Remove-Item Env:GATEWAY_AGENT_TOKEN -ErrorAction SilentlyContinue }
# Nothing inherited from the machine environment should override backend.json.
foreach ($v in 'HUB_URL', 'NODE_NAME', 'SLOTS', 'BASE_PORT', 'PLAYWRIGHT_CLI', 'STATUS_FILE', 'LOG_DIR') { Remove-Item "Env:$v" -ErrorAction SilentlyContinue }

Write-BackendLog "Supervisor starting: node '$($config.nodeName)', $($config.slots) slots, hub $($config.hubUrl), token $(if ($token) { 'set' } else { 'not set' })"

$delay = 5
while ($true) {
    $out = Join-Path $logDir 'agent.log'
    $err = Join-Path $logDir 'agent.err.log'
    # Keep the previous run's logs so a crash is still diagnosable after the restart.
    foreach ($f in $out, $err) { if (Test-Path $f) { Move-Item $f "$f.prev" -Force } }
    $started = Get-Date
    try {
        $proc = Start-Process -FilePath $config.nodeExe -ArgumentList $agentArgs -WorkingDirectory $app `
            -RedirectStandardOutput $out -RedirectStandardError $err -WindowStyle Hidden -PassThru
        # Touch the handle so ExitCode is populated once the process exits (Windows PowerShell 5.1 quirk).
        $null = $proc.Handle
        Write-BackendLog "Started agent (pid $($proc.Id))"
        $proc.WaitForExit()
        $code = $proc.ExitCode
    } catch {
        $code = "error: $($_.Exception.Message)"
    }
    # An agent that dies within a minute is crash-looping (exit 2 = bad config); back off up to 5 minutes.
    if (((Get-Date) - $started).TotalSeconds -ge 60) { $delay = 5 }
    Write-BackendLog "Agent exited ($code); restarting in ${delay}s. See $err" 'WARN'
    Start-Sleep -Seconds $delay
    $delay = [math]::Min(300, $delay * 2)
}
