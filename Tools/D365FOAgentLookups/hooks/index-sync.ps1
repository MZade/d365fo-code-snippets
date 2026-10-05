<#
.SYNOPSIS
    Claude Code PostToolUse hook (matcher: Edit|Write|MultiEdit). Keeps the d365fo CLI index current.

.DESCRIPTION
    After the agent changes an AOT XML file, re-index the model that contains it, so the
    next CLI lookup sees the change. Without this, the CLI index goes stale just like the
    cross-reference database does.

    CLI location: D365FO_CLI (default: d365fo on PATH).
.NOTES
    © 2026 Mehrdad Ghazvinizadeh. All rights reserved, except as granted under CC BY-NC 4.0 (see ../LICENSE.md).
#>
$ErrorActionPreference = 'Stop'
$evt = [Console]::In.ReadToEnd() | ConvertFrom-Json
$path  = $evt.tool_input.file_path

if (-not $path -or $path -notmatch '\.xml$' -or $path -notmatch '[\\/]Ax[A-Za-z]+[\\/]') { exit 0 }

$cli = if ($env:D365FO_CLI) { $env:D365FO_CLI } else { 'd365fo' }
try {
    & $cli index sync $path *> $null
    if ($LASTEXITCODE -ne 0) { [Console]::Error.WriteLine("d365fo index sync failed for $path (exit $LASTEXITCODE). Run 'd365fo index refresh' before the next CLI lookup.") }
} catch {
    [Console]::Error.WriteLine("d365fo CLI not found ($cli). Set D365FO_CLI. The CLI index is now stale for $path.")
}
exit 0
