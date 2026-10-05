<#
.SYNOPSIS
    Named, names-only queries against the D365 F&O cross-reference database (DYNAMICSXREFDB).

.DESCRIPTION
    Wraps the queries an AI agent (or a developer) needs most, so the cheapest lookup route
    is also the easiest one to call. Every command returns distinct names only, one per line.

    The cross-reference database is filled by Visual Studio when you build with
    "Update cross references" switched on. Code written after that build is missing.

.EXAMPLE
    .\xref.ps1 uses SalesLine.SalesPrice -Module ApplicationSuite -Kind Classes
    .\xref.ps1 uses SalesLine.calcLineAmount
    .\xref.ps1 coc SalesLineType
    .\xref.ps1 tableext SalesTable
    .\xref.ps1 handlers SalesLine
    .\xref.ps1 fields-by-edt SalesPrice
    .\xref.ps1 relations SalesLine
    .\xref.ps1 fields SalesLine
    .\xref.ps1 status
.NOTES
    © 2026 Mehrdad Ghazvinizadeh. All rights reserved, except as granted under CC BY-NC 4.0 (see LICENSE.md).
#>
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('uses', 'coc', 'tableext', 'handlers', 'fields-by-edt', 'relations', 'fields', 'status')]
    [string] $Command,

    [Parameter(Position = 1)]
    [string] $Name,

    # Restrict the referencing objects to one module (package), e.g. ApplicationSuite.
    [string] $Module,

    # Restrict the referencing objects to one kind of object.
    [ValidateSet('All', 'Classes', 'Tables', 'Forms', 'DataEntityViews', 'Queries', 'Views')]
    [string] $Kind = 'All',

    [string] $Server   = $(if ($env:D365FO_XREF_SERVER) { $env:D365FO_XREF_SERVER } else { '(localdb)\MSSQLLocalDB' }),
    [string] $Database = $env:D365FO_XREF_DB
)

$ErrorActionPreference = 'Stop'

if (-not $Database) {
    throw 'Set -Database or the D365FO_XREF_DB environment variable to the cross-reference database name.'
}
if ($Command -ne 'status' -and -not $Name) {
    throw "The '$Command' command needs a name, e.g. .\xref.ps1 $Command SalesLine"
}

function Quote([string] $s) { "'" + $s.Replace("'", "''") + "'" }

function Invoke-Xref([string] $sql) {
    $out = & sqlcmd -S $Server -d $Database -h -1 -W -b -Q "SET NOCOUNT ON; $sql"
    if ($LASTEXITCODE -ne 0) { throw "sqlcmd failed: $out" }
    $out | Where-Object { $_ -and $_.Trim() } | ForEach-Object { $_.Trim() }
}

# Joins and filters on the referencing (source) object, shared by the reference queries.
$sourceFilter = ''
if ($Kind -ne 'All') { $sourceFilter += " AND s.Path LIKE " + (Quote "/$Kind/%") }
if ($Module)         { $sourceFilter += " AND m.Module = " + (Quote $Module) }
$from = 'FROM [References] r JOIN Names t ON t.Id = r.TargetId JOIN Names s ON s.Id = r.SourceId JOIN Modules m ON m.Id = s.ModuleId'

# Top-level object of a code path: '/Classes/X/Methods/y' -> '/Classes/X'.
$sourceObject = "LEFT(s.Path, CHARINDEX('/', s.Path + '/', CHARINDEX('/', s.Path, 2) + 1) - 1)"

switch ($Command) {
    'uses' {
        # Owner.member: a table field or method, or a class method.
        $owner, $member = $Name -split '\.', 2
        if (-not $member) { throw "Use Owner.member, e.g. SalesLine.SalesPrice" }
        $targets = @(
            "/Tables/$owner/Fields/$member",
            "/Tables/$owner/Methods/$member",
            "/Classes/$owner/Methods/$member",
            "/Maps/$owner/Fields/$member",
            "/Views/$owner/Fields/$member"
        ) | ForEach-Object { Quote $_ }
        Invoke-Xref "SELECT DISTINCT $sourceObject $from WHERE t.Path IN ($($targets -join ','))$sourceFilter ORDER BY 1"
    }
    'coc' {
        # Class-level references from a class that carries [ExtensionOf(...)] to the target class.
        Invoke-Xref "SELECT DISTINCT s.Path $from WHERE t.Path = $(Quote "/Classes/$Name") AND s.Path LIKE '/Classes/%' AND s.Path NOT LIKE '/Classes/%/%'$sourceFilter AND EXISTS (SELECT 1 FROM [References] a JOIN Names an ON an.Id = a.TargetId WHERE a.SourceId = s.Id AND an.Path = '/Classes/ExtensionOf') ORDER BY 1"
    }
    'tableext' {
        $p = "TableExtension/$Name."
        Invoke-Xref "SELECT Path FROM Names WHERE Path LIKE $(Quote "$p%") AND Path NOT LIKE $(Quote "$p%/%") AND Path NOT LIKE '%?%' ORDER BY 1"
    }
    'handlers' {
        # Methods that carry a data event handler attribute and reference the table.
        Invoke-Xref "SELECT DISTINCT s.Path $from WHERE t.Path = $(Quote "/Tables/$Name") AND s.Path LIKE '/Classes/%/Methods/%'$sourceFilter AND EXISTS (SELECT 1 FROM [References] a JOIN Names an ON an.Id = a.TargetId WHERE a.SourceId = s.Id AND an.Path = '/Classes/DataEventHandlerAttribute') ORDER BY 1"
    }
    'fields-by-edt' {
        Invoke-Xref "SELECT DISTINCT LEFT(s.Path, CHARINDEX('?', s.Path + '?') - 1) $from WHERE t.Path LIKE $(Quote "Edt%/$Name") AND t.Path NOT LIKE '%?%' AND s.Path LIKE 'Table/%/TableField%'$sourceFilter ORDER BY 1"
    }
    'relations' {
        $n = "Table/$Name/".Length + 1
        Invoke-Xref "SELECT DISTINCT LEFT(x, PATINDEX('%[/?]%', x + '/') - 1) FROM (SELECT SUBSTRING(Path, CHARINDEX('/', Path, $n) + 1, 400) x FROM Names WHERE Path LIKE $(Quote "Table/$Name/TableRelation%/%")) q ORDER BY 1"
    }
    'fields' {
        $p = "Table/$Name/TableField"
        $n = "Table/$Name/".Length + 1
        Invoke-Xref "SELECT SUBSTRING(Path, $n, 400) FROM Names WHERE Path LIKE $(Quote "$p%") AND Path NOT LIKE $(Quote "$p%/%/%") AND Path NOT LIKE $(Quote "Table/$Name/TableFieldGroup%") AND Path NOT LIKE '%?%' ORDER BY 1"
    }
    'status' {
        # Approximate freshness: last write time of the database files, plus row counts.
        $files = Invoke-Xref "SELECT physical_name FROM sys.database_files WHERE type = 0"
        $counts = Invoke-Xref "SELECT 'Names=' + CAST(COUNT(*) AS varchar) FROM Names UNION ALL SELECT 'References=' + CAST(COUNT(*) AS varchar) FROM [References]"
        foreach ($f in $files) {
            if (Test-Path $f) { "LastWrite=$((Get-Item $f).LastWriteTime.ToString('yyyy-MM-dd HH:mm'))  $f" }
        }
        $counts
    }
}
