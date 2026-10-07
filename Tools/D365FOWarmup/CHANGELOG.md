# Changelog

All notable changes to D365FO Warmup are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the tool uses [semantic versioning](https://semver.org/).

## [1.1.0] - 2026-10-07

### Added
- **Include list** (`include_mi` in `config.json`, or `--mi A,B`): screens that always run first, in the listed order, whatever `--count`, `--duration` or `--modules` say. An entry is a menu item name, or an object with `mi`, `company`, `deep`, `filters` and `max_combinations`.
- `--include-only` (or `"include_only": true`) runs only the include list. No menu harvest is needed.
- **Deep warmup** on include-list screens:
  - expands every collapsed FastTab;
  - opens every tab and sub-tab, including vertical tabs on parameter pages;
  - sorts every sortable grid column ascending and descending;
  - with `walk_rows`, moves row by row through the main grid or list (for example the list on the left of a list/details page). On each row it warms the detail tabs and grids again.
- Deep warmup options in `deep`, per page layout in `deep_by_pattern` (for example `SimpleListDetails` or `TableOfContents`), and per include entry.
- **Filter combinations** (`filters` on an include entry): for each combination of filter values, the deep warmup is run again. Values come from a list, from all options of a combo box (`"all"`), or from all rows of a lookup (`"lookup"`). For example, Location directives (`WHSLocDirTable`) can be warmed for every work order type × warehouse.
- `deep_log.csv`: one line per deep warmup action (filter, FastTab, tab, sort, row) with its time and status.
- `--no-deep` opens include-list screens without the deep warmup.
- New selector keys for the deep warmup and filters, which can be overridden in `selectors`.
- `CHANGELOG.md`.

### Changed
- `form_ready` no longer matches `.rootContent`.
- The README now documents every configuration setting with its default. It also has example configurations and run time guidance.

### Fixed
- An unknown or inaccessible menu item was logged as `ok`, because the error box also matched the `form_ready` selector. It's now logged as `error` with F&O's message.
- A screen that loads without a form is now logged as `noform`.

### Safety
- Only display menu items can be included. Other types are rejected when the config loads, and so are entries the menu cache lists as action or output items.
- The deep warmup never clicks toolbar buttons, links or grid cells. It selects rows with the Down arrow key, so a hyperlink cell can't open another screen.
- It only types into the filter controls you configure, and only on forms that open in view mode. It stops if the form leaves view mode.
- It doesn't walk rows on forms that open in edit mode, unless `walk_rows_in_edit_mode` is set.
- FactBoxes and action pane tabs are skipped.

## [1.0.0] - 2026-09-24

### Added
- First version: reads the navigation pane into `menu_items.json` and opens display menu items in random order.
- Reuses an Entra ID sign-in from a persistent browser profile, so runs can be headless.
- `--count`, `--duration`, `--delay`, `--company`, `--modules`, `--seed`, `--refresh`, `--headed` and `--inspect`.
- `warmup_log.csv` with the load time of each screen, and a summary of the slowest screens.

[1.1.0]: https://github.com/MZade/d365fo-code-snippets/commits/master/Tools/D365FOWarmup
[1.0.0]: https://github.com/MZade/d365fo-code-snippets/commit/dba470a
