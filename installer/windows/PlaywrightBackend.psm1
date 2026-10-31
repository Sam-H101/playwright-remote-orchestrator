# Shared helpers for the Playwright Windows backend scripts.
# Kept ASCII-only so Windows PowerShell 5.1 reads it correctly without a BOM.

$script:LogFile = $null

function Set-BackendLogFile {
    param([string]$Path)
    $dir = Split-Path -Parent $Path
    # Best effort: a non-elevated run against an elevated install can't write there, and must still report why.
    if ($dir -and -not (Test-Path $dir)) { try { New-Item -ItemType Directory -Path $dir -Force | Out-Null } catch { } }
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
    if ($script:LogFile) {
        try {
            Add-Content -Path $script:LogFile -Value $line -Encoding ASCII -ErrorAction Stop
        } catch {
            Write-Host "Could not write to $($script:LogFile): $($_.Exception.Message)" -ForegroundColor Yellow
            $script:LogFile = $null
        }
    }
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

# Same rule as the agent's sanitizeName: the hub only accepts [A-Za-z0-9._-], 1-128 chars.
function ConvertTo-NodeName {
    param([string]$Name)
    $clean = ($Name -replace '[^A-Za-z0-9._-]', '-')
    if ($clean.Length -gt 128) { $clean = $clean.Substring(0, 128) }
    if (-not $clean) { $clean = 'node' }
    return $clean
}

$script:AdminsSid = 'S-1-5-32-544'
$script:TrustedSids = @('S-1-5-18', 'S-1-5-32-544')   # SYSTEM, Administrators

# Taking ownership needs SeTakeOwnershipPrivilege enabled: a file's owner can always change its permissions,
# whatever they say, so a file another user created stays theirs until an administrator takes it.
function Initialize-BackendAcl {
    if (-not ('BackendAcl' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
public static class BackendAcl {
    [StructLayout(LayoutKind.Sequential, Pack = 4)] struct Privilege { public int Count; public long Luid; public int Attributes; }
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool OpenProcessToken(IntPtr process, int access, out IntPtr token);
    [DllImport("advapi32.dll", SetLastError = true, CharSet = CharSet.Unicode)] static extern bool LookupPrivilegeValue(string system, string name, out long luid);
    [DllImport("advapi32.dll", SetLastError = true)] static extern bool AdjustTokenPrivileges(IntPtr token, bool disableAll, ref Privilege state, int length, IntPtr previous, IntPtr returned);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("advapi32.dll", CharSet = CharSet.Unicode)] static extern int SetNamedSecurityInfo(string name, int type, int info, byte[] owner, byte[] group, byte[] dacl, byte[] sacl);
    public static void EnableTakeOwnership() {
        IntPtr token;
        if (!OpenProcessToken(GetCurrentProcess(), 0x28, out token)) throw new Win32Exception();   // ADJUST_PRIVILEGES | QUERY
        try {
            foreach (string name in new[] { "SeTakeOwnershipPrivilege", "SeRestorePrivilege" }) {
                Privilege p = new Privilege { Count = 1, Attributes = 2 };                           // SE_PRIVILEGE_ENABLED
                if (!LookupPrivilegeValue(null, name, out p.Luid) || !AdjustTokenPrivileges(token, false, ref p, 0, IntPtr.Zero, IntPtr.Zero)) throw new Win32Exception();
                int err = Marshal.GetLastWin32Error();
                if (err != 0) throw new Win32Exception(err, name + ": " + new Win32Exception(err).Message);
            }
        } finally { CloseHandle(token); }
    }
    static void Set(string path, int info, byte[] owner, byte[] dacl) {
        int err = SetNamedSecurityInfo(path, 1, info, owner, null, dacl, null);                     // SE_FILE_OBJECT
        if (err != 0) throw new Win32Exception(err, path + ": " + new Win32Exception(err).Message);
    }
    public static void SetOwner(string path, string sid) {
        SecurityIdentifier s = new SecurityIdentifier(sid);
        byte[] b = new byte[s.BinaryLength];
        s.GetBinaryForm(b, 0);
        Set(path, 1, b, null);                                                                       // OWNER_SECURITY_INFORMATION
    }
    // Drops the item's own entries and protection, so it only has what it inherits from its folder.
    public static void InheritOnly(string path) {
        RawAcl acl = new RawAcl(2, 0);
        byte[] b = new byte[acl.BinaryLength];
        acl.GetBinaryForm(b, 0);
        Set(path, 0x20000004, null, b);                                                              // DACL | UNPROTECTED_DACL
    }
}
'@
    }
    [BackendAcl]::EnableTakeOwnership()
}

# Full control for SYSTEM and Administrators (plus the current user when not elevated, because a -NoService
# supervisor runs as that user), optionally read access for Users, nothing inherited, and Administrators as
# the owner when elevated. A folder's rules are inherited by its children.
function New-BackendSecurity {
    param([switch]$Container, [switch]$UsersCanRead)
    if ($Container) {
        $acl = New-Object System.Security.AccessControl.DirectorySecurity
        $inherit = 'ContainerInherit, ObjectInherit'
    } else {
        $acl = New-Object System.Security.AccessControl.FileSecurity
        $inherit = 'None'
    }
    $acl.SetAccessRuleProtection($true, $false)
    $rules = @(@('S-1-5-18', 'FullControl'), @($script:AdminsSid, 'FullControl'))
    if (Test-IsAdmin) {
        $acl.SetOwner((New-Object System.Security.Principal.SecurityIdentifier $script:AdminsSid))
    } else {
        $rules += , @([Security.Principal.WindowsIdentity]::GetCurrent().User.Value, 'FullControl')
    }
    if ($UsersCanRead) { $rules += , @('S-1-5-32-545', 'ReadAndExecute') }
    foreach ($r in $rules) {
        $who = New-Object System.Security.Principal.SecurityIdentifier $r[0]
        $acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule $who, $r[1], $inherit, 'None', 'Allow'))
    }
    return $acl
}

