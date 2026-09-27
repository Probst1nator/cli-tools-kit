"""Shortcuts and aliases whose tool is gone."""

from __future__ import annotations

import os
from typing import List, NamedTuple

from . import host
from . import install
from . import state


# ================= ORPHAN DETECTION =================

class OrphanDesktopFile(NamedTuple):
    """Represents an orphaned .desktop file from a removed tool."""
    path: str          # Full path to the .desktop file
    name: str          # Display name from the desktop file
    tool_path: str     # Path= value (tool directory that no longer exists)
    filename: str      # Just the filename


class OrphanAlias(NamedTuple):
    """Represents an orphaned alias pointing to a missing script."""
    name: str          # Alias name
    command: str       # The command it points to
    script_path: str   # The script path extracted from command


def find_orphan_desktop_files() -> List[OrphanDesktopFile]:
    """Find .desktop files from this installer whose tools no longer exist.

    Identifies our desktop files by the identity marker, e.g. the default
    Keywords=probable.work;ai;tool; — a third-party org's installer carries its
    own slug there and so never sweeps another org's shortcuts.
    Then checks if the Path= directory (or script from Exec=) still exists.

    Several installer trees (tools/, AutomatedAlchemy/, …) can coexist on one
    host, each writing its own self-shortcut via cli_install_self() with the
    'installer-self' keyword. A self-shortcut is never a "tool" in the
    Path/main.py sense, so it must never be orphan-swept by ANY installer —
    not just the one whose SELF_DESKTOP_FILE matches this filename. Before
    2026-07, the check only skipped `filename == SELF_DESKTOP_FILE`, so each
    installer's orphan cleanup deleted every *other* installer's shortcut
    (its Path= is an org root with no main.py) on every run.

    Returns:
        List of OrphanDesktopFile entries for shortcuts pointing to removed tools.
    """
    orphans = []

    if not os.path.exists(state.APPS_DIR):
        return orphans

    for filename in os.listdir(state.APPS_DIR):
        if not filename.endswith('.desktop'):
            continue

        desktop_path = os.path.join(state.APPS_DIR, filename)
        tool_path = None
        exec_line = None
        is_ours = False
        is_installer_self = False
        name = filename.replace('.desktop', '')

        try:
            with open(desktop_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if 'Keywords=' in line and state.IDENTITY.marker_token in line and 'ai' in line and 'tool' in line:
                        is_ours = True
                        if 'installer-self' in line:
                            is_installer_self = True
                    elif line.startswith('Path='):
                        tool_path = line.split('=', 1)[1]
                    elif line.startswith('Exec='):
                        exec_line = line.split('=', 1)[1]
                    elif line.startswith('Name='):
                        name = line.split('=', 1)[1]
        except (IOError, OSError):
            continue

        if not is_ours:
            continue

        # Skip any installer's own desktop file — this instance's (by
        # filename, for shortcuts written before the marker existed) and
        # every other installer's (by the 'installer-self' marker).
        if filename == state.SELF_DESKTOP_FILE or is_installer_self:
            continue

        # If no Path=, try to extract script path from Exec= line
        # Format: /path/to/python "/path/to/script.py" [args]
        if not tool_path and exec_line:
            if '"' in exec_line:
                parts = exec_line.split('"')
                for part in parts:
                    if part.endswith('.py') and os.path.isabs(part):
                        tool_path = os.path.dirname(part)
                        break

        # Check if tool still exists
        if tool_path:
            main_py = os.path.join(tool_path, 'main.py')
            if not os.path.exists(main_py):
                orphans.append(OrphanDesktopFile(
                    path=desktop_path,
                    name=name,
                    tool_path=tool_path,
                    filename=filename
                ))

    return orphans


def find_orphan_aliases() -> List[OrphanAlias]:
    """Find aliases in ~/.tools_aliases pointing to scripts that no longer exist.

    Returns:
        List of OrphanAlias entries for aliases pointing to removed tools.
    """
    orphans = []

    if not host.IS_WINDOWS and not os.path.exists(state.ALIASES_FILE):
        return orphans

    aliases = install._load_aliases()

    for alias_name, cmd in aliases.items():
        # Extract script path from command
        # Format is typically: /usr/bin/python3 "/path/to/script.py" [args]
        # or: /path/to/python "/path/to/script.py" [args]
        script_path = None

        # Try to find quoted path first
        if '"' in cmd:
            parts = cmd.split('"')
            for part in parts:
                if part.endswith('.py') and os.path.isabs(part):
                    script_path = part
                    break

        # Fallback: look for .py in space-separated parts
        if not script_path:
            for part in cmd.split():
                if part.endswith('.py') and os.path.isabs(part):
                    script_path = part.strip('"\'')
                    break

        if script_path and not os.path.exists(script_path):
            orphans.append(OrphanAlias(
                name=alias_name,
                command=cmd,
                script_path=script_path
            ))

    return orphans


def remove_orphan_desktop_file(orphan: OrphanDesktopFile) -> tuple[bool, str]:
    """Remove an orphaned desktop file.

    Returns:
        (success, message)
    """
    try:
        os.remove(orphan.path)
        return True, f"Removed {orphan.filename}"
    except OSError as e:
        return False, f"Failed to remove {orphan.filename}: {e}"


def remove_orphan_alias(orphan: OrphanAlias) -> tuple[bool, str]:
    """Remove an orphaned alias from ~/.tools_aliases.

    Returns:
        (success, message)
    """
    try:
        aliases = install._load_aliases()
        if orphan.name in aliases:
            del aliases[orphan.name]
            install._save_aliases(aliases)
            return True, f"Removed alias '{orphan.name}'"
        return True, f"Alias '{orphan.name}' already removed"
    except Exception as e:
        return False, f"Failed to remove alias '{orphan.name}': {e}"
