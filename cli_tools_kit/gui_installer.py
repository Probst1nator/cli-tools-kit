#!/usr/bin/env python3
"""
Reusable tkinter GUI installer engine.

Autodetects tools that speak the cli-tools-kit ``--advertise`` protocol and
offers batch management of their desktop shortcuts / bash aliases / skills.

This module used to live as ``tools/installer.py``; it was lifted into
cli-tools-kit so multiple project trees can share one full-featured GUI instead
of each maintaining a forked copy. It is driven entirely by module-level
configuration (see the CONFIGURATION block below) which a thin wrapper sets via
``run(...)`` before launching:

    # tools/installer.py
    from cli_tools_kit import gui_installer as gi
    gi.run(root_dir=HERE, entry_script=__file__)

    # AutomatedAlchemy/installer.py — flat tree + repo-cache + skill-only check
    gi.DISCOVERER = alchemy_discoverer
    gi.PRE_DISCOVERY = ensure_repos
    gi.CHECK_RECONCILE_SHORTCUTS = False
    ...
    gi.run(root_dir=HERE, entry_script=__file__, window_title="...")

Standalone (``python -m cli_tools_kit.gui_installer`` / the ``cli-tool-installer``
console script) it scans the current working directory with the default
``tools_*/<tool>/main.py`` layout. Other layouts are supported through the
``discoverer`` hook, and ``group_by`` chooses whether the GUI bands rows by the
advertised ``capability`` (default) or by the ``category`` the discoverer
assigned.
"""

# Annotations are deferred so the module imports without Pillow. Several
# signatures mention ImageTk/Image, which are None when Pillow is absent;
# evaluating them at def time made "pip install cli-tools-kit" (no [gui]
# extra) fail at import, despite the icon code degrading fine at runtime.
from __future__ import annotations

import os
import sys
import subprocess
import stat
import shlex
import argparse
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor

# tkinter is a separate package on Debian/Ubuntu (python3-tk) and absent on
# servers. The headless paths and the curses screen (tui_installer) must keep
# working without it, so the import is optional; main() picks the screen.
try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    _HAVE_TK = True
except ImportError:  # pragma: no cover - exercised only where python3-tk is missing
    tk = ttk = filedialog = messagebox = None
    _HAVE_TK = False
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence

from . import host
from .identity import InstallerIdentity, LEGACY_IDENTITY

# Pillow renders tool icons. It is an optional extra (``cli-tools-kit[gui]``);
# without it the GUI still runs, just without per-tool icon thumbnails, and the
# headless paths (--list / --check) work regardless.
try:
    from PIL import Image, ImageTk
    _HAVE_PIL = True
except ImportError:  # pragma: no cover - exercised only on minimal installs
    Image = None
    ImageTk = None
    _HAVE_PIL = False

# Usage tracking (optional - graceful fallback if module missing)
try:
    from _shared.usage_tracker import get_all_usage_counts
except ImportError:
    def get_all_usage_counts() -> Dict[str, int]:
        return {}

# Gemini client for prompt variation suggestions (optional)
try:
    from _shared.gemini import GeminiClient, ModelTier
except ImportError:
    GeminiClient = None
    ModelTier = None

# ================= CONFIGURATION =================

DEBUG_LAYOUT = False  # Set to True to color-code layout frames for debugging

# Right-hand table columns — (key, pixel width) — shared by the header row and
# every tool row via InstallerApp._build_right_cells, so the 1px column grid
# lines align across header, single, parent and child rows.
RIGHT_COLS = (
    ("uses", 55),
    ("status", 115),
    ("skill", 65),
    ("icon", 50),
    ("autostart", 85),
)

_DEBUG_CELL_COLORS = {
    "uses": "#00ffff",
    "status": "#0000ff",
    "skill": "#00ff88",
    "icon": "#ffff00",
    "autostart": "#ff00ff",
}

# Outer left/right margin of the tools table. Header, category bands and tool
# rows all use the same margin (via _table_row_surface) so the table's outer
# edges form one continuous line.
TABLE_PADX = (10, 20)

# ttk style swaps applied to a row's widgets while hovered (base → hover
# variant), plus the reverse for restore. The hover variants only tint the
# widget backgrounds to the row-highlight color — no size or text-color change.
# See _set_row_emphasis / _apply_theme.
_ROW_HOVER_STYLES = {
    "Card.TFrame": "CardHover.TFrame",
    "Card.TLabel": "CardHover.TLabel",
    "CardToolName.TLabel": "CardHoverToolName.TLabel",
    "CardMuted.TLabel": "CardHoverMuted.TLabel",
    "Card.TCheckbutton": "CardHover.TCheckbutton",
}
_ROW_HOVER_STYLES_REV = {v: k for k, v in _ROW_HOVER_STYLES.items()}

# ROOT_DIR — the project tree being managed. Defaults to the directory of this
# module for standalone use, but a wrapper almost always overrides it via
# run(root_dir=...) to point at its own tree (so discovery, .env, and the
# self-shortcut Path= all anchor to the wrapper, not the cli-tools-kit checkout).
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# ENTRY_SCRIPT — the script a wrapper wants launched by the manager .desktop and
# the login-check autostart entry. Defaults to this module; run(entry_script=...)
# points it at the wrapper so those launchers invoke the wrapper (which restores
# the wrapper's configuration), never this bare engine.
ENTRY_SCRIPT = os.path.abspath(__file__)


def _load_env() -> None:
    """Best-effort load of ROOT_DIR/.env (re-callable after ROOT_DIR changes)."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT_DIR, ".env"))
    except ImportError:
        pass  # dotenv not installed, rely on system environment


_load_env()

# Where a tool's shortcut goes: the freedesktop applications directory, or the
# Start Menu on Windows.
APPS_DIR = (host.start_menu_dir() if host.IS_WINDOWS
            else os.path.join(os.path.expanduser("~"), ".local", "share", "applications"))

# IDENTITY — who this installer is on the host: the .desktop marker it claims,
# and where its config, icon overrides, alias file and cache live. Defaults to
# the historical first-party names so an existing install is untouched; a third
# party passes run(identity=InstallerIdentity(slug="acme-tools")) and gets its
# own namespace for all of it. See identity.py and README § Reusing the
# installer in your org.
IDENTITY: InstallerIdentity = LEGACY_IDENTITY

CONFIG_DIR = IDENTITY.config_path
CONFIG_FILE = IDENTITY.config_file
CUSTOM_ICONS_DIR = IDENTITY.icons_dir  # Custom tool icons
CLAUDE_SKILLS_DIR = os.path.join(os.path.expanduser("~"), ".claude", "skills")

# Runtime config — overridable by thin wrappers that re-use this module as a
# library (e.g. tools/installer.py, AutomatedAlchemy/installer.py). Wrappers
# import this module, mutate these globals (or pass them to run()), then launch.
# All defaults match the historical tools/installer.py behavior, so this module
# stays runnable standalone.
WINDOW_TITLE = IDENTITY.display_title
DISCOVERY_ROOTS: List[str] = []   # Set lazily in discover_tools() to [ROOT_DIR] if empty.
DISCOVERER: Optional[Callable] = None  # callable(root) -> List[(entry_point_path, category)].
                                   # When None: the tools_* / main.py walk for a
                                   # single ROOT_DIR, and the wider
                                   # _walk_tools_discoverer once DISCOVERY_ROOTS
                                   # is set.

# GROUP_BY — which ToolEntry field labels the GUI's row bands. "capability" (the
# default) clusters every agent, every tts, regardless of folder. "category" lets
# a wrapper band by whatever label its discoverer assigned (tools/ computes a
# semantic group from a committed JSON file). The same label is used for the
# band headers, the search show/hide bookkeeping and expand/collapse.
GROUP_BY: str = "capability"   # "capability" | "category"


# PRE_DISCOVERY — optional callable(refresh: bool) -> None run once at the top of
# discover_tools() BEFORE scanning, for side effects like cloning/pulling repos
# into a cache (AutomatedAlchemy uses this to bootstrap repos.json checkouts).
# It is skipped on the login-check path (discover_tools(run_pre=False)) so a
# login hook can never touch the network. REFRESH_REPOS is the bool handed to it.
PRE_DISCOVERY: Optional[Callable] = None
REFRESH_REPOS = False

# CHECK_RECONCILE_SHORTCUTS — login-check policy. When True (tools default) the
# headless --check also network-free-reinstalls drifted shortcuts via the tool's
# own --install (skip_deps). A tree whose --install has side effects unsafe for a
# login hook (cron daemons, an interactive login, a ~/.bashrc function — as in
# AutomatedAlchemy) sets this False to make --check skill-reconciliation ONLY.
CHECK_RECONCILE_SHORTCUTS = True

# The curses screen (tui_installer) that main() opens instead of tkinter on a
# host without a display. SKILL_TARGETS lists where a skill can be registered
# (None = the default ~/.claude/skills target only); TUI_PRESELECT controls
# the initial ticks (None = tick everything on a host with nothing installed
# yet, else mirror the host; True/False force one or the other).
SKILL_TARGETS: Optional[List] = None
TUI_PRESELECT: Optional[bool] = None

# Identity of the login update-check artifacts. Distinct names let several
# wrappers' autostart entries / logs / state files coexist on one host.
AUTOSTART_CHECK_DESKTOP_NAME = IDENTITY.check_desktop
CHECK_LOG_NAME = IDENTITY.check_log
CHECK_STATE_NAME = IDENTITY.check_state

# Identity of the manager's OWN desktop shortcut (cli_install_self) and GUI
# window, so two installers' app entries / WM classes don't collide.
SELF_DESKTOP_FILE = IDENTITY.self_desktop_file
SELF_DESKTOP_NAME = IDENTITY.self_desktop_name
SELF_DESKTOP_ICON = IDENTITY.icon  # icon name (freedesktop) or absolute path
WM_CLASS = IDENTITY.self_wm_class
NOTIFY_APP = IDENTITY.notify_label  # notify-send application label on the --check path


def _skill_installed(skill_name: str) -> bool:
    """True if ~/.claude/skills/<skill_name>/SKILL.md exists."""
    if not skill_name:
        return False
    return os.path.isfile(os.path.join(CLAUDE_SKILLS_DIR, skill_name, "SKILL.md"))


class ToolEntry(NamedTuple):
    """Represents a single installable tool/shortcut."""
    name: str           # Display name
    desktop_file: str   # Desktop filename (e.g., "ai_search_auto.desktop")
    script_path: str    # Full path to the python script
    args: List[str]     # Arguments for the tool
    icon: str           # Icon name
    description: str    # Short description
    terminal: bool      # Whether it needs a terminal
    category: str       # Folder-derived provenance label (e.g. "Research"); the stable identity/usage key. Optional-legacy.
    capability: str = ""  # Controlled capability word (e.g. "scrape") — the real taxonomy; the GUI groups rows by this. See validate_structure.CAPABILITY_VOCAB.
    domain: str = ""    # Optional free distinguisher within a capability (e.g. "youtube", "embedding")
    tags: List[str] = []  # Install-capability tags: GUI, CLI, Icon
    alias: str = ""     # Alias name for CLI tools (required if no Icon tag)
    default_autostart: bool = False  # Suggested default for the Auto-Start checkbox
    cron_schedule: str = ""          # Cron schedule string (e.g. "@reboot") for CLI autostart
    cron_args: List[str] = []        # Args to pass when running as cron job
    skill_name: str = ""             # If non-empty, tool can install a Claude Code skill via --install-skill / --uninstall-skill
    skill_status: str = ""           # Advertised skill freshness: "absent"|"current"|"stale" ("" = tool didn't report it)


def _group_label(entry: "ToolEntry") -> str:
    """The band label for a row: the GROUP_BY field of *entry*.

    Everything that keys on a band (the headers, expand/collapse, the search
    show/hide) must go through this, so all of them agree on one label.
    """
    return getattr(entry, GROUP_BY, "") or entry.capability


class ToolGroup(NamedTuple):
    """A group of tools from the same script (parent + children)."""
    parent: ToolEntry
    children: List[ToolEntry]  # Empty if single tool


def group_tools(tools: List[ToolEntry]) -> List[ToolGroup]:
    """Group tools by script_path. First tool becomes parent, rest are children."""
    by_script: Dict[str, List[ToolEntry]] = {}
    for t in tools:
        by_script.setdefault(t.script_path, []).append(t)

    groups = []
    for script_path, script_tools in by_script.items():
        if len(script_tools) == 1:
            groups.append(ToolGroup(parent=script_tools[0], children=[]))
        else:
            groups.append(ToolGroup(parent=script_tools[0], children=script_tools[1:]))
    return groups

def load_config() -> dict:
    """Load config from file, return empty dict if not found."""
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_config(config: dict):
    """Save config to file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f)


def get_auto_update_on_startup() -> bool:
    """Whether this GUI should silently apply pending local updates the next
    time it launches (the "Auto-update on startup" checkbox next to the
    Up-to-date badge)."""
    return bool(load_config().get("auto_update_on_startup", False))


def set_auto_update_on_startup(enabled: bool):
    """Persist the 'Auto-update on startup' checkbox state."""
    config = load_config()
    config["auto_update_on_startup"] = enabled
    save_config(config)


def get_custom_icon_path(tool_key: str) -> Optional[str]:
    """Get custom icon path for a tool if one exists.

    Args:
        tool_key: Unique key like "Category_ToolName"

    Returns:
        Path to custom icon file, or None
    """
    config = load_config()
    custom_icons = config.get("custom_icons", {})
    icon_path = custom_icons.get(tool_key)
    if icon_path and os.path.exists(icon_path):
        return icon_path
    return None


def set_custom_icon(tool_key: str, icon_path: str):
    """Set a custom icon for a tool.

    Args:
        tool_key: Unique key like "Category_ToolName"
        icon_path: Path to the icon file
    """
    config = load_config()
    if "custom_icons" not in config:
        config["custom_icons"] = {}
    config["custom_icons"][tool_key] = icon_path
    save_config(config)


def get_system_icons() -> List[tuple]:
    """Get list of system icons available on the system.

    Returns:
        List of (icon_name, icon_path) tuples, sorted by name
    """
    icons = {}
    for name, path in iter_system_icons():
        # Prefer larger/higher quality versions
        if name not in icons or 'scalable' in path or '128' in path:
            icons[name] = path
    # Sort by name
    return sorted(icons.items(), key=lambda x: x[0].lower())


def iter_system_icons():
    """Iterate through system icons, yielding (name, path) as they're found.

    This is a generator that yields icons incrementally, useful for background loading.
    """
    icon_dirs = [
        "/usr/share/icons/hicolor/48x48/apps",
        "/usr/share/icons/hicolor/64x64/apps",
        "/usr/share/icons/hicolor/128x128/apps",
        "/usr/share/icons/hicolor/scalable/apps",
        "/usr/share/icons/Adwaita/48x48/apps",
        "/usr/share/icons/Adwaita/64x64/apps",
        "/usr/share/icons/breeze/apps/48",
        "/usr/share/icons/breeze/apps/64",
        "/usr/share/pixmaps",
        os.path.expanduser("~/.local/share/icons/hicolor/48x48/apps"),
        os.path.expanduser("~/.local/share/icons/hicolor/128x128/apps"),
    ]

    for icon_dir in icon_dirs:
        if not os.path.isdir(icon_dir):
            continue
        try:
            for f in os.listdir(icon_dir):
                if f.endswith(('.png', '.svg', '.xpm')):
                    name = os.path.splitext(f)[0]
                    yield (name, os.path.join(icon_dir, f))
        except (OSError, PermissionError):
            continue


def clear_custom_icon(tool_key: str):
    """Remove custom icon for a tool, reverting to default."""
    config = load_config()
    if "custom_icons" in config and tool_key in config["custom_icons"]:
        del config["custom_icons"][tool_key]
        save_config(config)


def get_icon_gen_settings() -> dict:
    """Get saved icon generation settings.

    Returns:
        Dict with keys: steps, samples, guidance, selected_models, expanded, sys_icons_expanded
    """
    config = load_config()
    return config.get("icon_gen_settings", {
        "steps": 20,
        "samples": 4,
        "guidance": 7.5,
        "selected_models": [],  # Empty means select all available
        "expanded": True,  # Show advanced options by default
        "sys_icons_expanded": True
    })


def save_icon_gen_settings(steps: int, samples: int, guidance: float, selected_models: list, expanded: bool, sys_icons_expanded: bool = True):
    """Save icon generation settings."""
    config = load_config()
    config["icon_gen_settings"] = {
        "steps": steps,
        "samples": samples,
        "guidance": guidance,
        "selected_models": selected_models,
        "expanded": expanded,
        "sys_icons_expanded": sys_icons_expanded
    }
    save_config(config)


