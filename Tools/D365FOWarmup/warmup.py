"""
D365 Finance & Operations warmup crawler.

Harvests the menu items reachable from the navigation pane (Modules) and opens
them in random order so the AOS caches are warm before users arrive.

Menu items in config.json -> include_mi always run first. On those pages the tool
can also do a deep warmup: expand FastTabs, open every tab, sort every grid column
ascending/descending and walk the rows of the main grid or list.

Usage:
    python warmup.py --headed --refresh --count 5   # first run: sign in, build menu cache
    python warmup.py --count 200                    # headless, reuses saved session
    python warmup.py --duration 30 --delay 1-3      # run for 30 minutes
    python warmup.py --include-only                 # only the include_mi pages (with deep warmup)
    python warmup.py --mi PaymTerm,InventLocations  # ad-hoc include list
    python warmup.py --headed --inspect             # print nav-pane DOM hints (selector debugging)

Exit codes: 0 ok, 1 unexpected failure, 2 login required (run once with --headed).
"""
import argparse
import csv
import itertools
import json
import random
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "config.json"
MENU_FILE = HERE / "menu_items.json"
LOG_FILE = HERE / "warmup_log.csv"
DEEP_LOG_FILE = HERE / "deep_log.csv"
PROFILE_DIR = HERE / "profile"

EXIT_OK, EXIT_FAIL, EXIT_LOGIN = 0, 1, 2

# Candidate selectors, tried in order. Override any of these in config.json -> "selectors".
SELECTORS = {
    # Element that proves the F&O shell is loaded (user is signed in)
    "shell_ready": [".navigationBar", "#navBar", "[data-dyn-role='NavigationBar']", ".dashboard"],
    # Button that opens the navigation pane / modules list
    "nav_opener": ["#modulesPaneOpener", ".modulesPane-opener"],
    # "Modules" group inside the navigation pane (clicked to expand, if collapsed)
    "modules_header": ["#navPaneModuleID", ".modulesPane-groupHeading[aria-label='Modules']"],
    # One entry per module in the modules list
    "module_item": ["a.modulesPane-module", ".modulesPane [role='treeitem'][aria-level='2']"],
    # Container of the flyout that shows a module's menu items
    "flyout": [".modulesFlyout-container"],
    # "Expand all" button in the flyout (menu groups are rendered only when expanded)
    "flyout_expand_all": [".modulesFlyout-ExpandAll"],
    # Flyout entries: menu links and workspace tiles
    "flyout_link": [".modulesFlyout-link, .modulesFlyout-tile"],
    # Busy / processing indicators shown while a form loads
    "busy": ["#ShellProcessingDiv:visible", ".shellProcessing:visible", ".processingDialog:visible",
             ".blockingOverlay:visible"],
    # Element present once a form has rendered. Not ".rootContent": the error box for an
    # unknown menu item is also a .rootContent.
    "form_ready": ["[data-dyn-form-name]:visible", "[data-dyn-role='Form']:visible"],
    # Errors shown instead of / on top of the form (e.g. "menu item ... does not exist")
    "error": ["[data-dyn-role='LightBox'] [data-dyn-role='ListBox'] li:visible", ".messageBar-error:visible",
              ".notification-error:visible"],
    # ---- deep warmup (plain CSS: these are also used inside page JavaScript)
    # Collapsed FastTab header button
    "fasttab_collapsed": ["button.section-page-caption[aria-expanded='false']"],
    # Tab headers (horizontal tabs, vertical tabs, pivots)
    "tab": ["[role='tablist'] > [role='tab']"],
    # Tabs inside these are not opened (action pane tabs)
    "tab_exclude": ["[data-dyn-role^='AppBar']", "[data-dyn-role='ActionPane']"],
    # Grids and lists
    "grid": ["[data-dyn-role='ReactList']", "[data-dyn-role='Grid']"],
    # Sortable column header (non-sortable columns have no header popup)
    "grid_header": [".dyn-headerCell.isFilterable"],
    # Popup that opens when a column header is clicked, and its sort buttons
    "sort_popup": [".columnHeader-popup"],
    "sort_ascending": ["[data-dyn-controlname^='Ascending_']"],
    "sort_descending": ["[data-dyn-controlname^='Descending_']"],
    # Lookup button of a filter control, and the grid of the opened lookup
    "lookup_button": [".lookupButton"],
    "lookup_grid": [".lookup-popup [data-dyn-role='ReactList']", ".lookup-popup [data-dyn-role='Grid']"],
}

# Deep warmup options. Override in config.json -> "deep", per form pattern in
# "deep_by_pattern", or per include_mi entry in its "deep" key.
DEFAULT_DEEP = {
    "enabled": True,                # deep warmup for include_mi entries
    "expand_fasttabs": True,
    "open_tabs": True,
    "sort_columns": True,
    "sort_directions": ["ascending", "descending"],
    "max_columns_per_grid": 0,      # 0 = all sortable columns
    "walk_rows": False,             # move row by row through the main grid / list
    "max_rows": 10,                 # 0 = all rows
    "explore_each_row": True,       # on each row, open tabs/FastTabs and sort the detail grids again
    "walk_rows_in_edit_mode": False,  # forms that open in edit mode (e.g. parameters) are not row-walked
    "max_actions": 300,             # safety cap per page (per row when explore_each_row)
    "action_timeout_sec": 30,
}

