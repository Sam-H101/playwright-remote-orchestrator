<#
.SYNOPSIS
    Stops and removes the Playwright Windows backend.

.DESCRIPTION
    Stops the supervisor and every server, unregisters the scheduled task and removes the firewall rule.
    Files (including downloaded browsers) are kept unless -RemoveFiles is passed, so a reinstall is fast.
#>
[CmdletBinding()]
param(
    # Defaults to the folder this script is in when run from an install, otherwise the installer's default.
    [string]$InstallDir = $(if (Test-Path (Join-Path $PSScriptRoot 'backend.json')) { $PSScriptRoot } else { 'C:\PlaywrightBackend' }),
    [switch]$RemoveFiles
)

$ErrorActionPreference = 'Stop'
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

Get-NetFirewallRule -DisplayName "Playwright Backend ($taskName)" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-BackendLog 'Removed firewall rule'

if ($RemoveFiles) {
    # Give Windows a moment to release handles held by the processes we just stopped.
    Start-Sleep -Seconds 2
    Set-Location $env:TEMP
    Remove-Item -Path $InstallDir -Recurse -Force
    Write-BackendLog "Deleted $InstallDir"
}

Write-BackendLog 'Uninstall complete'
exit 0
