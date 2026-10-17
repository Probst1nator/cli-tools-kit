# Changelog

All notable changes to cli-tools-kit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From 1.0.0 on the
project follows [Semantic Versioning](https://semver.org/); see README §
Stability for what counts as the public surface.

## [Unreleased]

### Changed
- The grouping runs each tool's `--advertise` once per build, up to 16 at a
  time, as discovery does. Before, each tier probed again, and the capability
  and embedding tiers probed one tool at a time. New
  `taxonomy.corpus.tool_advertisements()`; `tool_blurbs`, `tool_capabilities`
  and `tool_documents` take its result.
- The window's update badge counts newer versions as well as stale shortcuts.
  Its tooltip lists both, and when nothing is pending it says when versions
  were last checked online and what that check leaves out. A click refreshes
  the shortcuts and checks versions online at once, outside the once-a-day
  limit. The upgrade strip at the top stays the place to upgrade. New
  `upgrade.last_checked()`.

### Fixed
- Without gsettings, the icon theme lookup took the first `Theme=` line of
  KDE's `kdeglobals`, which can be the sound theme (`[Sounds]`). It now reads
  only the `[Icons]` group.
- The window's right-hand columns could be cut off at startup when the tool
  table was wider than the window. The table kept the width it was laid out
  at while the window was hidden. It now stays out of the window until the
  window is sized, which also saves laying it out and drawing it twice.
- A tool's name line (name, tag pills, domain) cannot wrap, and the window's
  width did not count it, so long names lost their pills and domain. The
  window now grows until the longest name line fits, by at most 500 px of
  text width.

## [1.6.0] - 2026-10-17

### Added
- `--startup-time [RUNS]`: starts the window RUNS times as new processes,
  each stopping at the first drawn frame, and prints how long each startup
  phase took (imports, discovery and its `--advertise` probes, `Tk()`, row
  building with icon loading, layout, first draw). The measured runs skip the
  orphan warning, the startup auto-update and the upgrade check, so they
  rewrite no shortcut and upgrade nothing. Action flags such as `--apply` and
  `--refresh`, which every run would repeat, and `--tui` are refused with it.
  Module `cli_tools_kit.startup_timing`.
- An MCP column. A tool that advertises `mcp_name` and `mcp_command` (the argv
  that starts its stdio server) gets an MCP checkbox, and the installer
  registers that command with each agent harness on the host. Built in:
  Claude Code (`claude mcp add-json` / `remove`, user scope) and opencode
  (`opencode mcp add`; removing edits its JSONC config and keeps the
  comments, and a symlinked config stays a symlink). Wrappers pass others with
  `run(mcp_targets=[...])`. A box that starts ticked adds no new harness on
  Apply; unticking removes the server from every harness that has it; a box
  that starts unticked and stays so changes nothing, also when a config could
  not be read. The text screen follows the same rules, and its MCP target line
  chooses where a newly ticked server is added. `--apply` takes
  `--mcp-target KEYS`; `--list` and `--uninstall-all` cover the servers.
  `testing.validate_advertise` checks that `mcp_command` holds non-empty
  strings and absolute paths. Module `cli_tools_kit.mcp_servers`.
- Skills for opencode: `tui_installer.opencode_target()`. opencode reads
  `~/.claude/skills`, so a skill there counts as installed for it; otherwise
  it gets its own copy under `~/.config/opencode/skills`, which is written
  again on the next Apply once a `.py` or `.md` file of the tool changed.
  Removing the skill fails with a message while opencode still finds it in
  `~/.claude/skills` or `~/.agents/skills`. `SkillTarget` has a new optional
  `available` field. An exception from a skill target counts as an error of
  that one step instead of ending the Apply.
- `--list` shows the skill of every tool that advertises `skill_name`: the
  keys of the skill targets that have it (`claude`, `opencode`, a wrapper's
  own), "update available" when one is stale, or "not installed".

### Changed
- The window's Skill column now uses the built-in Claude Code and opencode
  targets instead of only `~/.claude/skills`, each where the host has the
  harness. A wrapper's own `skill_targets` (such as AutomatedAlchemy's
  fauclaude target) still apply on the text screen only.
- `run(skill_targets=[])` now means no skill targets, as `mcp_targets=[]`
  means no harnesses. Before, an empty list meant the built-in targets.