# Form patterns, read from the form's CSS classes, for config.json -> deep_by_pattern
FORM_PATTERNS = ["SimpleListDetails", "SimpleList", "SimpleDetails", "DetailsMaster", "DetailsTransaction",
                 "ListPage", "TableOfContents", "Workspace", "Dialog", "DropDialog", "Lookup", "Wizard",
                 "TaskSingle", "TaskDouble", "Operational"]

DEFAULT_CONFIG = {
    "base_url": "https://YOUR-ENV.operations.dynamics.com",
    "company": "USMF",
    "browser_channel": "msedge",
    "form_timeout_sec": 60,
    "login_wait_sec": 300,
    "delay": "2-5",
    "skip_menu_item_types": ["action", "output"],
    "exclude_mi": [],
    "exclude_modules": [],
    "include_mi": [],
    "include_only": False,
    "deep": {},
    "deep_by_pattern": {},
    "selectors": {},
}


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        log(f"Created {CONFIG_FILE.name} - set base_url and company, then run again.")
        sys.exit(EXIT_FAIL)
    cfg = {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}
    if "YOUR-ENV" in cfg["base_url"]:
        log(f"Set base_url in {CONFIG_FILE.name} first.")
        sys.exit(EXIT_FAIL)
    cfg["base_url"] = cfg["base_url"].rstrip("/")
    for key, value in cfg.get("selectors", {}).items():
        SELECTORS[key] = [value] if isinstance(value, str) else value
    cfg["include_mi"] = parse_include(cfg["include_mi"])
    return cfg


def parse_include(entries):
    """Normalize include_mi entries ("Name" or {"mi": ..., "company": ..., "deep": ...}) to dicts.

    Only display menu items are supported: the tool opens them as ?mi=<name>, which the
    F&O client resolves as a display menu item.
    """
    result = []
    for entry in entries:
        item = {"mi": entry} if isinstance(entry, str) else dict(entry)
        if not item.get("mi"):
            raise ValueError(f"include_mi entry without 'mi': {entry}")
        if str(item.get("type", "display")).lower() != "display":
            raise ValueError(f"include_mi '{item['mi']}': only display menu items are supported")
        for flt in item.get("filters", []):
            values = flt.get("values", "all")
            if not flt.get("control") or not (isinstance(values, list) or values in ("all", "lookup")):
                raise ValueError(f"include_mi '{item['mi']}': a filter needs 'control' and 'values' "
                                 f"(a list, \"all\" or \"lookup\"): {flt}")
        result.append(item)
    return result


def deep_options(cfg, item, pattern):
    """Resolve the deep warmup options for one include item: defaults < deep < deep_by_pattern < item."""
    override = item.get("deep", True)
    if override is False:
        return None
    opts = {**DEFAULT_DEEP, **cfg["deep"], **cfg["deep_by_pattern"].get(pattern, {})}
    if isinstance(override, dict):
        opts.update({"enabled": True, **override})
    elif "deep" in item:
        opts["enabled"] = True  # an explicit "deep": true wins over a global "enabled": false
    return opts if opts["enabled"] else None


def first_visible(page, key, timeout_ms=0):
    """Return the first locator from SELECTORS[key] that is visible, polling up to timeout_ms."""
    deadline = time.time() + timeout_ms / 1000
    while True:
        for sel in SELECTORS[key]:
            loc = page.locator(sel).first
            try:
                if loc.count() and loc.is_visible():
                    return loc
            except PlaywrightError:
                pass
        if time.time() >= deadline:
            return None
        page.wait_for_timeout(250)


def is_login_page(page):
    host = urlparse(page.url).netloc.lower()
    return "login.microsoftonline.com" in host or "login.live.com" in host or "adfs" in host


# ---------------------------------------------------------------- login

def ensure_logged_in(page, cfg, headed):
    log(f"Opening {cfg['base_url']}")
    page.goto(f"{cfg['base_url']}/?cmp={cfg['company']}", wait_until="domcontentloaded", timeout=120_000)
    deadline = time.time() + (cfg["login_wait_sec"] if headed else 60)
    prompted = False
    while time.time() < deadline:
        if first_visible(page, "shell_ready"):
            log("Signed in, F&O shell loaded.")
            return
        if is_login_page(page):
            if not headed:
                log("Login required - run once with --headed to sign in (session is saved in profile folder).")
                sys.exit(EXIT_LOGIN)
            if not prompted:
                log(f"Please sign in in the browser window (waiting up to {cfg['login_wait_sec']}s)...")
                prompted = True
        page.wait_for_timeout(1000)
    log("F&O shell did not load in time (check base_url or the 'shell_ready' selector).")
    sys.exit(EXIT_LOGIN if is_login_page(page) else EXIT_FAIL)


