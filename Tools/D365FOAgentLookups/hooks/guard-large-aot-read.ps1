<#
.SYNOPSIS
    Claude Code PreToolUse hook (matcher: Read). Blocks reading a whole, large AOT XML file.

.DESCRIPTION
    A table like SalesLine.xml is about 1.2 MB, or roughly 289,000 tokens: more than a
    whole context window. This hook refuses such a read and tells the agent what to use
    instead. Reads with an offset or limit (a slice of the file) are allowed.

    Threshold: D365FO_MAX_AOT_READ_KB (default 100).
.NOTES
    © 2026 Mehrdad Ghazvinizadeh. All rights reserved, except as granted under CC BY-NC 4.0 (see ../LICENSE.md).
#>
$ErrorActionPreference = 'Stop'
$evt = [Console]::In.ReadToEnd() | ConvertFrom-Json
$ti = $evt.tool_input
$path  = $ti.file_path

if (-not $path -or $path -notmatch '\.xml$' -or $path -notmatch '[\\/]Ax[A-Za-z]+[\\/]') { exit 0 }
if ($ti.offset -or $ti.limit) { exit 0 }
if (-not (Test-Path -LiteralPath $path)) { exit 0 }

$maxKb = if ($env:D365FO_MAX_AOT_READ_KB) { [int]$env:D365FO_MAX_AOT_READ_KB } else { 100 }
$kb = [math]::Round((Get-Item -LiteralPath $path).Length / 1KB)
if ($kb -le $maxKb) { exit 0 }

$tokens = [math]::Round((Get-Item -LiteralPath $path).Length / 4 / 1000)
[Console]::Error.WriteLine(@"
Blocked: $([IO.Path]::GetFileName($path)) is $kb KB (about ${tokens}k tokens). Don't read whole AOT files.
Instead:
- names only: xref.ps1 fields|relations|uses ... (cross-reference database)
- structure: d365fo get table|class|form <Name>
- one method: d365fo read, or Read with offset/limit on the lines you need
"@)
exit 2
