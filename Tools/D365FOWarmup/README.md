# D365FO Warmup

A small Python + Playwright tool that warms up a Dynamics 365 Finance & Operations environment. It collects every screen reachable from the navigation pane (**Modules**) and opens them in random order. After a deployment, restart or refresh, the first users no longer pay the "cold form" penalty.

- Standalone: plain Python. It runs from the console, Windows Task Scheduler or a pipeline agent.
- It uses your normal Entra ID sign-in (with MFA) once. After that it reuses the saved browser session headless.
- It opens **display** menu items only. Action and output menu items (batch jobs, reports, posting) are skipped by default.
- It logs each screen's load time to CSV and prints the slowest screens. This also makes it a quick performance probe.
- An **include list** names screens that always run first. On those screens a **deep warmup** can expand every FastTab, open every tab, sort every grid column ascending and descending, walk row by row through the main grid or list, and repeat all of that for every combination of filter values (for example every work order type × warehouse).

## How it works
1. It opens the environment in a persistent Edge (or Chromium) profile. The first run is `--headed` so you can sign in.
2. It opens the navigation pane, expands each module and **Expand all** in the module flyout, and reads the menu items. The flyout links have no `href`. The tool reads `MenuItemName`, `MenuItemType` and `Label` from the React props behind each `.modulesFlyout-link`. The result is cached in `menu_items.json`.
3. It opens `?cmp=<company>&mi=<menuItem>` for the include list first, then for a random selection of screens. It waits until the form is rendered and the F&O client stays idle (`$dyn.clientBusy` is false) for 1 second, then records the load time.
4. On include-list screens it runs the [deep warmup](#deep-warmup).

## Setup
```powershell
git clone https://github.com/MZade/d365fo-code-snippets.git
cd d365fo-code-snippets\Tools\D365FOWarmup
pip install -r requirements.txt
copy config.example.json config.json   # then set base_url and company
```
The tool uses the installed Microsoft Edge by default (`"browser_channel": "msedge"`). To use Playwright's bundled Chromium, set `"browser_channel": ""` and run `python -m playwright install chromium`.

If `config.json` is missing, the tool creates one with default values and stops. It also stops while `base_url` still contains `YOUR-ENV`.

## First run: sign in and build the menu cache
```powershell
python warmup.py --headed --refresh --count 5
```
Sign in in the browser window. The session is kept in `profile\`, which is git-ignored. **Treat this folder as a secret**, because it contains your session cookies.

## Normal runs
```powershell
python warmup.py --count 150                 # headless, 150 random screens
python warmup.py --duration 30 --delay 1-3   # keep going for 30 minutes
python warmup.py --modules "General ledger,Accounts payable"
python warmup.py --headed                    # watch it work
python warmup.py --refresh                   # rebuild menu_items.json after menu changes
python warmup.py --include-only              # only the include_mi screens, with deep warmup
python warmup.py --mi PaymTerm,InventLocations --include-only --headed
```

## Command-line options
Command-line options apply to one run. Where an option and `config.json` both set something, the option wins.

| Option | Meaning |
|---|---|
| `--headed` | Show the browser. Needed for the first sign-in; useful for debugging |
| `--refresh` | Re-read the navigation pane into `menu_items.json` |
| `--count N` | Number of random screens to open (default: all). Include-list screens always run on top |
| `--duration MIN` | Keep opening random screens for MIN minutes, looping over the list in a new random order |
| `--delay A-B` | Random pause between screens, in seconds. Overrides `delay` |
| `--company X` | Legal entity. Overrides `company` (not the `company` of an include entry) |
| `--modules "A,B"` | Only random screens from these modules (case-insensitive) |
| `--seed N` | Random seed for a reproducible order |
| `--mi A,B` | Display menu items to run first, added after the `include_mi` entries. They use the global `deep` options |
| `--include-only` | Open only the include-list screens. Same as `"include_only": true`. No menu harvest is needed |
| `--no-deep` | Open the include-list screens without the deep warmup |
| `--inspect` | Print nav-pane DOM hints for fixing selectors, then exit |

Exit codes: `0` ok, `1` failure, `2` sign-in required. On `2`, run once with `--headed`.

## Configuration (`config.json`)
`config.example.json` contains every setting with an example. A key you leave out keeps its default.

### General settings
| Key | Default | Meaning |
|---|---|---|
| `base_url` | *(must be set)* | Environment URL, e.g. `https://myenv.operations.dynamics.com`. A trailing `/` is removed |
| `company` | `"USMF"` | Default legal entity, added to every URL as `?cmp=` |
| `browser_channel` | `"msedge"` | `"msedge"`, `"chrome"`, or `""` for Playwright's bundled Chromium |
| `form_timeout_sec` | `60` | Longest wait for one screen to load and go idle |
| `login_wait_sec` | `300` | Longest wait for you to sign in on a `--headed` run. Headless runs wait 60 seconds |
| `delay` | `"2-5"` | Random pause between screens, in seconds (`"min-max"`) |

### Random warmup
These settings only affect the random screens from `menu_items.json`, not the include list.

| Key | Default | Meaning |
|---|---|---|
| `skip_menu_item_types` | `["action", "output"]` | Menu item types to skip. Keep these skipped: they run batch jobs, posting and reports |
| `exclude_mi` | `[]` | Menu item names to skip (exact name), e.g. setup forms whose `init` creates records |
| `exclude_modules` | `[]` | Module names to skip (exact name). Also skipped when the menu is read |

### Include list
| Key | Default | Meaning |
|---|---|---|
| `include_mi` | `[]` | Screens that always run first, in the listed order, whatever `--count`, `--duration` or `--modules` say. See [Include entries](#include-entries) |
| `include_only` | `false` | `true` = run only the include list (same as `--include-only`) |
| `deep` | `{}` | Deep warmup options for all include entries. See [Deep warmup options](#deep-warmup-options) |
| `deep_by_pattern` | `{}` | Deep warmup options per page layout. See [Per page layout](#per-page-layout-deep_by_pattern) |

### Selector overrides
| Key | Default | Meaning |
|---|---|---|
| `selectors` | `{}` | CSS selector overrides, in case a platform update changes the F&O page structure. See [Selectors](#selectors) |

## Include entries
An entry in `include_mi` is either a menu item name or an object:

```json
"include_mi": [
  "PaymTerm",
  { "mi": "InventLocations", "deep": { "walk_rows": true, "max_rows": 5 } },
  { "mi": "CustTableListPage", "company": "DEMF", "deep": false },
  {
    "mi": "WHSLocDirTable",
    "filters": [
      { "control": "WorkTransType", "values": "all" },
      { "control": "InventLocationId", "values": "lookup" }
    ],
    "deep": { "walk_rows": true, "max_rows": 0 }
  }
]
```

| Entry key | Default | Meaning |
|---|---|---|
| `mi` | *(required)* | Menu item name, as in `?mi=` |
| `company` | `company` | Legal entity for this screen |
| `deep` | *(not set)* | Not set: follows `deep.enabled`. `true`: deep warmup with the merged options. `false`: just open the screen. An object: deep warmup with these options on top |
| `filters` | `[]` | Filter controls to loop over. See [Filter combinations](#filter-combinations) |
| `max_combinations` | `0` | Run only the first N filter combinations (`0` = all) |
| `type` | `"display"` | Only `display` is supported. Anything else is rejected when the config loads |

Only **display** menu items are supported. The tool opens `?mi=<name>`, which F&O resolves as a display menu item. If `menu_items.json` lists the name as an action or output item, the entry is skipped with a message. An entry that isn't in the menu cache still runs; it's logged with module `(include)`.

## Deep warmup
After an include-list screen loads, the deep warmup repeats these steps until nothing is left:
1. Expand each collapsed **FastTab**.
2. On each visible **grid**, sort each sortable column (A to Z, then Z to A) through the column header menu.
3. Open each **tab** that is not selected: horizontal tabs, vertical tabs (parameter pages) and nested tabs, inner tabs first. The grids and FastTabs on a tab are handled before the next tab.

With `walk_rows`, it then moves down the **main grid or list** with the Down arrow key. The main grid is the first visible grid with a selected row: the list on the left of a list/details page, or the grid of a list page. On each row it repeats steps 1–3 for the details (`explore_each_row`). The main grid itself is not sorted when its rows are walked, because a sort can move the selected row away from the top and the walk would then miss the rows above it.

With `filters`, all of the above is repeated for every [filter combination](#filter-combinations).

What the deep warmup touches:
- FastTab headers, tab headers, the sort buttons in column header menus, the Down arrow key, and the filter controls you configure.
- It never clicks a toolbar button, a link or a grid cell. To select a row, it focuses the active row and presses Down, so a hyperlink cell can't open another screen.
- It only types into the filter controls you list in `filters`.
- FactBoxes and action pane tabs are left alone.

### Deep warmup options
| `deep` key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | Deep warmup for include entries that don't set `deep` themselves |
| `expand_fasttabs` | `true` | Expand collapsed FastTabs |
| `open_tabs` | `true` | Open every tab and sub-tab |
| `sort_columns` | `true` | Sort grid columns |
| `sort_directions` | `["ascending", "descending"]` | Sort directions per column. `["ascending"]` halves the sorts |
| `max_columns_per_grid` | `0` | Sort at most N columns per grid (`0` = all sortable columns) |
| `walk_rows` | `false` | Walk the rows of the main grid / list |
| `max_rows` | `10` | Rows to visit, including the first (`0` = all rows) |
| `explore_each_row` | `true` | On each row, expand FastTabs, open tabs and sort the detail grids again |
| `walk_rows_in_edit_mode` | `false` | Also walk rows on forms that open in edit mode (e.g. parameter pages) |
| `max_actions` | `300` | Safety cap on actions per screen; per row with `explore_each_row` |
| `action_timeout_sec` | `30` | Longest wait for one action (sort, tab, row, filter) to finish |

Options are merged in this order, the last one wins:
1. Built-in defaults (the table above)
2. `deep`
3. `deep_by_pattern[<pattern of the page>]`
4. The entry's own `deep` object

### Per page layout (`deep_by_pattern`)
The tool reads the form pattern of each page and uses it to pick options. For example, walk rows on every list/details page but don't sort on parameter pages:

```json
"deep_by_pattern": {
  "SimpleListDetails": { "walk_rows": true, "max_rows": 5 },
  "TableOfContents": { "sort_columns": false }
}
```

Recognised patterns: `SimpleListDetails`, `SimpleList`, `SimpleDetails`, `DetailsMaster`, `DetailsTransaction`, `ListPage`, `TableOfContents`, `Workspace`, `Dialog`, `DropDialog`, `Lookup`, `Wizard`, `TaskSingle`, `TaskDouble`, `Operational`. Anything else is `Other`. `deep_log.csv` shows the pattern of every screen.

### Filter combinations
Some list/details pages have filter controls above the list, for example **Work order type** and **Warehouse** on Location directives (`WHSLocDirTable`). With `filters`, the deep warmup sets every combination of values. For each combination it warms the page again: FastTabs, tabs, detail grid sorts and the row walk.

```json
"filters": [
  { "control": "WorkTransType", "values": "all" },
  { "control": "InventLocationId", "values": ["24", "61"] }
]
```

| Filter key | Default | Meaning |
|---|---|---|
| `control` | *(required)* | Control name of the filter. To find it, right-click the field in F&O and choose **Form information**, or look for `data-dyn-controlname` in the browser's developer tools |
| `values` | `"all"` | A list of values; `"all"` = every option of a combo box (for a lookup field the same as `"lookup"`); `"lookup"` = open the field's lookup and read every row |
| `lookup_column` | `0` | Lookup column to read, by position (`0` = first column) |
| `max_values` | `0` | Read at most N values from the lookup (`0` = all) |

How it works:
- The first filter is the outer loop.
- A value is typed into the filter and committed with Tab, as a user would. The field gets keyboard focus rather than a click, because a filled lookup field is shown as a link.
- A filter that already has the value isn't set again, so the list reloads only when a filter actually changes.
- A value that F&O doesn't accept is logged as an `error` in `deep_log.csv`.

For safety, filters are only used on a form that opens in **view mode**. There the record fields are locked, so the only editable fields are unbound controls such as filters. A read-only control is rejected, and the deep warmup stops if the form leaves view mode.

### Run time
Deep warmup takes time, because every sort reloads the grid (about a second each):
- One row with two detail grids of 10 and 6 columns: 16 columns × 2 directions = 32 sorts, about 30 seconds.
- A list of 10 rows with `explore_each_row`: about 5 minutes.
- With filters, multiply by the number of combinations. 24 work order types × 35 warehouses is 840 combinations; empty lists are quick, but a full run can take many hours.

To keep it in check: `max_combinations`, a fixed `values` list, `max_values`, `max_rows`, `sort_directions: ["ascending"]`, `max_columns_per_grid`, or `explore_each_row: false`.

## Selectors
The tool finds F&O elements with CSS selectors. If a platform update changes the page structure, override a selector in `config.json` without changing the code. A value is one selector or a list tried in order:

```json
"selectors": {
  "nav_opener": ["#modulesPaneOpener", ".newOpenerClass"],
  "grid_header": ".dyn-headerCell.isFilterable"
}
```

| Key | Finds |
|---|---|
| `shell_ready` | An element that proves the F&O shell has loaded (you're signed in) |
| `nav_opener` | The button that opens the navigation pane |
| `modules_header` | The **Modules** group in the navigation pane |
| `module_item` | One module in the modules list |
| `flyout` | The flyout with a module's menu items |
| `flyout_expand_all` | **Expand all** in the flyout |
| `flyout_link` | Menu links and workspace tiles in the flyout |
| `busy` | Loading indicators |
| `form_ready` | A rendered form |
| `error` | Error messages, including the box for an unknown menu item |
| `fasttab_collapsed` | A collapsed FastTab header button |
| `tab` | A tab header |
| `tab_exclude` | Areas whose tabs are not opened (action pane) |
| `grid` | A grid or list |
| `grid_header` | A sortable column header |
| `sort_popup` | The menu that opens on a column header |
| `sort_ascending` / `sort_descending` | **Sort A to Z** / **Sort Z to A** in that menu |
| `lookup_button` | The button that opens a lookup |
| `lookup_grid` | The grid of an open lookup |

The selectors from `fasttab_collapsed` down run inside the page, so they must be plain CSS. The ones above it may use Playwright extensions such as `:visible`. The defaults are in `SELECTORS` in `warmup.py`. To see what the page looks like now, run `python warmup.py --headed --inspect`.

## Output
`warmup_log.csv` gets one line per screen: `timestamp, module, label, mi, seconds, status, detail`.
- `status` is `ok`, `timeout`, `error` (for example an unknown menu item, with F&O's message in `detail`) or `noform`.
- For deep-warmed screens, `detail` holds a summary such as `deep 84.2s: 7 fasttab, 21 tab, 30 sort, 4 row, 2 skipped`.

`deep_log.csv` gets one line per deep warmup action: `timestamp, mi, form, pattern, filters, row, action, target, seconds, status, detail`. Use it to find which filter combination, row, tab or sort is slow. `action` is `filter`, `fasttab`, `tab`, `sort`, `row`, `form` or `limit`; `status` is `ok`, `timeout`, `skipped` or `error`.

At the end, the console shows the average and median load time and the 10 slowest screens.

## Example configurations
**Daily random warmup of 150 screens, plus two key screens:**
```json
{
  "base_url": "https://myenv.operations.dynamics.com",
  "company": "USMF",
  "include_mi": ["SalesTableListPage", "PurchTableListPage"],
  "deep": { "enabled": false }
}
```
```powershell
python warmup.py --count 150
```

**Deep warmup of all list/details setup pages you name, walking up to 5 rows each:**
```json
{
  "include_mi": ["PaymTerm", "InventLocations"],
  "include_only": true,
  "deep_by_pattern": { "SimpleListDetails": { "walk_rows": true, "max_rows": 5 } }
}
```

**Location directives for every work order type and warehouse:**
```json
{
  "include_mi": [{
    "mi": "WHSLocDirTable",
    "filters": [
      { "control": "WorkTransType", "values": "all" },
      { "control": "InventLocationId", "values": "lookup" }
    ],
    "deep": { "walk_rows": true, "max_rows": 0 }
  }],
  "include_only": true
}
```
For a quick trial, add `"max_combinations": 10`.

## Run time of the random warmup
Harvesting a standard environment takes a few minutes and finds around 2,500 display screens. At 5–8 seconds per screen, a full pass takes hours, so for a daily warmup use `--count` or `--duration`.

## Scheduling
```powershell
schtasks /Create /TN "D365FO Warmup" /SC DAILY /ST 06:30 /RL LIMITED `
  /TR "cmd /c cd /d <path>\Tools\D365FOWarmup && python warmup.py --count 150 >> warmup_console.log 2>&1"
```
Run the task as the same Windows user who did the `--headed` sign-in, because the profile folder holds that user's session. Sessions expire eventually. Watch for exit code `2`.

## Notes and safety
- Run it against **non-production** environments first, and use an account with appropriate (read-only) permissions where possible.
- Opening forms can still trigger form `init` logic, such as default records or number sequences on some setup forms. Add such screens to `exclude_mi`, or leave them out of `include_mi`.
- Some screens open in edit mode (for example parameter pages). The deep warmup doesn't type or save there, doesn't use filters there, and doesn't walk rows there unless `walk_rows_in_edit_mode` is set.
- Selectors were verified against the F&O web client at the time of writing. If a platform update breaks something, run `--headed --inspect` and override the selectors in `config.json`.

For the version history, see [CHANGELOG.md](CHANGELOG.md).

See the repository [disclaimer](../../README.md#disclaimer) and [license](../../LICENSE).