# ---------------------------------------------------------------- menu harvest

# Flyout links have no href: the menu item lives in the props of the React component
# that renders each link (MenuItemName / MenuItemType / Label).
COLLECT_FLYOUT_JS = """
(sel) => Array.from(document.querySelectorAll(sel)).map(el => {
    const fk = Object.keys(el).find(k => k.startsWith('__reactFiber'));
    let f = fk && el[fk];
    for (let i = 0; i < 6 && f; i++, f = f.return) {
        const p = f.memoizedProps;
        if (p && p.MenuItemName) return {mi: p.MenuItemName, mt: p.MenuItemType, label: p.Label};
    }
    return {mi: null, label: el.getAttribute('aria-label')};
})
"""


def open_nav_pane(page):
    if first_visible(page, "module_item"):
        return True
    opener = first_visible(page, "nav_opener", timeout_ms=5000)
    if opener:
        opener.click()
    header = first_visible(page, "modules_header", timeout_ms=3000)
    if header and header.get_attribute("aria-expanded") != "true":
        header.click()
    return first_visible(page, "module_item", timeout_ms=10000) is not None


def flyout_link_count(page):
    return sum(page.locator(sel).count() for sel in SELECTORS["flyout_link"])


def wait_flyout_stable(page, timeout_ms=20000, settle_ms=1500):
    """Wait until the module flyout stops loading and its link count stays unchanged for settle_ms."""
    deadline = time.time() + timeout_ms / 1000
    last, since = -1, time.time()
    while time.time() < deadline:
        loading = page.locator(".modulesFlyout-isLoading").count() > 0
        count = flyout_link_count(page)
        if loading or count != last:
            last, since = count, time.time()
        elif count > 0 and (time.time() - since) * 1000 >= settle_ms:
            return count
        page.wait_for_timeout(250)
    return last


def harvest_menu_items(page, cfg):
    if not open_nav_pane(page):
        raise RuntimeError("Could not open the navigation pane modules list. Run with --headed --inspect "
                           "and put working selectors in config.json -> selectors.")
    module_sel = next(s for s in SELECTORS["module_item"] if page.locator(s).count())
    modules = page.locator(module_sel)
    names = [(modules.nth(i).inner_text() or "").strip() for i in range(modules.count())]
    log(f"Found {len(names)} modules.")

    items = {}
    for i, name in enumerate(names):
        if not name or name in cfg["exclude_modules"]:
            continue
        try:
            if not first_visible(page, "module_item"):
                open_nav_pane(page)
            module = page.locator(module_sel).nth(i)
            module.click()
            # Wait until this module is selected and its (lazy-loaded) flyout has settled
            deadline = time.time() + 10
            while module.get_attribute("data-dyn-selected") not in ("true", None) and time.time() < deadline:
                page.wait_for_timeout(200)
            wait_flyout_stable(page)
            expand = first_visible(page, "flyout_expand_all", timeout_ms=2000)
            if expand:
                expand.click()
                wait_flyout_stable(page)  # groups render their links after expanding
            links = []
            for sel in SELECTORS["flyout_link"]:
                links = page.evaluate(COLLECT_FLYOUT_JS, sel)
                if links:
                    break
            added = 0
            for link in links:
                if link["mi"] and link["mi"] not in items:
                    items[link["mi"]] = {"module": name, "label": link["label"], "mi": link["mi"],
                                         "mt": (link.get("mt") or "Display").lower()}
                    added += 1
            log(f"  {name}: {added} new items ({len(links)} links)")
        except PlaywrightError as e:
            log(f"  {name}: failed ({e.__class__.__name__}: {str(e).splitlines()[0]})")
    result = sorted(items.values(), key=lambda x: (x["module"], x["label"]))
    MENU_FILE.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    log(f"Saved {len(result)} menu items to {MENU_FILE.name}")
    return result


def inspect(page):
    """Print DOM hints for fixing selectors when the F&O UI changes."""
    info = page.evaluate(
        """() => {
            const pick = el => ({tag: el.tagName, cls: el.className && el.className.toString().slice(0, 80),
                                 aria: el.getAttribute('aria-label'), role: el.getAttribute('role'),
                                 text: (el.innerText || '').trim().slice(0, 40)});
            const q = s => Array.from(document.querySelectorAll(s)).slice(0, 15).map(pick);
            return {
              buttons: q('button[aria-label], [role=button][aria-label]'),
              treeitems: q('[role=treeitem]'),
              modulesish: q('[class*=odule]'),
              flyoutLinks: document.querySelectorAll('.modulesFlyout-link').length,
            };
        }"""
    )
    print(json.dumps(info, indent=2))
    for key in ("shell_ready", "nav_opener", "modules_header", "module_item", "flyout", "form_ready"):
        hits = [s for s in SELECTORS[key] if page.locator(s).count()]
        print(f"{key:15} matches: {hits or 'NONE'}")


