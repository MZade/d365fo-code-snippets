#!/usr/bin/env bash
# © 2026 Mehrdad Ghazvinizadeh. All rights reserved, except as granted under CC BY-NC 4.0 (see LICENSE.md).
# Token benchmark for AI-agent code lookups in D365 F&O:
#   d365fo CLI  vs  cross-reference database (SQL)  vs  grep on AOT XML.
#
# Each scenario asks the same question three ways and records what an agent would pay:
# characters returned (tokens ~ characters / 4), lines, and wall-clock time.
#
# Run from Git Bash (Windows) on a D365 F&O development VM:
#   export D365FO_CLI="/c/path/to/d365fo.exe"
#   export D365FO_PACKAGES="/c/AosService/PackagesLocalDirectory"   # or your PackagesLocalDirectory
#   export D365FO_XREF_SERVER='(localdb)\MSSQLLocalDB'
#   export D365FO_XREF_DB='<your cross-reference database name>'
#   ./bench.sh            # all scenarios
#   ./bench.sh S3         # one scenario
#
# Output: results/run-<timestamp>/  (one file per scenario and method) + summary.csv

CLI="${D365FO_CLI:-d365fo}"
PLD="${D365FO_PACKAGES:?Set D365FO_PACKAGES to your PackagesLocalDirectory}"
XSERVER="${D365FO_XREF_SERVER:-(localdb)\\MSSQLLocalDB}"
XDB="${D365FO_XREF_DB:?Set D365FO_XREF_DB to your cross-reference database name}"
# The CLI names models, not packages: ApplicationSuite's model is Foundation.
APPSUITE_MODEL="${D365FO_APPSUITE_MODEL:-Foundation}"

OUT="$(cd "$(dirname "$0")" && pwd)/results/run-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$OUT"
ONLY="${1:-}"
sql() { sqlcmd -S "$XSERVER" -d "$XDB" -h -1 -W -Q "SET NOCOUNT ON; $1"; }

run() { # id method command
  [ -n "$ONLY" ] && [[ "$1" != $ONLY* ]] && return
  local f="$OUT/$1_$2.txt" t0 t1 c
  t0=$(date +%s%N); (cd "$PLD" && eval "$3") > "$f" 2>/dev/null; t1=$(date +%s%N)
  c=$(wc -c < "$f")
  printf '%s,%s,%d,%d,%d,%d\n' "$1" "$2" "$c" "$(wc -l < "$f")" $(( (c + 3) / 4 )) $(( (t1 - t0) / 1000000 )) | tee -a "$OUT/summary.csv"
}

echo "scenario,method,chars,lines,tokens,ms" | tee "$OUT/summary.csv"

# S1 Field usage: ApplicationSuite classes that use SalesLine.SalesPrice
run S1 cli  "\"$CLI\" find refs 'SalesLine.SalesPrice' --kind class --model $APPSUITE_MODEL -l 2000"
run S1 xref "sql \"SELECT DISTINCT SUBSTRING(s.Path,10,CHARINDEX('/',s.Path+'/',10)-10) FROM [References] r JOIN Names t ON t.Id=r.TargetId JOIN Names s ON s.Id=r.SourceId JOIN Modules m ON m.Id=s.ModuleId WHERE t.Path='/Tables/SalesLine/Fields/SalesPrice' AND s.Path LIKE '/Classes/%' AND m.Module='ApplicationSuite'\""
run S1 grep "cd ApplicationSuite/Foundation/AxClass && grep -liE 'salesLine[A-Za-z0-9_]*\.SalesPrice\b' *.xml"

# S2 Method callers: objects that call SalesLine.calcLineAmount
run S2 cli  "\"$CLI\" find refs 'SalesLine.calcLineAmount' -l 2000"
run S2 xref "sql \"SELECT DISTINCT LEFT(s.Path, CHARINDEX('/',s.Path+'/',CHARINDEX('/',s.Path,2)+1)-1) FROM [References] r JOIN Names t ON t.Id=r.TargetId JOIN Names s ON s.Id=r.SourceId WHERE t.Path='/Tables/SalesLine/Methods/calcLineAmount' AND r.Kind=1\""
run S2 grep "grep -rliE --include='*.xml' '[sS]alesLine[A-Za-z0-9_]*\.calcLineAmount\(' */*/AxClass */*/AxTable */*/AxForm */*/AxDataEntityView */*/AxClassExtension 2>/dev/null"

