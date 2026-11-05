# Warns when Claude Desktop's AbletonMCP server is running older code than this checkout.
#
# Claude Desktop starts `uv --directory <this folder> run ableton-mcp` once, when the app
# starts, and keeps that process (and its tool list) until the app is fully quit from the
# tray. Closing the window is not enough. Run this after pulling or committing server code.
#
#   powershell -ExecutionPolicy Bypass -File scripts\check_fresh.ps1
#
# Exit code 0 = everything current, 1 = something needs attention (printed).

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$problems = @()

# 1. This checkout: branch, clean tree, and up to date with origin/dev.
$branch = (git -C $repo branch --show-current).Trim()
if ($branch -ne "dev") { $problems += "Checkout is on '$branch', not 'dev'." }
if (git -C $repo status --porcelain) { $problems += "Checkout has uncommitted changes (Claude Desktop runs them as-is)." }
git -C $repo fetch --quiet origin 2>$null
$head = (git -C $repo rev-parse HEAD).Trim()
$remote = (git -C $repo rev-parse origin/dev 2>$null)
if ($remote -and $head -ne $remote.Trim()) {
    $behind = (git -C $repo rev-list --count "HEAD..origin/dev").Trim()
    $ahead = (git -C $repo rev-list --count "origin/dev..HEAD").Trim()
    $problems += "Checkout differs from origin/dev (behind $behind, ahead $ahead). Pull or push."
}

# 2. Newest server source change vs. the running server processes.
$newest = Get-ChildItem (Join-Path $repo "MCP_Server") -Recurse -Filter *.py |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$servers = Get-CimInstance Win32_Process |
    Where-Object { $_.Name -eq "uv.exe" -and $_.CommandLine -match "run ableton-mcp" }
if (-not $servers) {
    Write-Output "No ableton-mcp server running (Claude Desktop not started, or the server failed)."
} else {
    foreach ($p in $servers) {
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
        $who = if ($parent) { $parent.Name } else { "pid $($p.ParentProcessId)" }
        $line = "Server pid $($p.ProcessId) (started by $who) started $($p.CreationDate)"
        if ($p.CreationDate -lt $newest.LastWriteTime) {
            $problems += "$line, BEFORE $($newest.Name) changed at $($newest.LastWriteTime): stale code. Quit Claude Desktop from the tray and reopen it."
        } else {
            Write-Output "${line}: current."
        }
    }
}

# 3. Expected Remote Script version (what the server will demand from Live).
$expected = Select-String -Path (Join-Path $repo "MCP_Server\remote_script_install.py") `
    -Pattern 'EXPECTED_REMOTE_SCRIPT_VERSION = "([^"]+)"' | ForEach-Object { $_.Matches[0].Groups[1].Value }
Write-Output "Checkout: $branch @ $($head.Substring(0,7)); server expects Remote Script $expected."

if ($problems) { $problems | ForEach-Object { Write-Output "WARNING: $_" }; exit 1 }
Write-Output "OK: Claude Desktop's server runs this checkout's current code."
exit 0