# Applies New-BackendSecurity to an existing file or folder. Elevated, it first takes ownership: the owner
# keeps the right to change the permissions back, so a folder another user created would otherwise stay theirs.
function Protect-BackendPath {
    param([string]$Path, [switch]$UsersCanRead)
    $item = Get-Item -LiteralPath $Path -Force
    if (Test-IsAdmin) {
        Initialize-BackendAcl
        [BackendAcl]::SetOwner($item.FullName, $script:AdminsSid)
    }
    $item.SetAccessControl((New-BackendSecurity -Container:$item.PSIsContainer -UsersCanRead:$UsersCanRead))
}

# Creates a folder that is protected from the start, so no other user can put anything in it first.
function New-BackendDirectory {
    param([string]$Path)
    [IO.Directory]::CreateDirectory($Path, (New-BackendSecurity -Container -UsersCanRead)) | Out-Null
}

# Why other users could have changed what is under a folder, or $null when only SYSTEM and Administrators
# can: it must be owned by one of them and not inherit or grant write access to anyone else.
function Get-BackendPathProblem {
    param([string]$Path)
    try { $acl = Get-Acl -LiteralPath $Path } catch { return "has permissions that lock out Administrators ($($_.Exception.Message))" }
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($script:TrustedSids -notcontains $owner) { return "is owned by $($acl.Owner)" }
    if (-not $acl.AreAccessRulesProtected) { return 'inherits its permissions from its parent folder' }
    # GENERIC_ALL/WRITE, WRITE_OWNER, WRITE_DAC, DELETE, and every write, append and delete-child right.
    $writeMask = 0x500D0156
    $writers = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object {
        $_.AccessControlType -eq 'Allow' -and $script:TrustedSids -notcontains $_.IdentityReference.Value -and ([int]$_.FileSystemRights -band $writeMask)
    })
    if ($writers.Count) {
        $names = $writers | ForEach-Object { $id = $_.IdentityReference; try { $id.Translate([Security.Principal.NTAccount]).Value } catch { $id.Value } }
        return "is writable by $(@($names | Select-Object -Unique) -join ', ')"
    }
    return $null
}