# ---------------------------------------------------------------- warmup

CLIENT_BUSY_JS = """() => {
    try { return !!(window.$dyn && $dyn.clientBusy && $dyn.value($dyn.clientBusy)); }
    catch (e) { return false; }
}"""


def is_busy(page):
    try:
        if page.evaluate(CLIENT_BUSY_JS):
            return True
    except PlaywrightError:
        return True  # page is navigating
    return first_visible(page, "busy") is not None


def wait_idle(page, timeout_sec, settle_sec=1.0):
    """Wait until the F&O client stays idle for settle_sec."""
    deadline = time.time() + timeout_sec
    quiet_since = None
    while time.time() < deadline:
        if is_busy(page):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= settle_sec:
            return True
        page.wait_for_timeout(200)
    return False


def wait_form_loaded(page, cfg):
    """Wait for a form (or an error box), then for the F&O client to stay idle for 1 second."""
    deadline = time.time() + cfg["form_timeout_sec"]
    while time.time() < deadline and not (first_visible(page, "form_ready") or first_visible(page, "error")):
        page.wait_for_timeout(250)
    return wait_idle(page, max(deadline - time.time(), 1))


def page_error_text(page):
    loc = first_visible(page, "error")
    try:
        return loc.inner_text(timeout=500).strip().splitlines()[0][:150] if loc else ""
    except (PlaywrightError, IndexError):
        return ""


def open_page(page, cfg, item):
    """Open a display menu item. Returns (seconds, status, detail)."""
    url = f"{cfg['base_url']}/?cmp={item.get('company') or cfg['company']}&mi={item['mi']}"
    t0 = time.time()
    status, detail = "ok", ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=cfg["form_timeout_sec"] * 1000)
        if is_login_page(page):
            log("Session expired - run once with --headed to sign in again.")
            sys.exit(EXIT_LOGIN)
        if not wait_form_loaded(page, cfg):
            status = "timeout"
        detail = page_error_text(page)
        if detail:
            status = "error"
        elif status == "ok" and not first_visible(page, "form_ready"):
            status = "noform"
    except PlaywrightTimeout:
        status = "timeout"
    except PlaywrightError as e:
        status, detail = "error", str(e).splitlines()[0][:150]
    return round(time.time() - t0, 2), status, detail


# ---------------------------------------------------------------- deep warmup

def css(key):
    return ", ".join(SELECTORS[key])


FORM_INFO_JS = """(patterns) => {
    const forms = [...document.querySelectorAll('[data-dyn-form-name]')]
        .filter(f => f.getClientRects().length && !f.parentElement.closest('[data-dyn-form-name]'));
    const f = forms.find(x => x.classList.contains('active-form')) || forms[0];
    if (!f) return null;
    return {name: f.dataset.dynFormName,
            pattern: patterns.find(p => f.classList.contains(p)) || 'Other',
            editMode: f.classList.contains('editMode')};
}"""

# Finds what is left to do on the form: collapsed FastTabs, sortable columns of visible
# grids, and tabs that are not selected. Each candidate is tagged with data-warmup-id so
# Python can click it. Controls of FactBoxes (nested forms) and the action pane are skipped.
DISCOVER_JS = """([formName, sel, opts, done, skipGrids]) => {
    const doneSet = new Set(done), skip = new Set(skipGrids);
    const root = [...document.querySelectorAll('[data-dyn-form-name]')]
        .find(f => f.dataset.dynFormName === formName && f.getClientRects().length);
    if (!root) return null;
    const vis = e => e.getClientRects().length > 0;
    const own = e => e.closest('[data-dyn-form-name]') === root;
    const name = e => { const c = e.closest('[data-dyn-controlname]'); return c ? c.dataset.dynControlname : ''; };
    const text = e => (e.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 60);
    const tag = e => {
        if (!e.dataset.warmupId) {
            window.__warmupSeq = (window.__warmupSeq || 0) + 1;
            e.dataset.warmupId = String(window.__warmupSeq);
        }
        return e.dataset.warmupId;
    };
    const todo = [], seen = [];
    if (opts.expand_fasttabs)
        root.querySelectorAll(sel.fasttab_collapsed).forEach(b => {
            const key = 'fasttab:' + name(b);
            if (vis(b) && own(b) && !doneSet.has(key)) todo.push({kind: 'fasttab', key, label: text(b), id: tag(b)});
        });
    if (opts.sort_columns)
        root.querySelectorAll(sel.grid).forEach(g => {
            const grid = g.dataset.dynControlname || '';
            if (!vis(g) || !own(g) || skip.has(grid)) return;
            let n = 0;
            g.querySelectorAll(sel.grid_header).forEach(h => {
                if (!vis(h) || h.closest(sel.grid) !== g) return;
                if (opts.max_columns_per_grid && ++n > opts.max_columns_per_grid) return;
                const key = 'sort:' + grid + ':' + (h.dataset.dynControlname || text(h));
                if (!doneSet.has(key)) todo.push({kind: 'sort', key, label: grid + ' / ' + text(h), id: tag(h)});
            });
        });
    if (opts.open_tabs) {
        const tabs = [];
        root.querySelectorAll(sel.tab).forEach(t => {
            if (!vis(t) || !own(t) || t.closest(sel.tab_exclude)) return;
            const key = 'tab:' + name(t) + ':' + text(t);
            if (t.getAttribute('aria-selected') === 'true') { seen.push(key); return; }
            if (doneSet.has(key)) return;
            let depth = 0;
            for (let p = t.parentElement; p && p !== root; p = p.parentElement)
                if (p.getAttribute('role') === 'tabpanel') depth++;
            tabs.push({kind: 'tab', key, label: text(t), id: tag(t), depth});
        });
        tabs.sort((a, b) => b.depth - a.depth);  // nested tabs before the next outer tab
        todo.push(...tabs);
    }
    return {todo, seen};
}"""

