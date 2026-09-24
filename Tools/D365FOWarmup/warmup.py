"""
D365 Finance & Operations warmup crawler.

Harvests the menu items reachable from the navigation pane (Modules) and opens
them in random order so the AOS caches are warm before users arrive.

Usage:
    python warmup.py --headed --refresh --count 5   # first run: sign in, build menu cache
    python warmup.py --count 200                    # headless, reuses saved session
    python warmup.py --duration 30 --delay 1-3      # run for 30 minutes
    python warmup.py --headed --inspect             # print nav-pane DOM hints (selector debugging)

Exit codes: 0 ok, 1 unexpected failure, 2 login required (run once with --headed).
"""
import argparse
import csv
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
    # Element present once a form has rendered
    "form_ready": ["[data-dyn-role='Form']", ".rootContent", "form[data-dyn-form-name]"],
}

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
    return cfg


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


def wait_form_loaded(page, cfg):
    """Wait for a form element, then for the F&O client to stay idle for 1 second."""
    deadline = time.time() + cfg["form_timeout_sec"]
    first_visible(page, "form_ready", timeout_ms=cfg["form_timeout_sec"] * 1000)
    quiet_since = None
    while time.time() < deadline:
        if is_busy(page):
            quiet_since = None
        elif quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= 1.0:
            return True
        page.wait_for_timeout(200)
    return False


def page_error_text(page):
    loc = page.locator(".messageBar-error:visible, .notification-error:visible").first
    try:
        return loc.inner_text(timeout=500).strip().splitlines()[0][:150] if loc.count() else ""
    except PlaywrightError:
        return ""


def warm(page, cfg, items, args):
    lo, hi = (float(x) for x in (args.delay or cfg["delay"]).split("-"))
    rng = random.Random(args.seed)
    order = items[:]
    rng.shuffle(order)
    if args.count:
        order = order[: args.count]
    end_at = time.time() + args.duration * 60 if args.duration else None

    new_file = not LOG_FILE.exists()
    results = []
    with LOG_FILE.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        if new_file:
            writer.writerow(["timestamp", "module", "label", "mi", "seconds", "status", "detail"])
        i = 0
        while True:
            if end_at:
                if time.time() >= end_at:
                    break
                if i >= len(order):  # duration mode: start over in a new random order
                    rng.shuffle(order)
                    i = 0
            elif i >= len(order):
                break
            item = order[i]
            i += 1
            url = f"{cfg['base_url']}/?cmp={cfg['company']}&mi={item['mi']}"
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
            except PlaywrightTimeout:
                status = "timeout"
            except PlaywrightError as e:
                status, detail = "error", str(e).splitlines()[0][:150]
            secs = round(time.time() - t0, 2)
            results.append((secs, item, status))
            writer.writerow([datetime.now().isoformat(timespec="seconds"), item["module"], item["label"],
                             item["mi"], secs, status, detail])
            fh.flush()
            log(f"{len(results):4} {status:7} {secs:6.1f}s  {item['module']} / {item['label']} ({item['mi']})")
            try:
                page.keyboard.press("Escape")  # close stray dialogs
            except PlaywrightError:
                pass
            page.wait_for_timeout(int(rng.uniform(lo, hi) * 1000))
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
    args = ap.parse_args()

    cfg = load_config()
    if args.company:
        cfg["company"] = args.company

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

            if args.refresh or not MENU_FILE.exists():
                items = harvest_menu_items(page, cfg)
            else:
                items = json.loads(MENU_FILE.read_text(encoding="utf-8"))
                log(f"Loaded {len(items)} menu items from {MENU_FILE.name}")

            skip_types = {t.lower() for t in cfg["skip_menu_item_types"]}
            wanted = {m.strip().lower() for m in args.modules.split(",")} if args.modules else None
            items = [
                it for it in items
                if it.get("mt", "display") not in skip_types
                and it["mi"] not in cfg["exclude_mi"]
                and it["module"] not in cfg["exclude_modules"]
                and (wanted is None or it["module"].lower() in wanted)
            ]
            if not items:
                log("No menu items to open after filtering.")
                return EXIT_FAIL
            log(f"Warming {min(args.count or len(items), len(items))} of {len(items)} screens "
                f"in company {cfg['company']}")
            summary(warm(page, cfg, items, args))
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