# Makes Administrators the owner of everything under a folder and drops each item's own permissions, so
# all of it only inherits the folder's. Links are deleted, not followed: they could point anywhere.
function Reset-BackendTree {
    param([string]$Path)
    Initialize-BackendAcl
    # The \\?\ form reaches past MAX_PATH, which node_modules and browser folders can exceed.
    $long = if ($Path -match '^[A-Za-z]:\\') { '\\?\' + $Path } else { $Path }
    foreach ($child in [IO.Directory]::GetFileSystemEntries($long)) {
        $attr = [IO.File]::GetAttributes($child)
        $isDir = [bool]($attr -band [IO.FileAttributes]::Directory)
        if ($attr -band [IO.FileAttributes]::ReparsePoint) {
            if ($isDir) { [IO.Directory]::Delete($child) } else { [IO.File]::Delete($child) }
            continue
        }
        [BackendAcl]::SetOwner($child, $script:AdminsSid)
        [BackendAcl]::InheritOnly($child)
        if ($isDir) { Reset-BackendTree $child }
    }
}

# Writes the token to a new file that is locked from the moment it exists. An existing file is deleted
# first: its owner, or a handle opened on it earlier, could still read whatever is written into it.
function Set-BackendToken {
    param([string]$Path, [string]$Token)
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Force }
    $stream = [IO.File]::Create($Path, 4096, [IO.FileOptions]::None, (New-BackendSecurity))
    try {
        $bytes = (New-Object Text.UTF8Encoding $false).GetBytes($Token)
        $stream.Write($bytes, 0, $bytes.Length)
    } finally {
        $stream.Dispose()
    }
}

function Get-BackendToken {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path $Path)) { return $null }
    $t = ([IO.File]::ReadAllText($Path)).Trim()
    if ($t) { return $t } else { return $null }
}

# Everything the backend runs, oldest generation included: run-backend.ps1 supervisors, node.exe
# processes under <InstallDir>\app (the agent and its run-servers, or a pre-agent install's run-servers)
# and the browsers they launched from <InstallDir>\browsers.
function Get-BackendProcesses {
    param([string]$InstallDir)
    $dir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\') + '\'
    $script = $dir + 'run-backend.ps1'
    $app = $dir + 'app\'
    Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
        $cmd = $_.CommandLine
        $_.ProcessId -ne $PID -and (
            ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase)) -or
            ($cmd -and $_.Name -eq 'node.exe' -and $cmd.IndexOf($app, [StringComparison]::OrdinalIgnoreCase) -ge 0) -or
            ($cmd -and $_.Name -match '^(powershell|pwsh)\.exe$' -and $cmd.IndexOf($script, [StringComparison]::OrdinalIgnoreCase) -ge 0))
    }
}

# Supervisors go first so they can't restart the agent while it is being stopped; then waits for the
# processes to exit, because they lock files under app\ and browsers\ that an upgrade replaces.
function Stop-BackendProcesses {
    param([string]$InstallDir)
    $procs = @(Get-BackendProcesses -InstallDir $InstallDir | Sort-Object { $_.Name -notmatch '^(powershell|pwsh)\.exe$' })
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($procs.Count) { Wait-Process -Id ($procs | ForEach-Object { $_.ProcessId }) -Timeout 15 -ErrorAction SilentlyContinue }
    return $procs.Count
}

