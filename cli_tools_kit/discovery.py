"""Finding the tools in a tree and reading their ``--advertise`` metadata."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import host
from . import plugins
from . import autostart_gate
from . import startup_timing
from . import state
from .autostart_gate import KNOWN_CONDITIONS


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
    autostart_conditions: List[str] = []  # Conditions this tool's autostart supports ("time_window", "network"); the values live in the installer's autostart.json, never in the tool. See cli_tools_kit.autostart_gate.
    claude_plugin: str = ""        # Claude Code plugin id ("name@marketplace"); the row installs that plugin instead of a shortcut or alias. See cli_tools_kit.plugins.
    claude_marketplace: str = ""   # Where the plugin's marketplace comes from (owner/repo, URL or path)
    claude_config_dir: str = ""    # Which Claude Code config dir the row targets ("" = ~/.claude); set by discovery from PLUGIN_TARGETS, not advertised
    mcp_name: str = ""             # If non-empty, the tool brings a stdio MCP server of that name; see cli_tools_kit.mcp_servers
    mcp_command: Tuple[str, ...] = ()  # The argv that starts that server (absolute paths); the installer registers it with each harness


def _group_label(entry: "ToolEntry") -> str:
    """The band label for a row: the GROUP_BY field of *entry*.

    Everything that keys on a band (the headers, expand/collapse, the search
    show/hide) must go through this, so all of them agree on one label.
    """
    return getattr(entry, state.GROUP_BY, "") or entry.capability


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
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
            env=host.child_env(),
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

                # Alias is required if no Icon tag, except for a plugin row,
                # which installs neither a shortcut nor an alias.
                has_icon = "Icon" in tags
                alias = item.get("alias", "")
                claude_plugin = str(item.get("claude_plugin", "") or "")
                if not has_icon and not alias and not claude_plugin:
                    # Default alias from desktop_file stem
                    alias = item.get("desktop_file", "").replace(".desktop", "")
                # A server needs both its name and the command that starts it.
                mcp_command = item.get("mcp_command")
                mcp_command = (tuple(mcp_command) if isinstance(mcp_command, list) and mcp_command
                               and all(isinstance(a, str) and a for a in mcp_command) else ())
                mcp_name = str(item.get("mcp_name", "") or "") if mcp_command else ""
                mcp_command = mcp_command if mcp_name else ()

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
                    autostart_conditions=[
                        c for c in item.get("autostart_conditions", []) or []
                        if c in KNOWN_CONDITIONS
                    ],
                    claude_plugin=claude_plugin,
                    claude_marketplace=str(item.get("claude_marketplace", "") or ""),
                    mcp_name=mcp_name,
                    mcp_command=mcp_command,
                ))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception):
        # If a tool fails to advertise, it is ignored.
        pass

    return entries


# ================= ADVERTISE CACHE =================
#
# Probing every tool with --advertise starts one Python process per tool, which
# took 0.45 s of every start on a 41-tool tree, 16 at a time (2026-10-17). A
# tool's answer changes when its files do, so discover_tools keeps each answer
# with a fingerprint: the kit version and the modules that parse the answer,
# the interpreter, every file below the tool's directory and the installed copy
# of each skill it names, because skill_status is computed against that copy.
# Any changed mtime or size means a new probe. Of the directories in
# _FINGERPRINT_SKIP only the existence counts: virtualenvs and caches, and the
# data/, logs/ and cache/ where tools keep the state they write while they run,
# which would otherwise send a tool that ran since the last start through its
# probe again. A tool that offers its MCP server once mcp_server/.venv exists
# still gets probed when that directory appears. Code a tool imports from
# outside its own directory is not covered; --refresh probes everything again.
# A directory of more than _FINGERPRINT_MAX_FILES files is probed every time.

ADVERTISE_CACHE_NAME = "advertise-cache.json"
_FINGERPRINT_SKIP = frozenset({".venv", "venv", "__pycache__", ".git", "node_modules",
                               ".mypy_cache", ".pytest_cache", "data", "logs", "cache"})
_FINGERPRINT_MAX_FILES = 5000


def _advertise_cache_file() -> str:
    return os.path.join(state.IDENTITY.cache_path, ADVERTISE_CACHE_NAME)


def _tree_signature(path: str, limit: Optional[int] = None) -> Optional[List[list]]:
    """[relative path, mtime_ns, size] of every file below *path*, sorted.

    A directory in _FINGERPRINT_SKIP appears as its path with a trailing
    slash and no contents. None when there are more than *limit* files:
    stat-ing them would cost more than the probe it saves.
    """
    sig = []
    pending = [(path, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as it:
                entries = list(it)
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name in _FINGERPRINT_SKIP:
                        sig.append([prefix + entry.name + "/", 0, 0])
                    else:
                        pending.append((entry.path, prefix + entry.name + "/"))
                    continue
                st = entry.stat()
            except OSError:
                continue
            sig.append([prefix + entry.name, st.st_mtime_ns, st.st_size])
            if limit is not None and len(sig) > limit:
                return None
    sig.sort()
    return sig


def _advertise_fingerprint(file_path: str, category: str, tool_sig: List[list],
                           entries: List[ToolEntry]) -> str:
    from . import __version__  # noqa: PLC0415 — the package imports this module

    skills = sorted({e.skill_name for e in entries if e.skill_name})
    blob = json.dumps([__version__, _kit_signature(), sys.executable, file_path, category,
                       tool_sig,
                       [[name, _tree_signature(os.path.join(
                           os.path.expanduser("~"), ".claude", "skills", name))]
                        for name in skills]])
    return hashlib.sha256(blob.encode()).hexdigest()


def _kit_signature() -> list:
    """ToolEntry's fields and the modules that parse an answer, for a kit
    checkout whose __version__ stays the same across edits."""
    sig: list = [list(ToolEntry._fields)]
    for module in (__file__, autostart_gate.__file__):
        try:
            st = os.stat(module)
        except OSError:
            continue
        sig.append([st.st_mtime_ns, st.st_size])
    return sig


def _entries_from(rows) -> Optional[List[ToolEntry]]:
    """The ToolEntry list a cache record holds, or None when it does not fit."""
    try:
        return [ToolEntry(**{**row, "mcp_command": tuple(row.get("mcp_command") or ())})
                for row in rows]
    except (TypeError, AttributeError):
        return None


def _load_advertise_cache() -> Dict[str, dict]:
    try:
        with open(_advertise_cache_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_advertise_cache(cache: Dict[str, dict]) -> None:
    """Write *cache* atomically, minus the scripts that no longer exist."""
    kept = {path: entry for path, entry in cache.items() if os.path.isfile(path)}
    path = _advertise_cache_file()
    tmp = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(kept, fh)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        # The next start probes again.
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _cache_hit(key: str, category: str, tool_sig: Optional[List[list]],
               cache: Dict[str, dict]) -> Optional[List[ToolEntry]]:
    """The cached entries for the script *key* while its fingerprint holds, else None."""
    record = cache.get(key)
    if tool_sig is None or state.REFRESH_REPOS or not isinstance(record, dict):
        return None
    entries = _entries_from(record.get("entries") or [])
    if entries and record.get("fingerprint") == _advertise_fingerprint(
            key, category, tool_sig, entries):
        return entries
    return None


def expand_plugin_targets(tools: List[ToolEntry], targets) -> List[ToolEntry]:
    """One row per plugin target for every tool that installs a Claude Code plugin.

    A target without a directory (the default ``~/.claude``) keeps the
    advertised row. Every other target gets a copy with its key appended to the
    name and the desktop_file, which the screens key rows on, and passes its
    directory to the tool as ``--claude-config-dir``. Without targets every
    plugin row targets the default.
    """
    if not targets:
        return tools
    out: List[ToolEntry] = []
    for tool in tools:
        if not tool.claude_plugin:
            out.append(tool)
            continue
        for target in targets:
            if not target.config_dir:
                out.append(tool)
                continue
            stem = os.path.splitext(tool.desktop_file)[0]
            out.append(tool._replace(
                name=f"{tool.name} ({target.key})",
                desktop_file=f"{stem}-{target.key}.desktop",
                description=f"{tool.description} Installs into {target.label}.",
                args=list(tool.args) + [plugins.CONFIG_DIR_ARG, target.config_dir],
                claude_config_dir=target.config_dir,
            ))
    return out


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
            or name in state.DISCOVERY_PRUNE or name in state.EXTRA_PRUNE)


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
                             if not _walk_pruned(d) and depth < state.MAX_DISCOVERY_DEPTH)
        entry = _walk_entry_point(dirpath, filenames)
        if entry:
            found.append((entry, _walk_category(root, dirpath)))
    return found


def _timed_probe(file_path: str, category: str) -> List[ToolEntry]:
    with startup_timing.probe(file_path):
        return get_metadata_native(file_path, category)


def discover_tools(run_pre: bool = True) -> List[ToolEntry]:
    """Scan configured DISCOVERY_ROOTS for installable tools.

    By default scans ROOT_DIR with the tools_* / main.py layout. A wrapper
    can set DISCOVERY_ROOTS and/or DISCOVERER to plug in a different layout
    (e.g. AutomatedAlchemy's flat project-per-dir tree).

    If PRE_DISCOVERY is set it runs first (for repo-clone bootstrapping). Pass
    run_pre=False to skip it — the login-check path does this so a login hook
    can never reach the network.
    """
    if run_pre and state.PRE_DISCOVERY is not None:
        with startup_timing.phase("pre-discovery hook (clone, pull)"):
            state.PRE_DISCOVERY(state.REFRESH_REPOS)

    roots = state.DISCOVERY_ROOTS or [state.ROOT_DIR]
    # A single root_dir keeps the flat/tools_* layouts it has always used; the
    # category label of a tools_<cat>/ tree is only produced there. Several
    # roots mean repos of different shapes, so those get the wider walk.
    discoverer = state.DISCOVERER or (_walk_tools_discoverer if state.DISCOVERY_ROOTS
                                else _default_tools_discoverer)

    # Each tool is probed by spawning it with --advertise (a short-lived
    # subprocess that exits before its heavy imports). That makes discovery
    # I/O-bound, so probe every tool concurrently instead of paying the spawn
    # latency serially — this scan runs on every GUI launch and every login
    # `--check`, so the serial cost (≈Ntools × spawn) was the whole startup
    # delay. map() preserves discovery order, get_metadata_native swallows its
    # own errors (returns []), and subprocess.run drops the GIL while waiting,
    # so threads parallelize the wall-clock time. A tool whose files have not
    # changed since its last probe is answered from the advertise cache.
    with startup_timing.phase("discoverer (lists the tool directories)"):
        pairs = [(entry_point, category)
                 for root in roots
                 for entry_point, category in discoverer(root)]
    tools = []
    if pairs:
        cache = _load_advertise_cache()
        keys = [os.path.abspath(entry_point) for entry_point, _ in pairs]
        with startup_timing.phase("advertise cache (fingerprints)"):
            # Taken before any probe, so a file edited while its tool runs
            # makes the next start probe again.
            sigs = [_tree_signature(os.path.dirname(key), _FINGERPRINT_MAX_FILES)
                    for key in keys]
            found = [_cache_hit(key, category, sig, cache)
                     for key, (_, category), sig in zip(keys, pairs, sigs)]
        misses = [i for i, entries in enumerate(found) if entries is None]
        if misses:
            workers = min(len(misses), 16)
            startup_timing.info(probe_workers=workers)
            stored = False
            with startup_timing.phase("--advertise probes"), \
                    ThreadPoolExecutor(max_workers=workers) as pool:
                for i, entries in zip(misses, pool.map(lambda i: _timed_probe(*pairs[i]),
                                                       misses)):
                    found[i] = entries
                    sig = sigs[i]
                    if entries and sig is not None:
                        cache[keys[i]] = {
                            "fingerprint": _advertise_fingerprint(keys[i], pairs[i][1],
                                                                  sig, entries),
                            "entries": [e._asdict() for e in entries],
                        }
                        stored = True
            if stored:
                _save_advertise_cache(cache)
        for entries in found:
            tools.extend(entries or [])
    with startup_timing.phase("plugin rows"):
        tools = expand_plugin_targets(tools, state.PLUGIN_TARGETS)
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