# Emoji collection for icon browser - generated from Unicode emoji ranges
def _build_emoji_list():
    """Build emoji list dynamically from Unicode emoji codepoint ranges."""
    import unicodedata
    _EMOJI_RANGES = [
        (0x2600, 0x26FF),    # Misc symbols (sun, cloud, umbrella, etc)
        (0x2700, 0x27BF),    # Dingbats (scissors, pencil, etc)
        (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs
        (0x1F600, 0x1F64F),  # Emoticons (faces)
        (0x1F680, 0x1F6FF),  # Transport and Map
        (0x1F7E0, 0x1F7EB),  # Large colored circles/squares
        (0x1F900, 0x1F9FF),  # Supplemental Symbols (animals, people, objects)
        (0x1FA70, 0x1FAFF),  # Symbols Extended-A (newer emoji)
    ]
    result = []
    for start, end in _EMOJI_RANGES:
        for cp in range(start, end + 1):
            try:
                name = unicodedata.name(chr(cp))
                result.append((chr(cp), name.lower()))
            except ValueError:
                continue
    return result


EMOJI_LIST = _build_emoji_list()


def _get_emoji_cache_dir():
    """Get/create emoji icon cache directory."""
    cache_dir = os.path.join(IDENTITY.cache_path, "emoji_icons")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir


def _find_emoji_font():
    """Find a color emoji font on the system."""
    font_paths = [
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/opentype/noto/NotoColorEmoji.ttf",
        "/usr/share/fonts/google-noto-color-emoji/NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/unifont/unifont.ttf",
    ]
    for path in font_paths:
        if os.path.exists(path):
            return path
    # Try fc-match as last resort
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", ":family=Noto Color Emoji"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip() and os.path.exists(result.stdout.strip()):
            return result.stdout.strip()
    except Exception:
        pass
    return None


def render_emoji_icon(emoji_char: str, output_path: str, size: int = 128) -> bool:
    """Render an emoji character to a PNG file.

    Uses NotoColorEmoji at its native bitmap size (109) and scales to target.
    Falls back to monochrome rendering if color font unavailable.
    Returns True if successful.
    """
    from PIL import ImageDraw, ImageFont

    # Method 1: Color emoji font at native bitmap size, then scale
    font_path = _find_emoji_font()
    if font_path:
        # NotoColorEmoji is a CBDT bitmap font with fixed sizes.
        # Try the native size (109 for Noto), then common alternatives.
        for native_size in [109, 128, 64, 48, 32, 16]:
            try:
                font = ImageFont.truetype(font_path, native_size)
                break
            except OSError:
                continue
        else:
            font = None

        if font is not None:
            try:
                render_size = native_size + 60  # Extra room for centering
                img = Image.new("RGBA", (render_size, render_size), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)

                bbox = draw.textbbox((0, 0), emoji_char, font=font)
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                x = (render_size - w) // 2 - bbox[0]
                y = (render_size - h) // 2 - bbox[1]

                draw.text((x, y), emoji_char, font=font, embedded_color=True)

                if img.getbbox():
                    # Crop to content, resize to target with padding
                    cropped = img.crop(img.getbbox())
                    # Scale to fit within size with margin
                    target_inner = int(size * 0.85)
                    cropped.thumbnail((target_inner, target_inner), Image.Resampling.LANCZOS)
                    final = Image.new("RGBA", (size, size), (0, 0, 0, 0))
                    ox = (size - cropped.width) // 2
                    oy = (size - cropped.height) // 2
                    final.paste(cropped, (ox, oy))
                    final.save(output_path)
                    return True
            except (TypeError, Exception):
                pass

    # Method 2: Fallback monochrome rendering with DejaVu or default font
    try:
        font_size = int(size * 0.7)
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
        except Exception:
            font = ImageFont.load_default()

        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        bbox = draw.textbbox((0, 0), emoji_char, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        x = (size - w) // 2 - bbox[0]
        y = (size - h) // 2 - bbox[1]

        draw.text((x, y), emoji_char, font=font, fill=(255, 255, 255, 255))

        if img.getbbox():
            img.save(output_path)
            return True
    except Exception:
        pass

    return False


def get_tool_prompt(tool_key: str, default_name: str) -> str:
    """Get saved prompt for a specific tool."""
    config = load_config()
    prompts = config.get("icon_prompts", {})
    return prompts.get(tool_key, f"app icon for {default_name}, flat design, minimal")


def save_tool_prompt(tool_key: str, prompt: str):
    """Save prompt for a specific tool."""
    config = load_config()
    if "icon_prompts" not in config:
        config["icon_prompts"] = {}
    config["icon_prompts"][tool_key] = prompt
    save_config(config)


# ================= METADATA EXTRACTION =================

def get_metadata_native(file_path: str, category: str) -> List[ToolEntry]:
    """
    Gets metadata by running the script with --advertise.
    This is the ONLY supported discovery method.
    """
    entries = []
    try:
        # Use sys.executable to ensure we use the same python environment
        result = subprocess.run(
            [sys.executable, file_path, "--advertise"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            # Tolerate a single-object advertise, and skip any non-dict entry
            # rather than letting one malformed tool's JSON abort discovery for
            # the whole tree (an uncaught AttributeError here would do exactly that).
            if isinstance(data, dict):
                data = [data]
            if not isinstance(data, list):
                data = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                # Parse tags (new protocol) or infer from cli_only (backward compat)
                tags = item.get("tags", [])
                if not tags:
                    # Backward compatibility: infer tags from cli_only
                    cli_only = item.get("cli_only", False)
                    if cli_only:
                        tags = ["CLI"]
                    else:
                        tags = ["GUI", "Icon"]

                # Alias is required if no Icon tag
                has_icon = "Icon" in tags
                alias = item.get("alias", "")
                if not has_icon and not alias:
                    # Default alias from desktop_file stem
                    alias = item.get("desktop_file", "").replace(".desktop", "")

                entries.append(ToolEntry(
                    name=item.get("name", "Unknown"),
                    desktop_file=item.get("desktop_file", "unknown.desktop"),
                    script_path=file_path,
                    args=item.get("args", []),
                    icon=item.get("icon", "system-run"),
                    description=item.get("desc", ""),
                    terminal=item.get("terminal", False),
                    category=category,
                    # capability is the real taxonomy and the GUI group key; fall
                    # back to the folder-derived category for tools not yet
                    # migrated to the tag schema so grouping never sees "".
                    capability=item.get("capability") or category.lower(),
                    domain=item.get("domain", ""),
                    tags=tags,
                    alias=alias,
                    default_autostart=bool(item.get("default_autostart", False)),
                    cron_schedule=item.get("cron_schedule", ""),
                    cron_args=item.get("cron_args", []),
                    skill_name=item.get("skill_name", ""),
                    skill_status=item.get("skill_status", ""),
                ))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        # If a tool fails to advertise, it is ignored.
        pass

    return entries

def _is_tool_dir(path: str) -> bool:
    """A directory is a tool when it holds both main.py and requirements.txt."""
    return (os.path.isfile(os.path.join(path, "main.py"))
            and os.path.isfile(os.path.join(path, "requirements.txt")))


def _default_tools_discoverer(root: str) -> List[tuple]:
    """Find tools in either standard layout, returning (entry_point, category).

    Two shapes are recognised, and a tree may mix them:

    * **flat** — ``<root>/<tool>/main.py``. The common case, and what a new
      organisation gets by default. Category is empty, so rows band by each
      tool's advertised ``capability``.
    * **nested** — ``<root>/tools_<category>/<tool>/main.py``. The original
      layout, where the folder supplies the category label.

    Directories starting with "_" or "." are skipped in both (``_shared``,
    ``_archive``, ``.git``). A wrapper with a different shape passes its own
    ``discoverer``; see README § Reusing the installer in your org.
    """
    found = []
    if not os.path.isdir(root):
        return found
    for item in sorted(os.listdir(root)):
        item_path = os.path.join(root, item)
        if not os.path.isdir(item_path) or item.startswith(("_", ".")):
            continue

        if item.startswith("tools_"):
            category = item.replace("tools_", "").title()
            for sub_item in sorted(os.listdir(item_path)):
                sub_path = os.path.join(item_path, sub_item)
                if not os.path.isdir(sub_path) or sub_item.startswith(("_", ".")):
                    continue
                if _is_tool_dir(sub_path):
                    found.append((os.path.join(sub_path, "main.py"), category))
        elif _is_tool_dir(item_path):
            found.append((os.path.join(item_path, "main.py"), ""))
    return found


# How deep below a discovery root a tool is still found, and the directory names
# the walk never enters. `vendor*` catches vendored checkouts (vendor-G2).
MAX_DISCOVERY_DEPTH = 4
DISCOVERY_PRUNE = {".venv", "venv", ".git", "node_modules", "__pycache__",
                   "out", "cache", "build", "dist", "archive"}

# Names a wrapper adds to DISCOVERY_PRUNE for its own tree, via run(prune=...).
# It extends the default set rather than replacing it.
EXTRA_PRUNE: set = set()


def _walk_entry_point(dirpath: str, filenames) -> Optional[str]:
    """The entry point of a tool directory, or None when it is not one.

    A directory is a tool when it holds ``requirements.txt`` next to either
    ``main.py`` or ``<dirname>.py`` with dashes written as underscores, which is
    how a one-tool repo names its script (manim-kit ships ``manim_kit.py``).
    """
    if "requirements.txt" not in filenames:
        return None
    own = os.path.basename(dirpath).replace("-", "_") + ".py"
    for name in ("main.py", own):
        if name in filenames:
            return os.path.join(dirpath, name)
    return None


def _walk_pruned(name: str) -> bool:
    return (name.startswith(".") or name.startswith("vendor")
            or name in DISCOVERY_PRUNE or name in EXTRA_PRUNE)


def _walk_category(root: str, tool_dir: str) -> str:
    """The tool's parent directory name, or the root's name when the tool is the root."""
    if tool_dir == root:
        return os.path.basename(root)
    parent = os.path.dirname(tool_dir)
    return "" if parent == root else os.path.basename(parent)


def _walk_tools_discoverer(root: str) -> List[tuple]:
    """Find tools anywhere under one root, returning (entry_point, category).

    The root itself counts, so a repo whose script sits at its top level is one
    tool. Below it the walk goes at most ``MAX_DISCOVERY_DEPTH`` levels and
    skips the names in ``DISCOVERY_PRUNE`` and ``EXTRA_PRUNE``, anything
    starting with a dot, and anything starting with ``vendor``. A wrapper fills
    ``EXTRA_PRUNE`` by passing ``prune``. This is the default when a wrapper passes
    ``discovery_roots``, because a tree of several repos puts tools at depths
    the flat/``tools_*`` layouts do not describe.
    """
    found = []
    if not os.path.isdir(root):
        return found
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = sorted(d for d in dirnames
                             if not _walk_pruned(d) and depth < MAX_DISCOVERY_DEPTH)
        entry = _walk_entry_point(dirpath, filenames)
        if entry:
            found.append((entry, _walk_category(root, dirpath)))
    return found


def discover_tools(run_pre: bool = True) -> List[ToolEntry]:
    """Scan configured DISCOVERY_ROOTS for installable tools.

    By default scans ROOT_DIR with the tools_* / main.py layout. A wrapper
    can set DISCOVERY_ROOTS and/or DISCOVERER to plug in a different layout
    (e.g. AutomatedAlchemy's flat project-per-dir tree).

    If PRE_DISCOVERY is set it runs first (for repo-clone bootstrapping). Pass
    run_pre=False to skip it — the login-check path does this so a login hook
    can never reach the network.
    """
    if run_pre and PRE_DISCOVERY is not None:
        PRE_DISCOVERY(REFRESH_REPOS)

    roots = DISCOVERY_ROOTS or [ROOT_DIR]
    # A single root_dir keeps the flat/tools_* layouts it has always used; the
    # category label of a tools_<cat>/ tree is only produced there. Several
    # roots mean repos of different shapes, so those get the wider walk.
    discoverer = DISCOVERER or (_walk_tools_discoverer if DISCOVERY_ROOTS
                                else _default_tools_discoverer)

    # Each tool is probed by spawning it with --advertise (a short-lived
    # subprocess that exits before its heavy imports). That makes discovery
    # I/O-bound, so probe every tool concurrently instead of paying the spawn
    # latency serially — this scan runs on every GUI launch and every login
    # `--check`, so the serial cost (≈Ntools × spawn) was the whole startup
    # delay. map() preserves discovery order, get_metadata_native swallows its
    # own errors (returns []), and subprocess.run drops the GIL while waiting,
    # so threads parallelize the wall-clock time.
    pairs = [(entry_point, category)
             for root in roots
             for entry_point, category in discoverer(root)]
    tools = []
    if pairs:
        with ThreadPoolExecutor(max_workers=min(len(pairs), 16)) as pool:
            for entries in pool.map(lambda p: get_metadata_native(*p), pairs):
                tools.extend(entries)
    # Take note of skills whose installed copy is out of date vs. the tool's
    # bundled version (reported via the --advertise `skill_status` field) and
    # suggest the update. Printed once per discovery so a terminal run surfaces
    # it even without the GUI; the GUI also flags these rows (see
    # _update_status_labels) and re-applies them on demand.
    for t in tools:
        if getattr(t, "skill_status", "") == "stale":
            print(
                f"[installer] Skill update available: '{t.skill_name}' (bundled with "
                f"{t.name}) — the installed ~/.claude/skills/{t.skill_name}/ is out of "
                f"date. Tick its Skill box and Apply, or run: "
                f"{sys.executable} {t.script_path} --install-skill",
                file=sys.stderr,
            )
    return tools

ALIASES_FILE = IDENTITY.aliases_path
AUTOSTART_DIR = (host.startup_dir() if host.IS_WINDOWS
                 else os.path.join(os.path.expanduser("~"), ".config", "autostart"))

# --- Login update-check autostart -----------------------------------------
# A ~/.config/autostart entry that runs `installer.py --check` once per login.
# The check APPLIES network-free reconciliations (drifted .desktop Exec paths,
# renamed aliases, stale installed SKILL.md — all rewritten from already-synced
# source via skip_deps, no pip) and only NOTIFIES for updates that would touch
# the network (a new, not-yet-installed tool) or that add a new skill. The
# pip/network gate stays behind an explicit human action.
# Derived from the configurable *_NAME knobs above. run() recomputes these after
# a wrapper overrides the names; the defaults keep tools/installer.py unchanged.
AUTOSTART_CHECK_DESKTOP = os.path.join(AUTOSTART_DIR, AUTOSTART_CHECK_DESKTOP_NAME)
CHECK_LOG = os.path.join(os.path.expanduser("~"), ".local", "log", CHECK_LOG_NAME)
# Remembers the last actionable (new tool / new skill / failure) set so the
# login check notifies once when it CHANGES instead of nagging every login.
CHECK_STATE = os.path.join(os.path.expanduser("~"), ".local", "state", CHECK_STATE_NAME)


def _apply_identity(identity: InstallerIdentity) -> None:
    """Point every per-host artifact at the given identity.

    Called by run(identity=...) before the explicit name kwargs, so a wrapper
    can take the whole namespace from a slug and still override one name.
    """
    global IDENTITY, CONFIG_DIR, CONFIG_FILE, CUSTOM_ICONS_DIR, ALIASES_FILE
    global WINDOW_TITLE, SELF_DESKTOP_FILE, SELF_DESKTOP_NAME, SELF_DESKTOP_ICON
    global WM_CLASS, NOTIFY_APP
    global AUTOSTART_CHECK_DESKTOP_NAME, CHECK_LOG_NAME, CHECK_STATE_NAME

    IDENTITY = identity
    CONFIG_DIR = identity.config_path
    CONFIG_FILE = identity.config_file
    CUSTOM_ICONS_DIR = identity.icons_dir
    ALIASES_FILE = identity.aliases_path
    WINDOW_TITLE = identity.display_title
    SELF_DESKTOP_FILE = identity.self_desktop_file
    SELF_DESKTOP_NAME = identity.self_desktop_name
    SELF_DESKTOP_ICON = identity.icon
    WM_CLASS = identity.self_wm_class
    NOTIFY_APP = identity.notify_label
    AUTOSTART_CHECK_DESKTOP_NAME = identity.check_desktop
    CHECK_LOG_NAME = identity.check_log
    CHECK_STATE_NAME = identity.check_state
    _recompute_check_paths()


def _recompute_check_paths() -> None:
    """Re-derive the login-check artifact paths from the *_NAME knobs (call after
    a wrapper overrides them, e.g. inside run())."""
    global AUTOSTART_CHECK_DESKTOP, CHECK_LOG, CHECK_STATE
    AUTOSTART_CHECK_DESKTOP = os.path.join(AUTOSTART_DIR, AUTOSTART_CHECK_DESKTOP_NAME)
    CHECK_LOG = os.path.join(os.path.expanduser("~"), ".local", "log", CHECK_LOG_NAME)
    CHECK_STATE = os.path.join(os.path.expanduser("~"), ".local", "state", CHECK_STATE_NAME)


def _skill_md_path(skill_name: str) -> str:
    return os.path.join(CLAUDE_SKILLS_DIR, skill_name, "SKILL.md")


def _file_sig(path: str):
    """sha256 of a file's bytes, or None if absent — for before/after change-detection."""
    import hashlib
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def autostart_check_enabled() -> bool:
    return os.path.exists(AUTOSTART_CHECK_DESKTOP)


def enable_autostart_check() -> str:
    """Write the login update-check autostart entry. Returns its path."""
    os.makedirs(AUTOSTART_DIR, exist_ok=True)
    exec_line = f"{sys.executable} {ENTRY_SCRIPT} --check"
    content = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        f"Name={SELF_DESKTOP_NAME} — login update check\n"
        "Comment=Apply network-free tool reconciliations on login; notify for new tools\n"
        f"Exec={exec_line}\n"
        "Icon=system-software-update\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "X-KDE-autostart-after=panel\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
    with open(AUTOSTART_CHECK_DESKTOP, "w") as f:
        f.write(content)
    return AUTOSTART_CHECK_DESKTOP


def disable_autostart_check() -> bool:
    """Remove the login update-check autostart entry. True if one was present."""
    if os.path.exists(AUTOSTART_CHECK_DESKTOP):
        os.remove(AUTOSTART_CHECK_DESKTOP)
        return True
    return False


def _notify_send(summary: str, body: str = "") -> None:
    """Best-effort KDE/GNOME desktop notification; silently no-ops without notify-send."""
    if host.IS_WINDOWS:
        return  # no notify-send here
    try:
        subprocess.run(
            ["notify-send", "-a", NOTIFY_APP, "-i", "system-software-update",
             "-t", "15000", summary] + ([body] if body else []),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass


def ensure_apps_dir():
    if not os.path.exists(APPS_DIR):
        os.makedirs(APPS_DIR)

def tool_shortcut_path(tool: ToolEntry) -> str:
    """Where this tool's shortcut lives: a .desktop file, or a .lnk on Windows."""
    if host.IS_WINDOWS:
        return os.path.join(APPS_DIR,
                            os.path.splitext(tool.desktop_file)[0] + ".lnk")
    return os.path.join(APPS_DIR, tool.desktop_file)


def _load_shims() -> dict:
    """Windows equivalent of _load_aliases: the installed shims, read back as
    alias name -> command line, so the callers below need no Windows branch."""
    shim_dir = IDENTITY.shim_path
    commands = {}
    try:
        names = os.listdir(shim_dir)
    except OSError:
        return commands
    for name in names:
        if not name.endswith(".cmd"):
            continue
        try:
            with open(os.path.join(shim_dir, name), "r") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
        except OSError:
            continue
        for line in lines:
            if line.lower().startswith("@echo"):
                continue
            commands[name[:-4]] = line.removesuffix(" %*").strip()
            break
    return commands


def _load_aliases() -> dict:
    """Load existing aliases from file. Returns dict of alias_name -> command."""
    if host.IS_WINDOWS:
        return _load_shims()
    aliases = {}
    if os.path.exists(ALIASES_FILE):
        with open(ALIASES_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("alias ") and "=" in line:
                    parts = line[6:].split("=", 1)
                    if len(parts) == 2:
                        name = parts[0].strip()
                        cmd = parts[1].strip()
                        # Only strip the OUTER wrapping quotes — never a blanket
                        # .strip("'\""), which would eat a balanced inner quote
                        # (e.g. the closing `"` of `python "/path/main.py"`) and
                        # corrupt the alias on the next save.
                        if (cmd.startswith("'") and cmd.endswith("'")) or \
                           (cmd.startswith('"') and cmd.endswith('"')):
                            cmd = cmd[1:-1]
                        aliases[name] = cmd
    return aliases

def _find_alias_for_script(script_path: str) -> str | None:
    """Find which alias (if any) points to a given script path.

    Returns the alias name if found, None otherwise.
    """
    aliases = _load_aliases()
    for alias_name, cmd in aliases.items():
        # Commands look like: /usr/bin/python3 "/path/to/script.py"
        if script_path in cmd:
            return alias_name
    return None

def _save_aliases(aliases: dict) -> None:
    """Save aliases to file.

    On Windows there is no alias file: the shims are the aliases, so this
    deletes the shims of every name that is no longer in the dict. The
    remaining shims are written by each tool's own --install.
    """
    if host.IS_WINDOWS:
        shim_dir = IDENTITY.shim_path
        for name in _load_shims():
            if name not in aliases:
                host.remove_shims(shim_dir, name)
        return
    with open(ALIASES_FILE, "w") as f:
        f.write("# Auto-generated by tools installer - do not edit manually\n")
        f.write("# Source this file in your .bashrc/.zshrc:\n")
        f.write(f"#   [ -f {ALIASES_FILE} ] && source {ALIASES_FILE}\n\n")
        for name, cmd in sorted(aliases.items()):
            # shlex.quote so an inner quote (e.g. the `"` around the script
            # path) survives the round-trip instead of being written into a
            # raw '{cmd}' wrapper that leaves the inner quote unbalanced.
            f.write(f"alias {name}={shlex.quote(cmd)}\n")

def is_installed(tool: ToolEntry) -> bool:
    """Check if a tool is installed based on its tags."""
    has_icon = "Icon" in tool.tags
    if has_icon:
        # Check for the .desktop file (a .lnk on Windows)
        return os.path.exists(tool_shortcut_path(tool))
    else:
        # Check if ANY alias points to this script (not just the expected one)
        return _find_alias_for_script(tool.script_path) is not None

def needs_update(tool: ToolEntry) -> bool:
    """Check if an installed tool has outdated metadata (e.g., renamed alias).

    Returns True if the tool is installed but its configuration differs from
    what's advertised (different alias name, different args, etc.)
    """
    has_icon = "Icon" in tool.tags
    if has_icon:
        if host.IS_WINDOWS:
            return False  # a .lnk is binary; nothing to compare the path against
        # For desktop files, check if the Exec path matches the current script path
        desktop_path = os.path.join(APPS_DIR, tool.desktop_file)
        if not os.path.exists(desktop_path):
            return False
        # Parse the desktop file to check if Exec path matches
        try:
            with open(desktop_path, "r") as f:
                for line in f:
                    if line.startswith("Exec="):
                        # Exec line format: Exec=/path/to/python "/path/to/script.py" --args
                        # Check if our script_path is in the Exec line
                        if tool.script_path not in line:
                            return True  # Script path changed, needs update
                        break
        except (IOError, OSError):
            pass
        return False
    else:
        # For aliases, this entry is current iff *its own* alias points at the
        # script. Several entries can legitimately share one script (e.g. the
        # `scrape` dispatcher + `scrape-yt` plugin both alias scrape/main.py), so
        # comparing against the first-found alias would false-flag every-but-one
        # of them forever. Only when NONE of this script's aliases is this entry's
        # alias is it a genuine rename that needs updating.
        aliases_for_script = [name for name, cmd in _load_aliases().items()
                              if tool.script_path in cmd]
        if not aliases_for_script:
            return False  # Not installed
        return tool.alias not in aliases_for_script  # installed under a different name → rename

def install_tool(tool: ToolEntry, skip_deps: bool = False) -> tuple[bool, str]:
    """Invokes the tool's own --install argument. Returns (success, output).

    When skip_deps is True, sets TOOLS_INSTALLER_SKIP_DEPS=1 in the child
    environment so the shared ToolInstaller writes the .desktop/alias without
    running 'pip install'. This keeps a shortcut refresh local and network-free.
    """
    try:
        cmd = [sys.executable, tool.script_path, "--install"] + tool.args
        env = os.environ.copy()
        # The tool writes its own .desktop and alias via ToolInstaller, so it
        # needs to know which installer asked — otherwise a third-party org's
        # tools get branded with the first-party marker and land in the
        # first-party alias file.
        env.update(IDENTITY.env())
        if skip_deps:
            env["TOOLS_INSTALLER_SKIP_DEPS"] = "1"
        result = subprocess.run(cmd, cwd=os.path.dirname(tool.script_path),
                                capture_output=True, text=True, env=env)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return True, output
        else:
            return False, output or f"Exit code: {result.returncode}"
    except Exception as e:
        return False, str(e)

def remove_tool(tool: ToolEntry) -> tuple[bool, str]:
    """Invokes the tool's own --remove argument. Returns (success, output)."""
    try:
        cmd = [sys.executable, tool.script_path, "--remove"] + tool.args
        env = os.environ.copy()
        env.update(IDENTITY.env())   # remove the alias from OUR alias file
        result = subprocess.run(cmd, cwd=os.path.dirname(tool.script_path),
                                capture_output=True, text=True, env=env)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return True, output
        else:
            return False, output or f"Exit code: {result.returncode}"
    except Exception as e:
        return False, str(e)

def install_skill_for_tool(tool: ToolEntry) -> tuple[bool, str]:
    """Invokes the tool's --install-skill argument. Returns (success, output).

    Used by the installer GUI when the per-row 'Skill' checkbox is toggled
    on for a tool that advertises a skill_name. Idempotent: tools should
    detect an up-to-date SKILL.md and no-op.
    """
    try:
        cmd = [sys.executable, tool.script_path, "--install-skill"] + tool.args
        result = subprocess.run(cmd, cwd=os.path.dirname(tool.script_path),
                                capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return True, output
        return False, output or f"Exit code: {result.returncode}"
    except Exception as e:
        return False, str(e)


def uninstall_skill_for_tool(tool: ToolEntry) -> tuple[bool, str]:
    """Invokes the tool's --uninstall-skill argument. Returns (success, output)."""
    try:
        cmd = [sys.executable, tool.script_path, "--uninstall-skill"] + tool.args
        result = subprocess.run(cmd, cwd=os.path.dirname(tool.script_path),
                                capture_output=True, text=True)
        output = (result.stdout + result.stderr).strip()
        if result.returncode == 0:
            return True, output
        return False, output or f"Exit code: {result.returncode}"
    except Exception as e:
        return False, str(e)


def refresh_desktop_database():
    if host.IS_WINDOWS:
        return
    for cmd in ["update-desktop-database", "kbuildsycoca5"]:
        try:
            subprocess.run([cmd, APPS_DIR if cmd == "update-desktop-database" else ""],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except:
            pass


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

    if not os.path.exists(APPS_DIR):
        return orphans

    for filename in os.listdir(APPS_DIR):
        if not filename.endswith('.desktop'):
            continue

        desktop_path = os.path.join(APPS_DIR, filename)
        tool_path = None
        exec_line = None
        is_ours = False
        is_installer_self = False
        name = filename.replace('.desktop', '')

        try:
            with open(desktop_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if 'Keywords=' in line and IDENTITY.marker_token in line and 'ai' in line and 'tool' in line:
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
        if filename == SELF_DESKTOP_FILE or is_installer_self:
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

    if not host.IS_WINDOWS and not os.path.exists(ALIASES_FILE):
        return orphans

    aliases = _load_aliases()

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
        aliases = _load_aliases()
        if orphan.name in aliases:
            del aliases[orphan.name]
            _save_aliases(aliases)
            return True, f"Removed alias '{orphan.name}'"
        return True, f"Alias '{orphan.name}' already removed"
    except Exception as e:
        return False, f"Failed to remove alias '{orphan.name}': {e}"


# ================= AUTOSTART UTILITIES =================

def get_autostart_path(tool: ToolEntry) -> str:
    """Where a tool's autostart entry lives: a .desktop symlink in
    ~/.config/autostart, or a copy of its .lnk in the Startup folder."""
    if host.IS_WINDOWS:
        return os.path.join(AUTOSTART_DIR,
                            os.path.splitext(tool.desktop_file)[0] + ".lnk")
    return os.path.join(AUTOSTART_DIR, tool.desktop_file)


def _cron_line_for_tool(tool: ToolEntry) -> str:
    """Build the crontab line for a cron-based tool."""
    parts = [tool.cron_schedule, sys.executable, tool.script_path] + list(tool.cron_args)
    return " ".join(parts)


def _cron_contains(line: str) -> bool:
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        return result.returncode == 0 and line in result.stdout
    except Exception:
        return False


def is_autostart_enabled(tool: ToolEntry) -> bool:
    """Check if autostart is enabled for a tool."""
    if "Icon" in tool.tags:
        return os.path.exists(get_autostart_path(tool))
    if tool.cron_schedule:
        return _cron_contains(_cron_line_for_tool(tool))
    return False


def enable_autostart(tool: ToolEntry) -> tuple[bool, str]:
    """Enable autostart for a tool.

    Icon tools: create a .desktop symlink in ~/.config/autostart.
    Cron tools: add an @reboot (or other schedule) crontab entry.

    Returns (success, message).
    """
    if "Icon" in tool.tags:
        if host.IS_WINDOWS:
            stem = os.path.splitext(tool.desktop_file)[0]
            lnk_path = os.path.join(APPS_DIR, stem + ".lnk")
            if not os.path.exists(lnk_path):
                return False, f"Shortcut not found: {lnk_path}"
            os.makedirs(AUTOSTART_DIR, exist_ok=True)
            try:
                import shutil
                shutil.copyfile(lnk_path, get_autostart_path(tool))
                return True, f"Autostart enabled: {tool.name}"
            except OSError as e:
                return False, f"Failed to copy shortcut: {e}"

        desktop_path = os.path.join(APPS_DIR, tool.desktop_file)
        if not os.path.exists(desktop_path):
            return False, f"Desktop file not found: {desktop_path}"

        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        autostart_path = get_autostart_path(tool)

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
            result   = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            existing = result.stdout if result.returncode == 0 else ""
            if line in existing:
                return True, "Cron entry already present"
            new_crontab = existing.rstrip("\n") + ("\n" if existing else "") + line + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            return True, f"Cron entry added: {line}"
        except Exception as e:
            return False, f"Failed to add cron entry: {e}"

    return False, "Tool has no supported autostart method"


def disable_autostart(tool: ToolEntry) -> tuple[bool, str]:
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
            result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
            if result.returncode != 0 or line not in result.stdout:
                return True, "Already disabled"
            new_crontab = "\n".join(l for l in result.stdout.splitlines() if l != line) + "\n"
            subprocess.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            return True, f"Cron entry removed: {tool.name}"
        except Exception as e:
            return False, f"Failed to remove cron entry: {e}"

    return True, "Already disabled"


# ================= ICON UTILITIES =================

def _get_current_icon_theme() -> str:
    """Get current icon theme from gsettings or kdeglobals."""
    # Try gsettings (GNOME)
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"],
            capture_output=True, text=True, timeout=2
        )
        if result.returncode == 0:
            return result.stdout.strip().strip("'")
    except Exception:
        pass

    # Try KDE config
    kde_config = os.path.expanduser("~/.config/kdeglobals")
    if os.path.exists(kde_config):
        try:
            with open(kde_config) as f:
                for line in f:
                    if line.startswith("Theme="):
                        return line.split("=", 1)[1].strip()
        except Exception:
            pass

    return "hicolor"  # Fallback


def find_icon_path(icon_name: str, size: int = 32) -> Optional[str]:
    """Find the path to an icon file from the system theme.

    Args:
        icon_name: The freedesktop icon name (e.g., "applications-graphics")
        size: Preferred icon size in pixels

    Returns:
        Path to the icon file, or None if not found
    """
    if not icon_name:
        return None

    # If it's already a path, return it
    if os.path.isabs(icon_name) and os.path.exists(icon_name):
        return icon_name

    # Icon theme directories (prioritize user's current theme)
    theme = _get_current_icon_theme()
    icon_dirs = [
        # User's current theme first (matches desktop appearance)
        f"/usr/share/icons/{theme}",
        os.path.expanduser(f"~/.local/share/icons/{theme}"),
        # Breeze variants (KDE default)
        "/usr/share/icons/breeze-dark",
        "/usr/share/icons/breeze",
        # Fallback themes
        "/usr/share/icons/hicolor",
        os.path.expanduser("~/.local/share/icons/hicolor"),
        "/usr/share/icons/Adwaita",
        "/usr/share/icons/gnome",
        "/usr/share/pixmaps",
    ]

    # Preferred sizes (in order of preference)
    sizes = [str(size), "32", "48", "24", "64", "22", "16", "256"]

    # Extensions to try (SVG supported via ImageMagick convert)
    extensions = [".svg", ".png", ".xpm"]

    for icon_dir in icon_dirs:
        if not os.path.exists(icon_dir):
            continue

        # Try size-specific directories
        for sz in sizes:
            for category in ["apps", "categories", "mimetypes", "actions", "places", "devices"]:
                for ext in extensions:
                    # Standard freedesktop structure: theme/size/category/name.ext
                    path = os.path.join(icon_dir, f"{sz}x{sz}", category, f"{icon_name}{ext}")
                    if os.path.exists(path):
                        return path
                    # Some themes use: theme/category/size/name.ext
                    path = os.path.join(icon_dir, category, sz, f"{icon_name}{ext}")
                    if os.path.exists(path):
                        return path

        # Try scalable SVGs last (they're harder to load)
        for category in ["apps", "categories", "mimetypes", "actions", "places", "devices"]:
            path = os.path.join(icon_dir, "scalable", category, f"{icon_name}.svg")
            if os.path.exists(path):
                return path

        # Try direct lookup in pixmaps
        for ext in extensions:
            path = os.path.join(icon_dir, f"{icon_name}{ext}")
            if os.path.exists(path):
                return path

    return None


def load_icon_image(icon_name: str, size: int = 24) -> Optional[ImageTk.PhotoImage]:
    """Load an icon as a PhotoImage for use in tkinter.

    Args:
        icon_name: The freedesktop icon name
        size: Desired size in pixels

    Returns:
        PhotoImage object, or None if icon couldn't be loaded
    """
    path = find_icon_path(icon_name, size)
    if not path:
        return None

    try:
        if path.endswith(".svg"):
            # Convert SVG to PNG using rsvg-convert (best quality) or fallbacks
            import io

            # Try rsvg-convert first (proper SVG rendering with correct colors)
            result = subprocess.run(
                ["rsvg-convert", "-w", str(size), "-h", str(size), path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and result.stdout:
                img = Image.open(io.BytesIO(result.stdout))
            else:
                # Fallback: try cairosvg
                try:
                    import cairosvg
                    png_data = cairosvg.svg2png(url=path, output_width=size, output_height=size)
                    img = Image.open(io.BytesIO(png_data))
                except (ImportError, Exception):
                    # Last resort: ImageMagick (may have color issues)
                    result = subprocess.run(
                        ["convert", "-background", "none", "-resize", f"{size}x{size}", path, "png:-"],
                        capture_output=True, timeout=5
                    )
                    if result.returncode != 0:
                        return None
                    img = Image.open(io.BytesIO(result.stdout))
        else:
            img = Image.open(path)
            img = img.resize((size, size), Image.Resampling.LANCZOS)

        # Convert to RGBA if necessary
        if img.mode != "RGBA":
            img = img.convert("RGBA")

        return ImageTk.PhotoImage(img)
    except Exception:
        return None


# --- Thread-safe PIL image cache for async icon browser ---
_pil_image_cache: Dict[tuple, Optional[Image.Image]] = {}


def load_pil_image(path: str, size: int) -> Optional[Image.Image]:
    """Load an icon file as a PIL Image (thread-safe, cached).

    Unlike load_icon_image(), this takes a resolved file path (no icon name lookup)
    and returns a PIL Image instead of PhotoImage (which must be created on the main thread).
    """
    key = (path, size)
    if key in _pil_image_cache:
        return _pil_image_cache[key]

    try:
        if path.endswith(".svg"):
            import io
            result = subprocess.run(
                ["rsvg-convert", "-w", str(size), "-h", str(size), path],
                capture_output=True, timeout=5
            )
            if result.returncode == 0 and result.stdout:
                img = Image.open(io.BytesIO(result.stdout))
            else:
                try:
                    import cairosvg
                    png_data = cairosvg.svg2png(url=path, output_width=size, output_height=size)
                    img = Image.open(io.BytesIO(png_data))
                except (ImportError, Exception):
                    result = subprocess.run(
                        ["convert", "-background", "none", "-resize", f"{size}x{size}", path, "png:-"],
                        capture_output=True, timeout=5
                    )
                    if result.returncode != 0:
                        _pil_image_cache[key] = None
                        return None
                    img = Image.open(io.BytesIO(result.stdout))
        else:
            img = Image.open(path)
            img = img.resize((size, size), Image.Resampling.LANCZOS)

        if img.mode != "RGBA":
            img = img.convert("RGBA")

        img.load()  # Detach from file handle
        _pil_image_cache[key] = img
        return img
    except Exception:
        _pil_image_cache[key] = None
        return None


# ================= GUI APPLICATION =================

class InstallerApp:
    # Theme definitions: emoji, name, colors
    THEMES = {
        "light": {
            "emoji": "☀️",
            "name": "Light",
            "bg": "#f5f5f5",
            "fg": "#2c3e50",
            "title": "#2980b9",
            "category": "#e67e22",
            "accent": "#3498db",
            "muted": "#7f8c8d",
            "success": "#27ae60",
            "error": "#e74c3c",
            "tag_gui": "#3498db",   # Blue for GUI
            "tag_cli": "#8e44ad",   # Purple for CLI
            "tag_icon": "#27ae60",  # Green for Icon
            "canvas_bg": "#f5f5f5",
            "panel": "#ffffff",
            "panel2": "#ececec",
            "border": "#dcdcdc",
            "log_bg": "#1e1e1e",
            "log_fg": "#d4d4d4",
        },
        "dark": {
            "emoji": "🌙",
            "name": "Dark",
            "bg": "#1e1e1e",
            "fg": "#d4d4d4",
            "title": "#61afef",
            "category": "#e5c07b",
            "accent": "#61afef",
            "muted": "#5c6370",
            "success": "#98c379",
            "error": "#e06c75",
            "tag_gui": "#61afef",   # Blue for GUI
            "tag_cli": "#c678dd",   # Purple for CLI
            "tag_icon": "#98c379",  # Green for Icon
            "canvas_bg": "#1e1e1e",
            "panel": "#282c34",
            "panel2": "#22262d",
            "border": "#3a3f4b",
            "log_bg": "#282c34",
            "log_fg": "#abb2bf",
        },
        "forest": {
            "emoji": "🌲",
            "name": "Forest",
            "bg": "#1a2f1a",
            "fg": "#c8e6c9",
            "title": "#81c784",
            "category": "#a5d6a7",
            "accent": "#66bb6a",
            "muted": "#6b8e6b",
            "success": "#4caf50",
            "error": "#ef5350",
            "tag_gui": "#64b5f6",   # Blue for GUI
            "tag_cli": "#ba68c8",   # Purple for CLI
            "tag_icon": "#81c784",  # Green for Icon
            "canvas_bg": "#1a2f1a",
            "panel": "#234023",
            "panel2": "#1e371e",
            "border": "#335633",
            "log_bg": "#0d1f0d",
            "log_fg": "#a5d6a7",
        },
        "ocean": {
            "emoji": "🌊",
            "name": "Ocean",
            "bg": "#0a1929",
            "fg": "#b2ebf2",
            "title": "#4dd0e1",
            "category": "#80deea",
            "accent": "#26c6da",
            "muted": "#546e7a",
            "success": "#26a69a",
            "error": "#ef5350",
            "tag_gui": "#4dd0e1",   # Cyan for GUI
            "tag_cli": "#ce93d8",   # Purple for CLI
            "tag_icon": "#26a69a",  # Teal for Icon
            "canvas_bg": "#0a1929",
            "panel": "#0f2740",
            "panel2": "#0c2034",
            "border": "#1c3a57",
            "log_bg": "#001e3c",
            "log_fg": "#80deea",
        },
        "sunset": {
            "emoji": "🌅",
            "name": "Sunset",
            "bg": "#2d1b2d",
            "fg": "#ffd6e0",
            "title": "#ff8a80",
            "category": "#ffab91",
            "accent": "#ff7043",
            "muted": "#8d6e8d",
            "success": "#c5e1a5",
            "error": "#ff5252",
            "tag_gui": "#ff8a80",   # Coral for GUI
            "tag_cli": "#ea80fc",   # Pink for CLI
            "tag_icon": "#c5e1a5",  # Light green for Icon
            "canvas_bg": "#2d1b2d",
            "panel": "#3d283d",
            "panel2": "#342134",
            "border": "#523a52",
            "log_bg": "#1a0a1a",
            "log_fg": "#ffccbc",
        },
    }

    def __init__(self, root: tk.Tk, tools: List[ToolEntry]):
        self.root = root
        self.root.withdraw()  # Hide until properly sized
        self.tools = tools
        self.root.title(WINDOW_TITLE)
        self._set_window_icon()
        saved_theme = load_config().get("theme", "ocean")
        self.current_theme = saved_theme if saved_theme in self.THEMES else "ocean"

        self.check_vars: Dict[str, tk.BooleanVar] = {}
        self.autostart_vars: Dict[str, tk.BooleanVar] = {}  # Autostart checkboxes
        self.skill_vars: Dict[str, tk.BooleanVar] = {}      # Per-row "Skill" checkbox (only for tools with skill_name)
        self.status_labels: Dict[str, ttk.Label] = {}
        self.icon_labels: Dict[str, ttk.Label] = {}  # For displaying tool icons
        self.icon_cache: Dict[str, Optional[ImageTk.PhotoImage]] = {}  # Prevent GC
        self.tools_by_key: Dict[str, ToolEntry] = {}  # For icon click lookup
        self.tk_frames: List[tk.Frame] = []  # tk.Frame instances that need bg updates on theme change
        self.tk_widgets: List[tk.Widget] = []  # Other tk widgets that need bg updates
        self.usage_counts: Dict[str, int] = get_all_usage_counts()  # Tool usage statistics

        # Search/filter tracking
        self.search_var = tk.StringVar()
        self.category_widgets: Dict[str, tk.Widget] = {}  # category_name -> frame
        self.tool_group_data: list = []  # list of dicts with filter info

        # Index tools by key for quick lookup
        for tool in tools:
            key = f"{tool.category}_{tool.name}"
            self.tools_by_key[key] = tool

        # Detect orphaned shortcuts (fast operation)
        self.orphan_desktops = find_orphan_desktop_files()
        self.orphan_aliases = find_orphan_aliases()

        self._setup_ui()
        self._position_window()
        self.root.deiconify()  # Show now that it's properly sized

        # Show orphan warning after window is visible
        if self.orphan_desktops or self.orphan_aliases:
            self.root.after(100, self._show_orphan_warning)

        # Opt-in: silently apply pending local updates on this launch, visibly
        # logged (see _maybe_auto_update_on_startup). Runs after the orphan
        # warning so the two don't fight over the log/dialog at the same tick.
        self.root.after(200, self._maybe_auto_update_on_startup)

    # Standard freedesktop icon sizes. wm iconphoto silently fails to set
    # _NET_WM_ICON at all when handed only a source-resolution (e.g. 512x512)
    # image on some Tk/X11 combinations, so multiple smaller sizes are passed
    # and the window manager picks the one it wants.
    _ICON_SIZES = (16, 24, 32, 48, 64, 128)

    def _set_window_icon(self) -> None:
        """Apply SELF_DESKTOP_ICON to the live window (taskbar/titlebar).

        SELF_DESKTOP_ICON is otherwise only written into the .desktop file's
        Icon= line, so a run straight from a terminal showed the generic Tk
        icon instead. Only an absolute image path can be used here (a
        freedesktop icon *name* has no file to load without a theme lookup).
        """
        icon_path = SELF_DESKTOP_ICON
        if not icon_path or not os.path.isabs(icon_path) or not os.path.isfile(icon_path):
            return
        try:
            if _HAVE_PIL:
                source = Image.open(icon_path).convert("RGBA")
                images = [ImageTk.PhotoImage(source.resize((size, size), Image.LANCZOS))
                          for size in self._ICON_SIZES]
            else:
                images = [tk.PhotoImage(file=icon_path)]
            self.root.iconphoto(True, *images)
            self._icon_photos = images  # keep references; Tk drops the icon otherwise
        except Exception:
            pass  # cosmetic only; never block startup over a bad/unsupported icon

    def _get_primary_monitor_geometry(self) -> tuple[int, int, int, int]:
        """Get primary monitor geometry (x, y, width, height). Falls back to tkinter defaults."""
        try:
            import subprocess
            result = subprocess.run(
                ["xrandr", "--query"],
                capture_output=True, text=True, timeout=2
            )
            for line in result.stdout.splitlines():
                if " primary " in line:
                    # Parse: "DP-0 connected primary 2560x1440+0+0 ..."
                    parts = line.split()
                    for part in parts:
                        if "x" in part and "+" in part:
                            # Format: WIDTHxHEIGHT+X+Y
                            geom = part.split("+")
                            dims = geom[0].split("x")
                            return int(geom[1]), int(geom[2]), int(dims[0]), int(dims[1])
        except Exception:
            pass
        # Fallback: assume single monitor at origin
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def _position_window(self):
        """Position window: fit to content if possible, otherwise use full screen height with scrollbar."""
        self.root.update_idletasks()

        mon_x, mon_y, mon_width, mon_height = self._get_primary_monitor_geometry()

        # Get actual content dimensions
        scrollable_height = self.scrollable_frame.winfo_reqheight()
        content_width = self.scrollable_frame.winfo_reqwidth() + 60  # padding
        # Add height for title (~60), footer (~60), log header (~40), status bar (~30), padding (~40)
        non_scrollable_height = 230
        content_height = scrollable_height + non_scrollable_height

        # Clamp width to reasonable bounds (max 800 for usability)
        window_width = max(500, min(content_width, 800, mon_width - 100))

        # Use content height if it fits, otherwise use full monitor height
        if content_height <= mon_height:
            window_height = content_height
            # Center vertically on primary monitor
            y = mon_y + (mon_height - window_height) // 2
            # Hide scrollbar initially - content fits
            self.scrollbar.grid_forget()
        else:
            # Use full monitor height, align to top
            window_height = mon_height
            y = mon_y
            # Show scrollbar initially - content exceeds screen
            self.scrollbar.grid(row=0, column=1, sticky="ns")

        # Center horizontally on primary monitor
        x = mon_x + (mon_width - window_width) // 2

        self.root.geometry(f"{window_width}x{window_height}+{x}+{y}")
        # Allow resizing in both directions
        self.root.resizable(True, True)

        # Always enable mouse wheel scrolling
        self._bind_mousewheel()

        # Bind to canvas configure to dynamically show/hide scrollbar on resize
        self.canvas.bind("<Configure>", self._on_canvas_configure)

    def _bind_mousewheel(self):
        """Bind mouse wheel events for scrolling."""
        def _on_mousewheel(event):
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        def _on_mousewheel_linux(event):
            if event.num == 4:
                self.canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                self.canvas.yview_scroll(1, "units")

        # Linux uses Button-4 and Button-5 for scroll
        self.canvas.bind_all("<Button-4>", _on_mousewheel_linux)
        self.canvas.bind_all("<Button-5>", _on_mousewheel_linux)
        # Windows/Mac use MouseWheel
        self.canvas.bind_all("<MouseWheel>", _on_mousewheel)

    def _fit_canvas_width(self):
        """Stretch the scrollable content to the canvas' current width.

        The canvas window item keeps the width it is given, so without this the
        table falls back to its requested width — the minimum every column
        needs — and leaves the right of the window empty.
        """
        width = self.canvas.winfo_width()
        if width > 1:
            self.canvas.itemconfig(self.canvas_window, width=width)

    def _on_canvas_configure(self, event):
        """Handle canvas resize: adjust content width and show/hide scrollbar."""
        # Make content fill canvas width
        self.canvas.itemconfig(self.canvas_window, width=event.width)

        # Show scrollbar only when content exceeds visible height
        bbox = self.canvas.bbox("all")
        if bbox:
            content_height = bbox[3] - bbox[1]
            canvas_height = event.height
            if content_height > canvas_height:
                self.scrollbar.grid(row=0, column=1, sticky="ns")
            else:
                self.scrollbar.grid_forget()

    def _create_checkbox_images(self, theme: dict):
        """Create custom checkbox images with thicker checkmark.

        Runs on every theme/accent application. ttk elements can't be
        redefined in place, so each pass registers a fresh, uniquely-named
        image element and repoints the TCheckbutton layout at it — this is
        what makes the checkmark follow theme switches and 🎨 accent picks.
        """
        # Skip if the palette that drives the images hasn't changed.
        sig = (theme.get("panel", theme["bg"]), theme["fg"], theme["accent"])
        if getattr(self, "_checkbox_images_sig", None) == sig:
            return
        self._checkbox_images_sig = sig

        size = 14
        # Store references to prevent garbage collection. Older generations
        # stay referenced too — their elements remain registered in ttk.
        if not hasattr(self, '_checkbox_image_refs'):
            self._checkbox_image_refs = []
        self._checkbox_images = {}

        # Colors from theme. Box interior uses the card "panel" surface so
        # the indicator blends into the card rows it sits on.
        bg = theme.get("panel", theme["bg"])
        fg = theme["fg"]
        accent = theme["accent"]

        # Create unchecked box
        unchecked = tk.PhotoImage(width=size, height=size)
        unchecked.put(fg, to=(0, 0, size, 1))  # top
        unchecked.put(fg, to=(0, size-1, size, size))  # bottom
        unchecked.put(fg, to=(0, 0, 1, size))  # left
        unchecked.put(fg, to=(size-1, 0, size, size))  # right
        unchecked.put(bg, to=(1, 1, size-1, size-1))  # fill
        self._checkbox_images['unchecked'] = unchecked

        # Create checked box with thick checkmark
        checked = tk.PhotoImage(width=size, height=size)
        checked.put(accent, to=(0, 0, size, 1))  # top
        checked.put(accent, to=(0, size-1, size, size))  # bottom
        checked.put(accent, to=(0, 0, 1, size))  # left
        checked.put(accent, to=(size-1, 0, size, size))  # right
        checked.put(bg, to=(1, 1, size-1, size-1))  # fill

        # Draw thick checkmark (multiple pixels wide)
        for offset in range(-1, 2):  # -1, 0, 1 for 3px thickness
            # Short leg of check: from (3,7) to (5,9)
            for i in range(3):
                x, y = 3 + i, 7 + i
                if 1 <= x + offset < size - 1 and 1 <= y < size - 1:
                    checked.put(accent, to=(x + offset, y, x + offset + 1, y + 1))
            # Long leg of check: from (5,9) to (11,3)
            for i in range(7):
                x, y = 5 + i, 9 - i
                if 1 <= x + offset < size - 1 and 1 <= y < size - 1:
                    checked.put(accent, to=(x + offset, y, x + offset + 1, y + 1))
        self._checkbox_images['checked'] = checked
        self._checkbox_image_refs += [unchecked, checked]

        # Register a fresh element (old ones can't be redefined) and repoint
        # the shared TCheckbutton layout at it. Derived styles like
        # Card.TCheckbutton fall back to this layout automatically.
        seq = getattr(self, '_checkbox_element_seq', 0) + 1
        self._checkbox_element_seq = seq
        element = f"custom.indicator{seq}"
        self.style.element_create(element, "image", unchecked,
                                  ("selected", checked), sticky="w")
        self.style.layout("TCheckbutton", [
            ("Checkbutton.padding", {"sticky": "nswe", "children": [
                (element, {"side": "left", "sticky": ""}),
                ("Checkbutton.label", {"side": "left", "sticky": "nswe"})
            ]})
        ])

    @staticmethod
    def _mix_hex(a: str, b: str, frac: float) -> str:
        """Blend hex color *a* toward *b* by *frac* (0..1) and return hex."""
        def to_rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
        ar, ag, ab = to_rgb(a)
        br, bg, bb = to_rgb(b)
        r = round(ar + (br - ar) * frac)
        g = round(ag + (bg - ag) * frac)
        bl = round(ab + (bb - ab) * frac)
        return f"#{r:02x}{g:02x}{bl:02x}"

    @staticmethod
    def _derive_theme(stock: dict, base_hex: str) -> dict:
        """Re-derive a whole theme from a base color.

        Every color slot keeps its lightness (contrast structure) and
        roughly its saturation from the stock theme, but is re-hued to the
        base color's hue — so one pick re-tints the entire UI, scaled by
        the base's saturation (a gray base gives a monochrome theme).
        Semantic colors (success/error) are kept; the CLI/Icon tag badges
        get fixed hue offsets so the three tags stay distinguishable.
        """
        import colorsys

        def hex_to_rgb(h):
            h = h.lstrip("#")
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

        base_h, _base_l, base_s = colorsys.rgb_to_hls(*hex_to_rgb(base_hex))
        sat_scale = 0.25 + 0.75 * base_s  # gray base → desaturated theme

        def rehue(hex_color, hue_offset=0.0, min_s=0.0):
            h, l, s = colorsys.rgb_to_hls(*hex_to_rgb(hex_color))
            s = min(1.0, max(s, min_s) * sat_scale)
            r, g, b = colorsys.hls_to_rgb((base_h + hue_offset) % 1.0, l, s)
            return f"#{round(r * 255):02x}{round(g * 255):02x}{round(b * 255):02x}"

        derived = dict(stock)
        # Surfaces: subtle tint of the base hue at the stock lightness.
        for slot in ("bg", "canvas_bg", "panel", "panel2", "border", "log_bg"):
            derived[slot] = rehue(stock[slot], min_s=0.18)
        # Text: faint tint so it harmonises without losing readability.
        for slot in ("fg", "muted", "log_fg"):
            derived[slot] = rehue(stock[slot], min_s=0.10)
        # Highlights: full re-hue; accent is the picked color itself.
        for slot in ("title", "category", "tag_gui"):
            derived[slot] = rehue(stock[slot], min_s=0.45)
        derived["accent"] = base_hex
        derived["tag_cli"] = rehue(stock["tag_cli"], hue_offset=1 / 3, min_s=0.45)
        derived["tag_icon"] = rehue(stock["tag_icon"], hue_offset=-1 / 3, min_s=0.45)
        # success / error / emoji / name stay stock (semantic).
        return derived

    def _select_theme(self, theme_name: str):
        """Explicit theme-button click: reset any accent override, then apply."""
        cfg = load_config()
        overrides = cfg.get("accent_overrides", {})
        had_override = overrides.pop(theme_name, None) is not None
        if had_override:
            cfg["accent_overrides"] = overrides
            save_config(cfg)
        self._apply_theme(theme_name)
        # Same theme re-clicked to shed its override: _apply_theme won't
        # rebuild (no name change), so rebuild here to drop the old accent.
        if had_override and theme_name == self.current_theme:
            self._rebuild_ui()

    def _pick_accent(self):
        """🎨 button: pick a base color the whole theme is re-derived from."""
        from tkinter import colorchooser
        rgb = colorchooser.askcolor(color=self.theme["accent"], parent=self.root,
                                    title="Theme base color")[1]
        if not rgb:
            return
        cfg = load_config()
        cfg.setdefault("accent_overrides", {})[self.current_theme] = rgb
        save_config(cfg)
        # _rebuild_ui → _setup_ui → _apply_theme re-derives the palette
        # from the stored base color.
        self._rebuild_ui()

    def _apply_theme(self, theme_name: str):
        """Apply a color theme to the UI. On theme change, rebuilds entire UI."""
        if theme_name not in self.THEMES:
            return

        old_theme = getattr(self, 'current_theme', None)
        self.current_theme = theme_name
        cfg = load_config()
        cfg["theme"] = theme_name
        save_config(cfg)
        # Effective theme dict: stock palette, or — when the 🎨 picker set a
        # base color for this theme (persisted in config) — a full palette
        # re-derived from that base color.
        t = dict(self.THEMES[theme_name])
        override = cfg.get("accent_overrides", {}).get(theme_name)
        if override:
            t = self._derive_theme(t, override)
        self.theme = t

        # Configure ttk styles with theme colors
        self.style.configure(".", background=t["bg"], foreground=t["fg"])
        self.style.configure("TFrame", background=t["bg"])
        self.style.configure("TLabel", background=t["bg"], foreground=t["fg"])
        self.style.configure("TCheckbutton", background=t["bg"], foreground=t["fg"])
        self.style.configure("TButton", background=t["bg"])
        self.style.configure("Header.TLabel", font=("", 12, "bold"), foreground=t["fg"])
        self.style.configure("Title.TLabel", font=("", 18, "bold"), foreground=t["title"])
        self.style.configure("ToolName.TLabel", font=("", 10, "bold"), background=t["bg"], foreground=t["fg"])
        self.style.configure("Category.TLabel", font=("", 11, "bold"), foreground=t["category"], background=t["bg"])
        self.style.configure("Accent.TButton", font=("", 10, "bold"))
        self.style.configure("Muted.TLabel", foreground=t["muted"], background=t["bg"])
        self.style.configure("Success.TLabel", foreground=t["success"], background=t["bg"])
        # Tag badge styles
        self.style.configure("TagGUI.TLabel", font=("", 8, "bold"), foreground=t["tag_gui"], background=t["bg"])
        self.style.configure("TagCLI.TLabel", font=("", 8, "bold"), foreground=t["tag_cli"], background=t["bg"])
        self.style.configure("TagIcon.TLabel", font=("", 8, "bold"), foreground=t["tag_icon"], background=t["bg"])
        self.style.configure("Theme.TButton", padding=2)
        # Card styles — tool rows render on a "panel" surface one step
        # lighter than the window bg, with a thin "border" outline.
        self.style.configure("Card.TFrame", background=t["panel"])
        self.style.configure("Card.TLabel", background=t["panel"], foreground=t["fg"])
        self.style.configure("CardToolName.TLabel", font=("", 10, "bold"),
                             background=t["panel"], foreground=t["fg"])
        self.style.configure("CardMuted.TLabel", foreground=t["muted"], background=t["panel"])
        self.style.configure("Card.TCheckbutton", background=t["panel"], foreground=t["fg"])
        self.style.map("Card.TCheckbutton", background=[("active", t["panel"])])
        # Hover-emphasis card styles — a row lifts onto a slightly accent-tinted
        # surface while hovered. Backgrounds only: the name keeps its normal font
        # and color so nothing resizes or recolors. Swapped in per-row by
        # _bind_row_hover; the panel color is stashed on the theme so tk-frame
        # backgrounds can match.
        panel_hover = self._mix_hex(t["panel"], t["accent"], 0.14)
        t["panel_hover"] = panel_hover
        self.style.configure("CardHover.TFrame", background=panel_hover)
        self.style.configure("CardHover.TLabel", background=panel_hover, foreground=t["fg"])
        self.style.configure("CardHoverToolName.TLabel", font=("", 10, "bold"),
                             background=panel_hover, foreground=t["fg"])
        self.style.configure("CardHoverMuted.TLabel", foreground=t["muted"], background=panel_hover)
        self.style.configure("CardHover.TCheckbutton", background=panel_hover, foreground=t["fg"])
        self.style.map("CardHover.TCheckbutton", background=[("active", panel_hover)])
        # LabelFrame styling for dialogs
        self.style.configure("TLabelframe", background=t["bg"], foreground=t["fg"])
        self.style.configure("TLabelframe.Label", background=t["bg"], foreground=t["accent"])
        # Scrollbar styling - make it visible and theme-aware
        scrollbar_trough = t["bg"]
        scrollbar_thumb = t["muted"]
        scrollbar_thumb_active = t["accent"]
        self.style.configure("TScrollbar",
                             background=scrollbar_thumb,
                             troughcolor=scrollbar_trough,
                             bordercolor=scrollbar_trough,
                             arrowcolor=t["fg"],
                             width=14)
        self.style.map("TScrollbar",
                       background=[("active", scrollbar_thumb_active),
                                   ("pressed", scrollbar_thumb_active)])
        # Custom checkbox images with thicker checkmark
        self._create_checkbox_images(t)

        self.root.configure(bg=t["bg"])

        # If theme changed (not initial setup), rebuild the entire UI
        if old_theme is not None and old_theme != theme_name:
            self._rebuild_ui()

    def _rebuild_ui(self):
        """Destroy and recreate the entire UI for clean theme switch."""
        # Tear down any open update tooltip / pending timer before its anchor dies.
        self._hide_update_tip()
        # Save current window geometry
        geometry = self.root.geometry()

        # Clear all tracking lists
        self.check_vars.clear()
        self.autostart_vars.clear()
        self.skill_vars.clear()
        self.status_labels.clear()
        self.icon_labels.clear()
        self.tk_frames.clear()
        self.tk_widgets.clear()
        self.category_widgets.clear()
        self.tool_group_data.clear()

        # Destroy all children of root
        for child in self.root.winfo_children():
            child.destroy()

        # Rebuild UI
        self._setup_ui()

        # Restore window geometry
        self.root.geometry(geometry)

        # _setup_ui built a new canvas, and the old one died with the old UI.
        # The mouse wheel is bound to the widget by name, so re-bind it, and
        # give the new window item the canvas width at once: without it the
        # table keeps its requested width, which is the sum of the minimum
        # column widths, and sits at the left with empty space to its right.
        self._bind_mousewheel()
        self.root.update_idletasks()
        self._fit_canvas_width()

    def _setup_ui(self):
        # Configure styles
        self.style = ttk.Style()
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self._apply_theme(self.current_theme)
        
        # Configure the root window weight
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        main_container = ttk.Frame(self.root, padding="20")
        main_container.grid(row=0, column=0, sticky="nsew")
        main_container.columnconfigure(0, weight=1)
        main_container.rowconfigure(2, weight=1)

        # Title
        title_frame = ttk.Frame(main_container)
        title_frame.grid(row=0, column=0, pady=(0, 10), sticky="ew")

        ttk.Label(title_frame, text=WINDOW_TITLE, style="Title.TLabel").pack(side="left")
        self.tools_count_label = ttk.Label(title_frame, text=f"({len(self.tools)} tools found)", style="Muted.TLabel")
        self.tools_count_label.pack(side="left", padx=10, pady=(10, 0))

        # Theme buttons (right side)
        theme_frame = ttk.Frame(title_frame)
        theme_frame.pack(side="right")
        t = self.theme
        self.theme_buttons = {}
        for theme_name, theme_data in self.THEMES.items():
            btn = tk.Button(
                theme_frame,
                text=theme_data["emoji"],
                font=("", 14),
                width=2,
                relief="flat",
                bd=0,
                highlightthickness=0,
                bg=t["bg"],
                activebackground=t["accent"],
                cursor="hand2",
                command=lambda tn=theme_name: self._select_theme(tn)
            )
            btn.pack(side="left", padx=2)
            self.theme_buttons[theme_name] = btn

        # Base-color picker — re-derives the whole current theme from one
        # picked color, persisted per theme; re-clicking the emoji resets.
        accent_btn = tk.Button(
            theme_frame,
            text="🎨",
            font=("", 14),
            width=2,
            relief="flat",
            bd=0,
            highlightthickness=0,
            bg=t["bg"],
            fg=t["accent"],
            activebackground=t["accent"],
            cursor="hand2",
            command=self._pick_accent,
        )
        accent_btn.pack(side="left", padx=(8, 2))
        self._attach_tooltip(accent_btn,
                             "Re-tint this theme from a base color "
                             "(re-click the theme emoji to reset)")

        # Search bar
        search_frame = ttk.Frame(main_container)
        search_frame.grid(row=1, column=0, pady=(0, 10), sticky="ew")

        self.search_entry = tk.Entry(
            search_frame, textvariable=self.search_var,
            font=("", 11), bg=t["log_bg"], fg=t["fg"],
            insertbackground=t["fg"], relief="flat",
            highlightthickness=1, highlightcolor=t["accent"],
            highlightbackground=t["muted"],
        )
        self.search_entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.tk_widgets.append(self.search_entry)

        # Placeholder text
        self._search_placeholder = "Search tools..."
        self._search_has_focus = False
        # search_var persists across theme rebuilds — reset to a single
        # placeholder only when no real query is stored, never append.
        current_query = self.search_var.get()
        if not current_query or current_query.startswith(self._search_placeholder):
            self.search_entry.delete(0, tk.END)
            self.search_entry.insert(0, self._search_placeholder)
            self.search_entry.config(fg=t["muted"])
        else:
            self.search_entry.config(fg=t["fg"])

        def _on_search_focus_in(event):
            self._search_has_focus = True
            if self.search_entry.get() == self._search_placeholder:
                self.search_entry.delete(0, tk.END)
                self.search_entry.config(fg=t["fg"])

        def _on_search_focus_out(event):
            self._search_has_focus = False
            if not self.search_entry.get():
                self.search_entry.insert(0, self._search_placeholder)
                self.search_entry.config(fg=t["muted"])

        self.search_entry.bind("<FocusIn>", _on_search_focus_in)
        self.search_entry.bind("<FocusOut>", _on_search_focus_out)
        # search_var survives rebuilds — register the filter trace only once,
        # or every theme switch stacks another copy.
        if not getattr(self, "_search_trace_added", False):
            self.search_var.trace_add("write", lambda *_: self._apply_search_filter())
            self._search_trace_added = True

        # Clear button
        self.search_clear_btn = tk.Button(
            search_frame, text="✕", font=("", 10),
            relief="flat", bd=0, highlightthickness=0,
            bg=t["log_bg"], fg=t["muted"],
            activebackground=t["log_bg"], activeforeground=t["fg"],
            cursor="hand2",
            command=self._clear_search,
        )
        self.search_clear_btn.pack(side="left", padx=(0, 0))
        self.tk_widgets.append(self.search_clear_btn)

        # Scrollable Area
        self.outer_frame = ttk.Frame(main_container, relief="flat")
        self.outer_frame.grid(row=2, column=0, sticky="nsew")
        self.outer_frame.columnconfigure(0, weight=1)
        self.outer_frame.rowconfigure(0, weight=1)

        bg_color = self.root.cget("bg")
        self.canvas = tk.Canvas(self.outer_frame, highlightthickness=0, bg=bg_color)
        self.scrollbar = ttk.Scrollbar(self.outer_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )

        self.canvas_window = self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        # Make scrollable frame expand to fill canvas width
        self.scrollable_frame.columnconfigure(0, weight=1)

        # Group rows by the GROUP_BY field (`capability` by default, `category`
        # when the wrapper bands by its own label), then by script_path within
        # each band. Both fields are always populated (get_metadata_native falls
        # back to the folder label), so this never keys on "". Sorted so the band
        # headers are stable across runs.
        # NB: must not reuse `t` here — it holds the theme dict for the whole
        # of _setup_ui (footer, log box and search-focus closures read it).
        tools_by_group: Dict[str, List[ToolEntry]] = {}
        for entry in self.tools:
            tools_by_group.setdefault(_group_label(entry), []).append(entry)

        # Convert to groups within each band
        categories: Dict[str, List[ToolGroup]] = {}
        for group_label in sorted(tools_by_group):
            categories[group_label] = group_tools(tools_by_group[group_label])

        self.expand_vars: Dict[str, tk.BooleanVar] = {}  # Track expanded state
        self.children_frames: Dict[str, ttk.Frame] = {}  # Track child frames for show/hide

        # Column headers — the first row of the tools table (draws the top line)
        header_container = ttk.Frame(self.scrollable_frame)
        header_container.grid(row=0, column=0, sticky="ew", pady=(5, 0))
        theme_bg = self.theme["bg"]
        header_row = self._table_row_surface(header_container, theme_bg, top_line=True)

        # Left: Checkbox spacer
        ttk.Label(header_row, text="", width=3).pack(side="left", padx=(10, 10))

        # Right side headers container (pack right so it claims space first)
        right_headers = tk.Frame(header_row, bg=theme_bg, highlightthickness=0)
        right_headers.pack(side="right", fill="y")
        self.tk_frames.append(right_headers)

        # Tool name header (takes remaining space)
        ttk.Label(header_row, text="Tool", style="Header.TLabel").pack(
            side="left", fill="x", expand=True, pady=4)

        # Fixed-width column headers on the shared table grid
        header_cells = self._build_right_cells(right_headers, theme_bg)
        for col_key, text in (("uses", "Uses"), ("status", "Status"),
                              ("skill", "Skill"), ("icon", "Icon"),
                              ("autostart", "Auto-Start")):
            ttk.Label(header_cells[col_key], text=text, style="Muted.TLabel",
                      anchor="center").pack(expand=True, fill="both")

        current_row = 1
        for category, cat_groups in categories.items():
            # Category header — a full-width section band inside the table,
            # on the window bg so it reads darker than the panel-bg tool rows.
            cat_container = ttk.Frame(self.scrollable_frame)
            cat_container.grid(row=current_row, column=0, sticky="ew")
            band = self._table_row_surface(cat_container, theme_bg)

            accent_bar = tk.Frame(band, bg=self.theme["accent"], width=3, height=14)
            accent_bar.pack(side="left", padx=(8, 8), pady=7)
            self.tk_frames.append(accent_bar)
            ttk.Label(band, text=category.upper(), style="Category.TLabel").pack(side="left")
            self.category_widgets[category] = cat_container

            current_row += 1

            for group in cat_groups:
                current_row = self._render_tool_group(group, current_row)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        # Bound here rather than only in _position_window, which runs once at
        # startup: a theme switch rebuilds the UI, and the new canvas needs the
        # binding that keeps the table as wide as the window.
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        # Scrollbar initially hidden - will be shown in _position_window if needed

        # Footer / Buttons
        footer = ttk.Frame(main_container, padding=(0, 20, 0, 0))
        footer.grid(row=3, column=0, sticky="ew")

        ttk.Button(footer, text="Select All", command=self._select_all).pack(side="left", padx=5)
        ttk.Button(footer, text="Select None", command=self._select_none).pack(side="left", padx=5)

        ttk.Separator(footer, orient="vertical").pack(side="left", fill="y", padx=15)

        # Auto-update-on-startup toggle: persists to config.json so that the
        # NEXT launch of this GUI silently applies any pending local updates —
        # the same network-free reconciliation as clicking the badge to its
        # right — logging what it did to the Operation Log so it stays visible
        # even though it ran automatically. Sits immediately left of the badge.
        self._auto_update_startup_var = tk.BooleanVar(value=get_auto_update_on_startup())
        auto_update_cb = ttk.Checkbutton(
            footer, variable=self._auto_update_startup_var,
            command=self._toggle_auto_update_startup)
        auto_update_cb.pack(side="left", padx=(5, 2))
        self._attach_tooltip(
            auto_update_cb,
            "Auto-update on startup\n\n"
            "When checked, the next time this installer GUI starts it "
            "automatically applies any pending local updates (the same "
            "network-free shortcut/alias refresh as clicking the badge to "
            "the right) and logs what it did to the Operation Log below.")

        # Stale-shortcut indicator (the "Update all" replacement): a count badge —
        # an "N updates pending" pill that falls back to a muted "Up to date", with a
        # hover tooltip detailing which tools drifted and when. Local-only (orphan
        # cleanup + .desktop/alias refresh, no pip).
        self._build_update_btn(footer)

        # Login auto-check toggle: enable/disable the ~/.config/autostart entry that
        # runs `installer.py --check` once per login — applies the same local-only
        # reconciliations the badge does (no pip) and notifies for new tools. Same
        # safety class as the badge, so it sits in the badge's group.
        ttk.Separator(footer, orient="vertical").pack(side="left", fill="y", padx=15)
        self._autostart_check_var = tk.BooleanVar(value=autostart_check_enabled())
        self._autostart_check_cb = ttk.Checkbutton(
            footer, text="Check on login", variable=self._autostart_check_var,
            command=self._toggle_autostart_check)
        self._autostart_check_cb.pack(side="left", padx=5)
        self._attach_tooltip(
            self._autostart_check_cb,
            "Check for updates on login (local, no network)\n\n"
            "Installs a ~/.config/autostart entry that, once per login, applies "
            "network-free reconciliations (drifted shortcuts, renamed aliases, and "
            "stale installed skills — rewritten from your synced source, never pip) "
            "and only notifies for updates that need the network (a new, "
            "not-yet-installed tool).\n\n"
            f"Log: {CHECK_LOG}")

        # "Reinstall deps" is the deliberate, network-touching pip action — kept in
        # its own group (own separator) so it reads as distinct from the local badge.
        ttk.Separator(footer, orient="vertical").pack(side="left", fill="y", padx=15)
        self._reinstall_btn = ttk.Button(footer, text="Reinstall deps", command=self._reinstall_deps)
        self._reinstall_btn.pack(side="left", padx=5)
        self._attach_tooltip(
            self._reinstall_btn,
            "Reinstall dependencies (network)\n\n"
            "Runs 'pip install' from PyPI for every installed tool, then refreshes "
            "all shortcuts. Use this after pulling new code or when a tool fails to "
            "launch with a missing-module error.\n\n"
            "Unlike the update badge (local shortcut refresh, no network), this "
            "reaches the internet and confirms before running.")

        ttk.Separator(footer, orient="vertical").pack(side="left", fill="y", padx=15)

        self._apply_btn = ttk.Button(footer, text="Apply Changes", style="Accent.TButton", command=self._apply_changes)
        self._apply_btn.pack(side="right", padx=5)
        self._apply_highlighted = False
        self._op_in_progress = False  # guards against overlapping bulk operations
        ttk.Button(footer, text="Refresh Status", command=self._update_status_labels).pack(side="right", padx=5)

        # Collapsible Log Area
        self.log_expanded = tk.BooleanVar(value=False)

        log_container = ttk.Frame(main_container)
        log_container.grid(row=4, column=0, sticky="nsew", pady=(15, 0))
        log_container.columnconfigure(0, weight=1)

        # Header row (clickable to expand/collapse)
        log_header = ttk.Frame(log_container)
        log_header.grid(row=0, column=0, sticky="ew")
        log_header.columnconfigure(1, weight=1)

        self.log_toggle_icon = ttk.Label(log_header, text="▶", font=("", 10), cursor="hand2")
        self.log_toggle_icon.grid(row=0, column=0, padx=(0, 5))

        self.log_toggle_label = ttk.Label(log_header, text="Operation Log", font=("", 10, "bold"),
                                          foreground="#7f8c8d", cursor="hand2")
        self.log_toggle_label.grid(row=0, column=1, sticky="w")

        self.log_copy_btn = ttk.Button(log_header, text="📋 Copy", command=self._copy_log)
        self.log_clear_btn = ttk.Button(log_header, text="Clear", command=self._clear_log)

        # Bind click events to toggle
        for widget in (self.log_toggle_icon, self.log_toggle_label):
            widget.bind("<Button-1>", lambda e: self._toggle_log())

        # Log content frame (hidden by default)
        self.log_content_frame = ttk.Frame(log_container)
        self.log_content_frame.columnconfigure(0, weight=1)
        self.log_content_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(self.log_content_frame, height=8, wrap="word", font=("monospace", 9),
                                bg=t["log_bg"], fg=t["log_fg"], insertbackground=t["fg"],
                                relief="flat", highlightthickness=1,
                                highlightbackground=t["border"], highlightcolor=t["border"])
        log_scrollbar = ttk.Scrollbar(self.log_content_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scrollbar.set)

        self.log_text.grid(row=0, column=0, sticky="nsew", pady=(5, 0))
        log_scrollbar.grid(row=0, column=1, sticky="ns", pady=(5, 0))

        # Enable Ctrl+A to select all
        def select_all_log(event):
            self.log_text.tag_add(tk.SEL, "1.0", tk.END)
            self.log_text.mark_set(tk.INSERT, "1.0")
            self.log_text.see(tk.INSERT)
            return "break"
        self.log_text.bind("<Control-a>", select_all_log)

        # Configure log text tags for colored output (theme-derived)
        self.log_text.tag_configure("info", foreground=t["log_fg"])
        self.log_text.tag_configure("success", foreground=t["success"])
        self.log_text.tag_configure("error", foreground=t["error"])
        self.log_text.tag_configure("header", foreground=t["muted"], font=("monospace", 9, "bold"))

        # Replay log history so theme rebuilds don't wipe the log.
        for msg, tag in getattr(self, "_log_history", []):
            self.log_text.insert("end", msg + "\n", tag)
        self.log_text.see("end")

        indicator_frame = ttk.Frame(main_container)
        indicator_frame.grid(row=5, column=0, sticky="ew", pady=(10, 0))

        self.status_bar = ttk.Label(indicator_frame, text="Ready", foreground=t["muted"], font=("", 9, "italic"))
        self.status_bar.pack(side="left")

        # Dynamic command hint (right side) - shown when tools suggest commands
        self.hint_frame = ttk.Frame(indicator_frame)
        # Initially hidden - will be shown when a command hint is set
        self.hint_command = None  # The command to copy

        self.hint_label = ttk.Label(
            self.hint_frame,
            text="",
            font=("monospace", 9),
            foreground="#7f8c8d"
        )
        self.hint_label.pack(side="left", padx=(0, 5))

        self.hint_copy_btn = ttk.Button(
            self.hint_frame,
            text="📋",
            width=3,
            command=self._copy_hint_command
        )
        self.hint_copy_btn.pack(side="left")

        self._update_status_labels()

    def _get_tool_icon(self, icon_name_or_path: str) -> Optional[ImageTk.PhotoImage]:
        """Get or load a tool icon, using cache to prevent garbage collection."""
        if icon_name_or_path in self.icon_cache:
            return self.icon_cache[icon_name_or_path]

        photo = load_icon_image(icon_name_or_path, size=24)
        self.icon_cache[icon_name_or_path] = photo
        return photo

    def _get_effective_icon(self, tool: ToolEntry) -> tuple[str, Optional[ImageTk.PhotoImage]]:
        """Get the effective icon for a tool, checking custom icons first.

        Returns:
            Tuple of (icon_name_or_path, PhotoImage or None)
        """
        key = f"{tool.category}_{tool.name}"
        custom_path = get_custom_icon_path(key)
        if custom_path:
            return custom_path, self._get_tool_icon(custom_path)
        return tool.icon, self._get_tool_icon(tool.icon)

    def _show_icon_dialog(self, tool_key: str):
        """Show dialog to customize a tool's icon."""
        tool = self.tools_by_key.get(tool_key)
        if not tool:
            return

        t = self.theme
        bg, fg, accent, muted = t["bg"], t["fg"], t["accent"], t["muted"]
        input_bg = t.get("canvas_bg", bg)

        dialog = tk.Toplevel(self.root)
        dialog.title(f"Customize Icon - {tool.name}")
        dialog.geometry("600x800")
        dialog.configure(background=bg)
        dialog.update_idletasks()

        # Create scrollable container
        outer_frame = tk.Frame(dialog, bg=bg)
        outer_frame.pack(fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(outer_frame, bg=bg, highlightthickness=0)
        scrollbar = tk.Scrollbar(outer_frame, orient=tk.VERTICAL, command=canvas.yview)
        content_frame = tk.Frame(canvas, bg=bg)

        content_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=content_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Make content fill canvas width
        def on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)
        canvas.bind("<Configure>", on_canvas_configure)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Bind mouse wheel scrolling
        def on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def on_mousewheel_linux(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
        canvas.bind_all("<MouseWheel>", on_mousewheel)
        canvas.bind_all("<Button-4>", on_mousewheel_linux)
        canvas.bind_all("<Button-5>", on_mousewheel_linux)

        # Cleanup function for scroll events
        def cleanup_scroll_bindings():
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        # State for generated images
        generated_images = []  # List of generated image paths
        selected_image = [None]  # Currently selected image path
        dialog._photos = []  # Keep references to prevent GC

        # Find available models
        models_dir = os.path.join(ROOT_DIR, "tools_personal", "diffusers_gui", "models")
        available_models = []
        if os.path.exists(models_dir):
            available_models = [f for f in os.listdir(models_dir) if f.endswith('.safetensors')]

        # Grid preview area (for multiple generated images)
        grid_frame = tk.Frame(content_frame, bg=bg)
        grid_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(20, 5))

        # Current icon display
        current_frame = tk.Frame(grid_frame, bg=bg)
        current_frame.pack(pady=(0, 10))

        icon_path, _ = self._get_effective_icon(tool)
        preview_photo = load_icon_image(icon_path, size=96)
        if preview_photo:
            dialog._photos.append(preview_photo)

        current_label = tk.Label(current_frame, text="Current Icon:", bg=bg, fg=muted, font=("", 9))
        current_label.pack()
        preview_label = tk.Label(current_frame, image=preview_photo if preview_photo else None,
                                 text="[No Icon]" if not preview_photo else "",
                                 bg=bg, fg=fg, font=("", 16))
        preview_label.pack()

        custom_path = get_custom_icon_path(tool_key)
        src = f"Custom: {os.path.basename(custom_path)}" if custom_path else f"Default: {tool.icon}"
        source_label = tk.Label(current_frame, text=src, bg=bg, fg=muted, font=("", 9))
        source_label.pack()

        # Generated images grid container
        gen_grid_label = tk.Label(grid_frame, text="Generated Images (click to select):", bg=bg, fg=fg, font=("", 10, "bold"))
        gen_grid_container = tk.Frame(grid_frame, bg=bg)
        grid_cells = []  # List of (frame, model_label, image_label, step_label) tuples for grid cells
        cell_size = [150]  # Default cell size, will be calculated dynamically
        grid_cols = [2]  # Number of columns in grid

        def setup_grid(count):
            """Setup grid parameters and clear existing cells."""
            nonlocal grid_cells
            # Hide and clear existing grid
            gen_grid_label.pack_forget()
            gen_grid_container.pack_forget()
            for cell_data in grid_cells:
                cell_data[0].destroy()
            grid_cells = []

            # Fixed cell size, calculate columns to fit available width
            cell_size[0] = 120
            padding = 6
            # Use grid_frame width (already has padding applied)
            dialog.update_idletasks()
            available_width = grid_frame.winfo_width()
            if available_width < 200:
                available_width = 540  # fallback before dialog is rendered
            grid_cols[0] = max(1, available_width // (cell_size[0] + padding))

            # Configure grid columns to expand evenly
            for c in range(grid_cols[0]):
                gen_grid_container.columnconfigure(c, weight=1)

        def create_cell(index):
            """Create a single grid cell at the given index."""
            # Show grid containers and recalculate columns when first cell is created
            if not grid_cells:
                gen_grid_label.pack(pady=(10, 5))
                gen_grid_container.pack(fill=tk.X)
                # Recalculate columns based on actual current width
                dialog.update_idletasks()
                available_width = grid_frame.winfo_width()
                if available_width > 200:
                    grid_cols[0] = max(1, available_width // (cell_size[0] + 6))
                    for c in range(grid_cols[0]):
                        gen_grid_container.columnconfigure(c, weight=1)

            row, col = index // grid_cols[0], index % grid_cols[0]
            cell_frame = tk.Frame(gen_grid_container, bg=muted, bd=3, relief=tk.FLAT)
            cell_frame.grid(row=row, column=col, padx=4, pady=4, sticky="nsew")

            # Model name label (above image)
            model_label = tk.Label(cell_frame, text="", bg=bg, fg=muted, font=("", 7))
            model_label.pack(padx=2, pady=(2, 0))

            # Image label - use pixel dimensions, not character dimensions
            image_label = tk.Label(cell_frame, text="Starting...", bg=bg, fg=muted, font=("", 9))
            image_label.pack(padx=2, pady=2)

            # Step progress label (overlaid at bottom)
            step_label = tk.Label(cell_frame, text="", bg=bg, fg=accent, font=("", 8, "bold"))
            step_label.pack()

            grid_cells.append((cell_frame, model_label, image_label, step_label))

        def update_grid_cell(index, image_path, step_info=None, model_name=None):
            """Update a specific grid cell with an image and optional step info."""
            # Create cell if it doesn't exist yet
            while index >= len(grid_cells):
                create_cell(len(grid_cells))
            frame, model_label, image_label, step_label = grid_cells[index]

            # Update model name if provided
            if model_name:
                model_label.configure(text=model_name)

            # Update step info if provided
            if step_info:
                step_label.configure(text=step_info)

            # Load and display image at cell size (skip if no path provided)
            if not image_path:
                return
            photo = load_icon_image(image_path, size=cell_size[0])
            if photo:
                dialog._photos.append(photo)
                image_label.configure(image=photo, text="", width=cell_size[0], height=cell_size[0])
                image_label._photo = photo

                # Make clickable (only bind once when we have a real image)
                def on_click(path=image_path, idx=index):
                    select_image(path, idx)
                image_label.bind("<Button-1>", lambda e: on_click())
                frame.bind("<Button-1>", lambda e: on_click())
                image_label.configure(cursor="hand2")

        def update_cell_step(index, step_info):
            """Update just the step info for a cell (only if it exists)."""
            if index < len(grid_cells):
                _, _, _, step_label = grid_cells[index]
                step_label.configure(text=step_info)

        def select_image(image_path, index):
            """Select an image from the grid."""
            selected_image[0] = image_path
            # Highlight selected cell
            for i, (frame, model_label, image_label, step_label) in enumerate(grid_cells):
                if i == index:
                    frame.configure(bg=accent, relief=tk.RAISED)
                else:
                    frame.configure(bg=muted, relief=tk.FLAT)
            # Update preview
            photo = load_icon_image(image_path, size=96)
            if photo:
                dialog._photos.append(photo)
                preview_label.configure(image=photo, text="")
                source_label.configure(text=f"Selected: {os.path.basename(image_path)}")
            # Show accept button
            accept_frame.pack(after=gen_grid_container, pady=10)
            status_var.set("Click Accept to use this icon")

        # Accept/Regenerate buttons (hidden initially)
        accept_frame = tk.Frame(grid_frame, bg=bg)

        def do_accept():
            if selected_image[0] and os.path.exists(selected_image[0]):
                save_current_settings()
                set_custom_icon(tool_key, selected_image[0])
                self._refresh_tool_icon(tool_key)
                cleanup_scroll_bindings()
                dialog.destroy()

        def do_regenerate():
            accept_frame.pack_forget()
            do_generate()

        tk.Button(accept_frame, text="✓ Accept", command=do_accept,
                  bg="#27ae60", fg="white", font=("", 10, "bold"), relief=tk.FLAT,
                  cursor="hand2", padx=20).pack(side=tk.LEFT, padx=5)
        tk.Button(accept_frame, text="↻ Regenerate", command=do_regenerate,
                  bg=accent, fg=bg, font=("", 10, "bold"), relief=tk.FLAT,
                  cursor="hand2", padx=5).pack(side=tk.LEFT, padx=5)

        # Separator
        tk.Frame(content_frame, height=2, bg=accent).pack(fill=tk.X, padx=20, pady=10)

        # Prompt entry
        tk.Label(content_frame, text="Generate with AI - Enter prompt:", bg=bg, fg=fg,
                 font=("", 10, "bold")).pack(anchor="w", padx=20)
        prompt_text = tk.Text(content_frame, height=2, bg=input_bg, fg=fg, font=("", 10),
                              insertbackground=fg, relief=tk.SUNKEN, bd=1)
        prompt_text.pack(fill=tk.X, padx=20, pady=(5, 5))
        prompt_text.insert("1.0", get_tool_prompt(tool_key, tool.name))

        # Enable Ctrl+A to select all
        def select_all(event):
            prompt_text.tag_add(tk.SEL, "1.0", tk.END)
            prompt_text.mark_set(tk.INSERT, "1.0")
            prompt_text.see(tk.INSERT)
            return "break"  # Prevent default behavior
        prompt_text.bind("<Control-a>", select_all)

        # Auto-save prompt on change (with debounce)
        prompt_save_pending = [None]
        def save_prompt_debounced(event=None):
            # Cancel any pending save
            if prompt_save_pending[0]:
                dialog.after_cancel(prompt_save_pending[0])
            # Schedule save after 500ms of no typing
            prompt_save_pending[0] = dialog.after(500, lambda: save_tool_prompt(tool_key, prompt_text.get("1.0", tk.END).strip()))
        prompt_text.bind("<KeyRelease>", save_prompt_debounced)

        # Load saved settings
        saved_settings = get_icon_gen_settings()

        # Advanced options variables (created now, UI packed after gen_btn)
        adv_expanded = tk.BooleanVar(value=saved_settings.get("expanded", False))
        steps_var = tk.IntVar(value=saved_settings.get("steps", 20))
        samples_var = tk.IntVar(value=saved_settings.get("samples", 1))
        guidance_var = tk.DoubleVar(value=saved_settings.get("guidance", 7.5))
        prompt_variations = []  # List to store generated prompt variations
        variations_frame = [None]  # Frame to display variations (created on demand)
        model_vars = {}
        saved_models = saved_settings.get("selected_models", [])
        for model_name in available_models:
            if saved_models:
                # Use saved selection
                is_selected = model_name in saved_models
            else:
                # No saved settings: select ALL models by default
                is_selected = True
            model_vars[model_name] = tk.BooleanVar(value=is_selected)

        def save_current_settings():
            """Save current settings to config."""
            selected = [m for m, v in model_vars.items() if v.get()]
            # Get sys_icons_expanded if it exists
            try:
                sys_expanded = sys_icons_expanded.get()
            except NameError:
                sys_expanded = True
            save_icon_gen_settings(
                steps=steps_var.get(),
                samples=samples_var.get(),
                guidance=guidance_var.get(),
                selected_models=selected,
                expanded=adv_expanded.get(),
                sys_icons_expanded=sys_expanded
            )
            # Save prompt per-tool (only if prompt_text exists)
            try:
                current_prompt = prompt_text.get("1.0", tk.END).strip()
                save_tool_prompt(tool_key, current_prompt)
            except NameError:
                pass  # prompt_text not yet created

        # Auto-save on settings change
        def on_setting_change(*args):
            save_current_settings()

        steps_var.trace_add("write", on_setting_change)
        samples_var.trace_add("write", on_setting_change)
        guidance_var.trace_add("write", on_setting_change)
        adv_expanded.trace_add("write", on_setting_change)
        for var in model_vars.values():
            var.trace_add("write", on_setting_change)

        status_var = tk.StringVar(value="")
        current_process = [None]  # Track current subprocess for interrupt
        interrupt_flag = [False]  # Signal to stop generation
        current_preview_file = [None]  # Track current preview for interrupt save

        def do_interrupt():
            """Interrupt the current generation."""
            if current_process[0] is not None:
                interrupt_flag[0] = True
                try:
                    current_process[0].terminate()
                except Exception:
                    pass

        def do_generate():
            prompt = prompt_text.get("1.0", tk.END).strip()
            if not prompt:
                return

            # Save settings before generating
            save_current_settings()

            diffusers = os.path.join(ROOT_DIR, "tools_personal", "diffusers_gui", "main.py")
            if not os.path.exists(diffusers):
                status_var.set("Error: Diffusers not found")
                return

            # Get selected models
            selected_models = [m for m, v in model_vars.items() if v.get()]
            if not selected_models:
                selected_models = available_models[:1] if available_models else []

            samples_per_model = samples_var.get()
            steps = steps_var.get()
            total_images = len(selected_models) * samples_per_model

            if total_images == 0:
                status_var.set("No models available")
                return

            os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
            import time
            import tempfile

            # Setup grid parameters (cells created on-demand)
            setup_grid(total_images)

            interrupt_flag[0] = False
            gen_btn.config(text="Interrupt", command=do_interrupt, bg="#c0392b")
            accept_frame.pack_forget()
            generated_images.clear()
            selected_image[0] = None
            dialog.update()

            # Generate images sequentially
            current_index = [0]  # Track which image we're generating

            def generation_finished():
                """Reset button state when generation completes or is interrupted."""
                gen_btn.config(text="Generate Icon", command=do_generate, bg=accent)
                current_process[0] = None
                if generated_images:
                    status_var.set(f"Generated {len(generated_images)} images - click to select")
                elif interrupt_flag[0]:
                    status_var.set("Generation interrupted")

            def generate_next():
                idx = current_index[0]
                if idx >= total_images or interrupt_flag[0]:
                    # All done or interrupted
                    generation_finished()
                    return

                model_idx = idx // samples_per_model
                sample_num = idx % samples_per_model
                model_name = selected_models[model_idx]
                model_path = os.path.join(models_dir, model_name)

                timestamp = int(time.time() * 1000)
                out = os.path.join(CUSTOM_ICONS_DIR, f"{tool_key.replace(' ', '_').lower()}_{timestamp}.png")
                preview_file = os.path.join(tempfile.gettempdir(), f"icon_preview_{timestamp}.png")

                short_model = model_name[:20] + "..." if len(model_name) > 23 else model_name.replace('.safetensors', '')
                status_var.set(f"Image {idx+1}/{total_images}: {short_model}")
                # Create cell and set model name immediately
                update_grid_cell(idx, None, "Starting...", model_name=short_model)
                dialog.update()

                # Start subprocess
                guidance = guidance_var.get()
                process = subprocess.Popen(
                    [sys.executable, diffusers, "--icon", "--icon-size", "256",
                     "-p", prompt, "-o", out, "-s", str(steps), "-g", str(guidance),
                     "-m", model_path, "--preview-path", preview_file],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1
                )
                current_process[0] = process
                current_preview_file[0] = preview_file

                last_preview_mtime = [0]
                current_step_info = ["Step 0/?"]

                # Use a queue for non-blocking stdout reading
                output_queue = queue.Queue()

                def read_output():
                    """Thread function to read stdout without blocking."""
                    try:
                        for line in iter(process.stdout.readline, ''):
                            if line:
                                output_queue.put(line)
                            if process.poll() is not None:
                                break
                    except Exception:
                        pass

                reader_thread = threading.Thread(target=read_output, daemon=True)
                reader_thread.start()

                def poll_progress():
                    retcode = process.poll()

                    # Read from queue (non-blocking)
                    try:
                        while True:
                            line = output_queue.get_nowait()
                            if "PREVIEW:" in line:
                                parts = line.strip().split("PREVIEW:")[1].split("/")
                                if len(parts) == 2:
                                    step, total = int(parts[0]), int(parts[1])
                                    current_step_info[0] = f"Step {step}/{total}"
                                    status_var.set(f"Image {idx+1}/{total_images}: Step {step}/{total}")
                                    update_cell_step(idx, current_step_info[0])
                    except queue.Empty:
                        pass

                    # Check for updated preview file
                    if os.path.exists(preview_file):
                        try:
                            mtime = os.path.getmtime(preview_file)
                            if mtime > last_preview_mtime[0]:
                                last_preview_mtime[0] = mtime
                                update_grid_cell(idx, preview_file, current_step_info[0])
                        except (OSError, IOError):
                            pass

                    if retcode is None and not interrupt_flag[0]:
                        dialog.after(150, poll_progress)
                    else:
                        # Check if interrupted - save preview as final image
                        was_interrupted = interrupt_flag[0] or retcode != 0
                        if was_interrupted and os.path.exists(preview_file):
                            # Save the preview as a partial result
                            import shutil
                            partial_out = os.path.join(CUSTOM_ICONS_DIR, f"{tool_key.replace(' ', '_').lower()}_{timestamp}_partial.png")
                            try:
                                shutil.copy2(preview_file, partial_out)
                                generated_images.append(partial_out)
                                update_grid_cell(idx, partial_out, "Partial")
                            except Exception:
                                pass

                        # Cleanup preview
                        try:
                            if os.path.exists(preview_file):
                                os.remove(preview_file)
                        except OSError:
                            pass

                        if not was_interrupted and retcode == 0 and os.path.exists(out):
                            generated_images.append(out)
                            update_grid_cell(idx, out, "Done ✓")

                        current_process[0] = None

                        # Stop if interrupted, otherwise generate next
                        if interrupt_flag[0]:
                            generation_finished()
                        else:
                            current_index[0] += 1
                            dialog.after(100, generate_next)

                dialog.after(100, poll_progress)

            generate_next()

        gen_btn = tk.Button(content_frame, text="Generate Icon", command=do_generate,
                            bg=accent, fg=bg, font=("", 10, "bold"), relief=tk.FLAT, cursor="hand2")
        gen_btn.pack(fill=tk.X, padx=20)

        # Advanced options (expandable) - below Generate button
        adv_header = tk.Frame(content_frame, bg=bg)
        adv_header.pack(fill=tk.X, padx=20, pady=(5, 0))

        adv_frame = tk.Frame(content_frame, bg=bg)

        def toggle_advanced():
            if adv_expanded.get():
                adv_frame.pack(fill=tk.X, padx=20, pady=(0, 5), after=adv_header)
                adv_toggle_btn.configure(text="▼ Advanced Options")
            else:
                adv_frame.pack_forget()
                adv_toggle_btn.configure(text="▶ Advanced Options")

        adv_toggle_btn = tk.Button(adv_header,
                                   text="▼ Advanced Options" if adv_expanded.get() else "▶ Advanced Options",
                                   bg=bg, fg=muted, font=("", 9), relief=tk.FLAT, cursor="hand2",
                                   command=lambda: (adv_expanded.set(not adv_expanded.get()), toggle_advanced()))
        adv_toggle_btn.pack(anchor="w")

        # Steps setting
        steps_frame = tk.Frame(adv_frame, bg=bg)
        steps_frame.pack(fill=tk.X, pady=3)
        tk.Label(steps_frame, text="Steps:", bg=bg, fg=fg, font=("", 9), width=12, anchor="w").pack(side=tk.LEFT)
        tk.Spinbox(steps_frame, from_=5, to=50, textvariable=steps_var, width=5,
                   bg=input_bg, fg=fg, font=("", 9), buttonbackground=bg).pack(side=tk.LEFT)
        tk.Label(steps_frame, text="(5-50, higher = better quality)", bg=bg, fg=muted, font=("", 8)).pack(side=tk.LEFT, padx=5)

        # Samples per model setting
        samples_frame = tk.Frame(adv_frame, bg=bg)
        samples_frame.pack(fill=tk.X, pady=3)
        tk.Label(samples_frame, text="Samples:", bg=bg, fg=fg, font=("", 9), width=12, anchor="w").pack(side=tk.LEFT)
        tk.Spinbox(samples_frame, from_=1, to=20, textvariable=samples_var, width=5,
                   bg=input_bg, fg=fg, font=("", 9), buttonbackground=bg).pack(side=tk.LEFT)
        tk.Label(samples_frame, text="(per model)", bg=bg, fg=muted, font=("", 8)).pack(side=tk.LEFT, padx=5)

        # Guidance scale setting
        guidance_frame = tk.Frame(adv_frame, bg=bg)
        guidance_frame.pack(fill=tk.X, pady=3)
        tk.Label(guidance_frame, text="Guidance:", bg=bg, fg=fg, font=("", 9), width=12, anchor="w").pack(side=tk.LEFT)
        tk.Spinbox(guidance_frame, from_=1.0, to=20.0, increment=0.5, textvariable=guidance_var, width=5,
                   bg=input_bg, fg=fg, font=("", 9), buttonbackground=bg).pack(side=tk.LEFT)
        tk.Label(guidance_frame, text="(1-20, higher = follow prompt more)", bg=bg, fg=muted, font=("", 8)).pack(side=tk.LEFT, padx=5)

        # Model selection
        tk.Label(adv_frame, text="Models:", bg=bg, fg=fg, font=("", 9)).pack(anchor="w", pady=(5, 2))
        models_frame = tk.Frame(adv_frame, bg=bg)
        models_frame.pack(fill=tk.X, padx=10)
        for model_name, var in model_vars.items():
            short_name = model_name[:30] + "..." if len(model_name) > 33 else model_name.replace('.safetensors', '')
            tk.Checkbutton(models_frame, text=short_name, variable=var,
                           bg=bg, fg=fg, selectcolor=input_bg, activebackground=bg,
                           activeforeground=fg, font=("", 8)).pack(anchor="w")

        if not available_models:
            tk.Label(models_frame, text="No models found in diffusers/models/", bg=bg, fg=muted, font=("", 8)).pack(anchor="w")

        # Prompt Variations section
        tk.Frame(adv_frame, height=1, bg=muted).pack(fill=tk.X, pady=(10, 5))
        tk.Label(adv_frame, text="Prompt Variations:", bg=bg, fg=fg, font=("", 9)).pack(anchor="w", pady=(0, 3))

        variations_status_var = tk.StringVar(value="")
        gen_varied_btn = [None]  # Forward reference for generate varied icons button

        def suggest_variations():
            """Use Gemini to generate prompt variations."""
            if GeminiClient is None:
                variations_status_var.set("Gemini client not available")
                return

            base_prompt = prompt_text.get("1.0", tk.END).strip()
            if not base_prompt:
                variations_status_var.set("Enter a prompt first")
                return

            count = samples_var.get()
            variations_status_var.set(f"Generating {count} variations...")
            dialog.update()

            def generate_in_thread():
                try:
                    client = GeminiClient(tier=ModelTier.WEAK)
                    system_instruction = (
                        "You are a wildly creative icon designer who thinks outside the box. "
                        "Your task is to generate RADICALLY DIFFERENT icon concepts - not minor rewrites. "
                        "Each variation must be a completely fresh take with:\n"
                        "- Different visual metaphors (abstract shapes, objects, symbols, characters)\n"
                        "- Different art styles (flat, 3D, isometric, pixel art, watercolor, neon, glass, clay, origami, etc.)\n"
                        "- Different color schemes (vibrant, pastel, monochrome, dark, gradient, duotone)\n"
                        "- Different compositions (centered, dynamic, geometric, organic, layered)\n"
                        "- Different moods (playful, professional, futuristic, vintage, minimal, ornate)\n\n"
                        "Be bold and unexpected! If the original is flat, make one 3D. If it's literal, make one abstract. "
                        "Never repeat the same adjectives or style keywords across variations. "
                        "Return ONLY the prompts, one per line, no numbering or prefixes."
                    )
                    user_msg = f"Generate {count} COMPLETELY DIFFERENT icon concepts inspired by this idea:\n\n{base_prompt}\n\nMake each one unique in style, metaphor, and mood. Be creative and unexpected!"

                    response = client.generate(user_msg, system=system_instruction)
                    if response:
                        # Parse response into list of variations
                        lines = [line.strip() for line in response.strip().split('\n') if line.strip()]
                        # Take only the requested count
                        variations = lines[:count]
                        dialog.after(0, lambda: display_variations(variations))
                    else:
                        dialog.after(0, lambda: variations_status_var.set("Failed to generate variations"))
                except Exception as e:
                    error_msg = str(e)[:50]
                    dialog.after(0, lambda: variations_status_var.set(f"Error: {error_msg}"))

            threading.Thread(target=generate_in_thread, daemon=True).start()

        def display_variations(variations):
            """Display generated variations in the UI."""
            prompt_variations.clear()
            prompt_variations.extend(variations)

            # Remove old variations frame if exists
            if variations_frame[0]:
                variations_frame[0].destroy()

            # Count selected models
            selected_count = len([m for m, v in model_vars.items() if v.get()])
            if selected_count == 0:
                selected_count = 1  # Will use first available
            total_images = len(variations) * selected_count

            variations_status_var.set(f"{len(variations)} variations × {selected_count} models = {total_images} images")

            # Create new frame for variations
            vf = tk.Frame(adv_frame, bg=bg)
            vf.pack(fill=tk.X, pady=(5, 0))
            variations_frame[0] = vf

            for i, variation in enumerate(variations):
                row = tk.Frame(vf, bg=bg)
                row.pack(fill=tk.X, pady=2)

                # Truncate display text but store full prompt
                display_text = variation[:60] + "..." if len(variation) > 63 else variation
                tk.Label(row, text=f"{i+1}. {display_text}", bg=bg, fg=fg, font=("", 8),
                        anchor="w", wraplength=400).pack(side=tk.LEFT, fill=tk.X, expand=True)

                # Use button to copy variation to main prompt
                def use_variation(v=variation):
                    prompt_text.delete("1.0", tk.END)
                    prompt_text.insert("1.0", v)
                    save_tool_prompt(tool_key, v)

                tk.Button(row, text="Use", command=use_variation, bg=input_bg, fg=fg,
                         font=("", 7), relief=tk.GROOVE, cursor="hand2", padx=4, pady=0).pack(side=tk.RIGHT)

            # Show "Generate Varied Icons" button with count
            if gen_varied_btn[0]:
                gen_varied_btn[0].config(text=f"Generate {total_images} Varied Icons")
                gen_varied_btn[0].pack(fill=tk.X, pady=(8, 0))

        def do_generate_varied():
            """Generate one icon per model × variation combination."""
            if not prompt_variations:
                variations_status_var.set("No variations to generate")
                return

            # Get ALL selected models (generate for each model × variation)
            selected_models = [m for m, v in model_vars.items() if v.get()]
            if not selected_models:
                selected_models = available_models[:1] if available_models else []

            if not selected_models:
                status_var.set("No models available")
                return

            diffusers = os.path.join(ROOT_DIR, "tools_personal", "diffusers_gui", "main.py")
            if not os.path.exists(diffusers):
                status_var.set("Error: Diffusers not found")
                return

            steps = steps_var.get()
            # Generate for each model × variation combination
            total_images = len(prompt_variations) * len(selected_models)

            os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
            import time
            import tempfile

            # Build list of (model_name, model_path, variation_prompt) tuples
            generation_queue = []
            for model_name in selected_models:
                model_path = os.path.join(models_dir, model_name)
                for variation_prompt in prompt_variations:
                    generation_queue.append((model_name, model_path, variation_prompt))

            # Setup grid parameters
            setup_grid(total_images)

            interrupt_flag[0] = False
            gen_btn.config(text="Interrupt", command=do_interrupt, bg="#c0392b")
            accept_frame.pack_forget()
            generated_images.clear()
            selected_image[0] = None
            dialog.update()

            current_index = [0]

            def generation_finished():
                gen_btn.config(text="Generate Icon", command=do_generate, bg=accent)
                current_process[0] = None
                if generated_images:
                    status_var.set(f"Generated {len(generated_images)} varied images - click to select")
                elif interrupt_flag[0]:
                    status_var.set("Generation interrupted")

            def generate_next_varied():
                idx = current_index[0]
                if idx >= total_images or interrupt_flag[0]:
                    generation_finished()
                    return

                model_name, model_path, variation_prompt = generation_queue[idx]
                timestamp = int(time.time() * 1000)
                out = os.path.join(CUSTOM_ICONS_DIR, f"{tool_key.replace(' ', '_').lower()}_{timestamp}.png")
                preview_file = os.path.join(tempfile.gettempdir(), f"icon_preview_{timestamp}.png")

                short_model = model_name[:20] + "..." if len(model_name) > 23 else model_name.replace('.safetensors', '')
                status_var.set(f"Image {idx+1}/{total_images} ({short_model})")
                update_grid_cell(idx, None, "Starting...", model_name=short_model)
                dialog.update()

                current_preview_file[0] = preview_file

                guidance = guidance_var.get()
                cmd = [
                    sys.executable, diffusers,
                    "--icon", "--icon-size", "256",
                    "-p", variation_prompt,
                    "-o", preview_file,
                    "-s", str(steps),
                    "-g", str(guidance),
                    "-m", model_path,
                ]

                def run_generation():
                    try:
                        proc = subprocess.Popen(
                            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
                        )
                        current_process[0] = proc

                        while True:
                            line = proc.stdout.readline()
                            if not line and proc.poll() is not None:
                                break
                            line = line.strip()
                            if line.startswith("STEP:"):
                                step_info = line.replace("STEP:", "").strip()
                                dialog.after(0, lambda s=step_info: update_grid_cell(idx, None, s))
                            elif line.startswith("PREVIEW:"):
                                pass

                        proc.wait()

                        if interrupt_flag[0]:
                            dialog.after(0, generation_finished)
                            return

                        if os.path.exists(preview_file):
                            import shutil
                            shutil.copy2(preview_file, out)
                            generated_images.append(out)

                            def update_ui():
                                # Pass the file path - update_grid_cell handles image loading
                                update_grid_cell(idx, out, "Done")
                                current_index[0] += 1
                                dialog.after(100, generate_next_varied)

                            dialog.after(0, update_ui)
                        else:
                            def mark_failed():
                                update_grid_cell(idx, None, "Failed")
                                current_index[0] += 1
                                dialog.after(100, generate_next_varied)
                            dialog.after(0, mark_failed)

                    except Exception as e:
                        def mark_error():
                            update_grid_cell(idx, None, f"Error: {str(e)[:20]}")
                            current_index[0] += 1
                            dialog.after(100, generate_next_varied)
                        dialog.after(0, mark_error)

                threading.Thread(target=run_generation, daemon=True).start()

            generate_next_varied()

        # Suggest variations button
        suggest_btn_frame = tk.Frame(adv_frame, bg=bg)
        suggest_btn_frame.pack(fill=tk.X, pady=(0, 3))
        tk.Button(suggest_btn_frame, text="Suggest Variations", command=suggest_variations,
                 bg=input_bg, fg=fg, font=("", 9), relief=tk.GROOVE, cursor="hand2").pack(side=tk.LEFT)
        tk.Label(suggest_btn_frame, textvariable=variations_status_var, bg=bg, fg=muted, font=("", 8)).pack(side=tk.LEFT, padx=10)

        # Generate varied icons button (hidden until variations exist)
        gen_varied_btn[0] = tk.Button(adv_frame, text="Generate Varied Icons", command=do_generate_varied,
                                      bg="#2980b9", fg="white", font=("", 9, "bold"), relief=tk.FLAT, cursor="hand2")
        # Initially hidden - shown after variations are generated

        # Show advanced options if saved as expanded
        if adv_expanded.get():
            adv_frame.pack(fill=tk.X, padx=20, pady=(0, 5), after=adv_header)

        # Separator
        tk.Frame(content_frame, height=2, bg=muted).pack(fill=tk.X, padx=20, pady=15)

        # Browse
        def do_browse():
            path = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.svg")])
            if path:
                import shutil
                save_current_settings()
                os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
                dest = os.path.join(CUSTOM_ICONS_DIR,
                                    f"{tool_key.replace(' ', '_').lower()}{os.path.splitext(path)[1]}")
                shutil.copy2(path, dest)
                set_custom_icon(tool_key, dest)
                self._refresh_tool_icon(tool_key)
                cleanup_scroll_bindings()
                dialog.destroy()

        tk.Button(content_frame, text="Browse for Image...", command=do_browse,
                  bg=bg, fg=fg, font=("", 10), relief=tk.GROOVE, cursor="hand2").pack(fill=tk.X, padx=20, pady=(0, 10))

        # === Icon Browser (tabbed: All / System Icons / Emojis) ===
        browser_frame = tk.Frame(content_frame, bg=bg)
        browser_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 10))

        # Keep sys_icons_expanded for backward compat with save_current_settings
        sys_icons_expanded = tk.BooleanVar(value=saved_settings.get("sys_icons_expanded", True))
        browser_container = tk.Frame(browser_frame, bg=bg)
        browser_loaded = [False]
        display_generation = [0]
        icon_thread_pool = [None]  # Will hold ThreadPoolExecutor once browser loads

        def select_browser_icon(icon_name, icon_path):
            """Select an icon from the browser (system icon or emoji)."""
            import shutil
            save_current_settings()
            # Cancel in-flight icon loading workers
            if browser_loaded[0] and icon_thread_pool[0]:
                display_generation[0] += 1
                icon_thread_pool[0].shutdown(wait=False)
            os.makedirs(CUSTOM_ICONS_DIR, exist_ok=True)
            ext = os.path.splitext(icon_path)[1]
            dest = os.path.join(CUSTOM_ICONS_DIR, f"{tool_key.replace(' ', '_').lower()}_icon{ext}")
            shutil.copy2(icon_path, dest)
            set_custom_icon(tool_key, dest)
            self._refresh_tool_icon(tool_key)
            cleanup_scroll_bindings()
            dialog.destroy()

        def load_icon_browser():
            """Load and display the tabbed icon browser."""
            if browser_loaded[0]:
                return
            browser_loaded[0] = True

            # --- Tab Bar ---
            tab_bar = tk.Frame(browser_container, bg=bg)
            tab_bar.pack(fill=tk.X, pady=(0, 5))

            active_tab = ["All"]
            tab_buttons = {}

            def switch_tab(tab_name):
                active_tab[0] = tab_name
                for name, btn in tab_buttons.items():
                    if name == tab_name:
                        btn.configure(bg=accent, fg=bg, relief=tk.SUNKEN)
                    else:
                        btn.configure(bg=input_bg, fg=fg, relief=tk.FLAT)
                redisplay_icons()

            for tab_name in ["All", "System Icons", "Emojis"]:
                btn = tk.Button(tab_bar, text=tab_name, font=("", 9, "bold"),
                                bg=accent if tab_name == "All" else input_bg,
                                fg=bg if tab_name == "All" else fg,
                                relief=tk.SUNKEN if tab_name == "All" else tk.FLAT,
                                cursor="hand2", padx=12, pady=2, bd=1,
                                command=lambda t=tab_name: switch_tab(t))
                btn.pack(side=tk.LEFT, padx=(0, 2))
                tab_buttons[tab_name] = btn

            # --- Search Box ---
            search_frame = tk.Frame(browser_container, bg=bg)
            search_frame.pack(fill=tk.X, pady=(0, 5))
            tk.Label(search_frame, text="Search:", bg=bg, fg=fg, font=("", 9)).pack(side=tk.LEFT)
            search_var = tk.StringVar()
            search_entry = tk.Entry(search_frame, textvariable=search_var, bg=input_bg, fg=fg,
                                    insertbackground=fg, font=("", 9))
            search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 0))

            def search_select_all(_event):
                search_entry.select_range(0, tk.END)
                return "break"
            search_entry.bind("<Control-a>", search_select_all)

            # --- Status Bar (packed before grid so it gets space at bottom) ---
            icons_status_frame = tk.Frame(browser_container, bg=bg)
            icons_status_frame.pack(fill=tk.X, pady=(2, 0), side=tk.BOTTOM)
            hover_path_var = tk.StringVar(value="")
            icon_count_var = tk.StringVar(value="Loading...")
            tk.Label(icons_status_frame, textvariable=icon_count_var, bg=bg, fg=accent,
                     font=("", 12), anchor="w").pack(side=tk.LEFT)
            tk.Label(icons_status_frame, textvariable=hover_path_var, bg=bg, fg=muted,
                     font=("", 8), anchor="w").pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))

            # --- Scrollable Grid ---
            icons_grid_frame = tk.Frame(browser_container, bg=bg)
            icons_grid_frame.pack(fill=tk.BOTH, expand=True)

            icons_canvas = tk.Canvas(icons_grid_frame, bg=bg, highlightthickness=0, width=540)
            icons_scrollbar = tk.Scrollbar(icons_grid_frame, orient=tk.VERTICAL, command=icons_canvas.yview)
            icons_inner = tk.Frame(icons_canvas, bg=bg)

            icons_canvas.configure(yscrollcommand=icons_scrollbar.set)
            icons_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
            icons_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

            icons_window = icons_canvas.create_window((0, 0), window=icons_inner, anchor="nw")
            icons_inner.bind("<Configure>", lambda _e: icons_canvas.configure(scrollregion=icons_canvas.bbox("all")))
            icons_canvas.bind("<Configure>", lambda _e: icons_canvas.itemconfig(icons_window, width=_e.width))

            # Mouse wheel scrolling - return "break" to prevent outer canvas scroll
            def on_icons_scroll(event):
                icons_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
                return "break"
            def on_icons_scroll_linux(event):
                if event.num == 4:
                    icons_canvas.yview_scroll(-1, "units")
                elif event.num == 5:
                    icons_canvas.yview_scroll(1, "units")
                return "break"

            icons_canvas.bind("<MouseWheel>", on_icons_scroll)
            icons_canvas.bind("<Button-4>", on_icons_scroll_linux)
            icons_canvas.bind("<Button-5>", on_icons_scroll_linux)
            icons_inner.bind("<MouseWheel>", on_icons_scroll)
            icons_inner.bind("<Button-4>", on_icons_scroll_linux)
            icons_inner.bind("<Button-5>", on_icons_scroll_linux)

            icon_size = 40
            padding = 4
            scrollbar_width = 20

            # --- Data Storage ---
            system_icons = []     # [(name, path), ...] - deduplicated system icons
            emoji_items = []      # [(name, path), ...] - rendered emoji PNGs
            seen_system = {}      # name -> path, for dedup
            icon_widgets = []     # Currently displayed grid widgets
            system_done = [False]
            emoji_done = [False]
            load_queue = queue.Queue()

            # --- Async Icon Loading State ---
            photo_cache: Dict[tuple, Optional[ImageTk.PhotoImage]] = {}
            icon_thread_pool[0] = ThreadPoolExecutor(max_workers=4)
            image_result_queue = queue.Queue()

            def get_or_create_photo(pil_img, path, size):
                """Convert PIL Image to PhotoImage with caching."""
                key = (path, size)
                if key in photo_cache:
                    cached = photo_cache[key]
                    if cached is not None:
                        return cached
                    return None
                if pil_img is None:
                    photo_cache[key] = None
                    return None
                try:
                    photo = ImageTk.PhotoImage(pil_img)
                    dialog._photos.append(photo)
                    photo_cache[key] = photo
                    return photo
                except Exception:
                    photo_cache[key] = None
                    return None

            def _load_icon_worker(generation, index, icon_path, size):
                """Background worker: load a single icon as PIL Image."""
                if generation != display_generation[0]:
                    return
                pil_img = load_pil_image(icon_path, size)
                if generation != display_generation[0]:
                    return
                image_result_queue.put((generation, index, icon_path, size, pil_img))

            def on_icon_hover(icon_path):
                hover_path_var.set(icon_path)

            def on_icon_leave():
                hover_path_var.set("")

            # --- Grid Display ---
            def get_items_for_tab():
                """Get the icon list for the active tab, filtered by search."""
                tab = active_tab[0]
                if tab == "All":
                    items = system_icons + emoji_items
                elif tab == "System Icons":
                    items = system_icons
                else:
                    items = emoji_items

                query = search_var.get().lower().strip()
                if query:
                    items = [(n, p) for n, p in items if query in n.lower()]
                return items

            def _setup_cell_bindings(cell, lbl, icon_path):
                """Bind click/scroll/hover events to a cell and its label."""
                lbl.bind("<Button-1>", lambda _e, p=icon_path: select_browser_icon("", p))
                cell.bind("<Button-1>", lambda _e, p=icon_path: select_browser_icon("", p))
                lbl.bind("<MouseWheel>", on_icons_scroll)
                lbl.bind("<Button-4>", on_icons_scroll_linux)
                lbl.bind("<Button-5>", on_icons_scroll_linux)
                lbl.bind("<Enter>", lambda _e, p=icon_path: on_icon_hover(p))
                lbl.bind("<Leave>", lambda _e: on_icon_leave())

            def redisplay_icons():
                """Redisplay icons with async image loading."""
                # Bump generation to cancel in-flight workers
                display_generation[0] += 1
                gen = display_generation[0]

                for widget, _, _ in icon_widgets:
                    widget.destroy()
                icon_widgets.clear()

                items = get_items_for_tab()

                canvas_width = icons_canvas.winfo_width() - scrollbar_width
                if canvas_width < 100:
                    canvas_width = 500 - scrollbar_width
                col_width = icon_size + padding * 2
                num_cols = max(1, canvas_width // col_width)
                cell_size = icon_size + padding * 2

                pending_count = [0]
                cell_refs = {}  # index -> (cell, lbl)

                for idx, (icon_name, icon_path) in enumerate(items):
                    row, col = idx // num_cols, idx % num_cols
                    cell = tk.Frame(icons_inner, bg=bg, cursor="hand2",
                                    width=cell_size, height=cell_size)
                    cell.grid(row=row, column=col, padx=padding, pady=padding)
                    cell.grid_propagate(False)

                    # Check caches: PhotoImage cache first, then PIL cache
                    pil_key = (icon_path, icon_size)
                    photo = None
                    if pil_key in photo_cache:
                        photo = photo_cache[pil_key]
                    elif pil_key in _pil_image_cache:
                        pil_img = _pil_image_cache[pil_key]
                        photo = get_or_create_photo(pil_img, icon_path, icon_size)

                    if photo:
                        lbl = tk.Label(cell, image=photo, bg=bg, cursor="hand2")
                        lbl.pack(expand=True)
                        _setup_cell_bindings(cell, lbl, icon_path)
                    else:
                        # Placeholder — will be replaced async
                        lbl = tk.Label(cell, text="...", bg=bg, fg=muted,
                                       cursor="hand2", width=3, height=1,
                                       font=("", 8))
                        lbl.pack(expand=True)
                        _setup_cell_bindings(cell, lbl, icon_path)
                        cell_refs[idx] = (cell, lbl, icon_path)
                        pending_count[0] += 1
                        icon_thread_pool[0].submit(_load_icon_worker, gen, idx, icon_path, icon_size)

                    icon_widgets.append((cell, icon_name, icon_path))

                icons_canvas.yview_moveto(0)
                icon_count_var.set(f"{len(items)} icons")

                # Start polling if there are pending loads
                if pending_count[0] > 0:
                    _start_result_polling(gen, cell_refs, pending_count)

            def _start_result_polling(generation, cell_refs, pending_count):
                """Poll image_result_queue and update placeholder cells."""
                def poll():
                    if generation != display_generation[0]:
                        return  # Stale generation, stop polling
                    processed = 0
                    while processed < 15:
                        try:
                            gen, idx, path, size, pil_img = image_result_queue.get_nowait()
                        except queue.Empty:
                            break
                        if gen != generation:
                            continue  # Skip stale results
                        processed += 1
                        pending_count[0] -= 1
                        if idx not in cell_refs:
                            continue
                        cell, lbl, icon_path = cell_refs[idx]
                        photo = get_or_create_photo(pil_img, path, size)
                        if photo:
                            try:
                                lbl.configure(image=photo, text="")
                            except tk.TclError:
                                pass  # Widget destroyed
                        del cell_refs[idx]

                    if pending_count[0] > 0 and generation == display_generation[0]:
                        dialog.after(30, poll)
                poll()

            # --- Queue Processing ---
            def process_load_queue():
                """Process items from background loading threads."""
                batch_size = 10
                processed = 0
                while processed < batch_size:
                    try:
                        item = load_queue.get_nowait()
                        item_type = item[0]

                        if item_type == "system_done":
                            system_done[0] = True
                            system_icons.sort(key=lambda x: x[0].lower())
                            if system_done[0] and emoji_done[0]:
                                redisplay_icons()
                            continue

                        if item_type == "emoji_done":
                            emoji_done[0] = True
                            if system_done[0] and emoji_done[0]:
                                redisplay_icons()
                            continue

                        if item_type == "system":
                            _, name, path = item
                            if name not in seen_system or 'scalable' in path or '128' in path:
                                if name in seen_system:
                                    system_icons[:] = [(n, p) for n, p in system_icons if n != name]
                                seen_system[name] = path
                                system_icons.append((name, path))

                        elif item_type == "emoji":
                            _, name, path = item
                            emoji_items.append((name, path))

                        processed += 1
                    except queue.Empty:
                        break

                all_done = system_done[0] and emoji_done[0]
                if not all_done:
                    total = len(system_icons) + len(emoji_items)
                    icon_count_var.set(f"Loading... {total}")
                    dialog.after(50, process_load_queue)
                else:
                    redisplay_icons()

            # --- Background Loaders ---
            def load_system_icons_thread():
                """Background thread: discover system icons."""
                for name, path in iter_system_icons():
                    load_queue.put(("system", name, path))
                load_queue.put(("system_done",))

            def load_emojis_thread():
                """Background thread: render emojis to cached PNGs."""
                cache_dir = _get_emoji_cache_dir()
                for emoji_char, emoji_name in EMOJI_LIST:
                    safe_name = emoji_name.replace(" ", "_").replace("/", "_")
                    cache_file = os.path.join(cache_dir, f"{safe_name}.png")
                    if not os.path.exists(cache_file):
                        render_emoji_icon(emoji_char, cache_file, size=128)
                    if os.path.exists(cache_file):
                        load_queue.put(("emoji", f"{emoji_char} {emoji_name}", cache_file))
                load_queue.put(("emoji_done",))

            # --- Debounced Search ---
            search_pending = [None]
            def do_search():
                redisplay_icons()

            def on_search_change(*_args):
                if search_pending[0]:
                    dialog.after_cancel(search_pending[0])
                search_pending[0] = dialog.after(200, do_search)

            search_var.trace_add("write", on_search_change)

            # --- Reflow on Resize ---
            last_canvas_width = [0]
            def on_icons_canvas_resize(event):
                if abs(event.width - last_canvas_width[0]) > icon_size + padding * 2:
                    last_canvas_width[0] = event.width
                    if system_done[0] and emoji_done[0]:
                        redisplay_icons()
            icons_canvas.bind("<Configure>", on_icons_canvas_resize, add="+")

            # --- Start Loading ---
            threading.Thread(target=load_system_icons_thread, daemon=True).start()
            threading.Thread(target=load_emojis_thread, daemon=True).start()
            dialog.after(50, process_load_queue)

        # --- Toggle Button ---
        def toggle_browser():
            if sys_icons_expanded.get():
                browser_container.pack(fill=tk.BOTH, expand=True, pady=(5, 0))
                browser_toggle.configure(text="▼ Icon Browser")
                load_icon_browser()
            else:
                browser_container.pack_forget()
                browser_toggle.configure(text="▶ Icon Browser")
            save_current_settings()

        browser_toggle = tk.Button(browser_frame,
                                   text="▼ Icon Browser" if sys_icons_expanded.get() else "▶ Icon Browser",
                                   bg=bg, fg=muted, font=("", 9), relief=tk.FLAT, cursor="hand2",
                                   command=lambda: (sys_icons_expanded.set(not sys_icons_expanded.get()), toggle_browser()))
        browser_toggle.pack(anchor="w")

        # Auto-expand if saved as expanded
        if sys_icons_expanded.get():
            dialog.after(100, toggle_browser)

        # Reset (if custom)
        if custom_path:
            def do_reset():
                save_current_settings()
                clear_custom_icon(tool_key)
                self.icon_cache.pop(custom_path, None)
                self._refresh_tool_icon(tool_key)
                cleanup_scroll_bindings()
                dialog.destroy()
            tk.Button(content_frame, text="Reset to Default", command=do_reset,
                      bg=bg, fg=muted, font=("", 10), relief=tk.GROOVE).pack(fill=tk.X, padx=20, pady=(0, 10))

        # Status
        tk.Label(content_frame, textvariable=status_var, bg=bg, fg=accent, wraplength=560).pack(pady=(5, 20))

        # Handle window close button (X) - save settings on close
        def do_close():
            save_current_settings()
            # Cancel in-flight icon loading workers
            if browser_loaded[0] and icon_thread_pool[0]:
                display_generation[0] += 1
                icon_thread_pool[0].shutdown(wait=False)
            # Interrupt any running generation
            if current_process[0] is not None:
                interrupt_flag[0] = True
                try:
                    current_process[0].terminate()
                except Exception:
                    pass
            cleanup_scroll_bindings()
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", do_close)

        # Force render
        dialog.update()
        dialog.lift()

    def _refresh_tool_icon(self, tool_key: str):
        """Refresh a tool's icon display after customization."""
        tool = self.tools_by_key.get(tool_key)
        if not tool:
            return

        icon_label = self.icon_labels.get(tool_key)
        if not icon_label:
            return

        # Get the new effective icon
        icon_path, icon_photo = self._get_effective_icon(tool)

        # Clear old cache entry if it's a custom path
        # (force reload on next access)

        if icon_photo:
            icon_label.configure(image=icon_photo, text="")
            # Store reference to prevent GC
            icon_label._photo = icon_photo
        else:
            icon_label.configure(image="", text="[?]")

    def _get_tool_usage_key(self, tool: ToolEntry) -> str:
        """Get the usage tracking key for a tool: its directory name.

        The key must stay stable across regroupings, so it is the tool's own
        directory name alone (e.g. "commit_generator"). It used to be
        "<Category>_<dirname>", but category is now a regroupable semantic label
        rather than a fixed folder, so it can no longer be part of the identity.
        The tracker merges legacy "<Category>_<dirname>" rows on read.
        """
        # script_path: /path/to/commit_generator/main.py -> commit_generator
        tool_dir = os.path.dirname(tool.script_path)
        return os.path.basename(tool_dir)

    def _format_desc_with_alias(self, tool: ToolEntry) -> str:
        """Format tool description with alias hint for CLI tools."""
        desc_text = tool.description
        if "Icon" not in tool.tags and tool.alias:
            desc_text = f"{tool.description}  ->  {tool.alias}"
        return desc_text

    def _table_row_surface(self, parent: tk.Widget, bg: str,
                           top_line: bool = False) -> tk.Frame:
        """Return a row surface for the tools table: a *bg*-colored frame in a
        border-colored wrapper that draws the row's 1px side and bottom grid
        lines (plus the top line when *top_line* — only the table's first,
        always-visible row draws one). Rows otherwise get their top line from
        the row above, so seams stay a single pixel and the table stays closed
        under per-row grid_remove() filtering."""
        wrap = tk.Frame(parent, bg=self.theme["border"], highlightthickness=0)
        wrap.pack(fill="x", padx=TABLE_PADX)
        inner = tk.Frame(wrap, bg=bg, highlightthickness=0)
        inner.pack(fill="both", expand=True, padx=1, pady=(1 if top_line else 0, 1))
        self.tk_frames.extend((wrap, inner))
        return inner

    def _set_row_emphasis(self, card: tk.Widget, on: bool) -> None:
        """Toggle the hovered-row look across *card*'s whole subtree: swap each
        Card ttk widget to its CardHover variant (accent-tinted surface, name
        brightened + a point larger) and repaint the plain tk panel frames to
        the matching hover color. Restores exactly on off. Tag pills (their own
        colored bg) and the 1px grid separators (border bg) are left untouched."""
        panel = self.theme["panel"]
        panel_hover = self.theme.get("panel_hover", panel)
        style_map = _ROW_HOVER_STYLES if on else _ROW_HOVER_STYLES_REV
        frame_from, frame_to = (panel, panel_hover) if on else (panel_hover, panel)

        def walk(w):
            try:
                cur = str(w.cget("style"))
            except tk.TclError:
                cur = ""
            if cur in style_map:
                try:
                    w.configure(style=style_map[cur])
                except tk.TclError:
                    pass
            else:
                # Plain tk widget: repaint only panel-colored surfaces so tag
                # pills and grid lines keep their own colors.
                try:
                    if str(w.cget("bg")) == frame_from:
                        w.configure(bg=frame_to)
                except tk.TclError:
                    pass
            for child in w.winfo_children():
                walk(child)

        walk(card)

    def _bind_row_hover(self, card: tk.Widget) -> None:
        """Light a tools-table row up while it's hovered: lift it onto an
        accent-tinted surface (_set_row_emphasis) on enter, restore on leave.
        No size or text-color change — only the row background tints.

        Enter/Leave crossing events fire on *card* for its whole descendant
        subtree, so binding on the card alone covers the row; a winfo_containing
        check on leave keeps moving between the row's own child widgets from
        being mistaken for leaving."""
        state = {"on": False}

        def set_on(on: bool) -> None:
            if on != state["on"]:
                state["on"] = on
                self._set_row_emphasis(card, on)

        def pointer_inside() -> bool:
            try:
                w = card.winfo_containing(
                    card.winfo_pointerx(), card.winfo_pointery())
            except tk.TclError:
                return False
            while w is not None:
                if w is card:
                    return True
                w = getattr(w, "master", None)
            return False

        card.bind("<Enter>", lambda _e: set_on(True), add="+")
        card.bind("<Leave>", lambda _e: set_on(pointer_inside()), add="+")

    def _build_right_cells(self, container: tk.Frame, bg: str) -> Dict[str, tk.Frame]:
        """Pack the fixed-width right-hand cells into *container*, each preceded
        by a 1px vertical grid line, and return them keyed by column.

        Header and row renderers all build their columns through this so the
        table grid stays aligned. Cells fill the container's height (the
        height=24 is only a floor for the header row, whose labels are short).
        """
        cells: Dict[str, tk.Frame] = {}
        for col_key, width in RIGHT_COLS:
            sep = tk.Frame(container, width=1, bg=self.theme["border"],
                           highlightthickness=0)
            sep.pack(side="left", fill="y")
            cell_bg = _DEBUG_CELL_COLORS[col_key] if DEBUG_LAYOUT else bg
            cell = tk.Frame(container, width=width, height=24, bg=cell_bg,
                            highlightthickness=0)
            cell.pack(side="left", fill="y")
            cell.pack_propagate(False)
            self.tk_frames.append(cell)
            cells[col_key] = cell
        return cells

    def _render_tag_pills(self, name_row, tags) -> None:
        """Render tag badges as filled pills on a card row."""
        t = self.theme
        colors = {"GUI": t["tag_gui"], "CLI": t["tag_cli"], "Icon": t["tag_icon"]}
        for tag in tags:
            lbl = tk.Label(name_row, text=f" {tag} ", font=("", 8, "bold"),
                           fg=t["panel"], bg=colors.get(tag, t["tag_cli"]),
                           padx=3, pady=0)
            lbl.pack(side="left", padx=(8, 0))
            self.tk_widgets.append(lbl)

    def _render_tool_row(self, tool: ToolEntry, parent_frame: ttk.Frame, indent: int = 0) -> None:
        """Render a single tool row of the tools table. *indent* adds extra
        left padding inside the row (child rows of a group) — the row itself
        always spans the full table width so the table edges stay straight."""
        t = self.theme
        panel_bg = "#333333" if DEBUG_LAYOUT else t["panel"]
        card = self._table_row_surface(parent_frame, panel_bg)

        key = f"{tool.category}_{tool.name}"

        # Right side columns container — a child of the card (not the padded
        # tool_row) so the 1px column grid lines span the full row height.
        # Packed first (side right) so it claims space before the info area.
        right_cols = tk.Frame(card, bg="#ff0000" if DEBUG_LAYOUT else panel_bg,
                              highlightthickness=0)
        right_cols.pack(side="right", fill="y")
        self.tk_frames.append(right_cols)
        cells = self._build_right_cells(right_cols, panel_bg)

        tool_row = ttk.Frame(card, padding=(10 + indent, 6, 10, 6), style="Card.TFrame")
        tool_row.pack(side="left", fill="both", expand=True)
        self._bind_row_hover(card)

        # Left side: Checkbox
        var = tk.BooleanVar(value=is_installed(tool))
        self.check_vars[key] = var
        var.trace_add("write", self._on_checkbox_changed)
        # Per-key trace so we know which install checkbox toggled — drives the
        # Install → Skill auto-check behavior. Has to come after self.skill_vars[key]
        # is potentially set below, so we register it at the end of the row.
        cb = ttk.Checkbutton(tool_row, variable=var, style="Card.TCheckbutton")
        cb.pack(side="left", padx=(0, 10))

        # Middle: Name and Description (takes remaining space)
        info_frame = ttk.Frame(tool_row, style="Card.TFrame")
        info_frame.pack(side="left", fill="both", expand=True)

        # Uses column
        uses_frame = cells["uses"]
        usage_key = self._get_tool_usage_key(tool)
        usage_count = self.usage_counts.get(usage_key, 0)
        usage_text = str(usage_count) if usage_count > 0 else ""
        uses_label = ttk.Label(uses_frame, text=usage_text, style="CardMuted.TLabel", anchor="center")
        uses_label.pack(expand=True, fill="both")

        # Status column
        status_label = ttk.Label(cells["status"], text="", style="Card.TLabel", anchor="center")
        status_label.pack(expand=True, fill="both")
        self.status_labels[key] = status_label

        # Skill column. Only renders a checkbox for tools that advertise a
        # skill_name; otherwise the cell stays empty so column widths align
        # across all rows.
        skill_frame = cells["skill"]
        if tool.skill_name:
            skill_var = tk.BooleanVar(value=_skill_installed(tool.skill_name))
            self.skill_vars[key] = skill_var
            skill_var.trace_add("write", self._on_checkbox_changed)
            skill_var.trace_add("write", lambda *_a, k=key: self._on_skill_var_changed(k))
            skill_cb = ttk.Checkbutton(skill_frame, variable=skill_var, style="Card.TCheckbutton")
            skill_cb.pack(expand=True)
            # Wire Install → Skill auto-check (only once skill_var exists).
            var.trace_add("write", lambda *_a, k=key: self._on_install_var_changed(k))

        # Icon column
        icon_frame = cells["icon"]
        _, icon_photo = self._get_effective_icon(tool)
        if icon_photo:
            icon_label = ttk.Label(icon_frame, image=icon_photo, cursor="hand2", style="Card.TLabel")
        else:
            icon_label = ttk.Label(icon_frame, text="[?]", style="CardMuted.TLabel", cursor="hand2")
        icon_label.pack(expand=True)
        icon_label.bind("<Button-1>", lambda e, k=key: self._show_icon_dialog(k))
        self.icon_labels[key] = icon_label

        # Auto-Start checkbox
        autostart_frame = cells["autostart"]
        if "Icon" in tool.tags or tool.cron_schedule:
            autostart_default = (
                is_autostart_enabled(tool)
                if is_installed(tool)
                else tool.default_autostart
            )
            autostart_var = tk.BooleanVar(value=autostart_default)
            self.autostart_vars[key] = autostart_var
            autostart_var.trace_add("write", self._on_checkbox_changed)
            autostart_cb = ttk.Checkbutton(autostart_frame, variable=autostart_var, style="Card.TCheckbutton")
            autostart_cb.pack(expand=True)

        # Name row with tag badges
        name_row = ttk.Frame(info_frame, style="Card.TFrame")
        name_row.pack(anchor="w", fill="x")
        ttk.Label(name_row, text=tool.name, style="CardToolName.TLabel").pack(side="left")
        self._render_tag_pills(name_row, tool.tags)
        # Optional domain distinguisher (e.g. "youtube") — dim, after the tags.
        if tool.domain:
            ttk.Label(name_row, text=f"· {tool.domain}", style="CardMuted.TLabel").pack(side="left", padx=(8, 0))

        # Description with alias hint and usage count
        desc_text = self._format_desc_with_alias(tool)
        desc_label = ttk.Label(info_frame, text=desc_text, style="CardMuted.TLabel", font=("", 9))
        desc_label.pack(anchor="w", fill="x")
        # Dynamic wraplength based on available width
        def update_wrap(event, lbl=desc_label):
            lbl.configure(wraplength=max(100, event.width - 10))
        info_frame.bind("<Configure>", update_wrap)

    def _render_tool_group(self, group: ToolGroup, current_row: int) -> int:
        """Render a tool group (parent + expandable children). Returns next row number."""
        parent = group.parent
        children = group.children

        if not children:
            # Single tool - render normally
            container = ttk.Frame(self.scrollable_frame)
            container.grid(row=current_row, column=0, sticky="ew")
            self._render_tool_row(parent, container)
            self.tool_group_data.append({
                # Band label — must match the key used for category_widgets so
                # search show/hide targets the right header.
                'category': _group_label(parent),
                'always_frames': [container],
                'expand_frame': None,
                'expand_key': None,
                'tools': [parent],
            })
            return current_row + 1

        # Group with children - render expandable
        group_key = f"{parent.category}_{parent.script_path}"

        # Check if any child is installed (for auto-expand)
        any_child_installed = any(is_installed(c) for c in children)
        parent_installed = is_installed(parent)

        # Expand by default only if a subtool is installed
        expand_var = tk.BooleanVar(value=any_child_installed)
        self.expand_vars[group_key] = expand_var

        # Parent row container
        parent_container = ttk.Frame(self.scrollable_frame)
        parent_container.grid(row=current_row, column=0, sticky="ew")

        # Parent row rendered on the shared table surface (matches _render_tool_row)
        t = self.theme
        panel_bg = t["panel"]
        card = self._table_row_surface(parent_container, panel_bg)

        # Right side columns — child of the card so grid lines span full height
        right_cols = tk.Frame(card, bg=panel_bg, highlightthickness=0)
        right_cols.pack(side="right", fill="y")
        self.tk_frames.append(right_cols)
        cells = self._build_right_cells(right_cols, panel_bg)

        parent_row = ttk.Frame(card, padding=(10, 6, 10, 6), style="Card.TFrame")
        parent_row.pack(side="left", fill="both", expand=True)
        self._bind_row_hover(card)

        key = f"{parent.category}_{parent.name}"

        # Left side: Expand toggle + Checkbox
        expand_icon = ttk.Label(parent_row, text="▼" if expand_var.get() else "▶",
                                font=("", 10), cursor="hand2", width=2, style="Card.TLabel")
        expand_icon.pack(side="left")

        var = tk.BooleanVar(value=parent_installed)
        self.check_vars[key] = var
        var.trace_add("write", self._on_checkbox_changed)
        cb = ttk.Checkbutton(parent_row, variable=var, style="Card.TCheckbutton")
        cb.pack(side="left", padx=(0, 10))

        # Middle: Name and Description (takes remaining space)
        info_frame = ttk.Frame(parent_row, style="Card.TFrame")
        info_frame.pack(side="left", fill="both", expand=True)

        # Uses column
        usage_key = self._get_tool_usage_key(parent)
        usage_count = self.usage_counts.get(usage_key, 0)
        usage_text = str(usage_count) if usage_count > 0 else ""
        uses_label = ttk.Label(cells["uses"], text=usage_text, style="CardMuted.TLabel", anchor="center")
        uses_label.pack(expand=True, fill="both")

        # Status column
        status_label = ttk.Label(cells["status"], text="", style="Card.TLabel", anchor="center")
        status_label.pack(expand=True, fill="both")
        self.status_labels[key] = status_label

        # Skill column. Only renders a checkbox for parents that advertise a
        # skill_name; otherwise the cell stays empty.
        skill_frame = cells["skill"]
        if parent.skill_name:
            skill_var = tk.BooleanVar(value=_skill_installed(parent.skill_name))
            self.skill_vars[key] = skill_var
            skill_var.trace_add("write", self._on_checkbox_changed)
            skill_var.trace_add("write", lambda *_a, k=key: self._on_skill_var_changed(k))
            skill_cb = ttk.Checkbutton(skill_frame, variable=skill_var, style="Card.TCheckbutton")
            skill_cb.pack(expand=True)
            var.trace_add("write", lambda *_a, k=key: self._on_install_var_changed(k))

        # Icon column
        icon_frame = cells["icon"]
        _, icon_photo = self._get_effective_icon(parent)
        if icon_photo:
            icon_label = ttk.Label(icon_frame, image=icon_photo, cursor="hand2", style="Card.TLabel")
        else:
            icon_label = ttk.Label(icon_frame, text="[?]", style="CardMuted.TLabel", cursor="hand2")
        icon_label.pack(expand=True)
        icon_label.bind("<Button-1>", lambda e, k=key: self._show_icon_dialog(k))
        self.icon_labels[key] = icon_label

        # Auto-Start checkbox
        autostart_frame = cells["autostart"]
        if "Icon" in parent.tags or parent.cron_schedule:
            autostart_default = (
                is_autostart_enabled(parent)
                if is_installed(parent)
                else parent.default_autostart
            )
            autostart_var = tk.BooleanVar(value=autostart_default)
            self.autostart_vars[key] = autostart_var
            autostart_var.trace_add("write", self._on_checkbox_changed)
            autostart_cb = ttk.Checkbutton(autostart_frame, variable=autostart_var, style="Card.TCheckbutton")
            autostart_cb.pack(expand=True)

        name_row = ttk.Frame(info_frame, style="Card.TFrame")
        name_row.pack(anchor="w", fill="x")
        ttk.Label(name_row, text=parent.name, style="CardToolName.TLabel").pack(side="left")
        self._render_tag_pills(name_row, parent.tags)

        # Description with alias hint
        desc_text = self._format_desc_with_alias(parent)
        desc_label = ttk.Label(info_frame, text=desc_text, style="CardMuted.TLabel", font=("", 9))
        desc_label.pack(anchor="w", fill="x")
        # Dynamic wraplength based on available width
        def update_wrap(event, lbl=desc_label):
            lbl.configure(wraplength=max(100, event.width - 10))
        info_frame.bind("<Configure>", update_wrap)

        current_row += 1

        # Children container (collapsible)
        children_frame = ttk.Frame(self.scrollable_frame)
        children_frame.grid(row=current_row, column=0, sticky="ew")
        self.children_frames[group_key] = children_frame

        for child in children:
            self._render_tool_row(child, children_frame, indent=25)

        # Show/hide based on initial state
        if not expand_var.get():
            children_frame.grid_remove()

        # Toggle function
        def toggle_expand():
            expand_var.set(not expand_var.get())
            if expand_var.get():
                expand_icon.config(text="▼")
                children_frame.grid()
            else:
                expand_icon.config(text="▶")
                children_frame.grid_remove()
            # Update scroll region
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        expand_icon.bind("<Button-1>", lambda e: toggle_expand())

        self.tool_group_data.append({
            # Band label (see single-tool branch above).
            'category': _group_label(parent),
            'always_frames': [parent_container],
            'expand_frame': children_frame,
            'expand_key': group_key,
            'tools': [parent] + children,
        })

        return current_row + 1

    def _update_status_labels(self):
        for tool in self.tools:
            key = f"{tool.category}_{tool.name}"
            label = self.status_labels.get(key)
            if label:
                skill_stale = bool(tool.skill_name) and tool.skill_status == "stale"
                if is_installed(tool):
                    if needs_update(tool) and skill_stale:
                        label.config(text="⟳ Update + skill", foreground="orange")
                    elif skill_stale:
                        label.config(text="⟳ Skill update", foreground="orange")
                    elif needs_update(tool):
                        label.config(text="⟳ Needs Update", foreground="orange")
                    else:
                        label.config(text="✓ Installed", foreground=self.theme["success"])
                else:
                    label.config(text="✗ Not installed", foreground=self.theme["muted"])
        self._refresh_update_btn()

    def _stale_items(self) -> list:
        """Itemise the local shortcut updates that are pending, with a human-readable
        reason and a 'since' timestamp where one can be derived. Drives both the badge
        count and the hover tooltip. Purely local — never reflects pip/PyPI state.

        Each item: {"name", "detail", "since": datetime | None}.
        """
        import datetime

        def mtime(path):
            try:
                return datetime.datetime.fromtimestamp(os.path.getmtime(path))
            except OSError:
                return None

        items: list = []
        # Orphaned .desktop shortcuts — the tool directory is gone.
        for o in self.orphan_desktops:
            items.append({"name": o.name,
                          "detail": f"orphaned shortcut — tool dir missing ({o.tool_path})",
                          "since": mtime(o.path)})
        # Orphaned aliases — the script they point at is gone.
        for o in self.orphan_aliases:
            items.append({"name": o.name,
                          "detail": f"orphaned alias → missing {o.script_path}",
                          "since": mtime(ALIASES_FILE)})
        # Installed tools whose shortcut drifted from advertised metadata.
        for t in self.tools:
            if not (is_installed(t) and needs_update(t)):
                continue
            if "Icon" in t.tags:
                detail = "shortcut path changed (tool moved/renamed)"
                since = mtime(os.path.join(APPS_DIR, t.desktop_file))
            else:
                old = _find_alias_for_script(t.script_path)
                detail = f"alias renamed: '{old}' → '{t.alias}'"
                since = mtime(ALIASES_FILE)
            items.append({"name": t.name, "detail": detail, "since": since})
        return items

    def _refresh_update_btn(self):
        """Recompute the pending-update set and push (stale?, count) into the badge.

        The visual is owned by the closure _build_update_btn installed; the itemised
        list is cached in self._stale_cache for the hover tooltip to render.
        """
        fn = getattr(self, "_update_state_fn", None)
        if fn is None:
            return
        self._stale_cache = self._stale_items()
        n = len(self._stale_cache)
        fn(n > 0, n)

    def _build_update_btn(self, parent):
        """Stale-shortcut indicator: a colour pill carrying the pending count beside
        neutral 'updates pending' text, falling back to a muted '✓ Up to date' when
        nothing is stale. Hovering reveals a tooltip detailing which tools drifted and
        when; clicking anywhere runs the local shortcut refresh. Greys out and ignores
        clicks while a bulk op is running (see _set_update_enabled). Installs the
        self._update_state_fn(stale, n) closure that _refresh_update_btn drives."""
        t = self.theme
        self._update_tip = None
        self._update_tip_show_after = None
        self._update_tip_hide_after = None
        self._update_enabled = True

        frame = tk.Frame(parent, bg=t["bg"])
        frame.pack(side="left", padx=5)
        pill = tk.Label(frame, bg=t["category"], fg=t["bg"], font=("", 9, "bold"),
                        padx=7, pady=1)
        label = tk.Label(frame, bg=t["bg"], fg=t["fg"], font=("", 10))
        self._update_frame, self._update_pill, self._update_label = frame, pill, label
        rest_fg = {"v": t["fg"]}
        stale_flag = {"v": False}

        def apply(stale, n):
            stale_flag["v"] = stale
            pill.pack_forget()
            label.pack_forget()
            if stale:
                pill.configure(text=str(n))
                pill.pack(side="left")
                label.configure(text="  updates pending", fg=t["fg"])
                rest_fg["v"] = t["fg"]
            else:
                label.configure(text="✓ Up to date", fg=t["muted"])
                rest_fg["v"] = t["muted"]
                self._hide_update_tip()
            label.pack(side="left")

        def on_click(_e):
            if self._update_enabled:
                self._update_all()

        def on_enter(_e):
            if not self._update_enabled:
                return
            label.configure(fg=t["accent"])
            self._cancel_tip_hide()
            if stale_flag["v"] and self._update_tip is None:
                self._cancel_tip_show()
                self._update_tip_show_after = self.root.after(
                    350, lambda: self._show_update_tip(frame))

        def on_leave(_e):
            if not self._update_enabled:
                return
            label.configure(fg=rest_fg["v"])
            self._cancel_tip_show()
            self._update_tip_hide_after = self.root.after(140, self._destroy_update_tip)

        for w in (frame, pill, label):
            w.configure(cursor="hand2")
            w.bind("<Button-1>", on_click)
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
        self._update_state_fn = apply

    def _set_update_enabled(self, enabled: bool):
        """Grey out / restore the update badge alongside the ttk action buttons.

        While disabled the badge ignores clicks and drops its hover affordances; on
        re-enable the correct colours are restored via _refresh_update_btn (the finish
        handlers also refresh status just before re-enabling)."""
        self._update_enabled = enabled
        frame = getattr(self, "_update_frame", None)
        pill = getattr(self, "_update_pill", None)
        label = getattr(self, "_update_label", None)
        cursor = "hand2" if enabled else "arrow"
        for w in (frame, pill, label):
            if w is not None:
                try:
                    w.configure(cursor=cursor)
                except tk.TclError:
                    pass
        if enabled:
            self._refresh_update_btn()
        else:
            self._hide_update_tip()
            t = self.theme
            try:
                if pill is not None:
                    pill.configure(bg=t["muted"], fg=t["bg"])
                if label is not None:
                    label.configure(fg=t["muted"])
            except tk.TclError:
                pass

    # ---- Hover tooltip for the update badge ------------------------------------

    @staticmethod
    def _since_str(dt) -> str:
        """Format a datetime as a relative '3 days ago (Jun 04)' string."""
        import datetime
        secs = (datetime.datetime.now() - dt).total_seconds()
        date = dt.strftime("%b %d")
        if secs < 300:
            return f"just now ({date})"
        if secs < 3600:
            return f"{int(secs // 60)}m ago ({date})"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago ({date})"
        days = int(secs // 86400)
        return f"{days} day{'s' if days != 1 else ''} ago ({date})"

    def _cancel_tip_show(self):
        aid = getattr(self, "_update_tip_show_after", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
            self._update_tip_show_after = None

    def _cancel_tip_hide(self):
        aid = getattr(self, "_update_tip_hide_after", None)
        if aid is not None:
            try:
                self.root.after_cancel(aid)
            except Exception:
                pass
            self._update_tip_hide_after = None

    def _destroy_update_tip(self):
        self._update_tip_hide_after = None
        tip = getattr(self, "_update_tip", None)
        if tip is not None:
            try:
                tip.destroy()
            except Exception:
                pass
            self._update_tip = None

    def _hide_update_tip(self):
        """Cancel any pending show and tear down the tooltip immediately."""
        self._cancel_tip_show()
        self._cancel_tip_hide()
        self._destroy_update_tip()

    def _show_update_tip(self, anchor):
        """Pop a borderless tooltip above the badge listing each pending update."""
        self._update_tip_show_after = None
        if not anchor.winfo_exists():
            return
        items = getattr(self, "_stale_cache", None) or []
        if not items:
            return
        self._destroy_update_tip()
        t = self.theme
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-type", "tooltip")  # WM hint (X11); ignored elsewhere
        except tk.TclError:
            pass
        tip.configure(bg=t["muted"])  # 1px border via padded inner frame
        inner = tk.Frame(tip, bg=t["log_bg"])
        inner.pack(padx=1, pady=1)

        n = len(items)
        tk.Label(inner, text=f"⚠ {n} shortcut update{'s' if n != 1 else ''} pending",
                 bg=t["log_bg"], fg=t["category"], font=("", 10, "bold"),
                 anchor="w", justify="left").pack(fill="x", padx=10, pady=(8, 5))

        MAX = 12
        for it in items[:MAX]:
            since = it.get("since")
            ago = self._since_str(since) if since else "since unknown"
            tk.Label(inner, text=f"•  {it['name']}", bg=t["log_bg"], fg=t["log_fg"],
                     font=("", 9, "bold"), anchor="w", justify="left").pack(fill="x", padx=12)
            tk.Label(inner, text=f"{it['detail']}   ·   {ago}", bg=t["log_bg"],
                     fg=t["muted"], font=("", 8), anchor="w",
                     justify="left").pack(fill="x", padx=24, pady=(0, 4))
        if n > MAX:
            tk.Label(inner, text=f"…and {n - MAX} more", bg=t["log_bg"], fg=t["muted"],
                     font=("", 8, "italic"), anchor="w").pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(inner, text="click the badge to refresh all shortcuts", bg=t["log_bg"],
                 fg=t["accent"], font=("", 8, "italic"), anchor="w").pack(
                     fill="x", padx=10, pady=(2, 8))

        tip.update_idletasks()
        x = anchor.winfo_rootx()
        y = anchor.winfo_rooty() - tip.winfo_height() - 6
        if y < 0:  # not enough room above — drop below instead
            y = anchor.winfo_rooty() + anchor.winfo_height() + 6
        sw = self.root.winfo_screenwidth()
        if x + tip.winfo_width() > sw:
            x = max(0, sw - tip.winfo_width() - 4)
        tip.wm_geometry(f"+{x}+{y}")
        self._update_tip = tip

    def _attach_tooltip(self, widget, text: str, delay: int = 500):
        """Attach a simple static-text hover tooltip to any widget.

        Self-contained per widget (own closure state), with the same show/hide debounce
        as the badge tooltip. Timers/Toplevel are guarded so a rebuild that destroys the
        widget mid-hover can't raise; the stray Toplevel (a root child) is swept by
        _rebuild_ui along with everything else."""
        state = {"tip": None, "show": None, "hide": None}

        def cancel(key):
            if state[key] is not None:
                try:
                    self.root.after_cancel(state[key])
                except Exception:
                    pass
                state[key] = None

        def destroy():
            state["hide"] = None
            if state["tip"] is not None:
                try:
                    state["tip"].destroy()
                except Exception:
                    pass
                state["tip"] = None

        def show():
            state["show"] = None
            if not widget.winfo_exists() or state["tip"] is not None:
                return
            t = self.theme
            tip = tk.Toplevel(self.root)
            tip.wm_overrideredirect(True)
            try:
                tip.attributes("-type", "tooltip")  # WM hint (X11); ignored elsewhere
            except tk.TclError:
                pass
            tip.configure(bg=t["muted"])  # 1px border via padded inner label
            tk.Label(tip, text=text, bg=t["log_bg"], fg=t["log_fg"], font=("", 9),
                     justify="left", anchor="w", wraplength=340,
                     padx=10, pady=7).pack(padx=1, pady=1)
            tip.update_idletasks()
            x = widget.winfo_rootx()
            y = widget.winfo_rooty() - tip.winfo_height() - 6
            if y < 0:  # not enough room above — drop below
                y = widget.winfo_rooty() + widget.winfo_height() + 6
            sw = self.root.winfo_screenwidth()
            if x + tip.winfo_width() > sw:
                x = max(0, sw - tip.winfo_width() - 4)
            tip.wm_geometry(f"+{x}+{y}")
            state["tip"] = tip

        def on_enter(_e):
            cancel("hide")
            if state["tip"] is None:
                cancel("show")
                state["show"] = self.root.after(delay, show)

        def on_leave(_e):
            cancel("show")
            state["hide"] = self.root.after(120, destroy)

        widget.bind("<Enter>", on_enter, add="+")
        widget.bind("<Leave>", on_leave, add="+")

    def _on_install_var_changed(self, key: str) -> None:
        """Auto-sync the Skill checkbox to the Install checkbox state.

        - Install false → true: set Skill true (only if tool advertises a skill)
        - Install true → false: set Skill false (can't keep a skill installed
          when its tool isn't)
        User can still uncheck Skill independently after that.
        """
        skill_var = self.skill_vars.get(key)
        if skill_var is None:
            return  # Tool doesn't advertise a skill
        install_var = self.check_vars.get(key)
        if install_var is None:
            return
        # Guard against recursion when this handler triggers another trace.
        if getattr(self, "_suppress_skill_sync", False):
            return
        self._suppress_skill_sync = True
        try:
            if install_var.get() and not skill_var.get():
                skill_var.set(True)
            elif not install_var.get() and skill_var.get():
                skill_var.set(False)
        finally:
            self._suppress_skill_sync = False

    def _on_skill_var_changed(self, key: str) -> None:
        """Enforce: Skill cannot be checked while Install is unchecked.

        If the user manually checks Skill on an uninstalled tool, revert it.
        Otherwise allow independent toggling.
        """
        if getattr(self, "_suppress_skill_sync", False):
            return
        skill_var = self.skill_vars.get(key)
        install_var = self.check_vars.get(key)
        if skill_var is None or install_var is None:
            return
        if skill_var.get() and not install_var.get():
            self._suppress_skill_sync = True
            try:
                skill_var.set(False)
            finally:
                self._suppress_skill_sync = False
            self.root.bell()  # audible cue that the toggle was refused

    def _on_checkbox_changed(self, *_args):
        """Highlight Apply Changes button when any checkbox is toggled."""
        if not self._apply_highlighted:
            self._apply_highlighted = True
            t = self.theme
            self.style.configure(
                "Accent.TButton",
                font=("", 10, "bold"),
                foreground=t["bg"],
                background=t["accent"],
            )
            self.style.map(
                "Accent.TButton",
                background=[("active", t["accent"])],
                foreground=[("active", t["bg"])],
            )
            self._apply_btn.configure(text="\u25b6 Apply Changes")

    def _unhighlight_apply_btn(self):
        """Reset Apply Changes button to default style."""
        self._apply_highlighted = False
        self.style.configure("Accent.TButton", font=("", 10, "bold"))
        self.style.map("Accent.TButton", background=[], foreground=[])
        self._apply_btn.configure(text="Apply Changes")

    def _select_all(self):
        for var in self.check_vars.values(): var.set(True)

    def _select_none(self):
        for var in self.check_vars.values(): var.set(False)

    def _clear_search(self):
        """Clear search entry and reset filter."""
        self.search_var.set("")
        self.search_entry.focus_set()

    @staticmethod
    def _fuzzy_match(query: str, text: str) -> bool:
        """Check if query fuzzy-matches text. All query words must match as substrings or subsequences."""
        query = query.lower()
        text = text.lower()
        if not query:
            return True
        for word in query.split():
            # Try substring first
            if word in text:
                continue
            # Fall back to subsequence matching
            qi = 0
            for ch in text:
                if qi < len(word) and ch == word[qi]:
                    qi += 1
            if qi < len(word):
                return False
        return True

    def _apply_search_filter(self):
        """Filter visible tools based on search query."""
        # During _rebuild_ui the placeholder insert fires this trace while
        # self.canvas still points at the just-destroyed old canvas — skip.
        if not hasattr(self, "canvas") or not self.canvas.winfo_exists():
            return
        query = self.search_var.get().strip()
        if query == self._search_placeholder:
            query = ""

        visible_categories: set = set()
        visible_count = 0

        for group_data in self.tool_group_data:
            category = group_data['category']
            always_frames = group_data['always_frames']
            expand_frame = group_data['expand_frame']
            expand_key = group_data['expand_key']
            tools = group_data['tools']

            matches = any(
                self._fuzzy_match(query, f"{t.name} {t.description}")
                for t in tools
            )

            if matches:
                visible_categories.add(category)
                visible_count += len(tools)
                for frame in always_frames:
                    frame.grid()
                # Restore expand_frame only if group is expanded
                if expand_frame is not None:
                    if expand_key and self.expand_vars.get(expand_key, tk.BooleanVar(value=True)).get():
                        expand_frame.grid()
                    else:
                        expand_frame.grid_remove()
            else:
                for frame in always_frames:
                    frame.grid_remove()
                if expand_frame is not None:
                    expand_frame.grid_remove()

        # Show/hide category headers
        for cat_name, cat_frame in self.category_widgets.items():
            if cat_name in visible_categories:
                cat_frame.grid()
            else:
                cat_frame.grid_remove()

        # Update count label
        if query:
            self.tools_count_label.config(text=f"({visible_count}/{len(self.tools)} matching)")
        else:
            self.tools_count_label.config(text=f"({len(self.tools)} tools found)")

        # Update scroll region
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _toggle_log(self, force_expand: bool = False):
        """Toggle log visibility or force expand."""
        if force_expand:
            self.log_expanded.set(True)
        else:
            self.log_expanded.set(not self.log_expanded.get())

        if self.log_expanded.get():
            self.log_toggle_icon.config(text="▼")
            self.log_content_frame.grid(row=1, column=0, sticky="nsew")
            self.log_copy_btn.grid(row=0, column=2, padx=(10, 0))
            self.log_clear_btn.grid(row=0, column=3, padx=(5, 0))
        else:
            self.log_toggle_icon.config(text="▶")
            self.log_content_frame.grid_forget()
            self.log_copy_btn.grid_forget()
            self.log_clear_btn.grid_forget()

    def _log(self, message: str, tag: str = "info"):
        """Append a message to the log with the specified tag (info, success, error, header)."""
        if not hasattr(self, "_log_history"):
            self._log_history = []
        self._log_history.append((message, tag))
        self.log_text.insert("end", message + "\n", tag)
        self.log_text.see("end")
        self.root.update_idletasks()

    def _clear_log(self):
        """Clear all log content."""
        self._log_history = []
        self.log_text.delete("1.0", "end")

    def _copy_log(self):
        """Copy log content to clipboard."""
        content = self.log_text.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)

    def _copy_hint_command(self):
        """Copy the hint command to clipboard."""
        if self.hint_command:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.hint_command)
            # Brief visual feedback
            original_text = self.hint_label.cget("text")
            self.hint_label.config(text="Copied!")
            self.root.after(1000, lambda: self.hint_label.config(text=original_text))

    def _set_hint(self, command: str):
        """Show a command hint with copy button."""
        self.hint_command = command
        self.hint_label.config(text=command)
        self.hint_frame.pack(side="right")

    def _clear_hint(self):
        """Hide the command hint."""
        self.hint_command = None
        self.hint_frame.pack_forget()

    def _extract_hint_from_output(self, output: str) -> str | None:
        """Extract a command hint from tool output.

        Matches patterns like:
        - "Run: source ~/.bashrc"
        - "Add this alias to your .bashrc:"  (followed by the alias line)
        """
        import re
        lines = output.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Match "Run: command" or "run: command"
            match = re.match(r'^(?:Run|run):\s*(.+)$', stripped)
            if match:
                return match.group(1).strip()
            # Match "Add this alias to your .bashrc:" followed by alias line
            if "add this" in stripped.lower() and ".bashrc" in stripped.lower():
                # Look for the alias command in the next few lines
                for j in range(i + 1, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    if next_line.startswith("alias "):
                        # Return the command to add to bashrc and source it
                        return host.shell_hint() if host.IS_WINDOWS else "source ~/.bashrc"
        return None

    def _apply_changes(self):
        """Apply checkbox/autostart changes. Thin driver: snapshots the Tk-var
        state on the main thread, then runs the blocking work on a background
        thread so the window stays responsive (see _bulk_reinstall)."""
        if self._op_in_progress:
            return  # an operation is already running; don't overlap
        self._op_in_progress = True
        self._unhighlight_apply_btn()
        self._set_action_buttons_state("disabled")

        # Main-thread UI prep
        self._clear_hint()
        self._toggle_log(force_expand=True)
        self._log("Applying changes...", "header")

        # Snapshot Tk-variable state on the main thread (the worker must not
        # touch Tk vars); the worker reads these plain dicts instead.
        check_state = {k: v.get() for k, v in self.check_vars.items()}
        autostart_state = {k: v.get() for k, v in self.autostart_vars.items()}
        skill_state = {k: v.get() for k, v in self.skill_vars.items()}

        events: queue.Queue = queue.Queue()
        threading.Thread(
            target=self._apply_worker,
            args=(check_state, autostart_state, skill_state, events),
            daemon=True,
        ).start()
        self.root.after(50, lambda: self._drain_op_events(events, self._finish_apply_changes))

    def _apply_worker(self, check_state, autostart_state, skill_state, events):
        """Background-thread body of _apply_changes.

        Runs the blocking install/remove/autostart work off the Tk main thread.
        MUST NOT touch any Tk widget or var -- it reads the snapshot dicts, logs
        via the queue, and emits ('uncheck_autostart', key) for the one var write
        the original did inline. Always emits a final ('done', result).
        """
        def log(msg, tag="info"):
            events.put(("log", msg, tag))

        installed = removed = updated = errors = 0
        autostart_enabled = autostart_disabled = 0
        skills_installed = skills_removed = 0
        hint_command = None
        try:
            for tool in self.tools:
                key = f"{tool.category}_{tool.name}"
                should_be = check_state.get(key, False)
                currently = is_installed(tool)
                stale = needs_update(tool)

                if should_be and not currently:
                    log(f"Installing: {tool.name}")
                    success, output = install_tool(tool)
                    if success:
                        installed += 1
                        log(f"  ✓ {tool.name} installed", "success")
                        hint = self._extract_hint_from_output(output)
                        if hint:
                            hint_command = hint
                    else:
                        errors += 1
                        log(f"  ✗ {tool.name} failed", "error")
                    if output:
                        for line in output.split("\n"):
                            log(f"    {line}", "success" if success else "error")

                elif should_be and currently and stale:
                    # Tool is installed but metadata changed (e.g., alias renamed)
                    log(f"Updating: {tool.name}")
                    # For alias changes, remove the OLD alias directly
                    # (the tool's --remove would look for the new name)
                    old_alias = _find_alias_for_script(tool.script_path)
                    if old_alias and old_alias != tool.alias:
                        log(f"  Renaming alias: {old_alias} → {tool.alias}")
                        aliases = _load_aliases()
                        if old_alias in aliases:
                            del aliases[old_alias]
                            _save_aliases(aliases)
                    # Then install with new metadata
                    success, output = install_tool(tool)
                    if success:
                        updated += 1
                        log(f"  ✓ {tool.name} updated", "success")
                        hint = self._extract_hint_from_output(output)
                        if hint:
                            hint_command = hint
                    else:
                        errors += 1
                        log(f"  ✗ {tool.name} update failed", "error")
                    if output:
                        for line in output.split("\n"):
                            log(f"    {line}", "success" if success else "error")

                elif not should_be and currently:
                    log(f"Removing: {tool.name}")
                    # Also disable autostart if enabled
                    if is_autostart_enabled(tool):
                        disable_autostart(tool)
                        if key in autostart_state:
                            events.put(("uncheck_autostart", key))
                    success, output = remove_tool(tool)
                    if success:
                        removed += 1
                        log(f"  ✓ {tool.name} removed", "success")
                    else:
                        errors += 1
                        log(f"  ✗ {tool.name} removal failed", "error")
                    if output:
                        for line in output.split("\n"):
                            log(f"    {line}", "success" if success else "error")

            # Handle autostart changes
            for tool in self.tools:
                key = f"{tool.category}_{tool.name}"
                if key not in autostart_state:
                    continue  # Not an Icon-tagged tool

                should_autostart = autostart_state.get(key, False)
                currently_autostart = is_autostart_enabled(tool)
                tool_installed = is_installed(tool)

                if should_autostart and not currently_autostart and tool_installed:
                    success, msg = enable_autostart(tool)
                    if success:
                        autostart_enabled += 1
                        log(f"  ✓ Autostart enabled: {tool.name}", "success")
                    else:
                        errors += 1
                        log(f"  ✗ Autostart failed: {tool.name} - {msg}", "error")
                elif not should_autostart and currently_autostart:
                    success, msg = disable_autostart(tool)
                    if success:
                        autostart_disabled += 1
                        log(f"  ✓ Autostart disabled: {tool.name}", "success")
                    else:
                        errors += 1
                        log(f"  ✗ Autostart disable failed: {tool.name} - {msg}", "error")
                elif should_autostart and not tool_installed:
                    # User checked autostart but tool isn't installed - uncheck it
                    events.put(("uncheck_autostart", key))

            # Handle Claude skill installation. The tool's own --install may
            # already have written SKILL.md as a side effect (scrape, studon,
            # …) — that's harmless: --install-skill is idempotent and
            # --uninstall-skill cleanly reverses it if the user unchecked the
            # Skill column.
            for tool in self.tools:
                if not tool.skill_name:
                    continue
                key = f"{tool.category}_{tool.name}"
                if key not in skill_state:
                    continue
                want_skill = skill_state[key]
                have_skill = _skill_installed(tool.skill_name)
                skill_stale = (tool.skill_status == "stale")
                tool_installed_now = is_installed(tool)
                # Install when missing, OR refresh in place when the installed
                # copy is stale (the tool reported skill_status=stale). Both go
                # through the idempotent --install-skill, which rewrites only if
                # the content differs.
                if want_skill and tool_installed_now and (not have_skill or skill_stale):
                    log(f"{'Updating' if have_skill else 'Installing'} skill: {tool.skill_name}")
                    success, output = install_skill_for_tool(tool)
                    if success:
                        skills_installed += 1
                        log(f"  ✓ skill '{tool.skill_name}' installed", "success")
                    else:
                        errors += 1
                        log(f"  ✗ skill '{tool.skill_name}' failed", "error")
                    if output:
                        for line in output.split("\n"):
                            log(f"    {line}", "success" if success else "error")
                elif have_skill and not want_skill:
                    log(f"Removing skill: {tool.skill_name}")
                    success, output = uninstall_skill_for_tool(tool)
                    if success:
                        skills_removed += 1
                        log(f"  ✓ skill '{tool.skill_name}' removed", "success")
                    else:
                        errors += 1
                        log(f"  ✗ skill '{tool.skill_name}' removal failed", "error")
                    if output:
                        for line in output.split("\n"):
                            log(f"    {line}", "success" if success else "error")

            refresh_desktop_database()
        except Exception as e:
            errors += 1
            log(f"  ✗ Unexpected error: {e}", "error")
        finally:
            events.put(("done", {
                "installed": installed,
                "updated": updated,
                "removed": removed,
                "errors": errors,
                "autostart_enabled": autostart_enabled,
                "autostart_disabled": autostart_disabled,
                "skills_installed": skills_installed,
                "skills_removed": skills_removed,
                "hint_command": hint_command,
            }))

    def _finish_apply_changes(self, result):
        """Main-thread finalize after an _apply_changes worker completes."""
        self._update_status_labels()
        errors = result["errors"]

        summary = (f"Done: {result['installed']} installed, "
                   f"{result['updated']} updated, {result['removed']} removed")
        autostart_changes = result["autostart_enabled"] + result["autostart_disabled"]
        if autostart_changes:
            summary += f", {autostart_changes} autostart changes"
        skill_changes = result.get("skills_installed", 0) + result.get("skills_removed", 0)
        if skill_changes:
            summary += f", {skill_changes} skill changes"
        if errors:
            summary += f", {errors} errors"
        self._log(summary, "warning" if errors else "success")
        self.status_bar.config(text=summary, foreground="blue" if not errors else "red")

        if result["hint_command"]:
            self._set_hint(result["hint_command"])

        self._set_action_buttons_state("normal")
        self._op_in_progress = False

    def _show_orphan_warning(self):
        """Show warning about orphaned shortcuts in the status bar and log."""
        total = len(self.orphan_desktops) + len(self.orphan_aliases)
        if total == 0:
            return

        # Update status bar with warning
        warning = f"⚠ {total} orphaned shortcut(s) found — click the 'updates pending' badge to clean up"
        self.status_bar.config(text=warning, foreground=self.theme["error"])

        # Log details
        self._toggle_log(force_expand=True)
        self._log(f"Found {total} orphaned shortcut(s) from removed tools:", "header")

        for orphan in self.orphan_desktops:
            self._log(f"  • Desktop: {orphan.name}", "info")
            self._log(f"    Missing: {orphan.tool_path}", "error")

        for orphan in self.orphan_aliases:
            self._log(f"  • Alias: {orphan.name}", "info")
            self._log(f"    Missing: {orphan.script_path}", "error")

        self._log("Click the 'updates pending' badge in the footer to remove orphans and refresh all shortcuts.", "info")

    def _update_all(self):
        """Refresh shortcuts: remove orphans, then rewrite the manager's plus only the
        drifted tools' .desktop/alias to match advertised metadata — exactly the items
        the badge counts, not every installed tool.

        Local-only: no pip, no network. Use 'Reinstall deps' for dependency installs.
        """
        self._bulk_reinstall(skip_deps=True, only_stale=True)

    def _toggle_auto_update_startup(self):
        """Persist the 'Auto-update on startup' checkbox to config.json."""
        set_auto_update_on_startup(self._auto_update_startup_var.get())

    def _maybe_auto_update_on_startup(self):
        """Run once per GUI launch, after the initial status scan. If the user
        has opted in via the startup checkbox, silently apply the same
        network-free reconciliation the Up-to-date badge offers — but log every
        step to the Operation Log (auto-expanding it) so the automatic run
        stays visible rather than happening silently in the background."""
        if not self._auto_update_startup_var.get():
            return
        self._toggle_log(force_expand=True)
        self._log("Auto-update on startup is enabled — checking for pending updates...", "header")
        if self._stale_cache:
            self._log(f"Found {len(self._stale_cache)} pending update(s) — applying automatically...", "info")
            self._update_all()
        else:
            self._log("Already up to date — nothing to do.", "success")

    def _toggle_autostart_check(self):
        """Enable/disable the login update-check autostart entry."""
        if self._autostart_check_var.get():
            path = enable_autostart_check()
            self._log(f"Login update check enabled → {path}", "success")
            self._log("  On login: applies local reconciliations (no pip), notifies for new tools.", "info")
        else:
            disable_autostart_check()
            self._log("Login update check disabled.", "info")

    def _reinstall_deps(self):
        """Full reinstall (pip dependencies + shortcuts) for every installed tool.

        This is the only action that reaches the network, so it confirms first.
        """
        installed_count = sum(1 for t in self.tools if is_installed(t))
        if not messagebox.askyesno(
            "Reinstall dependencies?",
            f"This runs 'pip install' from PyPI for {installed_count} installed "
            f"tool(s), plus a shortcut refresh.\n\n"
            f"Dependencies are fetched over the network. Continue?",
        ):
            return
        self._bulk_reinstall(skip_deps=False)

    def _bulk_reinstall(self, skip_deps: bool, only_stale: bool = False):
        """Shared driver for _update_all / _reinstall_deps.

        Cleans up orphaned shortcuts, then reinstalls the manager plus a set of tools.
        When only_stale is True (the badge 'update' action) that set is restricted to
        tools whose shortcut has actually drifted (needs_update) — so a click spawns a
        subprocess only for the drifted tools, not all installed ones. 'Reinstall deps'
        leaves it False to refresh every installed tool. When skip_deps is True the
        tools' --install runs with TOOLS_INSTALLER_SKIP_DEPS=1 so pip is never invoked.

        The blocking work runs on a background thread so the Tk event loop keeps
        running -- the window stays responsive and the log's Copy button keeps
        working while output streams in.
        """
        if self._op_in_progress:
            return  # an operation is already running
        self._op_in_progress = True
        self._set_action_buttons_state("disabled")

        # Main-thread UI prep
        self._clear_hint()
        self._toggle_log(force_expand=True)

        installed_tools = [t for t in self.tools if is_installed(t)]
        if only_stale:
            installed_tools = [t for t in installed_tools if needs_update(t)]
        events: queue.Queue = queue.Queue()

        threading.Thread(
            target=self._bulk_worker,
            args=(skip_deps, installed_tools, events),
            daemon=True,
        ).start()
        self.root.after(50, lambda: self._drain_op_events(events, self._finish_bulk_reinstall))

    def _bulk_worker(self, skip_deps, installed_tools, events):
        """Background-thread body of a bulk reinstall.

        Runs the blocking subprocess work off the Tk main thread. MUST NOT touch
        any Tk widget -- it only pushes ("log", msg, tag) tuples onto the queue.
        Always emits a final ("done", result) event, even on failure, so the
        poller can finish and re-enable the UI.
        """
        def log(msg, tag="info"):
            events.put(("log", msg, tag))

        updated = removed = errors = 0
        hint_command = None
        try:
            # Clean up orphans
            orphan_desktops = find_orphan_desktop_files()
            orphan_aliases = find_orphan_aliases()
            if orphan_desktops or orphan_aliases:
                log(f"Cleaning up {len(orphan_desktops)} orphaned shortcuts, "
                    f"{len(orphan_aliases)} orphaned aliases...", "header")
                for orphan in orphan_desktops:
                    log(f"Removing orphan: {orphan.name}")
                    success, msg = remove_orphan_desktop_file(orphan)
                    if success:
                        removed += 1
                        log(f"  ✓ {msg}", "success")
                    else:
                        errors += 1
                        log(f"  ✗ {msg}", "error")
                for orphan in orphan_aliases:
                    log(f"Removing orphan alias: {orphan.name}")
                    success, msg = remove_orphan_alias(orphan)
                    if success:
                        removed += 1
                        log(f"  ✓ {msg}", "success")
                    else:
                        errors += 1
                        log(f"  ✗ {msg}", "error")

            total = 1 + len(installed_tools)  # +1 for the installer itself
            scope = "shortcuts" if skip_deps else "tools (with dependencies)"
            log(f"Updating {total} {scope}...", "header")

            # Installer's own desktop file (always shortcut-only)
            log("Updating: Tools Installer")
            success, output = cli_install_self(quiet=True)
            if success:
                updated += 1
                log("  ✓ Tools Installer updated", "success")
            else:
                errors += 1
                log(f"  ✗ Tools Installer failed: {output}", "error")

            # Every installed tool
            for tool in installed_tools:
                log(f"Updating: {tool.name}")
                success, output = install_tool(tool, skip_deps=skip_deps)
                if success:
                    updated += 1
                    log(f"  ✓ {tool.name} updated", "success")
                    hint = self._extract_hint_from_output(output)
                    if hint:
                        hint_command = hint
                else:
                    errors += 1
                    log(f"  ✗ {tool.name} failed", "error")
                if output:
                    for line in output.split("\n"):
                        if line.strip():
                            log(f"    {line}", "success" if success else "error")

            refresh_desktop_database()
        except Exception as e:
            errors += 1
            log(f"  ✗ Unexpected error: {e}", "error")
        finally:
            events.put(("done", {
                "updated": updated,
                "removed": removed,
                "errors": errors,
                "hint_command": hint_command,
            }))

    def _drain_op_events(self, events, on_done):
        """Main-thread poller for a background operation worker.

        Drains queued events until 'done': 'log' appends a log line,
        'uncheck_autostart' resets a Tk autostart var, 'done' invokes the
        on_done finalize callback and stops polling.
        """
        try:
            while True:
                evt = events.get_nowait()
                kind = evt[0]
                if kind == "log":
                    self._log(evt[1], evt[2])
                elif kind == "uncheck_autostart":
                    var = self.autostart_vars.get(evt[1])
                    if var is not None:
                        var.set(False)
                elif kind == "done":
                    on_done(evt[1])
                    return  # stop polling
        except queue.Empty:
            pass
        self.root.after(50, lambda: self._drain_op_events(events, on_done))

    def _finish_bulk_reinstall(self, result):
        """Main-thread finalize after a bulk reinstall worker completes."""
        errors = result["errors"]

        # Cached orphans were cleaned up; clear before refreshing the status
        # labels so the 'updates pending' badge re-evaluates clean.
        self.orphan_desktops = []
        self.orphan_aliases = []
        self._update_status_labels()

        summary_parts = []
        if result["updated"]:
            summary_parts.append(f"{result['updated']} updated")
        if result["removed"]:
            summary_parts.append(f"{result['removed']} orphans removed")
        if errors:
            summary_parts.append(f"{errors} errors")
        summary = "Done: " + ", ".join(summary_parts) if summary_parts else "Done: nothing to do"
        self._log(summary, "warning" if errors else "success")
        self.status_bar.config(text=summary, foreground="blue" if not errors else "red")

        if result["hint_command"]:
            self._set_hint(result["hint_command"])

        self._set_action_buttons_state("normal")
        self._op_in_progress = False

    def _set_action_buttons_state(self, state: str):
        """Enable/disable the operation controls so a second run can't start mid-op.

        Greys the ttk action buttons and the custom update badge together; the badge
        also ignores clicks while disabled (see _set_update_enabled)."""
        for attr in ("_reinstall_btn", "_apply_btn"):
            btn = getattr(self, attr, None)
            if btn is not None:
                btn.configure(state=state)
        self._set_update_enabled(state == "normal")

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
                       for c in SELF_DESKTOP_NAME).strip(" .")
        stem = " ".join(stem.split()) or os.path.splitext(SELF_DESKTOP_FILE)[0]
        return os.path.join(APPS_DIR, stem + ".lnk")
    return os.path.join(APPS_DIR, SELF_DESKTOP_FILE)


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
        ensure_apps_dir()
        python_exec = sys.executable
        script_path = ENTRY_SCRIPT
        if host.IS_WINDOWS:
            lnk_path = _self_shortcut_path()
            ok = host.write_shortcut(lnk_path, python_exec, f'"{script_path}"',
                                     icon=_windows_icon(SELF_DESKTOP_ICON),
                                     workdir=ROOT_DIR)
            if not ok:
                return False, f"Could not write {lnk_path}"
            # Before 0.6.4 the .lnk was named after the desktop file's stem.
            # Drop that one, or the Start Menu keeps both.
            legacy = os.path.join(APPS_DIR,
                                  os.path.splitext(SELF_DESKTOP_FILE)[0] + ".lnk")
            if legacy != lnk_path and os.path.isfile(legacy):
                try:
                    os.remove(legacy)
                except OSError:
                    pass
            if not quiet:
                print(f"Installed: {lnk_path}")
            return True, lnk_path
        desktop_path = os.path.join(APPS_DIR, SELF_DESKTOP_FILE)

        content = f"""[Desktop Entry]
Type=Application
Name={SELF_DESKTOP_NAME}
Comment=Install and manage tool shortcuts
Exec={python_exec} "{script_path}"
Path={ROOT_DIR}
Icon={SELF_DESKTOP_ICON}
Terminal=false
Categories=Settings;Utility;
Keywords={IDENTITY.marker_token};ai;tool;installer-self;
StartupNotify=true
StartupWMClass={WM_CLASS}
"""
        with open(desktop_path, "w") as f: f.write(content)
        # Non-executable on purpose — see ToolInstaller._install_shortcut.
        os.chmod(
            desktop_path,
            os.stat(desktop_path).st_mode
            & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
        )
        refresh_desktop_database()
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
        refresh_desktop_database()
        print(f"Removed: {desktop_path}")
    else:
        print("Installer shortcut not found.")

def cli_uninstall_all(tools: List[ToolEntry]):
    removed = 0
    print(f"\nUninstalling all {len(tools)} tools...")
    for tool in tools:
        if is_installed(tool):
            success, output = remove_tool(tool)
            if success:
                print(f"  [RM] {tool.name}")
                removed += 1
            else:
                print(f"  [ERR] {tool.name}: {output}")

    refresh_desktop_database()
    print(f"\nDone. Removed {removed} shortcuts.\n")

def cli_cleanup(dry_run: bool = False):
    """Find and remove orphaned desktop files and aliases.

    Args:
        dry_run: If True, only list orphans without removing them.
    """
    orphan_desktops = find_orphan_desktop_files()
    orphan_aliases = find_orphan_aliases()

    if not orphan_desktops and not orphan_aliases:
        print("\nNo orphaned shortcuts found. All clean!\n")
        return

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
        return

    print()
    removed = 0
    errors = 0

    for orphan in orphan_desktops:
        success, msg = remove_orphan_desktop_file(orphan)
        if success:
            print(f"  [RM] {orphan.name}")
            removed += 1
        else:
            print(f"  [ERR] {msg}")
            errors += 1

    for orphan in orphan_aliases:
        success, msg = remove_orphan_alias(orphan)
        if success:
            print(f"  [RM] alias '{orphan.name}'")
            removed += 1
        else:
            print(f"  [ERR] {msg}")
            errors += 1

    if removed:
        refresh_desktop_database()

    summary = f"\nDone. Removed {removed} orphan(s)"
    if errors:
        summary += f", {errors} error(s)"
    print(summary + ".\n")


def cli_update_all(tools: List[ToolEntry]):
    """Sync: clean up orphans, then reinstall manager and all installed tool shortcuts."""
    removed = 0
    updated = 0
    errors = 0

    # First, clean up orphans
    orphan_desktops = find_orphan_desktop_files()
    orphan_aliases = find_orphan_aliases()

    if orphan_desktops or orphan_aliases:
        print(f"\nCleaning up {len(orphan_desktops)} orphaned desktop file(s), {len(orphan_aliases)} alias(es)...")

        for orphan in orphan_desktops:
            success, msg = remove_orphan_desktop_file(orphan)
            if success:
                print(f"  [RM] {orphan.name}")
                removed += 1
            else:
                print(f"  [ERR] {msg}")
                errors += 1

        for orphan in orphan_aliases:
            success, msg = remove_orphan_alias(orphan)
            if success:
                print(f"  [RM] alias '{orphan.name}'")
                removed += 1
            else:
                print(f"  [ERR] {msg}")
                errors += 1

    # Collect all items to update: manager first, then installed tools
    installed_tools = [t for t in tools if is_installed(t)]
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
        success, output = install_tool(tool)
        if success:
            print("[OK]")
            updated += 1
        else:
            print(f"[ERR] {output}")
            errors += 1

    refresh_desktop_database()

    summary_parts = []
    if updated:
        summary_parts.append(f"{updated} updated")
    if removed:
        summary_parts.append(f"{removed} orphans removed")
    if errors:
        summary_parts.append(f"{errors} errors")

    summary = "\nDone. " + ", ".join(summary_parts) if summary_parts else "\nDone."
    print(summary + ".\n")


def cli_check() -> int:
    """Login-time update check (headless; see AUTOSTART_CHECK_DESKTOP).

    Auto-applies the network-free reconciliations and notifies for the rest:

      APPLY (no pip, no network — rewritten from already-synced source):
        • a drifted shortcut (moved .desktop Exec path / renamed alias)  -> install_tool(skip_deps=True)
        • a currently-installed SKILL.md that has drifted                 -> idempotent --install-skill
          (only touched when the skill is already present, so a skill the
           user deliberately removed is never silently re-added)

      NOTIFY (would touch the network or add new capability — needs a human):
        • a discovered tool that is not installed (installing it may pip)
        • an installed tool whose advertised skill is absent (new / renamed skill)

    Never raises out of a login hook — always returns 0; the human-actionable
    items go to the notification and ~/.local/log/tools-installer-check.log.
    """
    import datetime

    try:
        # run_pre=False: a login hook must never reach the network (no repo clone).
        tools = discover_tools(run_pre=False)
    except Exception as e:
        _notify_send(f"{NOTIFY_APP}: check failed", str(e))
        return 0

    applied: list[str] = []      # silently reconciled "<tool>: <what>"
    failed: list[str] = []       # reconciliation that errored
    new_tools: list[str] = []    # uninstalled tool -> may pip -> human decides
    new_skills: list[str] = []   # installed tool, advertised skill absent -> human decides

    for t in tools:
        shortcut_installed = is_installed(t)
        skill_present = bool(t.skill_name) and _skill_installed(t.skill_name)
        # (1) drifted shortcut -> network-free refresh. Count it only if the
        #     install actually RESOLVED the drift (re-check needs_update), so a
        #     persistent/unresolvable mismatch can't be reported every login.
        #     Skipped entirely when CHECK_RECONCILE_SHORTCUTS is False (a tree
        #     whose --install has login-unsafe side effects, e.g. AutomatedAlchemy).
        if CHECK_RECONCILE_SHORTCUTS and shortcut_installed and needs_update(t):
            ok, out = install_tool(t, skip_deps=True)
            if not ok:
                failed.append(f"{t.name}: shortcut ({(out.splitlines() or ['failed'])[-1]})")
            elif not needs_update(t):
                applied.append(f"{t.name}: shortcut")
            # else: ran but drift persists -> stay silent (don't nag)
        # (2) skill reconciliation -> whenever the skill is INSTALLED (regardless
        #     of shortcut state). Count only if the file's CONTENT changed (hash
        #     before/after), independent of how a tool words its output.
        if skill_present:
            p = _skill_md_path(t.skill_name)
            before = _file_sig(p)
            ok, out = install_skill_for_tool(t)   # idempotent; refresh if drifted
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

    if applied:
        refresh_desktop_database()

    # --- log (always) ---
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"[{ts}] applied={len(applied)} failed={len(failed)} "
             f"new_tools={len(new_tools)} new_skills={len(new_skills)}"]
    for a in applied:    lines.append(f"  applied   {a}")
    for f_ in failed:    lines.append(f"  FAILED    {f_}")
    for n in new_tools:  lines.append(f"  new tool  {n}")
    for s in new_skills: lines.append(f"  new skill {s}")
    try:
        os.makedirs(os.path.dirname(CHECK_LOG), exist_ok=True)
        with open(CHECK_LOG, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    print("\n".join(lines))

    # --- notify only when the actionable set CHANGES (no per-login nagging) ---
    actionable = (sorted(new_tools)
                  + ["skill:" + s for s in sorted(new_skills)]
                  + ["fail:" + f for f in sorted(failed)])
    try:
        with open(CHECK_STATE) as fh:
            prev = json.load(fh).get("actionable", [])
    except (OSError, ValueError):
        prev = []
    try:
        os.makedirs(os.path.dirname(CHECK_STATE), exist_ok=True)
        with open(CHECK_STATE, "w") as fh:
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
        _notify_send(f"{NOTIFY_APP}: updates available", "\n".join(body))

    return 0

def main():
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
                        help="Install the login update-check autostart entry (~/.config/autostart)")
    parser.add_argument("--disable-autostart-check", action="store_true",
                        help="Remove the login update-check autostart entry")
    parser.add_argument("--refresh", action="store_true",
                        help="Before discovery, run the configured PRE_DISCOVERY hook in refresh "
                             "mode (e.g. ff-only pull every known repo checkout). No-op without a hook.")
    parser.add_argument("--apply", metavar="NAMES",
                        help="Headless install: a comma-separated list of tool aliases/names, "
                             "or 'all'. Tools not listed are left alone. Skills go to the "
                             "targets in --skill-target.")
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
        global REFRESH_REPOS
        REFRESH_REPOS = True

    if args.check:
        sys.exit(cli_check())

    if args.enable_autostart_check:
        path = enable_autostart_check()
        print(f"✓ Login update check enabled: {path}")
        print(f"  Runs: {sys.executable} {ENTRY_SCRIPT} --check")
        return

    if args.disable_autostart_check:
        print("✓ Login update check disabled" if disable_autostart_check()
              else "• Login update check was not enabled")
        return

    if args.uninstall:
        cli_uninstall_self()
        return

    if args.install:
        cli_install_self()
        return

    if args.cleanup:
        cli_cleanup(dry_run=not args.yes)
        return

    tools = discover_tools()

    if args.uninstall_all:
        cli_uninstall_all(tools)
        return

    if args.update_all:
        cli_update_all(tools)
        return

    if args.apply:
        from . import tui_installer
        sys.exit(tui_installer.apply_headless(tools, args.apply, args.skill_target,
                                              targets=SKILL_TARGETS))

    if args.list:
        print(f"\nDiscovered {len(tools)} tools:\n" + "="*60)
        for t in tools:
            installed = is_installed(t)
            stale = needs_update(t) if installed else False
            if stale:
                status = "⟳"  # Needs update
            elif installed:
                status = "✓"
            else:
                status = " "
            tags_str = ",".join(t.tags) if t.tags else "-"
            alias_info = f" ({t.alias})" if "Icon" not in t.tags and t.alias else ""
            # For stale tools, show what changed
            if stale and "Icon" not in t.tags:
                old_alias = _find_alias_for_script(t.script_path)
                alias_info = f" ({old_alias}→{t.alias})"
            print(f" [{status}] {tags_str:<12} {t.category:<12} | {t.name:<25}{alias_info}")
        return

    from . import tui_installer
    if tui_installer.prefer_tui(force_tui=args.tui, force_gui=args.gui, have_tk=_HAVE_TK):
        sys.exit(tui_installer.run_tui(tools, targets=SKILL_TARGETS,
                                       preselect=TUI_PRESELECT, title=WINDOW_TITLE))
    if not _HAVE_TK:
        if host.IS_WINDOWS:
            sys.exit("tkinter is not available in this Python; use --tui for "
                     "the text screen.")
        sys.exit("tkinter is not installed (Debian/Ubuntu: apt install python3-tk); "
                 "use --tui for the text screen.")

    root = tk.Tk(className=WM_CLASS)
    InstallerApp(root, tools)
    root.mainloop()


def run(*, identity: Optional[InstallerIdentity] = None,
        root_dir: Optional[str] = None, entry_script: Optional[str] = None,
        window_title: Optional[str] = None, discovery_roots: Optional[List[str]] = None,
        discoverer: Optional[Callable] = None, prune: Optional[Sequence[str]] = None,
        group_by: Optional[str] = None,
        pre_discovery: Optional[Callable] = None,
        check_reconcile_shortcuts: Optional[bool] = None,
        skill_targets: Optional[List] = None, tui_preselect: Optional[bool] = None,
        autostart_check_desktop_name: Optional[str] = None,
        check_log_name: Optional[str] = None, check_state_name: Optional[str] = None,
        self_desktop_file: Optional[str] = None, self_desktop_name: Optional[str] = None,
        self_desktop_icon: Optional[str] = None,
        wm_class: Optional[str] = None, notify_app: Optional[str] = None) -> None:
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

    ``prune`` adds directory names the default wider walk never enters, on top
    of ``DISCOVERY_PRUNE``. It does nothing when a wrapper passes its own
    ``discoverer``.

    Standalone use (the ``cli-tool-installer`` console script) calls this with no
    args; root_dir then defaults to the current working directory.
    """
    global ROOT_DIR, ENTRY_SCRIPT, WINDOW_TITLE, DISCOVERY_ROOTS, DISCOVERER, GROUP_BY
    global EXTRA_PRUNE
    global PRE_DISCOVERY, CHECK_RECONCILE_SHORTCUTS, SKILL_TARGETS, TUI_PRESELECT
    global AUTOSTART_CHECK_DESKTOP_NAME, CHECK_LOG_NAME, CHECK_STATE_NAME
    global SELF_DESKTOP_FILE, SELF_DESKTOP_NAME, SELF_DESKTOP_ICON, WM_CLASS, NOTIFY_APP

    # Identity first: it sets the whole namespace, and the individual name
    # kwargs below still win so a wrapper can override one of them.
    if identity is not None:
        _apply_identity(identity)
    elif entry_script is None and os.environ.get(InstallerIdentity.ENV_VAR, "") == "":
        # Bare engine: no identity, no wrapper pointing at itself. Opening the
        # GUI here would claim the first-party names on this host, so offer
        # setup instead. A wrapper that deliberately wants the legacy names
        # passes identity=LEGACY_IDENTITY.
        from .onboarding import print_onboarding
        raise SystemExit(print_onboarding(root_dir if root_dir is not None else os.getcwd()))

    ROOT_DIR = root_dir if root_dir is not None else os.getcwd()
    if entry_script is not None:
        ENTRY_SCRIPT = os.path.abspath(entry_script)
    if window_title is not None:
        WINDOW_TITLE = window_title
    if discovery_roots is not None:
        DISCOVERY_ROOTS = discovery_roots
    if discoverer is not None:
        DISCOVERER = discoverer
    if prune is not None:
        EXTRA_PRUNE = set(prune)
    if group_by is not None:
        if group_by not in ("capability", "category"):
            raise ValueError(
                f"group_by must be 'capability' or 'category', got {group_by!r}")
        GROUP_BY = group_by
    if pre_discovery is not None:
        PRE_DISCOVERY = pre_discovery
    if check_reconcile_shortcuts is not None:
        CHECK_RECONCILE_SHORTCUTS = check_reconcile_shortcuts
    if skill_targets is not None:
        SKILL_TARGETS = list(skill_targets)
    if tui_preselect is not None:
        TUI_PRESELECT = tui_preselect
    if autostart_check_desktop_name is not None:
        AUTOSTART_CHECK_DESKTOP_NAME = autostart_check_desktop_name
    if check_log_name is not None:
        CHECK_LOG_NAME = check_log_name
    if check_state_name is not None:
        CHECK_STATE_NAME = check_state_name
    if self_desktop_file is not None:
        SELF_DESKTOP_FILE = self_desktop_file
    if self_desktop_name is not None:
        SELF_DESKTOP_NAME = self_desktop_name
    if self_desktop_icon is not None:
        SELF_DESKTOP_ICON = self_desktop_icon
    if wm_class is not None:
        WM_CLASS = wm_class
    if notify_app is not None:
        NOTIFY_APP = notify_app

    _load_env()                 # re-read ROOT_DIR/.env now that ROOT_DIR is final
    _recompute_check_paths()    # re-derive login-check artifact paths from the names
    main()


if __name__ == "__main__":
    run()