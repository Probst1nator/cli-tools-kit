# Changelog

All notable changes to cli-tools-kit. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). From 1.0.0 on the
project follows [Semantic Versioning](https://semver.org/); see README §
Stability for what counts as the public surface.

## [Unreleased]

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
