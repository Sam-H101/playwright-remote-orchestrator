<#
.SYNOPSIS
    Stops and removes the Playwright runner node.

.DESCRIPTION
    Stops the supervisor, the agent and every run-server and browser it started, unregisters the scheduled
    task and removes the inbound firewall rule an older (pre-agent) install created. The hub marks the node
    offline when the agent disconnects; forget it in the Grid UI to drop it from the list.
    Files (including downloaded browsers) are kept unless -RemoveFiles is passed, so a reinstall is fast.
#>
[CmdletBinding()]
param(
    # Defaults to the folder this script is in when run from an install, otherwise the installer's default.
    [string]$InstallDir,
    [switch]$RemoveFiles
)

$ErrorActionPreference = 'Stop'
# Not a param default: Windows PowerShell 5.1 leaves $PSScriptRoot empty there when run with -File.
if (-not $InstallDir) { $InstallDir = if (Test-Path (Join-Path $PSScriptRoot 'backend.json')) { $PSScriptRoot } else { 'C:\PlaywrightBackend' } }
# Deprecated (see DEPRECATED.md): a pwrunner.exe install is removed by pwrunner.exe itself.
$pwrunner = Join-Path $InstallDir 'pwrunner.exe'
if (Test-Path $pwrunner) { & $pwrunner uninstall --install-dir $InstallDir --yes $(if ($RemoveFiles) { '--remove-files' }); exit $LASTEXITCODE }
# Load the module from memory: with -RemoveFiles this script deletes the folder it may be running from.
$moduleText = Get-Content (Join-Path $PSScriptRoot 'PlaywrightBackend.psm1') -Raw
New-Module -Name PlaywrightBackend -ScriptBlock ([scriptblock]::Create($moduleText)) | Import-Module -Force

$config = Get-BackendConfig -InstallDir $InstallDir
$taskName = if ($config -and $config.taskName) { $config.taskName } else { 'PlaywrightBackend' }

Write-BackendLog 'Stopping backend'
Stop-Backend -InstallDir $InstallDir -TaskName $taskName

if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-BackendLog "Removed scheduled task '$taskName'"
}

Remove-LegacyFirewallRule -TaskName $taskName

if ($RemoveFiles -and (Test-Path $InstallDir)) {
    # Give Windows a moment to release handles held by the processes we just stopped.
    Start-Sleep -Seconds 2
    Set-Location $env:TEMP
    # Remove-Item on 5.1 fails on paths over 260 characters (playwright-core nests deep); rd does not.
    try { Remove-Item -Path $InstallDir -Recurse -Force } catch { cmd /c rd /s /q $InstallDir }
    if (Test-Path $InstallDir) { throw "Could not delete $InstallDir; a process may still hold files in it." }
    Write-BackendLog "Deleted $InstallDir"
}

Write-BackendLog 'Uninstall complete'
exit 0