# The main grid / list: the first visible grid of the form that has an active row
MASTER_GRID_JS = """([formName, gridSel]) => {
    const root = [...document.querySelectorAll('[data-dyn-form-name]')]
        .find(f => f.dataset.dynFormName === formName && f.getClientRects().length);
    if (!root) return null;
    const g = [...root.querySelectorAll(gridSel)].find(g => g.getClientRects().length
        && g.closest('[data-dyn-form-name]') === root && g.querySelector("[role=row][data-dyn-row-active='true']"));
    return g ? g.dataset.dynControlname : null;
}"""

# Returns the aria-rowindex of the grid's active row. With focus=true it also focuses a
# field of that row (focus only, no click: clicking a hyperlink cell would open another form).
ACTIVE_ROW_JS = """([formName, gridSel, gridName, focus]) => {
    const root = [...document.querySelectorAll('[data-dyn-form-name]')]
        .find(f => f.dataset.dynFormName === formName && f.getClientRects().length);
    if (!root) return null;
    const g = [...root.querySelectorAll(gridSel)]
        .find(g => g.dataset.dynControlname === gridName && g.getClientRects().length);
    const r = g && g.querySelector("[role=row][data-dyn-row-active='true']");
    if (!r) return null;
    if (focus) { const f = r.querySelector('input, [tabindex]'); if (f) f.focus(); }
    return r.getAttribute('aria-rowindex');
}"""


def form_info(page):
    try:
        return page.evaluate(FORM_INFO_JS, FORM_PATTERNS)
    except PlaywrightError:
        return None


def sort_column(page, header, direction, opts):
    """Open the column header popup and click Sort A to Z / Z to A. Returns (status, detail)."""
    header.click(timeout=10_000)
    popup = page.locator(", ".join(s + ":visible" for s in SELECTORS["sort_popup"])).first
    button = popup.locator(css("sort_ascending" if direction == "ascending" else "sort_descending")).first
    try:
        button.wait_for(state="visible", timeout=3000)
    except PlaywrightTimeout:
        if popup.count():
            page.keyboard.press("Escape")
        return "skipped", "no sort option"
    button.click(timeout=10_000)
    return ("ok" if wait_idle(page, opts["action_timeout_sec"], 0.5) else "timeout"), ""


# ---- filter controls (e.g. Work order type / Warehouse above the list of a list/details page)

FILTER_INFO_JS = """([formName, control]) => {
    const root = [...document.querySelectorAll('[data-dyn-form-name]')]
        .find(f => f.dataset.dynFormName === formName && f.getClientRects().length);
    const c = root && [...root.querySelectorAll('[data-dyn-controlname]')]
        .find(e => e.dataset.dynControlname === control && e.getClientRects().length);
    if (!c) return null;
    const input = c.querySelector('input');
    return {editable: !!input && !input.readOnly && !input.disabled, value: input ? input.value : '',
            options: [...c.querySelectorAll('[role=option]')].map(o => o.textContent.trim())};
}"""

# Values of the rendered rows of the open lookup, in row order (the lookup grid is virtualized)
LOOKUP_ROWS_JS = """([gridSel, column]) => {
    const g = [...document.querySelectorAll(gridSel)].find(e => e.getClientRects().length);
    if (!g) return null;
    return [...g.querySelectorAll('[role=row][aria-rowindex]')]
        .map(r => [+r.getAttribute('aria-rowindex'), r.querySelectorAll('input')[column]])
        .filter(x => x[1]).sort((a, b) => a[0] - b[0]).map(x => x[1].value.trim()).filter(Boolean);
}"""


def filter_control(page, form, control):
    return page.locator(f'[data-dyn-form-name="{form["name"]}"] [data-dyn-controlname="{control}"]').first