function Stop-Backend {
    param([string]$InstallDir, [string]$TaskName)
    if ($TaskName -and (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    $n = Stop-BackendProcesses -InstallDir $InstallDir
    if ($n) { Write-BackendLog "Stopped $n backend process(es)" }
}

# Older installs opened the run-server ports inbound; the agent dials out, so the rule is no longer needed.
function Remove-LegacyFirewallRule {
    param([string]$TaskName)
    $name = "Playwright Backend ($TaskName)"
    $rule = Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue
    if (-not $rule) { return }
    if (-not (Test-IsAdmin)) {
        Write-BackendLog "Firewall rule '$name' from an older install is still open; remove it from an elevated PowerShell." 'WARN'
        return
    }
    $rule | Remove-NetFirewallRule
    Write-BackendLog "Removed legacy inbound firewall rule '$name'"
}

# The agent's status file (runtime.json), or $null if it is missing or mid-rename.
function Read-BackendRuntime {
    param([string]$InstallDir)
    $path = Join-Path $InstallDir 'runtime.json'
    for ($i = 0; $i -lt 3; $i++) {
        if (-not (Test-Path $path)) { return $null }
        try { return [IO.File]::ReadAllText($path) | ConvertFrom-Json } catch { Start-Sleep -Milliseconds 100 }
    }
    return $null
}

# The status file outlives the agent, so it only counts while its writer is alive and keeps it fresh.
function Test-AgentAlive {
    param($Runtime, [int]$MaxAgeSec = 30)
    if (-not $Runtime -or -not $Runtime.pid -or -not (Get-Process -Id $Runtime.pid -ErrorAction SilentlyContinue)) { return $false }
    $updated = $Runtime.updated
    if ($updated -isnot [datetime]) { $updated = [datetime]::Parse($updated, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind) }
    return ((Get-Date).ToUniversalTime() - $updated.ToUniversalTime()).TotalSeconds -le $MaxAgeSec
}

# Why the agent is not serving, or $null when it is connected, registered and every slot is healthy.
function Get-AgentProblem {
    param($Runtime, [int]$Slots)
    if (-not $Runtime) { return 'the agent has not written runtime.json yet' }
    if (-not (Test-AgentAlive $Runtime)) { return "the agent (pid $($Runtime.pid)) is not running or has stopped updating runtime.json" }
    if (-not $Runtime.connected) { return "not connected to the hub: $($Runtime.lastError)" }
    if (-not $Runtime.registered) { return 'connected but not registered with the hub yet' }
    if ($Slots -and @($Runtime.slots).Count -ne $Slots) { return "the agent runs $(@($Runtime.slots).Count) slots, expected $Slots" }
    $down = @($Runtime.slots | Where-Object { -not $_.healthy } | ForEach-Object { $_.index })
    if ($down.Count) { return "unhealthy slots: $($down -join ', ')" }
    return $null
}

function Wait-BackendAgent {
    param([string]$InstallDir, [int]$Slots, [int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ($true) {
        $runtime = Read-BackendRuntime -InstallDir $InstallDir
        $problem = Get-AgentProblem -Runtime $runtime -Slots $Slots
        if (-not $problem -or (Get-Date) -ge $deadline) { return [pscustomobject]@{ Runtime = $runtime; Problem = $problem } }
        Start-Sleep -Milliseconds 500
    }
}

function New-BackendReport {
    param($Config, $Runtime, [string]$Problem, [string]$Result, [string]$FailedStep, [string]$ErrorMessage, [double]$DurationSec, $Steps)
    $alive = Test-AgentAlive $Runtime
    $slots = @()
    if ($Runtime) {
        foreach ($s in $Runtime.slots) {
            $slots += [ordered]@{ index = [int]$s.index; port = [int]$s.port; healthy = [bool]($alive -and $s.healthy); restarts = [int]$s.restarts; pid = $s.pid }
        }
    }
    $node = $env:COMPUTERNAME
    if ($Runtime) { $node = $Runtime.name } elseif ($Config) { $node = $Config.nodeName }
    return [ordered]@{
        node              = $node
        hubUrl            = $(if ($Config) { $Config.hubUrl } else { $null })
        result            = $Result
        playwrightVersion = $(if ($Config) { $Config.playwrightVersion } else { $null })
        agentVersion      = $(if ($Runtime) { $Runtime.agentVersion } else { $null })
        connected         = [bool]($alive -and $Runtime.connected)
        registered        = [bool]($alive -and $Runtime.registered)
        problem           = $Problem
        lastError         = $(if ($Runtime) { $Runtime.lastError } else { $null })
        healthySlots      = @($slots | Where-Object { $_.healthy }).Count
        totalSlots        = $(if ($Config) { [int]$Config.slots } else { $slots.Count })
        slots             = $slots
        failedStep        = $FailedStep
        error             = $ErrorMessage
        durationSec       = [math]::Round($DurationSec, 1)
        steps             = $Steps
        timestamp         = (Get-Date).ToUniversalTime().ToString('o')
    }
}

function Write-BackendSlots {
    param($Report)
    $Report.slots | ForEach-Object { [pscustomobject]$_ } | Format-Table index, port, healthy, restarts, pid -AutoSize | Out-Host
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