- Discovery keeps each tool's `--advertise` answer in
  `<cache dir>/advertise-cache.json` and probes a tool again only when a file
  in its directory, the installed copy of its skill, the kit version or the
  Python interpreter changed. In `data/`, `logs/`, `cache/` and the
  virtualenv and cache directories only their existence counts, so a tool
  that wrote its state there since the last start is not probed again. On a
  41-tool tree discovery takes 0.01 s instead of 0.25 s when nothing changed,
  plus the time of one probe (0.1 to 0.3 s) when some tools have to be probed
  again. Symlinked directories are followed. Code a tool imports from outside
  its own directory is not watched; `--refresh` probes every tool again.

### Fixed
- `testing.validate_advertise` rejected the documented plugin record: the tag
  `Plugin` was unknown and it asked for an `alias`. It now accepts
  `claude_plugin` and `claude_marketplace`, and reports a plugin without its
  marketplace.
- The window's status line was plain blue (red on errors), which was hard to
  read on the dark themes. It now uses the theme's success and error colours.
- A click on "✓ Up to date" reported "Done: 1 updated" every time, because the
  installer's own shortcut was counted whether or not it changed. It now
  counts only when the file was created or changed, and a pass that changes
  nothing says "Already up to date, nothing changed".
- Hovering "✓ Up to date" shows a tooltip that says what the check covers.
  Before, the tooltip appeared only when updates were pending.
- A summary with errors ("Done: …, 2 errors") was logged with a `warning` tag
  that had no colour, so it showed as plain text. It now shows in the
  theme's warning colour.
- The window took 0.2 to 0.5 s of its start, depending on the host's load, to
  find the tool icons: every icon ran `gsettings` for the icon theme and tried
  up to 2,600 file paths, most of them in directories that do not exist. The
  theme is now read once and missing directories are remembered until the
  next Apply or theme change, which brings it to 0.03 s for 37 icons. The
  lookup order and its results are unchanged.
- The tool grouping never found `GEMINI_API_KEY` in the grouped tree's `.env`.
  It looked two directories above its own module, which for an installed kit
  is `site-packages`, so it fell back to a local model or the capability
  tier. `build_groups` now reads that one variable from `<root>/.env` when
  the environment has none. It keeps the key inside the kit, not in
  `os.environ`, so the tools the installer starts do not inherit it, and it
  leaves the file's other variables alone.

## [1.5.1] - 2026-10-15

### Fixed
- The window took about 20 s to open for a 41-tool tree. While it was sized,
  every description re-wrapped itself on each layout change, which changed the
  layout again: 3482 re-wraps for 50 rows, 14 s. The re-wrapping now waits
  until the window is sized, as it already did for the second layout pass.

### Changed
- The `claude plugin list --json` listing is also saved in the identity's
  cache directory (`claude-plugins.json`), so a new start of the installer
  does not call `claude` (1.4 s) until Claude Code rewrites its plugin files
  or is updated.
- `gui_installer` loads `_shared.gemini` when the icon dialog asks for prompt
  variations, not at import (about 1.5 s).

## [1.5.0] - 2026-10-08

### Added
- Claude Code plugins as tools. A tool advertises `claude_plugin`
  (`name@marketplace`) and `claude_marketplace` (`owner/repo`, URL or path),
  and its `--install`/`--remove` call `cli_tools_kit.plugins.main`. The
  installer counts the row as installed when `claude plugin list --json` shows
  the plugin enabled in the user scope; the listing is cached until Claude Code
  rewrites its plugin files. Installing a plugin disables the other enabled
  plugins of the same name, and `--apply all` leaves plugin rows out. The login
  check skips plugin rows, since every `claude` call goes online. New tag
  `Plugin`. See PROTOCOL.md § Claude Code plugins.
- `run(plugin_targets=[...])` (`plugins.PluginTarget`) lists each plugin once
  per Claude Code config directory, for a wrapper whose users also run Claude
  Code under another `CLAUDE_CONFIG_DIR`. A directory that is not set up yet
  shows nothing installed and refuses installs.

## [1.4.0] - 2026-10-07

### Added
- `--check`, the login check, removes orphaned shortcuts and aliases: those
  whose tool's `main.py` is gone because the tool was deleted, archived or
  moved out of the tree. Before, only the window's start, `--cleanup --yes` and
  `--update-all` did, so a host where nobody opened the installer kept them. It
  runs after the shortcut reconcile, so a tool that only moved keeps its
  rewritten shortcut. It skips an orphan whose tool's parent directory is
  missing too (a whole tree absent at login), and it does nothing when
  `check_reconcile_shortcuts` is `False`.

