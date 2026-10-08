"""Where one installer's files go and how it is configured, as module globals.

``run()`` and wrappers set these; every other engine module reads them at
call time as ``state.NAME``, so a change applies everywhere at once.
"""

from __future__ import annotations

import os
from typing import Callable, List, Optional, Tuple

from . import host
from .identity import InstallerIdentity, LEGACY_IDENTITY


# ROOT_DIR — the project tree being managed. Defaults to the directory of this
# module for standalone use, but a wrapper almost always overrides it via
# run(root_dir=...) to point at its own tree (so discovery, .env, and the
# self-shortcut Path= all anchor to the wrapper, not the cli-tools-kit checkout).
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# ENTRY_SCRIPT — the script a wrapper wants launched by the manager .desktop and
# the login-check autostart entry. Defaults to this module; run(entry_script=...)
# points it at the wrapper so those launchers invoke the wrapper (which restores
# the wrapper's configuration), never this bare engine.
ENTRY_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_installer.py")


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

# UPGRADE_REPOS — (name, path) of the tool repos the installer cloned and may
# pull, filled in by sources.run_installer. A checkout pinned by `path` is never
# in it. upgrade.check() looks at these, at the installer's own checkout and at
# cli-tools-kit. LAUNCH_ARGV is the command line the installer restarts with
# after an upgrade; None means sys.argv.
UPGRADE_REPOS: List[Tuple[str, str]] = []
LAUNCH_ARGV: Optional[List[str]] = None

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

# PLUGIN_TARGETS lists the Claude Code config directories a plugin tool can be
# installed into (plugins.PluginTarget). None = the default ~/.claude only;
# discovery lists a plugin once per target (discovery.expand_plugin_targets).
PLUGIN_TARGETS: Optional[List] = None
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

# How deep below a discovery root a tool is still found, and the directory names
# the walk never enters. `vendor*` catches vendored checkouts (vendor-G2).
MAX_DISCOVERY_DEPTH = 4
DISCOVERY_PRUNE = {".venv", "venv", ".git", "node_modules", "__pycache__",
                   "out", "cache", "build", "dist", "archive"}

# Names a wrapper adds to DISCOVERY_PRUNE for its own tree, via run(prune=...).
# It extends the default set rather than replacing it.
EXTRA_PRUNE: set = set()

ALIASES_FILE = IDENTITY.aliases_path
AUTOSTART_DIR = (host.startup_dir() if host.IS_WINDOWS
                 else os.path.join(os.path.expanduser("~"), ".config", "autostart"))


# --- Login update-check autostart -----------------------------------------
# A startup entry (~/.config/autostart, or the Start Menu's Startup folder on
# Windows) that runs `installer.py --check` once per login.
# The check APPLIES network-free reconciliations (drifted .desktop Exec paths,
# renamed aliases, stale installed SKILL.md — all rewritten from the source
# already on disk via skip_deps, no pip) and only NOTIFIES for updates that would touch
# the network (a new, not-yet-installed tool) or that add a new skill. The
# pip/network gate stays behind an explicit human action.
# Derived from the configurable *_NAME knobs above. run() recomputes these after
# a wrapper overrides the names; the defaults keep tools/installer.py unchanged.


def _autostart_check_path() -> str:
    """Where the login-check entry goes, named the way the platform runs it.

    Windows' Startup folder executes shortcuts, not XDG .desktop files, so the
    name carries .lnk there and the entry is written as one.
    """
    name = AUTOSTART_CHECK_DESKTOP_NAME
    if host.IS_WINDOWS:
        name = os.path.splitext(name)[0] + ".lnk"
    return os.path.join(AUTOSTART_DIR, name)


AUTOSTART_CHECK_DESKTOP = _autostart_check_path()
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
    AUTOSTART_CHECK_DESKTOP = _autostart_check_path()
    CHECK_LOG = os.path.join(os.path.expanduser("~"), ".local", "log", CHECK_LOG_NAME)
    CHECK_STATE = os.path.join(os.path.expanduser("~"), ".local", "state", CHECK_STATE_NAME)
