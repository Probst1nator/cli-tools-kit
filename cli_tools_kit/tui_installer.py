"""Curses installer for terminals without a display.

The tkinter engine in :mod:`gui_installer` needs an X or Wayland session and
the ``python3-tk`` package. A lab PC reached over SSH, WSL without a display
server, or a minimal server image has neither, so ``installer.py`` falls back
to this screen: the same rows (install / skill / status) and the same Apply
step, drawn with the stdlib ``curses`` module.

    installer.py --tui        force this screen
    installer.py --gui        force tkinter
    installer.py              tkinter when it can open a window, else this

Everything that touches the host goes through :mod:`gui_installer`'s
primitives, looked up on that module at call time, so a wrapper that replaced
them (FAU-WW3-Tools routes each tool through its own venv) is honoured here
as well.

Skill and MCP targets
---------------------
A tool that advertises ``skill_name`` can register its skill with more than
one agent harness. :func:`claude_target` writes ``~/.claude/skills/<name>/``
through the tool's own ``--install-skill``; :func:`opencode_target` makes the
skill visible to opencode. A wrapper replaces the list via
``run(skill_targets=[...])``. A tool that advertises ``mcp_name`` and
``mcp_command`` likewise registers its MCP server with the harnesses in
:func:`mcp_servers.targets`. The screen lists the targets the host has, the
first of each kind ticked, and the user picks which ones the Apply step writes
to. Targets the user left unticked are not touched. A skill directory that a
tool's own ``--install`` creates in ``~/.claude/skills`` is removed again when
the claude target is not chosen for that tool.

Keys
----
``Up``/``Down`` or ``j``/``k`` move, ``Space`` ticks Install, ``s`` ticks
Skill, ``m`` ticks MCP, ``1``..``9`` tick a target, ``a``/``n`` tick all/none,
``Enter`` applies, ``q`` quits.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from . import gui_installer as gi
from . import host, mcp_servers, skills, upgrade
from .gui_installer import ToolEntry


class SkillTarget(NamedTuple):
    """One place a tool's Claude Code skill can be registered."""
    key: str                                            # "claude" — also the tick label
    label: str                                          # shown in the target line
    installed: Callable[[ToolEntry], bool]              # is the skill there already?
    install: Callable[[ToolEntry], Tuple[bool, str]]    # (ok, output)
    uninstall: Callable[[ToolEntry], Tuple[bool, str]]  # (ok, output)
    stale: Callable[[ToolEntry], bool] = lambda tool: False  # reinstall even if present
    available: Callable[[], bool] = lambda: True            # does this host have the harness?


def claude_target() -> SkillTarget:
    """The default target: ``~/.claude/skills/<name>/`` via ``--install-skill``."""
    return SkillTarget(
        key="claude",
        label="Claude Code (~/.claude/skills)",
        installed=lambda tool: gi._skill_installed(tool.skill_name),
        install=lambda tool: gi.install_skill_for_tool(tool),
        uninstall=lambda tool: gi.uninstall_skill_for_tool(tool),
        stale=lambda tool: tool.skill_status == "stale",
    )


# opencode loads SKILL.md files from its own config dir and, unless an
# environment flag turns that off, from ~/.claude/skills and ~/.agents/skills.

def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true")


def _opencode_own_dir(tool: ToolEntry) -> str:
    return os.path.join(str(mcp_servers.opencode_config_dir()), "skills", tool.skill_name)


def _opencode_shared_dirs(tool: ToolEntry) -> List[str]:
    """The skill directories outside its own config that opencode reads."""
    if _env_flag("OPENCODE_DISABLE_EXTERNAL_SKILLS"):
        return []
    out = []
    if not (_env_flag("OPENCODE_DISABLE_CLAUDE_CODE")
            or _env_flag("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS")):
        out.append(os.path.join(gi.CLAUDE_SKILLS_DIR, tool.skill_name))
    out.append(os.path.join(os.path.expanduser("~"), ".agents", "skills", tool.skill_name))
    return out


def _opencode_installed(tool: ToolEntry) -> bool:
    dirs = [_opencode_own_dir(tool)] + _opencode_shared_dirs(tool)
    return any(os.path.isfile(os.path.join(d, "SKILL.md")) for d in dirs)


