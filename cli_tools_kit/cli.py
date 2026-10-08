"""The command line: ``main()``, ``run()`` and the headless ``--...`` actions."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
from typing import Callable, List, Optional, Sequence

from . import autostart
from . import discovery
from . import host
from . import install
from . import state
from . import sweep
from .identity import InstallerIdentity


def _file_sig(path: str):
    """sha256 of a file's bytes, or None if absent — for before/after change-detection."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def _notify_send(summary: str, body: str = "") -> None:
    """Best-effort KDE/GNOME desktop notification; silently no-ops without notify-send."""
    if host.IS_WINDOWS:
        return  # no notify-send here
    try:
        subprocess.run(
            ["notify-send", "-a", state.NOTIFY_APP, "-i", "system-software-update",
             "-t", "15000", summary] + ([body] if body else []),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


# ================= CLI FUNCTIONS =================

def _self_shortcut_path() -> str:
    """The manager's own shortcut: a .desktop file, or a .lnk on Windows.

    Windows shows the file name in the Start Menu, where freedesktop reads the
    Name= line inside the file, so the .lnk is named after SELF_DESKTOP_NAME.
    Characters a file name cannot hold become spaces, and a name that is empty
    or all-punctuation falls back to the desktop file's stem.
    """
    if host.IS_WINDOWS:
        stem = "".join(" " if c in '<>:"/\\|?*' else c
                       for c in state.SELF_DESKTOP_NAME).strip(" .")
        stem = " ".join(stem.split()) or os.path.splitext(state.SELF_DESKTOP_FILE)[0]
        return os.path.join(state.APPS_DIR, stem + ".lnk")
    return os.path.join(state.APPS_DIR, state.SELF_DESKTOP_FILE)


def _windows_icon(icon: str) -> Optional[str]:
    """The .ico Windows can draw for `icon`, or None.

    A .lnk renders only .ico/.exe/.dll; the shell stores a .png path without
    complaint and then draws nothing. Consumers configure one icon, normally a
    .png for freedesktop, so take a sibling .ico of the same stem when there is
    one and otherwise leave the shortcut on its default picture.
    """
    if not icon:
        return None
    for candidate in (icon, os.path.splitext(icon)[0] + ".ico"):
        if candidate.lower().endswith(".ico") and os.path.isfile(candidate):
            return candidate
    return None


def cli_install_self(quiet: bool = False) -> tuple[bool, str]:
    """Install the manager's own desktop shortcut. Returns (success, message)."""
    try:
        install.ensure_apps_dir()
        python_exec = sys.executable
        script_path = state.ENTRY_SCRIPT
        if host.IS_WINDOWS:
            lnk_path = _self_shortcut_path()
            ok = host.write_shortcut(lnk_path, python_exec, f'"{script_path}"',
                                     icon=_windows_icon(state.SELF_DESKTOP_ICON),
                                     workdir=state.ROOT_DIR)
            if not ok:
                return False, f"Could not write {lnk_path}"
            # Before 0.6.4 the .lnk was named after the desktop file's stem.
            # Drop that one, or the Start Menu keeps both.
            legacy = os.path.join(state.APPS_DIR,
                                  os.path.splitext(state.SELF_DESKTOP_FILE)[0] + ".lnk")
            if legacy != lnk_path and os.path.isfile(legacy):
                try:
                    os.remove(legacy)
                except OSError:
                    pass
            if not quiet:
                print(f"Installed: {lnk_path}")
            return True, lnk_path
        desktop_path = os.path.join(state.APPS_DIR, state.SELF_DESKTOP_FILE)

        content = f"""[Desktop Entry]
Type=Application
Name={state.SELF_DESKTOP_NAME}
Comment=Install and manage tool shortcuts
Exec={python_exec} "{script_path}"
Path={state.ROOT_DIR}
Icon={state.SELF_DESKTOP_ICON}
Terminal=false
Categories=Settings;Utility;
Keywords={state.IDENTITY.marker_token};ai;tool;installer-self;
StartupNotify=true
StartupWMClass={state.WM_CLASS}
"""
        with open(desktop_path, "w") as f: f.write(content)
        # Non-executable on purpose — see ToolInstaller._install_shortcut.
        os.chmod(
            desktop_path,
            os.stat(desktop_path).st_mode
            & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
        )
        install.refresh_desktop_database()
        if not quiet:
            print(f"Installed: {desktop_path}")
        return True, desktop_path
    except Exception as e:
        if not quiet:
            print(f"Error: {e}")
        return False, str(e)


def cli_uninstall_self():
    desktop_path = _self_shortcut_path()
    if os.path.exists(desktop_path):
        os.remove(desktop_path)
        install.refresh_desktop_database()
        print(f"Removed: {desktop_path}")
    else:
        print("Installer shortcut not found.")


def cli_uninstall_all(tools: List[discovery.ToolEntry]):
    removed = 0
    print(f"\nUninstalling all {len(tools)} tools...")
    for tool in tools:
        if install.is_installed(tool):
            success, output = install.remove_tool(tool)
            if success:
                print(f"  [RM] {tool.name}")
                removed += 1
            else:
                print(f"  [ERR] {tool.name}: {output}")

    install.refresh_desktop_database()
    print(f"\nDone. Removed {removed} shortcuts.\n")


def cli_cleanup(dry_run: bool = False) -> int:
    """Find and remove orphaned desktop files and aliases.

    Args:
        dry_run: If True, only list orphans without removing them.

    Returns the number of orphans that could not be removed.
    """
    orphan_desktops = sweep.find_orphan_desktop_files()
    orphan_aliases = sweep.find_orphan_aliases()

    if not orphan_desktops and not orphan_aliases:
        print("\nNo orphaned shortcuts found. All clean!\n")
        return 0

    print(f"\nFound {len(orphan_desktops)} orphaned desktop file(s), {len(orphan_aliases)} orphaned alias(es):\n")

    if orphan_desktops:
        print("Desktop files:")
        for orphan in orphan_desktops:
            print(f"  • {orphan.name}")
            print(f"    File: {orphan.filename}")
            print(f"    Missing: {orphan.tool_path}")

    if orphan_aliases:
        print("\nAliases:")
        for orphan in orphan_aliases:
            print(f"  • {orphan.name}")
            print(f"    Missing: {orphan.script_path}")

    if dry_run:
        print("\n(Dry run - no changes made. Use --cleanup --yes to remove.)\n")
        return 0

    print()
    removed = 0
    errors = 0

    for orphan in orphan_desktops:
        success, msg = sweep.remove_orphan_desktop_file(orphan)
        if success:
            print(f"  [RM] {orphan.name}")
            removed += 1
        else:
            print(f"  [ERR] {msg}")
            errors += 1

    for orphan in orphan_aliases:
        success, msg = sweep.remove_orphan_alias(orphan)
        if success:
            print(f"  [RM] alias '{orphan.name}'")
            removed += 1
        else:
            print(f"  [ERR] {msg}")
            errors += 1

    if removed:
        install.refresh_desktop_database()

    summary = f"\nDone. Removed {removed} orphan(s)"
    if errors:
        summary += f", {errors} error(s)"
    print(summary + ".\n")
    return errors


def cli_upgrade(tools: List[discovery.ToolEntry]) -> int:
    """Check for newer versions now and upgrade what is behind. Returns 0 or 1."""
    from . import upgrade  # noqa: PLC0415 — imports sources, only this path needs it
    print("\nChecking for upgrades...")
    items = upgrade.check(force=True)
    if not items:
        print("Everything is up to date.\n")
        return 0
    for item in items:
        print(f"  {item.label()}")
    print()
    result = upgrade.upgrade(items, tools, lambda msg, tag="info": print(msg))
    print(f"\nDone{', with errors' if result['errors'] else ''}.\n")
    return 1 if result["errors"] else 0


def cli_update_all(tools: List[discovery.ToolEntry]) -> int:
    """Sync: clean up orphans, then reinstall manager and all installed tool shortcuts.

    Runs no pip: tools are reinstalled with skip_deps=True, like the --check reconcile.
    Returns the number of steps that failed.
    """
    removed = 0
    updated = 0
    errors = 0

    # First, clean up orphans
    orphan_desktops = sweep.find_orphan_desktop_files()
    orphan_aliases = sweep.find_orphan_aliases()

    if orphan_desktops or orphan_aliases:
        print(f"\nCleaning up {len(orphan_desktops)} orphaned desktop file(s), {len(orphan_aliases)} alias(es)...")

        for orphan in orphan_desktops:
            success, msg = sweep.remove_orphan_desktop_file(orphan)
            if success:
                print(f"  [RM] {orphan.name}")
                removed += 1
            else:
                print(f"  [ERR] {msg}")
                errors += 1

        for orphan in orphan_aliases:
            success, msg = sweep.remove_orphan_alias(orphan)
            if success:
                print(f"  [RM] alias '{orphan.name}'")
                removed += 1
            else:
                print(f"  [ERR] {msg}")
                errors += 1

    # Collect all items to update: manager first, then installed tools
    installed_tools = [t for t in tools if install.is_installed(t)]
    all_names = ["Tools Installer"] + [t.name for t in installed_tools]
    max_name_len = max((len(name) for name in all_names), default=0)

    print(f"\nUpdating {len(all_names)} shortcuts...")

    # Update installer's own desktop file first
    label = "  Updating Tools Installer..."
    print(f"{label:<{max_name_len + 15}}", end=" ", flush=True)
    success, output = cli_install_self(quiet=True)
    if success:
        print("[OK]")
        updated += 1
    else:
        print(f"[ERR] {output}")
        errors += 1

    # Then update all installed tools
    for tool in installed_tools:
        label = f"  Updating {tool.name}..."
        print(f"{label:<{max_name_len + 15}}", end=" ", flush=True)
        success, output = install.install_tool(tool, skip_deps=True)
        if success:
            print("[OK]")
            updated += 1
        else:
            print(f"[ERR] {output}")
            errors += 1

    install.refresh_desktop_database()

    summary_parts = []
    if updated:
        summary_parts.append(f"{updated} updated")
    if removed:
        summary_parts.append(f"{removed} orphans removed")
    if errors:
        summary_parts.append(f"{errors} errors")

    summary = "\nDone. " + ", ".join(summary_parts) if summary_parts else "\nDone."
    print(summary + ".\n")
    return errors


def cli_check() -> int:
    """Login-time update check (headless; see AUTOSTART_CHECK_DESKTOP).

    Auto-applies the network-free reconciliations and notifies for the rest:

      APPLY (no pip, no network — rewritten from the source already on disk):
        • a drifted shortcut (moved .desktop Exec path / renamed alias)  -> install_tool(skip_deps=True)
        • a currently-installed SKILL.md that has drifted                 -> idempotent --install-skill
          (only touched when the skill is already present, so a skill the
           user deliberately removed is never silently re-added)
        • an orphaned shortcut or alias (its tool's main.py is gone)      -> removed, like --cleanup --yes

      NOTIFY (would touch the network or add new capability — needs a human):
        • a discovered tool that is not installed (installing it may pip)
        • an installed tool whose advertised skill is absent (new / renamed skill)

    Never raises out of a login hook — always returns 0; the human-actionable
    items go to the notification and ~/.local/log/tools-installer-check.log.
    """
    import datetime

    try:
        # run_pre=False: a login hook must never reach the network (no repo clone).
        tools = discovery.discover_tools(run_pre=False)
    except Exception as e:
        _notify_send(f"{state.NOTIFY_APP}: check failed", str(e))
        return 0

    applied: list[str] = []      # silently reconciled "<tool>: <what>"
    failed: list[str] = []       # reconciliation that errored
    new_tools: list[str] = []    # uninstalled tool -> may pip -> human decides
    new_skills: list[str] = []   # installed tool, advertised skill absent -> human decides

    for t in tools:
        if t.claude_plugin:
            continue  # no shortcut or skill to reconcile, and `claude` would go online
        shortcut_installed = install.is_installed(t)
        skill_present = bool(t.skill_name) and install._skill_installed(t.skill_name)
        # (1) drifted shortcut -> network-free refresh. Count it only if the
        #     install actually RESOLVED the drift (re-check needs_update), so a
        #     persistent/unresolvable mismatch can't be reported every login.
        #     Skipped entirely when CHECK_RECONCILE_SHORTCUTS is False (a tree
        #     whose --install has login-unsafe side effects, e.g. AutomatedAlchemy).
        if state.CHECK_RECONCILE_SHORTCUTS and shortcut_installed and install.needs_update(t):
            ok, out = install.install_tool(t, skip_deps=True)
            if not ok:
                failed.append(f"{t.name}: shortcut ({(out.splitlines() or ['failed'])[-1]})")
            elif not install.needs_update(t):
                applied.append(f"{t.name}: shortcut")
            # else: ran but drift persists -> stay silent (don't nag)
        # (2) skill reconciliation -> whenever the skill is INSTALLED (regardless
        #     of shortcut state). Count only if the file's CONTENT changed (hash
        #     before/after), independent of how a tool words its output.
        if skill_present:
            p = install._skill_md_path(t.skill_name)
            before = _file_sig(p)
            ok, out = install.install_skill_for_tool(t)   # idempotent; refresh if drifted
            if not ok:
                failed.append(f"{t.name}: skill {t.skill_name}")
            elif _file_sig(p) != before:
                applied.append(f"{t.name}: skill {t.skill_name}")
            # else current -> stay silent
        elif t.skill_name and shortcut_installed:
            new_skills.append(f"{t.skill_name} ({t.name})")  # installed tool, new skill
        # (3) genuinely new: neither shortcut nor skill present (a tool already
        #     in use via its skill is not "new", even if its shortcut install is
        #     a bashrc function the alias check can't see).
        if not shortcut_installed and not skill_present:
            new_tools.append(t.name)

    # (4) orphaned shortcut or alias -> removed. Its tool's main.py is gone, so
    #     it can start nothing. This runs after (1): a tool that only moved has
    #     its shortcut rewritten there and is no orphan here. An orphan whose
    #     tool's parent directory is gone too stays: a whole tree missing at
    #     login (an unmounted drive, a folder not synced yet) may come back.
    #     The GUI and --cleanup still remove those.
    if state.CHECK_RECONCILE_SHORTCUTS:
        for o in sweep.find_orphan_desktop_files():
            if not os.path.isdir(os.path.dirname(os.path.normpath(o.tool_path))):
                continue
            ok, msg = sweep.remove_orphan_desktop_file(o)
            (applied if ok else failed).append(f"{o.name}: {'orphan shortcut removed' if ok else msg}")
        for o in sweep.find_orphan_aliases():
            if not os.path.isdir(os.path.dirname(os.path.dirname(o.script_path))):
                continue
            ok, msg = sweep.remove_orphan_alias(o)
            (applied if ok else failed).append(f"{o.name}: {'orphan alias removed' if ok else msg}")

    if applied:
        install.refresh_desktop_database()

    # --- log (always) ---
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[{ts}] applied={len(applied)} failed={len(failed)} "
             f"new_tools={len(new_tools)} new_skills={len(new_skills)}"]
    for a in applied:    lines.append(f"  applied   {a}")
    for f_ in failed:    lines.append(f"  FAILED    {f_}")
    for n in new_tools:  lines.append(f"  new tool  {n}")
    for s in new_skills: lines.append(f"  new skill {s}")
    try:
        os.makedirs(os.path.dirname(state.CHECK_LOG), exist_ok=True)
        with open(state.CHECK_LOG, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    print("\n".join(lines))

    # --- notify only when the actionable set CHANGES (no per-login nagging) ---
    actionable = (sorted(new_tools)
                  + ["skill:" + s for s in sorted(new_skills)]
                  + ["fail:" + f for f in sorted(failed)])
    try:
        with open(state.CHECK_STATE) as fh:
            prev = json.load(fh).get("actionable", [])
    except (OSError, ValueError):
        prev = []
    try:
        os.makedirs(os.path.dirname(state.CHECK_STATE), exist_ok=True)
        with open(state.CHECK_STATE, "w") as fh:
            json.dump({"actionable": actionable, "ts": ts}, fh)
    except OSError:
        pass

    if actionable and actionable != prev:
        body = []
        if applied:
            body.append(f"Auto-applied {len(applied)} reconciliation(s).")
        if new_skills:
            body.append("New skill(s) — open the installer to add: " + ", ".join(new_skills[:6]))
        if new_tools:
            more = f" (+{len(new_tools) - 6} more)" if len(new_tools) > 6 else ""
            body.append("New tool(s) to review: " + ", ".join(new_tools[:6]) + more)
        if failed:
            body.append("Failed: " + ", ".join(failed[:4]))
        _notify_send(f"{state.NOTIFY_APP}: updates available", "\n".join(body))

    return 0


def main():
    host.harden_stdio()
    parser = argparse.ArgumentParser(description="Tools Manager")
    parser.add_argument("--install", action="store_true", help="Install manager shortcut")
    parser.add_argument("--uninstall", action="store_true", help="Remove manager shortcut")
    parser.add_argument("--uninstall-all", action="store_true", help="Remove ALL managed tool shortcuts")
    parser.add_argument("--update-all", action="store_true", help="Sync: cleanup orphans + update all installed shortcuts")
    parser.add_argument("--cleanup", action="store_true", help="Find and remove orphaned shortcuts (dry run)")
    parser.add_argument("--yes", "-y", action="store_true", help="With --cleanup: actually remove orphans")
    parser.add_argument("--list", action="store_true", help="List autodetected tools")
    parser.add_argument("--check", action="store_true",
                        help="Headless login check: auto-apply network-free reconciliations, notify for new tools")
    parser.add_argument("--enable-autostart-check", action="store_true",
                        help="Install the login update-check autostart entry")
    parser.add_argument("--disable-autostart-check", action="store_true",
                        help="Remove the login update-check autostart entry")
    parser.add_argument("--upgrade", action="store_true",
                        help="Check the installer's checkout, the tool repos it cloned and "
                             "cli-tools-kit for newer versions, and upgrade them (network).")
    parser.add_argument("--refresh", action="store_true",
                        help="Before discovery, run the configured PRE_DISCOVERY hook in refresh "
                             "mode (e.g. ff-only pull every known repo checkout). No-op without a hook.")
    parser.add_argument("--apply", metavar="NAMES", nargs="+",
                        help="Headless install: tool aliases/names, separated by commas "
                             "or spaces, or 'all'. Tools not listed are left alone. Skills "
                             "go to the targets in --skill-target.")
    parser.add_argument("--skill-target", metavar="KEYS", default="claude",
                        help="With --apply: comma-separated skill target keys "
                             "(default 'claude'; 'none' installs no skills).")
    screen = parser.add_mutually_exclusive_group()
    screen.add_argument("--tui", action="store_true",
                        help="Open the text screen (curses) instead of the tkinter window. "
                             "The default whenever no display is reachable or tkinter is missing.")
    screen.add_argument("--gui", action="store_true",
                        help="Insist on the tkinter window.")
    args = parser.parse_args()

    if args.refresh:
        state.REFRESH_REPOS = True

    if args.check:
        sys.exit(cli_check())

    if args.enable_autostart_check:
        path = autostart.enable_autostart_check()
        print(f"{host.symbol('✓', 'OK')} Login update check enabled: {path}")
        print(f"  Runs: {sys.executable} {state.ENTRY_SCRIPT} --check")
        return

    if args.disable_autostart_check:
        print(f"{host.symbol('✓', 'OK')} Login update check disabled" if autostart.disable_autostart_check()
              else "• Login update check was not enabled")
        return

    if args.uninstall:
        cli_uninstall_self()
        return

    if args.install:
        cli_install_self()
        return

    if args.cleanup:
        sys.exit(1 if cli_cleanup(dry_run=not args.yes) else 0)

    tools = discovery.discover_tools()

    if args.uninstall_all:
        cli_uninstall_all(tools)
        return

    if args.update_all:
        sys.exit(1 if cli_update_all(tools) else 0)

    if args.upgrade:
        sys.exit(cli_upgrade(tools))

    if args.apply:
        from . import tui_installer
        sys.exit(tui_installer.apply_headless(tools, ",".join(args.apply), args.skill_target,
                                              targets=state.SKILL_TARGETS))

    if args.list:
        print(f"\nDiscovered {len(tools)} tools:\n" + "="*60)
        for t in tools:
            installed = install.is_installed(t)
            stale = install.needs_update(t) if installed else False
            if stale:
                status = host.symbol("⟳", "u")  # Needs update
            elif installed:
                status = host.symbol("✓", "x")
            else:
                status = " "
            tags_str = ",".join(t.tags) if t.tags else "-"
            alias_info = f" ({t.alias})" if "Icon" not in t.tags and t.alias else ""
            if t.claude_plugin:
                alias_info = f" ({t.claude_plugin})"
            # For stale tools, show what changed
            if stale and "Icon" not in t.tags:
                old_alias = install._find_alias_for_script(t.script_path)
                alias_info = f" ({old_alias}→{t.alias})"
            print(f" [{status}] {tags_str:<12} {t.category:<12} | {t.name:<25}{alias_info}")
        return

    from . import gui_installer, tui_installer
    if tui_installer.prefer_tui(force_tui=args.tui, force_gui=args.gui,
                                have_tk=gui_installer._HAVE_TK):
        sys.exit(tui_installer.run_tui(tools, targets=state.SKILL_TARGETS,
                                       preselect=state.TUI_PRESELECT, title=state.WINDOW_TITLE))
    if not gui_installer._HAVE_TK:
        if host.IS_WINDOWS:
            sys.exit("tkinter is not available in this Python; use --tui for "
                     "the text screen.")
        sys.exit("tkinter is not installed (Debian/Ubuntu: apt install python3-tk); "
                 "use --tui for the text screen.")

    root = gui_installer.tk.Tk(className=state.WM_CLASS)
    gui_installer.InstallerApp(root, tools)
    root.mainloop()


def run(*, identity: Optional[InstallerIdentity] = None,
        root_dir: Optional[str] = None, entry_script: Optional[str] = None,
        window_title: Optional[str] = None, discovery_roots: Optional[List[str]] = None,
        discoverer: Optional[Callable] = None, prune: Optional[Sequence[str]] = None,
        group_by: Optional[str] = None,
        pre_discovery: Optional[Callable] = None,
        check_reconcile_shortcuts: Optional[bool] = None,
        skill_targets: Optional[List] = None, tui_preselect: Optional[bool] = None,
        plugin_targets: Optional[List] = None,
        autostart_check_desktop_name: Optional[str] = None,
        check_log_name: Optional[str] = None, check_state_name: Optional[str] = None,
        self_desktop_file: Optional[str] = None, self_desktop_name: Optional[str] = None,
        self_desktop_icon: Optional[str] = None,
        wm_class: Optional[str] = None, notify_app: Optional[str] = None,
        hooks: Optional[install.InstallHooks] = None,
        upgrade_repos: Optional[List] = None) -> None:
    """Configure the engine from a thin wrapper and dispatch the standard CLI/GUI.

    Every argument maps to a module-level config global; ``None`` leaves the
    default (so ``run()`` with no args reproduces the historical tools/installer
    behavior, scanning this module's directory). A wrapper that needs to keep its
    own argparse (e.g. AutomatedAlchemy) can instead set the globals directly and
    call the individual primitives (discover_tools/install_tool/cli_check/...).

    ``identity`` is the one argument a third-party organisation must pass. It
    namespaces every per-host artifact (config dir, alias file, icon cache, the
    manager's own .desktop, the WM class and the Keywords marker the orphan
    sweeper matches on) so two organisations' installers coexist. Passing none
    selects ``LEGACY_IDENTITY``, the historical first-party names.

    ``hooks`` replaces how one tool is installed, removed or given its skill,
    for a wrapper that builds a venv per tool itself. See :class:`InstallHooks`.

    ``upgrade_repos`` lists ``(name, path)`` of the tool repos the upgrade may
    pull; ``sources.run_installer`` fills it with the repos it cloned.

    ``plugin_targets`` lists the Claude Code config directories a plugin tool
    can go into (``plugins.PluginTarget``); each plugin is then offered once
    per target. Without it a plugin goes into ``~/.claude``.

    ``prune`` adds directory names the default wider walk never enters, on top
    of ``DISCOVERY_PRUNE``. It does nothing when a wrapper passes its own
    ``discoverer``.

    Standalone use (the ``cli-tool-installer`` console script) calls this with no
    args; root_dir then defaults to the current working directory.
    """

    # Identity first: it sets the whole namespace, and the individual name
    # kwargs below still win so a wrapper can override one of them.
    if identity is not None:
        state._apply_identity(identity)
    elif entry_script is None and os.environ.get(InstallerIdentity.ENV_VAR, "") == "":
        # Bare engine: no identity, no wrapper pointing at itself. Opening the
        # GUI here would claim the first-party names on this host, so offer
        # setup instead. A wrapper that deliberately wants the legacy names
        # passes identity=LEGACY_IDENTITY.
        from .onboarding import print_onboarding
        raise SystemExit(print_onboarding(root_dir if root_dir is not None else os.getcwd()))

    state.ROOT_DIR = root_dir if root_dir is not None else os.getcwd()
    if entry_script is not None:
        state.ENTRY_SCRIPT = os.path.abspath(entry_script)
    if window_title is not None:
        state.WINDOW_TITLE = window_title
    if discovery_roots is not None:
        state.DISCOVERY_ROOTS = discovery_roots
    if discoverer is not None:
        state.DISCOVERER = discoverer
    if prune is not None:
        state.EXTRA_PRUNE = set(prune)
    if group_by is not None:
        if group_by not in ("capability", "category"):
            raise ValueError(
                f"group_by must be 'capability' or 'category', got {group_by!r}")
        state.GROUP_BY = group_by
    if pre_discovery is not None:
        state.PRE_DISCOVERY = pre_discovery
    if check_reconcile_shortcuts is not None:
        state.CHECK_RECONCILE_SHORTCUTS = check_reconcile_shortcuts
    if skill_targets is not None:
        state.SKILL_TARGETS = list(skill_targets)
    if tui_preselect is not None:
        state.TUI_PRESELECT = tui_preselect
    if plugin_targets is not None:
        state.PLUGIN_TARGETS = list(plugin_targets)
    if autostart_check_desktop_name is not None:
        state.AUTOSTART_CHECK_DESKTOP_NAME = autostart_check_desktop_name
    if check_log_name is not None:
        state.CHECK_LOG_NAME = check_log_name
    if check_state_name is not None:
        state.CHECK_STATE_NAME = check_state_name
    if self_desktop_file is not None:
        state.SELF_DESKTOP_FILE = self_desktop_file
    if self_desktop_name is not None:
        state.SELF_DESKTOP_NAME = self_desktop_name
    if self_desktop_icon is not None:
        state.SELF_DESKTOP_ICON = self_desktop_icon
    if wm_class is not None:
        state.WM_CLASS = wm_class
    if notify_app is not None:
        state.NOTIFY_APP = notify_app
    if hooks is not None:
        install._apply_hooks(hooks)
    if upgrade_repos is not None:
        state.UPGRADE_REPOS = upgrade_repos

    state._load_env()                 # re-read ROOT_DIR/.env now that ROOT_DIR is final
    state._recompute_check_paths()    # re-derive login-check artifact paths from the names
    main()
