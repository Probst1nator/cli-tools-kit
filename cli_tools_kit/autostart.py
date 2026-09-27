"""Starting tools at login (autostart entry or cron) and the login update check.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys
from typing import Optional

from . import discovery
from . import host
from . import state
from .autostart_gate import build_exec_prefix, load_tool_conditions, save_tool_conditions
from .cron_installer import read_crontab


def autostart_check_enabled() -> bool:
    return os.path.exists(state.AUTOSTART_CHECK_DESKTOP)


def enable_autostart_check() -> str:
    """Write the login update-check autostart entry. Returns its path."""
    os.makedirs(state.AUTOSTART_DIR, exist_ok=True)

    if host.IS_WINDOWS:
        # The Startup folder runs shortcuts; a .desktop file dropped there is
        # never executed. pythonw keeps the console window from flashing up at
        # every login, since the check reports through a notification anyway.
        runner = sys.executable
        windowless = os.path.join(os.path.dirname(runner), "pythonw.exe")
        if os.path.exists(windowless):
            runner = windowless
        host.write_shortcut(
            state.AUTOSTART_CHECK_DESKTOP,
            target=runner,
            args=f'"{state.ENTRY_SCRIPT}" --check',
            workdir=os.path.dirname(state.ENTRY_SCRIPT),
        )
        return state.AUTOSTART_CHECK_DESKTOP

    exec_line = f"{sys.executable} {state.ENTRY_SCRIPT} --check"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={state.SELF_DESKTOP_NAME} — login update check\n"
        "Comment=Apply network-free tool reconciliations on login; notify for new tools\n"
        f"Exec={exec_line}\n"
        "Icon=system-software-update\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-KDE-autostart-after=panel\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    with open(state.AUTOSTART_CHECK_DESKTOP, "w") as f:
        f.write(content)
    return state.AUTOSTART_CHECK_DESKTOP


def disable_autostart_check() -> bool:
    """Remove the login update-check autostart entry. True if one was present."""
    removed = False
    # On Windows, also clear the .desktop an older version wrote into the
    # Startup folder, where it sat inert instead of running the check.
    stale = os.path.join(state.AUTOSTART_DIR, state.AUTOSTART_CHECK_DESKTOP_NAME)
    for path in {state.AUTOSTART_CHECK_DESKTOP, stale}:
        if os.path.exists(path):
            os.remove(path)
            removed = True
    return removed


# ================= AUTOSTART UTILITIES =================

def get_autostart_path(tool: discovery.ToolEntry) -> str:
    """Where a tool's autostart entry lives: a .desktop symlink in
    ~/.config/autostart, or a copy of its .lnk in the Startup folder."""
    if host.IS_WINDOWS:
        return os.path.join(state.AUTOSTART_DIR,
                            os.path.splitext(tool.desktop_file)[0] + ".lnk")
    return os.path.join(state.AUTOSTART_DIR, tool.desktop_file)


def _cron_line_for_tool(tool: discovery.ToolEntry) -> str:
    """Build the crontab line for a cron-based tool."""
    parts = [tool.cron_schedule, sys.executable, tool.script_path] + list(tool.cron_args)
    return " ".join(parts)


def _cron_contains(line: str) -> bool:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return result.returncode == 0 and line in result.stdout
    except Exception:
        return False


def is_autostart_enabled(tool: discovery.ToolEntry) -> bool:
    """Check if autostart is enabled for a tool."""
    if "Icon" in tool.tags:
        return os.path.exists(get_autostart_path(tool))
    if tool.cron_schedule:
        return _cron_contains(_cron_line_for_tool(tool))
    return False


def autostart_tool_key(tool: discovery.ToolEntry) -> str:
    """The key a tool's conditions are stored under.

    The .desktop stem: stable across renames of the display name, and already
    unique per installed shortcut.
    """
    return os.path.splitext(tool.desktop_file)[0]


def get_autostart_conditions(tool: discovery.ToolEntry) -> dict:
    """The conditions currently configured for *tool* on this host."""
    return load_tool_conditions(state.IDENTITY.slug, autostart_tool_key(tool))


def set_autostart_conditions(tool: discovery.ToolEntry, conditions: Optional[dict]) -> None:
    """Store *tool*'s conditions, then rewrite its entry if autostart is on.

    The Exec line differs between a gated and an ungated entry, so a change
    here only takes effect once the entry is rewritten.
    """
    save_tool_conditions(state.IDENTITY.slug, autostart_tool_key(tool), conditions)
    if is_autostart_enabled(tool):
        enable_autostart(tool)


def _read_desktop_exec(desktop_path: str) -> str:
    """The Exec= line of an installed .desktop, or "" if it has none."""
    try:
        with open(desktop_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("Exec="):
                    return line[len("Exec="):].strip()
    except OSError:
        pass
    return ""


def _write_gated_autostart(tool: discovery.ToolEntry, desktop_path: str,
                           autostart_path: str, conditions: dict) -> tuple[bool, str]:
    """Write an autostart .desktop whose Exec runs *tool* through the gate.

    A real file rather than the usual symlink: the app entry in the menu must
    keep launching the tool unconditionally, so only this copy carries the
    gate. Everything else is inherited from the installed entry.
    """
    exec_line = _read_desktop_exec(desktop_path)
    if not exec_line:
        return False, f"No Exec line in {desktop_path}"

    prefix = " ".join(shlex.quote(p) for p in
                      build_exec_prefix(state.IDENTITY.slug, autostart_tool_key(tool)))
    gated_exec = f"{prefix} {exec_line}"

    try:
        with open(desktop_path, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        return False, f"Failed to read {desktop_path}: {e}"

    out = []
    for line in lines:
        if line.startswith("Exec="):
            out.append(f"Exec={gated_exec}")
        elif line.startswith("X-CliToolsKit-Gated="):
            continue
        else:
            out.append(line)
    # Marks the entry as ours and generated, so it is obvious in a diff why
    # this one is a file where every other autostart entry is a symlink.
    out.append("X-CliToolsKit-Gated=true")

    try:
        if os.path.exists(autostart_path) or os.path.islink(autostart_path):
            os.remove(autostart_path)
        with open(autostart_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out) + "\n")
        # Deliberately not executable: systemd-xdg-autostart-generator warns
        # on every login about an executable entry in ~/.config/autostart.
        exec_bits = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        os.chmod(autostart_path, os.stat(autostart_path).st_mode & ~exec_bits)
    except OSError as e:
        return False, f"Failed to write {autostart_path}: {e}"

    return True, f"Autostart enabled (conditional): {tool.name}"


def enable_autostart(tool: discovery.ToolEntry) -> tuple[bool, str]:
    """Enable autostart for a tool.

    Icon tools: create a .desktop symlink in ~/.config/autostart.
    Cron tools: add an @reboot (or other schedule) crontab entry.

    Returns (success, message).
    """
    if "Icon" in tool.tags:
        if host.IS_WINDOWS:
            stem = os.path.splitext(tool.desktop_file)[0]
            lnk_path = os.path.join(state.APPS_DIR, stem + ".lnk")
            if not os.path.exists(lnk_path):
                return False, f"Shortcut not found: {lnk_path}"
            os.makedirs(state.AUTOSTART_DIR, exist_ok=True)
            try:
                import shutil
                shutil.copyfile(lnk_path, get_autostart_path(tool))
                return True, f"Autostart enabled: {tool.name}"
            except OSError as e:
                return False, f"Failed to copy shortcut: {e}"

        desktop_path = os.path.join(state.APPS_DIR, tool.desktop_file)
        if not os.path.exists(desktop_path):
            return False, f"Desktop file not found: {desktop_path}"

        os.makedirs(state.AUTOSTART_DIR, exist_ok=True)
        autostart_path = get_autostart_path(tool)

        # A tool with conditions configured gets a gated copy instead of the
        # plain symlink, so the menu entry stays unconditional.
        conditions = get_autostart_conditions(tool)
        if conditions:
            return _write_gated_autostart(tool, desktop_path, autostart_path, conditions)

        if os.path.exists(autostart_path) or os.path.islink(autostart_path):
            os.remove(autostart_path)

        try:
            os.symlink(desktop_path, autostart_path)
            return True, f"Autostart enabled: {tool.name}"
        except OSError as e:
            return False, f"Failed to create symlink: {e}"

    if tool.cron_schedule:
        if host.IS_WINDOWS:
            return False, "Cron autostart is not supported on Windows"
        line = _cron_line_for_tool(tool)
        try:
            existing = read_crontab()   # raises rather than read a failure as empty
            if line in existing:
                return True, "Cron entry already present"
            new_crontab = existing.rstrip("\n") + ("\n" if existing else "") + line + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            return True, f"Cron entry added: {line}"
        except Exception as e:
            return False, f"Failed to add cron entry: {e}"

    return False, "Tool has no supported autostart method"


def disable_autostart(tool: discovery.ToolEntry) -> tuple[bool, str]:
    """Disable autostart for a tool.

    Returns (success, message).
    """
    if "Icon" in tool.tags:
        autostart_path = get_autostart_path(tool)
        if not os.path.exists(autostart_path) and not os.path.islink(autostart_path):
            return True, "Already disabled"
        try:
            os.remove(autostart_path)
            return True, f"Autostart disabled: {tool.name}"
        except OSError as e:
            return False, f"Failed to remove {'shortcut' if host.IS_WINDOWS else 'symlink'}: {e}"

    if tool.cron_schedule:
        if host.IS_WINDOWS:
            return False, "Cron autostart is not supported on Windows"
        line = _cron_line_for_tool(tool)
        try:
            existing = read_crontab()
            if line not in existing:
                return True, "Already disabled"
            new_crontab = "\n".join(l for l in existing.splitlines() if l != line) + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            return True, f"Cron entry removed: {tool.name}"
        except Exception as e:
            return False, f"Failed to remove cron entry: {e}"

    return True, "Already disabled"