def lookup_values(page, form, flt):
    """Open the control's lookup, scroll through it and return the values of one column."""
    limit = flt.get("max_values", 0)
    filter_control(page, form, flt["control"]).locator(css("lookup_button")).first.click(timeout=10_000)
    grid = page.locator(", ".join(s + ":visible" for s in SELECTORS["lookup_grid"])).first
    grid.wait_for(state="visible", timeout=15_000)
    wait_idle(page, 15, 0.5)
    values, stale = [], 0
    try:
        while stale < 3 and not (limit and len(values) >= limit):
            rows = page.evaluate(LOOKUP_ROWS_JS, [css("lookup_grid"), flt.get("lookup_column", 0)]) or []
            new = [v for v in rows if v not in values]
            values += new
            stale = 0 if new else stale + 1
            grid.hover()
            page.mouse.wheel(0, 400)
            wait_idle(page, 15, 0.3)
    finally:
        page.keyboard.press("Escape")  # closes the lookup, not the form
        wait_idle(page, 15, 0.3)
    return values[:limit] if limit else values


def filter_values(page, form, flt):
    """Values to try for one filter: a list from the config, the combo box options, or the lookup rows."""
    info = page.evaluate(FILTER_INFO_JS, [form["name"], flt["control"]])
    if not info:
        raise ValueError(f"filter control '{flt['control']}' not found on {form['name']}")
    if not info["editable"]:
        raise ValueError(f"filter control '{flt['control']}' is read-only")
    values = flt.get("values", "all")
    if isinstance(values, list):
        return [str(v) for v in values]
    if values == "all" and info["options"]:
        return info["options"]
    return lookup_values(page, form, flt)


def set_filter(page, form, control, value, opts):
    """Type a value into a filter control and press Tab, like a user. Returns (status, detail)."""
    field = filter_control(page, form, control).locator("input").first
    field.focus()  # focus, not click: a filled lookup field is shown as a link
    field.press("Control+a")
    if value:
        field.press_sequentially(value)
    else:
        field.press("Delete")
    field.press("Tab")
    status = "ok" if wait_idle(page, opts["action_timeout_sec"], 0.5) else "timeout"
    actual = field.input_value()
    if actual.strip().lower() != value.strip().lower():
        return "error", f"value is '{actual}' after typing '{value}'"
    return status, page_error_text(page)


