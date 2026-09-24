# D365FO Warmup

A small Python + Playwright tool that warms up a Dynamics 365 Finance & Operations environment. It collects every screen reachable from the navigation pane (**Modules**) and opens them in random order. After a deployment, restart or refresh, the first users no longer pay the "cold form" penalty.

- Standalone: plain Python. It runs from the console, Windows Task Scheduler or a pipeline agent.
- It uses your normal Entra ID sign-in (with MFA) once. After that it reuses the saved browser session headless.
- It opens **display** menu items only. Action and output menu items (batch jobs, reports, posting) are skipped by default.
- It logs each screen's load time to CSV and prints the slowest screens. This also makes it a quick performance probe.

## How it works
1. It opens the environment in a persistent Edge (or Chromium) profile. The first run is `--headed` so you can sign in.
2. It opens the navigation pane, expands each module and **Expand all** in the module flyout, and reads the menu items. The flyout links have no `href`. The tool reads `MenuItemName`, `MenuItemType` and `Label` from the React props behind each `.modulesFlyout-link`. The result is cached in `menu_items.json`.
3. It opens `?cmp=<company>&mi=<menuItem>` for a random selection of screens. It waits until the form is rendered and the F&O client stays idle (`$dyn.clientBusy` is false) for 1 second, then records the load time.

## Setup
```powershell
git clone https://github.com/MZade/d365fo-code-snippets.git
cd d365fo-code-snippets\Tools\D365FOWarmup
pip install -r requirements.txt
copy config.example.json config.json   # then set base_url and company
```
The tool uses the installed Microsoft Edge by default (`"browser_channel": "msedge"`). To use Playwright's bundled Chromium, set `"browser_channel": ""` and run `python -m playwright install chromium`.

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
```

| Option | Meaning |
|---|---|
| `--headed` | Show the browser (first sign-in, debugging) |
| `--refresh` | Re-harvest the navigation pane |
| `--count N` | Number of screens to open (default: all) |
| `--duration MIN` | Keep going for N minutes, looping over the list |
| `--delay A-B` | Random pause between screens, in seconds |
| `--company` | Legal entity (overrides config) |
| `--modules` | Comma-separated module names to include |
| `--seed` | Random seed for a reproducible order |
| `--inspect` | Print nav-pane DOM hints for fixing selectors |

Exit codes: `0` ok, `1` failure, `2` sign-in required. On `2`, run once with `--headed`.

Results are appended to `warmup_log.csv` (`timestamp, module, label, mi, seconds, status, detail`).

## Run time
Harvesting a standard environment takes a few minutes and finds around 2,500 display screens. At 5–8 seconds per screen, a full pass takes hours, so for a daily warmup use `--count` or `--duration`.

## Scheduling
```powershell
schtasks /Create /TN "D365FO Warmup" /SC DAILY /ST 06:30 /RL LIMITED `
  /TR "cmd /c cd /d <path>\Tools\D365FOWarmup && python warmup.py --count 150 >> warmup_console.log 2>&1"
```
Run the task as the same Windows user who did the `--headed` sign-in, because the profile folder holds that user's session. Sessions expire eventually. Watch for exit code `2`.

## Configuration (`config.json`)
| Key | Meaning |
|---|---|
| `base_url` | Environment URL, e.g. `https://myenv.operations.dynamics.com` |
| `company` | Default legal entity |
| `browser_channel` | `msedge`, `chrome`, or `""` for bundled Chromium |
| `form_timeout_sec` | Max wait per screen |
| `delay` | Default random pause, e.g. `"2-5"` |
| `skip_menu_item_types` | Default `["action", "output"]`. Keep these skipped unless you know what they do |
| `exclude_mi` / `exclude_modules` | Screens or modules to skip |
| `selectors` | CSS selector overrides if a platform update changes the navigation pane DOM |

## Notes and safety
- Run it against **non-production** environments first, and use an account with appropriate (read-only) permissions where possible.
- Opening forms can still trigger form `init` logic, such as default records or number sequences on some setup forms. Add such screens to `exclude_mi`.
- Selectors were verified against the F&O web client at the time of writing. If a platform update breaks the harvest, run `--headed --inspect` and override the selectors in `config.json`.

See the repository [disclaimer](../../README.md#disclaimer) and [license](../../LICENSE).