# S3 Chain of Command: extensions of class SalesLineType
run S3 cli  "\"$CLI\" find coc SalesLineType"
run S3 xref "sql \"SELECT DISTINCT s.Path FROM [References] r JOIN Names t ON t.Id=r.TargetId JOIN Names s ON s.Id=r.SourceId WHERE t.Path='/Classes/SalesLineType' AND s.Path LIKE '/Classes/%' AND s.Path NOT LIKE '/Classes/%/%' AND EXISTS (SELECT 1 FROM [References] a JOIN Names an ON an.Id=a.TargetId WHERE a.SourceId=s.Id AND an.Path='/Classes/ExtensionOf')\""
run S3 grep "grep -rliE --include='*.xml' 'ExtensionOf\s*\(\s*classStr\s*\(\s*SalesLineType\s*\)' */*/AxClass"

# S4 Table extensions of SalesTable
run S4 cli  "\"$CLI\" find extensions SalesTable"
run S4 xref "sql \"SELECT Path FROM Names WHERE Path LIKE 'TableExtension/SalesTable.%' AND Path NOT LIKE 'TableExtension/SalesTable.%/%' AND Path NOT LIKE '%?%'\""
run S4 grep "ls -1 */*/AxTableExtension/SalesTable.*.xml"

# S5 Data event handlers subscribed to table SalesLine
run S5 cli  "\"$CLI\" find handlers SalesLine"
run S5 xref "sql \"SELECT DISTINCT s.Path FROM [References] r JOIN Names t ON t.Id=r.TargetId JOIN Names s ON s.Id=r.SourceId WHERE t.Path='/Tables/SalesLine' AND s.Path LIKE '/Classes/%/Methods/%' AND EXISTS (SELECT 1 FROM [References] a JOIN Names an ON an.Id=a.TargetId WHERE a.SourceId=s.Id AND an.Path='/Classes/DataEventHandlerAttribute')\""
run S5 grep "grep -rliE --include='*.xml' 'DataEventHandler\s*\(\s*tableStr\s*\(\s*SalesLine\s*\)' */*/AxClass"

# S6 Tables with a field typed by EDT SalesPrice
run S6 cli  "\"$CLI\" find fields SalesPrice"
run S6 xref "sql \"SELECT DISTINCT LEFT(s.Path, CHARINDEX('?',s.Path+'?')-1) FROM [References] r JOIN Names t ON t.Id=r.TargetId JOIN Names s ON s.Id=r.SourceId WHERE t.Path='EdtReal/SalesPrice' AND s.Path LIKE 'Table/%/TableField%'\""
run S6 grep "grep -rl --include='*.xml' '<ExtendedDataType>SalesPrice</ExtendedDataType>' */*/AxTable"

# S7 Relations of table SalesLine
run S7 cli  "\"$CLI\" find relations SalesLine"
run S7 xref "sql \"SELECT DISTINCT LEFT(x, PATINDEX('%[/?]%', x+'/')-1) FROM (SELECT SUBSTRING(Path, CHARINDEX('/',Path,17)+1, 400) x FROM Names WHERE Path LIKE 'Table/SalesLine/TableRelation%/%') q\""
run S7 grep "sed -n '/<Relations>/,/<\/Relations>/p' ApplicationSuite/Foundation/AxTable/SalesLine.xml | grep -E '^\s{3}<Name>|<RelatedTable>'"

# S8 Shape of table SalesLine (its fields)
run S8 cli  "\"$CLI\" get table SalesLine"
run S8 xref "sql \"SELECT SUBSTRING(Path,17,400) FROM Names WHERE Path LIKE 'Table/SalesLine/TableField%' AND Path NOT LIKE 'Table/SalesLine/TableField%/%/%' AND Path NOT LIKE 'Table/SalesLine/TableFieldGroup%' AND Path NOT LIKE '%?%'\""
run S8 grep "cat ApplicationSuite/Foundation/AxTable/SalesLine.xml"

echo "Results: $OUT"
