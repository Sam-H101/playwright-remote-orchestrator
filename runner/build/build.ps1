<#
.SYNOPSIS
    Builds the pwrunner release artefacts into runner\dist (Windows PowerShell 5.1 or PowerShell 7).

.DESCRIPTION
    Fetches and verifies the pinned Node and playwright-core archives (tools\fetchbundle), then builds:
      pwrunner-<v>-windows-amd64.exe        runtime embedded (-tags embedruntime)
      pwrunner-<v>-windows-arm64.exe        runtime embedded
      pwrunner-<v>-windows-amd64-slim.exe   downloads the runtime at install
      pwrunner-<v>-linux-amd64, pwrunner-<v>-linux-arm64
      pwrunner-runtime-<v>-windows-amd64.zip  the Node zip + playwright-core tgz, for install --offline-dir
      SHA256SUMS.txt
    Every build is CGO_ENABLED=0 with -ldflags "-s -w -X main.version=<v>".

.EXAMPLE
    .\build\build.ps1 -Version 1.1.0
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$OutDir = ''
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root 'dist' }
if (-not $Version) {
    $sha = (& git -C $root rev-parse --short HEAD).Trim()
    $Version = "1.1.0-dev+$sha"
}

function Invoke-Checked([string]$exe, [string[]]$arguments) {
    & $exe @arguments
    if ($LASTEXITCODE -ne 0) { throw "$exe $($arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

Push-Location $root
$saved = @{ GOOS = $env:GOOS; GOARCH = $env:GOARCH; CGO_ENABLED = $env:CGO_ENABLED }
try {
    New-Item -ItemType Directory -Force $OutDir | Out-Null
    $files = Join-Path $root 'internal\runtime\files'
    Invoke-Checked go @('run', './tools/fetchbundle', '--out', $files)

    $ldflags = "-s -w -X main.version=$Version"
    $builds = @(
        @{ os = 'windows'; arch = 'amd64'; tags = 'embedruntime'; name = "pwrunner-$Version-windows-amd64.exe" },
        @{ os = 'windows'; arch = 'arm64'; tags = 'embedruntime'; name = "pwrunner-$Version-windows-arm64.exe" },
        @{ os = 'windows'; arch = 'amd64'; tags = '';             name = "pwrunner-$Version-windows-amd64-slim.exe" },
        @{ os = 'linux';   arch = 'amd64'; tags = '';             name = "pwrunner-$Version-linux-amd64" },
        @{ os = 'linux';   arch = 'arm64'; tags = '';             name = "pwrunner-$Version-linux-arm64" }
    )
    $env:CGO_ENABLED = '0'
    foreach ($b in $builds) {
        $env:GOOS = $b.os
        $env:GOARCH = $b.arch
        $out = Join-Path $OutDir $b.name
        $buildArgs = @('build', '-trimpath', '-ldflags', $ldflags, '-o', $out)
        if ($b.tags) { $buildArgs += @('-tags', $b.tags) }
        Invoke-Checked go ($buildArgs + './cmd/pwrunner')
        Write-Host "built $($b.name) ($([math]::Round((Get-Item $out).Length / 1MB, 1)) MB)"
    }

    $runtimeZip = Join-Path $OutDir "pwrunner-runtime-$Version-windows-amd64.zip"
    if (Test-Path $runtimeZip) { Remove-Item $runtimeZip }
    $bundle = @(Get-ChildItem $files -File | Where-Object { $_.Name -like 'node-v*-win-x64.zip' -or $_.Name -like 'playwright-core-*.tgz' })
    Compress-Archive -Path ($bundle | ForEach-Object FullName) -DestinationPath $runtimeZip
    Write-Host "built $(Split-Path -Leaf $runtimeZip)"

    $sums = Get-ChildItem $OutDir -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' -and $_.Name -like "pwrunner*$Version*" } | Sort-Object Name | ForEach-Object {
        '{0}  {1}' -f (Get-FileHash -Algorithm SHA256 $_.FullName).Hash.ToLowerInvariant(), $_.Name
    }
    [IO.File]::WriteAllText((Join-Path $OutDir 'SHA256SUMS.txt'), (($sums -join "`n") + "`n"))
    Write-Host 'wrote SHA256SUMS.txt'
} finally {
    foreach ($k in $saved.Keys) {
        if ($null -eq $saved[$k]) { Remove-Item "Env:$k" -ErrorAction SilentlyContinue } else { Set-Item "Env:$k" $saved[$k] }
    }
    Pop-Location
}
