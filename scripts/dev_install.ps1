<#
.SYNOPSIS
    Copy this checkout's Remote Script into Live's User Library.

.DESCRIPTION
    Source of truth is AbletonMCP_Remote_Script\__init__.py. The script:
      1. syncs it into MCP_Server\bundled_ableton_remote_script\AbletonMCP_init.py
         (the copy the packaged installer ships; a test keeps them identical),
      2. backs up the installed <User Library>\Remote Scripts\AbletonMCP folder to
         <User Library>\Remote Scripts Backups\AbletonMCP-<timestamp>
         (outside Remote Scripts, so Live never loads a backup as a surface),
      3. installs the new __init__.py, removes the stale __pycache__, and
         verifies the bytes on disk match.

    Afterwards, restart Live or re-select AbletonMCP under
    Settings > Link, Tempo & MIDI > Control Surface.

.PARAMETER UserLibrary
    Ableton User Library folder. Defaults to <Documents>\Ableton\User Library,
    resolving Documents through Windows (so OneDrive redirection is followed).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\dev_install.ps1
#>
param(
    [string]$UserLibrary = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ableton\User Library')
)

$ErrorActionPreference = 'Stop'

$repo = Split-Path -Parent $PSScriptRoot
$src = Join-Path $repo 'AbletonMCP_Remote_Script\__init__.py'
$bundle = Join-Path $repo 'MCP_Server\bundled_ableton_remote_script\AbletonMCP_init.py'

if (-not (Test-Path $src)) { throw "Remote Script source not found: $src" }
if (-not (Test-Path $UserLibrary)) { throw "User Library not found: $UserLibrary (pass -UserLibrary <path>)" }

$version = (Select-String -Path $src -Pattern '^SCRIPT_VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$srcHash = (Get-FileHash $src).Hash

# 1. Keep the packaged copy identical to the source of truth.
if (-not (Test-Path $bundle) -or (Get-FileHash $bundle).Hash -ne $srcHash) {
    Copy-Item $src $bundle -Force
    Write-Host "synced bundle copy: $bundle"
}

$scriptsDir = Join-Path $UserLibrary 'Remote Scripts'
$destDir = Join-Path $scriptsDir 'AbletonMCP'
$dest = Join-Path $destDir '__init__.py'

# 2. Back up whatever is installed now.
if (Test-Path $destDir) {
    if ((Test-Path $dest) -and (Get-FileHash $dest).Hash -eq $srcHash) {
        Write-Host "installed script already matches the checkout (v$version); refreshing __pycache__ only"
    } else {
        $backupRoot = Join-Path $UserLibrary 'Remote Scripts Backups'
        $backup = Join-Path $backupRoot ("AbletonMCP-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        New-Item -ItemType Directory -Force $backupRoot | Out-Null
        Copy-Item $destDir $backup -Recurse
        Write-Host "backed up previous install to: $backup"
    }
} else {
    New-Item -ItemType Directory -Force $destDir | Out-Null
}

# 3. Install, drop compiled caches, verify.
Copy-Item $src $dest -Force
$pycache = Join-Path $destDir '__pycache__'
if (Test-Path $pycache) { Remove-Item $pycache -Recurse -Force }

if ((Get-FileHash $dest).Hash -ne $srcHash) { throw "verification failed: $dest does not match $src" }

# OneDrive Files On-Demand can leave a file as a cloud-only placeholder, which
# Live cannot read at startup. 0x400000 = FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS.
$attrs = [int](Get-Item $dest -Force).Attributes
if ($attrs -band 0x400000) {
    Write-Warning "$dest is a OneDrive cloud-only placeholder. Right-click the Remote Scripts folder > 'Always keep on this device'."
}

Write-Host "installed AbletonMCP Remote Script v$version -> $dest"
Write-Host "Next: restart Live, or re-select AbletonMCP in Settings > Link, Tempo & MIDI > Control Surface."