def deep_warm(page, item, form, opts, writer):
    """Expand FastTabs, open tabs, sort grid columns and walk rows on the current page.

    Only navigation inside the form is used: FastTab headers, tab headers, column header
    sort buttons, the Down arrow key and the filter controls listed in item["filters"].
    Nothing is edited, saved or run. With filters, all of this is repeated for every
    combination of filter values. Every action is written to deep_log.csv.
    Returns a one-line summary.
    """
    sel = {k: css(k) for k in ("fasttab_collapsed", "tab", "tab_exclude", "grid", "grid_header")}
    counts, t_start = {}, time.time()
    row_label, filter_label = "", ""

    def record(kind, target, secs, status, detail=""):
        key = kind if status == "ok" else status
        counts[key] = counts.get(key, 0) + 1
        writer.writerow([datetime.now().isoformat(timespec="seconds"), item["mi"], form["name"], form["pattern"],
                         filter_label, row_label, kind, target, round(secs, 2), status, detail])

    def run(cand):
        target = page.locator(f'[data-warmup-id="{cand["id"]}"]').first
        directions = opts["sort_directions"] if cand["kind"] == "sort" else [None]
        for direction in directions:
            t0, detail = time.time(), ""
            try:
                if direction:
                    status, detail = sort_column(page, target, direction, opts)
                else:
                    target.click(timeout=10_000)
                    status = "ok" if wait_idle(page, opts["action_timeout_sec"], 0.5) else "timeout"
            except PlaywrightError as e:
                status, detail = "error", str(e).splitlines()[0][:150]
            record(cand["kind"], cand["label"] + (f" {direction}" if direction else ""), time.time() - t0,
                   status, detail)
            if status == "skipped":
                break

    def explore(skip_grids):
        """Work through the form until nothing is left. Returns False if the form went away."""
        done = set()
        for _ in range(opts["max_actions"]):
            found = page.evaluate(DISCOVER_JS, [form["name"], sel, opts, list(done), skip_grids])
            if found is None:
                return False
            done.update(found["seen"])
            if not found["todo"]:
                return True
            cand = found["todo"][0]
            done.add(cand["key"])
            run(cand)
        record("limit", f"max_actions {opts['max_actions']} reached", 0, "skipped")
        return True

    def walk_rows(grid):
        """Move down the main grid / list with the Down key. Returns False if the form went away."""
        nonlocal row_label
        current = page.evaluate(ACTIVE_ROW_JS, [form["name"], sel["grid"], grid, False])
        row_label = f"{grid} #{current}"
        visited = 1  # the row the list opened on was explored already
        while not opts["max_rows"] or visited < opts["max_rows"]:
            t0 = time.time()
            if page.evaluate(ACTIVE_ROW_JS, [form["name"], sel["grid"], grid, True]) is None:
                return False
            page.keyboard.press("ArrowDown")
            status = "ok" if wait_idle(page, opts["action_timeout_sec"], 0.5) else "timeout"
            new = page.evaluate(ACTIVE_ROW_JS, [form["name"], sel["grid"], grid, False])
            if new is None:
                return False
            if new == current:
                break  # last row
            current, row_label = new, f"{grid} #{new}"
            visited += 1
            record("row", row_label, time.time() - t0, status)
            if opts["explore_each_row"] and not explore([grid]):
                return False
        return True

    def warm_once():
        nonlocal row_label
        row_label = ""
        walk = opts["walk_rows"]
        if walk and form["editMode"] and not opts["walk_rows_in_edit_mode"]:
            record("row", "form opens in edit mode", 0, "skipped")
            walk = False
        grid = page.evaluate(MASTER_GRID_JS, [form["name"], sel["grid"]]) if walk else None
        if walk and not grid:
            record("row", "no grid with an active row", 0, "skipped")
        # The main grid is not sorted when its rows are walked: a sort can move the active
        # row away from the top, and the walk would miss the rows above it.
        alive = explore([grid] if grid else [])
        return walk_rows(grid) if alive and grid else alive

    def combinations():
        """Every combination of filter values, as lists of (control, value)."""
        filters = item.get("filters", [])
        if not filters:
            return [[]]
        if form["editMode"]:
            raise ValueError("filters need a form that opens in view mode")
        lists = []
        for flt in filters:
            t0 = time.time()
            values = filter_values(page, form, flt)
            record("filter", f"{flt['control']}: {len(values)} values", time.time() - t0, "ok" if values else "error")
            lists.append([(flt["control"], v) for v in values])
        combos = [list(c) for c in itertools.product(*lists)]
        limit = item.get("max_combinations", 0)
        log(f"     {len(combos)} filter combinations" + (f", running the first {limit}" if limit else ""))
        return combos[:limit] if limit else combos

    try:
        for combo in combinations():
            filter_label = ", ".join(f"{c}={v}" for c, v in combo)
            for control, value in combo:
                info = page.evaluate(FILTER_INFO_JS, [form["name"], control])
                if info and info["value"].strip().lower() == value.strip().lower():
                    continue  # already set (only the changed filter reloads the list)
                t0 = time.time()
                status, detail = set_filter(page, form, control, value, opts)
                record("filter", f"{control}={value}", time.time() - t0, status, detail)
            if combo:
                info = form_info(page)
                if not info or info["name"] != form["name"] or info["editMode"]:
                    record("form", "form left view mode or closed after setting a filter", 0, "error")
                    break
            if not warm_once():
                record("form", "form closed or navigated away", 0, "error")
                break
    except ValueError as e:
        record("filter", "filters not applied", 0, "error", str(e))
    except PlaywrightError as e:
        record("form", "deep warmup aborted", 0, "error", str(e).splitlines()[0][:150])

    parts = ", ".join(f"{v} {k}" for k, v in counts.items()) or "nothing to do"
    return f"deep {time.time() - t_start:.1f}s: {parts}"


# ---------------------------------------------------------------- warmup loop

def open_log(path, header):
    new_file = not path.exists()
    fh = path.open("a", newline="", encoding="utf-8")
    writer = csv.writer(fh)
    if new_file:
        writer.writerow(header)
    return fh, writer


def warm(page, cfg, includes, items, args):
    """Open the include items first (with deep warmup), then the random items."""
    lo, hi = (float(x) for x in (args.delay or cfg["delay"]).split("-"))
    rng = random.Random(args.seed)
    order = items[:]
    rng.shuffle(order)
    if args.count:
        order = order[: args.count]

    results = []
    fh, writer = open_log(LOG_FILE, ["timestamp", "module", "label", "mi", "seconds", "status", "detail"])
    dfh, deep_writer = open_log(DEEP_LOG_FILE, ["timestamp", "mi", "form", "pattern", "filters", "row", "action",
                                                "target", "seconds", "status", "detail"])

    def visit(item, deep):
        secs, status, detail = open_page(page, cfg, item)
        if deep and status == "ok":
            form = form_info(page)
            opts = deep_options(cfg, item, form["pattern"]) if form else None
            if opts:
                log(f"     deep warmup of {form['name']} ({form['pattern']})...")
                detail = deep_warm(page, item, form, opts, deep_writer)
                dfh.flush()
        results.append((secs, item, status))
        writer.writerow([datetime.now().isoformat(timespec="seconds"), item["module"], item["label"],
                         item["mi"], secs, status, detail])
        fh.flush()
        log(f"{len(results):4} {status:7} {secs:6.1f}s  {item['module']} / {item['label']} ({item['mi']})"
            + (f"  {detail}" if detail else ""))
        try:
            page.keyboard.press("Escape")  # close stray dialogs
        except PlaywrightError:
            pass
        page.wait_for_timeout(int(rng.uniform(lo, hi) * 1000))

    try:
        for item in includes:
            visit(item, not args.no_deep)
        end_at = time.time() + args.duration * 60 if args.duration else None
        i = 0
        while order:
            if end_at:
                if time.time() >= end_at:
                    break
                if i >= len(order):  # duration mode: start over in a new random order
                    rng.shuffle(order)
                    i = 0
            elif i >= len(order):
                break
            visit(order[i], False)
            i += 1
    finally:
        fh.close()
        dfh.close()
    return results