def _opencode_install(tool: ToolEntry) -> Tuple[bool, str]:
    own = _opencode_own_dir(tool)
    shared = [d for d in _opencode_shared_dirs(tool) if os.path.isfile(os.path.join(d, "SKILL.md"))]
    if shared and not os.path.lexists(own):
        return True, f"opencode reads {shared[0]}"
    # Copy what --install-skill writes to ~/.claude/skills. A copy that was not
    # there before is removed again, so the Claude Code target stays as it was.
    claude_dir = os.path.join(gi.CLAUDE_SKILLS_DIR, tool.skill_name)
    had_claude = os.path.lexists(claude_dir)
    ok, out = gi.install_skill_for_tool(tool)
    if not ok:
        return False, out
    try:
        if not os.path.isfile(os.path.join(claude_dir, "SKILL.md")):
            return False, f"{tool.name} --install-skill did not write {claude_dir}/SKILL.md"
        if os.path.islink(own) or os.path.isfile(own):
            os.unlink(own)
        elif os.path.isdir(own):
            shutil.rmtree(own)
        shutil.copytree(claude_dir, own, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    finally:
        if not had_claude:
            gi.uninstall_skill_for_tool(tool)
    return True, f"copied the skill to {own}"


def _opencode_uninstall(tool: ToolEntry) -> Tuple[bool, str]:
    own = _opencode_own_dir(tool)
    if os.path.islink(own):
        os.unlink(own)
    elif os.path.isdir(own):
        shutil.rmtree(own)
    else:
        return True, f"{own} is not there"
    return True, f"removed {own}"


def _opencode_stale(tool: ToolEntry) -> bool:
    """opencode's own copy differs from the one in ~/.claude/skills."""
    own = skills.read_installed_skill(tool.skill_name, os.path.dirname(_opencode_own_dir(tool)))
    claude = skills.read_installed_skill(tool.skill_name, gi.CLAUDE_SKILLS_DIR)
    return own is not None and claude is not None and own != claude


def opencode_target() -> SkillTarget:
    """opencode: it reads ``~/.claude/skills``; else its own copy in its config dir."""
    return SkillTarget(
        key="opencode",
        label="opencode",
        installed=_opencode_installed,
        install=_opencode_install,
        uninstall=_opencode_uninstall,
        stale=_opencode_stale,
        available=lambda: (mcp_servers._opencode_exe() is not None
                           or mcp_servers.opencode_config_dir().is_dir()),
    )


def default_targets() -> List[SkillTarget]:
    return [claude_target(), opencode_target()]


def skill_targets() -> List[SkillTarget]:
    """The wrapper's ``run(skill_targets=...)``, else the built-in ones."""
    chosen = gi.SKILL_TARGETS
    return list(chosen) if chosen else default_targets()


def present(targets: List) -> List:
    """The skill or MCP targets whose harness this host has."""
    out = []
    for target in targets:
        try:
            if target.available():
                out.append(target)
        except Exception:  # a broken harness must not take the screen down
            continue
    return out


def prefer_tui(*, force_tui: bool = False, force_gui: bool = False,
               have_tk: bool = True, environ: Optional[Dict[str, str]] = None) -> bool:
    """Decide between this screen and tkinter.

    Explicit flags win. Without them, tkinter runs only when it is importable
    and a display is reachable (``DISPLAY`` or ``WAYLAND_DISPLAY`` set).

    Windows has no DISPLAY variable and ships tkinter with Python, so there the
    answer is just whether ``--tui`` was passed.
    """
    if force_tui:
        return True
    if force_gui:
        return False
    if host.IS_WINDOWS:
        return force_tui
    if not have_tk:
        return True
    env = os.environ if environ is None else environ
    return not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


@dataclass
class Row:
    tool: ToolEntry
    install: bool
    skill: bool
    mcp: bool = False

    @property
    def has_skill(self) -> bool:
        return bool(self.tool.skill_name)

    @property
    def has_mcp(self) -> bool:
        return bool(self.tool.mcp_name and self.tool.mcp_command)


def default_rows(tools: List[ToolEntry], preselect: Optional[bool] = None,
                 skill_installed: Optional[Callable[[ToolEntry], bool]] = None,
                 mcp_targets: Optional[List[mcp_servers.McpTarget]] = None) -> List[Row]:
    """Initial tick state.

    ``preselect=None`` ticks everything on a host where none of the tools is
    installed yet (a first run) and otherwise mirrors what is installed.
    ``True`` always ticks everything, ``False`` always mirrors the host.
    A Claude Code plugin row always mirrors the host: several rows can name
    one plugin, and installing one disables the others. MCP always mirrors
    the host (any of *mcp_targets*): a registered server starts with every
    session of its harness, so nothing ticks it by default.
    """
    installed = [gi.is_installed(t) for t in tools]
    if preselect is None:
        preselect = not any(installed)
    if skill_installed is None:
        skill_installed = lambda tool: gi._skill_installed(tool.skill_name)  # noqa: E731
    rows = []
    for tool, is_in in zip(tools, installed):
        want = True if preselect and not tool.claude_plugin else is_in
        skill = bool(tool.skill_name) and (want if preselect else skill_installed(tool))
        mcp = (bool(tool.mcp_name and tool.mcp_command)
               and bool(mcp_servers.registered_with(tool.mcp_name, mcp_targets or [])))
        rows.append(Row(tool, want, skill, mcp))
    return rows


class Step(NamedTuple):
    kind: str       # install | update | remove | skill_install | skill_remove | mcp_install | mcp_remove
    tool: ToolEntry
    target: Optional[object] = None     # a SkillTarget, or an McpTarget for mcp_*


def plan(rows: List[Row], targets: List[SkillTarget], active: Set[str],
         mcp_targets: Optional[List[mcp_servers.McpTarget]] = None,
         mcp_active: Optional[Set[str]] = None) -> List[Step]:
    """What Apply would do, in order: tools, then skills and MCP servers per active target."""
    steps: List[Step] = []
    for row in rows:
        current = gi.is_installed(row.tool)
        if row.install and not current:
            steps.append(Step("install", row.tool))
        elif row.install and current and gi.needs_update(row.tool):
            steps.append(Step("update", row.tool))
        elif not row.install and current:
            steps.append(Step("remove", row.tool))
    for row in rows:
        if not row.has_skill:
            continue
        for target in targets:
            if target.key not in active:
                continue
            have = target.installed(row.tool)
            if row.skill and row.install and (not have or target.stale(row.tool)):
                steps.append(Step("skill_install", row.tool, target))
            elif not row.skill and have:
                steps.append(Step("skill_remove", row.tool, target))
    for row in rows:
        if not row.has_mcp:
            continue
        argv = list(row.tool.mcp_command)
        for target in mcp_targets or []:
            if target.key not in (mcp_active or set()):
                continue
            try:
                have = target.registered().get(row.tool.mcp_name)
            except Exception:  # an unreadable config is left alone
                continue
            if row.mcp and row.install and have != argv:
                steps.append(Step("mcp_install", row.tool, target))
            elif not row.mcp and have is not None:
                steps.append(Step("mcp_remove", row.tool, target))
    return steps


_HINT_RE = re.compile(r"^(?:Run|run):\s*(.+)$")


def _hint(output: str) -> Optional[str]:
    """The ``Run: ...`` line a tool prints after installing, if any."""
    for line in output.splitlines():
        match = _HINT_RE.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def execute(steps: List[Step], log: Callable[[str], None]) -> Dict[str, object]:
    """Run the steps, logging one line per action plus the tool's own output."""
    counts: Counter = Counter()
    hint = None
    failed: Set[str] = set()   # script paths whose install or update failed
    # Script paths whose skill this Apply writes to ~/.claude/skills.
    claude_skill = {s.tool.script_path for s in steps if s.kind == "skill_install"
                    and s.target is not None and s.target.key == "claude"}
    for step in steps:
        tool, target = step.tool, step.target
        if step.kind == "skill_install" and tool.script_path in failed:
            # A skill tells the agent to run the tool; without the tool it misleads.
            log(f"Skill {tool.skill_name} skipped: {tool.name} did not install")
            continue
        if step.kind == "mcp_install" and tool.script_path in failed:
            log(f"MCP server {tool.mcp_name} skipped: {tool.name} did not install")
            continue
        # PROTOCOL.md lets a tool's --install write ~/.claude/skills/<name> by
        # itself. When that directory did not exist before and no step asks for
        # the claude target, the copy is removed after the install.
        stray = (step.kind in ("install", "update") and bool(tool.skill_name)
                 and tool.script_path not in claude_skill
                 and not os.path.lexists(os.path.join(gi.CLAUDE_SKILLS_DIR, tool.skill_name)))
        if step.kind == "install":
            log(f"Installing {tool.name}")
            ok, out = gi.install_tool(tool)
        elif step.kind == "update":
            log(f"Updating {tool.name}")
            old_alias = gi._find_alias_for_script(tool.script_path)
            if old_alias and old_alias != tool.alias:
                aliases = gi._load_aliases()
                if old_alias in aliases:
                    del aliases[old_alias]
                    gi._save_aliases(aliases)
                log(f"  alias {old_alias} -> {tool.alias}")
            ok, out = gi.install_tool(tool)
        elif step.kind == "remove":
            log(f"Removing {tool.name}")
            if gi.is_autostart_enabled(tool):
                gi.disable_autostart(tool)
            ok, out = gi.remove_tool(tool)
        elif step.kind == "skill_install" and target is not None:
            log(f"Skill {tool.skill_name} -> {target.key}")
            ok, out = target.install(tool)
        elif step.kind == "skill_remove" and target is not None:
            log(f"Skill {tool.skill_name} removed from {target.key}")
            ok, out = target.uninstall(tool)
        elif step.kind in ("mcp_install", "mcp_remove") and target is not None:
            adding = step.kind == "mcp_install"
            log(f"MCP server {tool.mcp_name} {'->' if adding else 'removed from'} {target.key}")
            try:
                ok, out = (target.install(tool.mcp_name, list(tool.mcp_command)) if adding
                           else target.uninstall(tool.mcp_name))
            except (mcp_servers.McpError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
                ok, out = False, str(exc)
        else:  # pragma: no cover - plan() never emits anything else
            continue
        counts[step.kind if ok else "errors"] += 1
        if not ok and step.kind in ("install", "update"):
            failed.add(tool.script_path)
        log(f"  {'ok' if ok else 'FAILED'}")
        for line in (out or "").splitlines():
            log(f"    {line}")
        if ok and out:
            hint = _hint(out) or hint
        if stray and gi._skill_installed(tool.skill_name):
            gone, gone_out = gi.uninstall_skill_for_tool(tool)
            if gone:
                log(f"  removed ~/.claude/skills/{tool.skill_name}: --install wrote it, "
                    "but the claude skill target is not chosen")
            else:
                counts["errors"] += 1
                log(f"  FAILED to remove ~/.claude/skills/{tool.skill_name}, which "
                    f"--install wrote: {gone_out}")
    gi.refresh_desktop_database()
    result: Dict[str, object] = dict(counts)
    result["hint"] = hint
    return result


def summary(result: Dict[str, object]) -> str:
    parts = []
    for kind, label in (("install", "installed"), ("update", "updated"),
                        ("remove", "removed"), ("skill_install", "skills written"),
                        ("skill_remove", "skills removed"),
                        ("mcp_install", "MCP servers registered"),
                        ("mcp_remove", "MCP servers removed"), ("errors", "errors")):
        if result.get(kind):
            parts.append(f"{result[kind]} {label}")
    return ", ".join(parts) if parts else "nothing to do"


# --- headless apply -----------------------------------------------------------

def _matches(tool: ToolEntry, name: str) -> bool:
    name = name.strip().lower()
    names = {tool.alias.lower(), tool.name.lower(), tool.skill_name.lower(),
             tool.desktop_file.lower().removesuffix(".desktop")}
    # The directory name stands for every row of a tool, but plugin rows
    # exclude each other, so those are matched by their own names only.
    if not tool.claude_plugin:
        names.add(os.path.basename(os.path.dirname(tool.script_path)).lower())
    return name in names - {""}


def apply_headless(tools: List[ToolEntry], names: str, target_keys: str = "claude",
                   targets: Optional[List[SkillTarget]] = None, mcp_keys: str = "",
                   mcp_targets: Optional[List[mcp_servers.McpTarget]] = None) -> int:
    """``--apply NAMES [--skill-target KEYS] [--mcp-target KEYS]``: install the
    named tools and their skills without a screen, and register their MCP
    servers with the harnesses in *mcp_keys* (none by default). Tools not named
    are left as they are. Returns 0, 1 on errors, 2 on an unknown name."""
    targets = list(targets) if targets else skill_targets()
    mcp_targets = list(mcp_targets) if mcp_targets is not None else mcp_servers.targets()
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if wanted == ["all"]:
        # Plugin rows exclude each other, so "all" leaves them to be named.
        chosen = [t for t in tools if not t.claude_plugin]
    else:
        chosen = []
        for name in wanted:
            hits = [t for t in tools if _matches(t, name)]
            if not hits:
                known = ", ".join(sorted({t.alias or t.name for t in tools}))
                print(f"Unknown tool '{name}'. Known: {known}", file=sys.stderr)
                return 2
            chosen.extend(h for h in hits if h not in chosen)
    keys = {k.strip() for k in target_keys.split(",") if k.strip()} - {"none"}
    unknown = keys - {t.key for t in targets}
    if unknown:
        print(f"Unknown skill target(s) {sorted(unknown)}. Known: "
              f"{[t.key for t in targets]}", file=sys.stderr)
        return 2
    mkeys = {k.strip() for k in mcp_keys.split(",") if k.strip()} - {"none"}
    unknown = mkeys - {t.key for t in mcp_targets}
    if unknown:
        print(f"Unknown MCP target(s) {sorted(unknown)}. Known: "
              f"{[t.key for t in mcp_targets]}", file=sys.stderr)
        return 2
    rows = [Row(t, True, bool(t.skill_name) and bool(keys),
                bool(t.mcp_name and t.mcp_command) and bool(mkeys)) for t in chosen]
    # Only the targets in mkeys are touched: without --mcp-target, none.
    steps = plan(rows, targets, keys, mcp_targets, mkeys)
    if not steps:
        print("Nothing to do: already installed.")
        return 0
    result = execute(steps, print)
    print(f"Done: {summary(result)}.")
    hint = result.get("hint")
    if hint:
        print(f"Run: {hint}")
    elif result.get("install") or result.get("update"):
        print(host.shell_hint())
    return 1 if result.get("errors") else 0


# --- the screen ---------------------------------------------------------------

@dataclass
class _State:
    rows: List[Row]
    targets: List[SkillTarget]
    active: Set[str]
    title: str
    mcp_targets: List[mcp_servers.McpTarget] = field(default_factory=list)
    mcp_active: Set[str] = field(default_factory=set)
    cursor: int = 0
    status: Dict[int, str] = field(default_factory=dict)   # row index -> label
    log: List[str] = field(default_factory=list)
    busy: bool = False
    result: Optional[Dict[str, object]] = None
    events: "queue.Queue" = field(default_factory=queue.Queue)
    upgrades: List["upgrade.Item"] = field(default_factory=list)
    restart: bool = False   # an upgrade changed the installer; start it again on exit

    def refresh_status(self) -> None:
        for i, row in enumerate(self.rows):
            if row.tool.claude_plugin:
                # Installing one plugin row can disable another; untick it so
                # the next Apply does not switch it back on.
                row.install = gi.is_installed(row.tool)
            if gi.is_installed(row.tool):
                self.status[i] = "update" if gi.needs_update(row.tool) else "installed"
            else:
                self.status[i] = "-"


def _worker(steps: List[Step], events: "queue.Queue") -> None:
    try:
        result = execute(steps, lambda msg: events.put(("log", msg)))
    except Exception as exc:  # the screen must come back whatever a tool did
        events.put(("log", f"  FAILED: {exc}"))
        result = {"errors": 1, "hint": None}
    events.put(("done", result))


def _check_worker(events: "queue.Queue") -> None:
    try:
        items = upgrade.check()
    except Exception:  # an upgrade check must never take the screen down
        items = []
    events.put(("upgrades", items))


def _upgrade_worker(items, tools: List[ToolEntry], events: "queue.Queue") -> None:
    try:
        result = upgrade.upgrade(items, tools, lambda msg, tag="info": events.put(("log", msg)))
    except Exception as exc:
        events.put(("log", f"  FAILED: {exc}"))
        result = {"changed": False, "errors": 1}
    events.put(("upgraded", result))


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(width - 1, 0)] + "…"


def _put(scr, y: int, x: int, text: str, attr: int = 0) -> None:
    """addstr that tolerates the bottom-right cell and a shrinking terminal."""
    import curses
    height, width = scr.getmaxyx()
    if y < 0 or y >= height or x >= width:
        return
    try:
        scr.addstr(y, x, _fit(text, width - x - 1), attr)
    except curses.error:
        pass


def _draw(scr, st: _State) -> None:
    import curses
    scr.erase()
    height, width = scr.getmaxyx()
    _put(scr, 0, 1, st.title, curses.A_BOLD)
    _put(scr, 1, 1, "Space install   s skill   m MCP   1-9 target   a all   n none   "
                    "Enter apply   " + ("u upgrade   " if st.upgrades else "") + "q quit",
         curses.A_DIM)
    tline = "Skills go to:"
    for i, target in enumerate(st.targets, start=1):
        tick = "x" if target.key in st.active else " "
        tline += f"   [{tick}] {i} {target.label}"
    _put(scr, 2, 1, tline)
    top = 3
    if st.mcp_targets and any(r.has_mcp for r in st.rows):
        mline = "MCP servers go to:"
        for i, target in enumerate(st.mcp_targets, start=len(st.targets) + 1):
            tick = "x" if target.key in st.mcp_active else " "
            mline += f"   [{tick}] {i} {target.label}"
        _put(scr, top, 1, mline)
        top += 1
    _put(scr, top, 1, "─" * (width - 2))
    name_w = max(12, min(28, max((len(r.tool.name) for r in st.rows), default=12)))
    header = f"  Inst  Skill  MCP  {'Tool':<{name_w}}  {'Alias / shortcut':<20}  Status"
    _put(scr, top + 1, 1, header, curses.A_UNDERLINE)

    log_lines = max(3, min(10, height - 8 - len(st.rows)))
    list_top = top + 2
    list_height = max(1, height - list_top - log_lines - 2)
    first = max(0, st.cursor - list_height + 1) if st.cursor >= list_height else 0
    for shown, i in enumerate(range(first, min(len(st.rows), first + list_height))):
        row = st.rows[i]
        tool = row.tool
        inst = "[x]" if row.install else "[ ]"
        if row.has_skill:
            skill = "[x]" if row.skill else "[ ]"
        else:
            skill = "   "
        mcp = ("[x]" if row.mcp else "[ ]") if row.has_mcp else "   "
        handle = tool.claude_plugin or (tool.alias if "Icon" not in tool.tags
                                         else tool.desktop_file)
        line = (f"{inst}   {skill}    {mcp}  {_fit(tool.name, name_w):<{name_w}}  "
                f"{_fit(handle, 20):<20}  {st.status.get(i, '')}")
        attr = curses.A_REVERSE if i == st.cursor and not st.busy else 0
        _put(scr, list_top + shown, 1, (">" if i == st.cursor else " ") + line, attr)
    if st.rows and st.cursor < len(st.rows):
        desc = st.rows[st.cursor].tool.description
        _put(scr, list_top + list_height, 1, _fit(desc, width - 3), curses.A_DIM)

    log_top = list_top + list_height + 1
    label = " working… " if st.busy else (f" done: {summary(st.result)} " if st.result else " log ")
    _put(scr, log_top, 1, "─" * 3 + label + "─" * max(0, width - 6 - len(label)))
    for shown, line in enumerate(st.log[-(log_lines - 1):] if log_lines > 1 else []):
        _put(scr, log_top + 1 + shown, 1, line)
    scr.refresh()


def _drain(st: _State) -> None:
    while True:
        try:
            kind, payload = st.events.get_nowait()
        except queue.Empty:
            return
        if kind == "log":
            st.log.append(str(payload))
        elif kind == "done":
            st.result = payload
            st.busy = False
            st.refresh_status()
            hint = payload.get("hint")
            st.log.append(f"Done: {summary(payload)}" + (f" — run: {hint}" if hint else ""))
        elif kind == "upgrades":
            st.upgrades = list(payload)
            if st.upgrades:
                st.log.append("Upgrade available: "
                              + " · ".join(item.label() for item in st.upgrades)
                              + " — press u")
        elif kind == "upgraded":
            st.busy = False
            if payload["changed"] and not payload["errors"]:
                st.restart = True
            else:
                st.log.append("Upgrade finished with errors; see above." if payload["errors"]
                              else "Nothing was upgraded.")


def _loop(scr, st: _State) -> None:
    import curses
    curses.curs_set(0)
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    scr.keypad(True)
    scr.timeout(100)
    while True:
        _drain(st)
        if st.restart:
            return
        _draw(scr, st)
        ch = scr.getch()
        if ch == -1 or st.busy:
            continue
        if ch in (ord("q"), 27):
            return
        if ch in (curses.KEY_UP, ord("k")):
            st.cursor = max(0, st.cursor - 1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            st.cursor = min(len(st.rows) - 1, st.cursor + 1)
        elif ch == ord(" ") and st.rows:
            row = st.rows[st.cursor]
            row.install = not row.install
            row.skill = row.install and row.has_skill
            row.mcp = row.mcp and row.install
        elif ch == ord("s") and st.rows:
            row = st.rows[st.cursor]
            if row.install and row.has_skill:
                row.skill = not row.skill
        elif ch == ord("m") and st.rows:
            row = st.rows[st.cursor]
            if row.install and row.has_mcp:
                row.mcp = not row.mcp
        elif ord("1") <= ch <= ord("9"):
            idx = ch - ord("1")
            if idx < len(st.targets):
                st.active.symmetric_difference_update({st.targets[idx].key})
            elif idx - len(st.targets) < len(st.mcp_targets):
                key = st.mcp_targets[idx - len(st.targets)].key
                st.mcp_active.symmetric_difference_update({key})
        elif ch == ord("a"):
            for row in st.rows:
                row.install, row.skill = True, row.has_skill
        elif ch == ord("n"):
            for row in st.rows:
                row.install = row.skill = row.mcp = False
        elif ch == ord("u") and st.upgrades:
            items, st.upgrades = st.upgrades, []
            st.result = None
            st.busy = True
            threading.Thread(target=_upgrade_worker,
                             args=(items, [row.tool for row in st.rows], st.events),
                             daemon=True).start()
        elif ch in (curses.KEY_ENTER, 10, 13, ord("i")):
            steps = plan(st.rows, st.targets, st.active, st.mcp_targets, st.mcp_active)
            if not steps:
                st.log.append("Nothing to do.")
                continue
            st.result = None
            st.busy = True
            threading.Thread(target=_worker, args=(steps, st.events), daemon=True).start()


def run_tui(tools: List[ToolEntry], *, targets: Optional[List[SkillTarget]] = None,
            preselect: Optional[bool] = None, title: str = "Tools installer",
            mcp_targets: Optional[List[mcp_servers.McpTarget]] = None) -> int:
    """Open the screen. Returns 0, or 1 when the last Apply reported errors."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("The text installer needs an interactive terminal. "
              "Use --list for a listing, or run this from a shell.", file=sys.stderr)
        return 2
    if not tools:
        print("No tools found.", file=sys.stderr)
        return 1
    try:
        import curses
    except ImportError:
        print("The text screen needs the curses module, which Python for "
              "Windows does not include. Install it with: "
              "pip install windows-curses", file=sys.stderr)
        return 1
    targets = list(targets) if targets else skill_targets()
    targets = present(targets) or targets
    mcp_targets = present(list(mcp_targets) if mcp_targets is not None else mcp_servers.targets())
    rows = default_rows(tools, preselect, skill_installed=targets[0].installed,
                        mcp_targets=mcp_targets)
    st = _State(rows=rows, targets=targets, active={targets[0].key}, title=title,
                mcp_targets=mcp_targets,
                mcp_active={mcp_targets[0].key} if mcp_targets else set())
    st.refresh_status()
    threading.Thread(target=_check_worker, args=(st.events,), daemon=True).start()
    curses.wrapper(lambda scr: _loop(scr, st))
    if st.restart:
        print("Upgraded. Starting the installer again...")
        host.restart(upgrade.restart_argv())
    if st.result is not None:
        print(f"Done: {summary(st.result)}.")
        hint = st.result.get("hint")
        if hint:
            print(f"Run: {hint}")
        elif st.result.get("install") or st.result.get("update"):
            print(host.shell_hint())
        return 1 if st.result.get("errors") else 0
    return 0