### Changed
- `--update-all` no longer runs pip. It reinstalls every installed tool with
  `skip_deps=True`, as the `--check` reconcile already did, so it only refreshes
  shortcuts. Dependencies are installed by `--install`, `--upgrade` and the
  GUI's "Reinstall deps". A wrapper whose install hook builds missing tool venvs
  (WW3) no longer does that on `--update-all`.

### Fixed
- Text screen and `--apply`: a skill landed in `~/.claude/skills` even when the
  claude skill target was not chosen (only a wrapper's own target, or `none`).
  PROTOCOL.md lets a tool's `--install` write its skill there by itself, and
  the Apply step did not undo that. It now runs the tool's `--uninstall-skill`
  after the install when the skill directory did not exist before and no step
  writes the claude target for that tool.
- A cron autostart line broke when the interpreter or the tool's script path
  contained a space: cron hands the line to `/bin/sh`, which split the path.
  Both are now quoted with `shlex.quote`. A plain path stays unquoted, so lines
  already in a crontab still match.
- `ToolInstaller.install_dependencies` ran `pip install -r` on a
  `requirements.txt` that held only comments and blank lines, and pip warned
  about an empty file in the install log. It now skips pip for such a file.
- A tool installed by an installer with `LEGACY_IDENTITY` wrote its alias to
  `~/.probable_work_aliases` instead of `~/.tools_aliases`, so the installer
  listed it as not installed. `InstallerIdentity.from_env()` rebuilt the
  identity from the slug `probable.work` alone, which loses every name the
  legacy identity overrides (alias file, config and cache dir, the `~/.bashrc`
  comment label). It now returns `LEGACY_IDENTITY` for that slug.

## [1.3.0] - 2026-09-29

### Changed
- The window applies pending local updates (drifted shortcuts and aliases, no
  pip, no network) at every start. It used to do that only with "Auto-update on
  startup" ticked. The Operation Log opens only when something was applied.

### Removed
- The "Auto-update on startup" checkbox, the "Refresh Status" button and the
  separator that stood before it. `settings.get_auto_update_on_startup` and
  `settings.set_auto_update_on_startup` are gone with the checkbox; the
  `auto_update_on_startup` key in an existing settings file is ignored.

## [1.2.0] - 2026-09-29

### Removed
- The "Check on login" checkbox in the window's footer. The login check itself
  stays: `--enable-autostart-check` and `--disable-autostart-check` switch it,
  and an entry that is already on keeps running `--check` at login.

## [1.1.1] - 2026-09-29

### Fixed
- Upgrade: after pulling the installer's own checkout, the kit step ran
  `pip install --upgrade -r requirements.txt` even when that file still pinned
  an older kit exactly, so the upgrade downgraded the kit it ran on. It now asks
  pip first and leaves the kit alone unless the pin allows a newer version.

## [1.1.0] - 2026-09-29

### Added
- Upgrades: when the window or the text screen opens, the installer checks in
  the background whether its own checkout or a tool repo it cloned is behind its
  upstream, and whether pip would install a newer cli-tools-kit within the
  installer's pin. A strip above the table (the `u` key on the text screen)
  pulls, upgrades the kit, reinstalls the tools of each pulled repo and starts
  the installer again. `--upgrade` does the same headless. The network is used
  at most once a day; `--check` still never touches it. New module `upgrade`,
  new `run(upgrade_repos=...)`, filled by `sources.run_installer`, and a
  `pullable` list argument on `sources.resolve_sources`.

### Fixed
- The window opened narrower than its footer: it was sized from the tool table
  alone and capped at 800 px, so "Reinstall deps", "Refresh Status" and "Apply
  Changes" were cut off. It now takes the width every row needs, within 90% of
  the monitor, and cannot be dragged narrower than the footer. The height is
  measured with the descriptions wrapped to that width instead of a guessed
  230 px for the fixed rows.
- The table's right-hand columns had fixed pixel widths, so "✗ Not installed"
  was cut off at display scalings above 96 dpi. They now grow with the scaling
  and fit their texts.
- "Apply Changes" is packed first, so a window narrower than the footer loses
  the left-hand controls before it.

## [1.0.0] - 2026-09-27

The first stable release. From here the public surface in README § Stability
follows Semantic Versioning, so consumers pin `cli-tools-kit>=1.0,<2`.

### Added
- `run(hooks=InstallHooks(...))`, also through `sources.run_installer`: the
  supported way for a wrapper to replace how one tool is installed, removed or
  given its skill. Assigning `gui_installer.install_tool` and friends still works.
- `CronInstaller.remove_unmarked(match)`: drop hand-written cron lines a tool is
  migrating away from, never touching a tagged line.
- `ToolInstaller.variants(desktop_file=None)`: the public name of `_select`.
- `host.harden_stdio()`, `host.child_env()` and `host.symbol()`.
- `cli_tools_kit.testing.assert_advertises(script)`: a tool's own test that its
  `--advertise` answer follows PROTOCOL.md.
- CI on GitHub Actions: ruff, pytest on Linux and Windows for Python 3.10 to
  3.13, a GUI smoke test under xvfb, and an install from the built wheel.
- `--apply` also takes names separated by spaces: `--apply a b`.

### Changed
- The engine is split out of `gui_installer` into `state`, `settings`,
  `discovery`, `install`, `sweep`, `autostart`, `icons` and `cli`.
  `gui_installer` keeps the window and forwards every moved name, so reading
  or assigning `gui_installer.IDENTITY`, `gui_installer.install_tool` and the
  rest works as before.

### Fixed
- `--update-all` and `--cleanup --yes` exited 0 when a tool or an orphan
  failed. They now exit 1, like `--apply`.
- A tool's skill was written even when the tool itself failed to install in
  the same run. It is now skipped, with a line saying so.
- Enabling a cron autostart while `crontab -l` failed (permissions, a locked
  spool) replaced the whole crontab with that one line. It now fails instead.
- Windows: `--list` crashed with UnicodeEncodeError once a tool was installed,
  whenever its output went to a pipe or a file (cp1252 has no ✓). It now prints
  `[x]` there, and `[✓]` where the output can carry it.
- Windows: a tool printing an emoji died in its own `print` while the installer
  captured its output. Tool subprocesses now write UTF-8 and the kit reads UTF-8.

## [0.8.4] - 2026-09-27

### Fixed
- Reverted the shared tool venvs that 0.8.3 shipped. With them, `--install` ran
  in a venv built from the tool's `requirements.txt` alone, without
  cli-tools-kit, and failed with ModuleNotFoundError for every tool with
  dependencies. They return in a later release once the venv also gets the kit.

## [0.8.3] - 2026-09-27 [YANKED]

Yanked: tool installs fail with ModuleNotFoundError. Use 0.8.4.

### Fixed
- The GUI crashed at startup with `NameError: name 'tool' is not defined` for a
  grouped tool (several variants on one script) with an Auto-Start box.
  Introduced in 0.8.0.
- A failed AI icon generation raised NameError instead of showing its error.

### Changed
- Each tool ran in a venv shared per upstream repo (see 0.8.4).

## [0.8.2] - 2026-09-17

### Added
- The login update check autostarts on Windows too, as a Startup-folder shortcut.

## [0.8.1] - 2026-09-17

### Fixed
- Emoji icons render on Windows and macOS and are no longer cut off; theme and
  accent buttons draw their emoji as an image.

## [0.8.0] - 2026-09-16 (not on PyPI)

### Added
- Conditional autostart: a tool advertises `autostart_conditions`
  (`time_window`, `network`), the user sets the values through the ⚙ beside
  its Auto-Start box, and `cli-tools-kit-autostart-gate` checks them at login.

## [0.7.1] - 2026-09-16

### Fixed
- Local path pins in `installer.local.toml` apply to org-derived sources too.

## [0.7.0] - 2026-09-16 (not on PyPI)

### Added
- Org sources in `installer.toml`: `org`, `topic`, `include`, `exclude`, with a
  one-day cache and fallbacks.

## [0.6.4] - 2026-09-16

### Fixed
- The manager's Windows shortcut carries its icon; the install location is
  remembered on Windows.

## [0.6.3] - 2026-09-10

### Fixed
- The window icon is passed in several sizes.

## [0.6.2] - 2026-09-10

### Fixed
- The desktop icon applies to the running window.

## [0.6.1] - 2026-09-10

### Fixed
- The installer asks where the tools go; the table keeps its width across a
  theme switch.

## [0.6.0] - 2026-09-10

### Changed
- Renamed from `cli-tool-kit` / `cli_tool_kit` to `cli-tools-kit` /
  `cli_tools_kit`, the first release on PyPI. Wider default discovery.