def summary(results):
    if not results:
        log("Nothing opened.")
        return
    times = [r[0] for r in results]
    by_status = {}
    for _, _, s in results:
        by_status[s] = by_status.get(s, 0) + 1
    log(f"Opened {len(results)} screens  avg {statistics.mean(times):.1f}s  "
        f"median {statistics.median(times):.1f}s  status {by_status}")
    print("Slowest 10:")
    for secs, item, status in sorted(results, key=lambda r: -r[0])[:10]:
        print(f"  {secs:6.1f}s  {status:7} {item['module']} / {item['label']} ({item['mi']})")


# ---------------------------------------------------------------- main

def resolve_includes(includes, menu_items):
    """Add module/label from the menu cache and drop entries the cache knows as non-display items."""
    by_mi = {it["mi"].lower(): it for it in menu_items}
    result = []
    for inc in includes:
        known = by_mi.get(inc["mi"].lower())
        if known and known.get("mt", "display") != "display":
            log(f"Skipping include {inc['mi']}: only display menu items are supported "
                f"(menu cache says '{known['mt']}').")
            continue
        result.append({"module": known["module"] if known else "(include)",
                       "label": known["label"] if known else inc["mi"], **inc})
    return result


def main():
    ap = argparse.ArgumentParser(description="Warm up D365 F&O by opening menu screens at random.")
    ap.add_argument("--headed", action="store_true", help="show the browser (first sign-in / debugging)")
    ap.add_argument("--refresh", action="store_true", help="re-harvest the navigation pane into menu_items.json")
    ap.add_argument("--inspect", action="store_true", help="print nav-pane DOM hints and exit")
    ap.add_argument("--count", type=int, help="number of screens to open (default: all)")
    ap.add_argument("--duration", type=float, help="keep going for N minutes (loops over the list)")
    ap.add_argument("--delay", help="random pause between screens in seconds, e.g. 2-5")
    ap.add_argument("--company", help="legal entity (overrides config)")
    ap.add_argument("--modules", help="comma-separated module names to include")
    ap.add_argument("--seed", type=int, help="random seed for a reproducible order")
    ap.add_argument("--mi", help="comma-separated display menu items to run first (added to include_mi)")
    ap.add_argument("--include-only", action="store_true", help="open only the include_mi / --mi pages")
    ap.add_argument("--no-deep", action="store_true", help="open include pages without deep warmup")
    args = ap.parse_args()

    cfg = load_config()
    if args.company:
        cfg["company"] = args.company
    includes = cfg["include_mi"] + (parse_include(m.strip() for m in args.mi.split(",") if m.strip())
                                    if args.mi else [])
    include_only = args.include_only or cfg["include_only"]
    if include_only and not includes:
        log("include_only is set but include_mi is empty.")
        return EXIT_FAIL

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel=cfg["browser_channel"] or None,
            headless=not args.headed,
            viewport={"width": 1600, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            ensure_logged_in(page, cfg, args.headed)
            if args.inspect:
                open_nav_pane(page)
                inspect(page)
                return EXIT_OK

            if args.refresh or (not MENU_FILE.exists() and not include_only):
                items = harvest_menu_items(page, cfg)
            elif MENU_FILE.exists():
                items = json.loads(MENU_FILE.read_text(encoding="utf-8"))
                log(f"Loaded {len(items)} menu items from {MENU_FILE.name}")
            else:
                items = []

            includes = resolve_includes(includes, items)
            include_names = {it["mi"].lower() for it in includes}
            skip_types = {t.lower() for t in cfg["skip_menu_item_types"]}
            wanted = {m.strip().lower() for m in args.modules.split(",")} if args.modules else None
            items = [] if include_only else [
                it for it in items
                if it.get("mt", "display") not in skip_types
                and it["mi"] not in cfg["exclude_mi"]
                and it["module"] not in cfg["exclude_modules"]
                and it["mi"].lower() not in include_names
                and (wanted is None or it["module"].lower() in wanted)
            ]
            if not items and not includes:
                log("No menu items to open after filtering.")
                return EXIT_FAIL
            log(f"Warming {len(includes)} include screens and "
                f"{min(args.count or len(items), len(items))} of {len(items)} random screens "
                f"in company {cfg['company']}")
            summary(warm(page, cfg, includes, items, args))
            return EXIT_OK
        finally:
            ctx.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("Interrupted.")
        sys.exit(EXIT_FAIL)
    except Exception as e:  # noqa: BLE001
        log(f"FAILED: {e}")
        sys.exit(EXIT_FAIL)
