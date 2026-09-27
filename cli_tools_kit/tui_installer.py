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

Skill targets
-------------
A tool that advertises ``skill_name`` can register its Claude Code skill in
more than one place. The default target, :func:`claude_target`, writes
``~/.claude/skills/<name>/`` through the tool's own ``--install-skill``. A
wrapper passes further :class:`SkillTarget` values via
``run(skill_targets=[...])``; the screen lists them and the user picks which
ones the Apply step writes to. Targets the user left unticked are not touched.

Keys
----
``Up``/``Down`` or ``j``/``k`` move, ``Space`` ticks Install, ``s`` ticks
Skill, ``1``..``9`` tick a skill target, ``a``/``n`` tick all/none, ``Enter``
applies, ``q`` quits.
"""

from __future__ import annotations

import os
import queue
import re
import sys
import threading
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from . import gui_installer as gi
from . import host
from .gui_installer import ToolEntry


class SkillTarget(NamedTuple):
    """One place a tool's Claude Code skill can be registered."""
    key: str                                            # "claude" — also the tick label
    label: str                                          # shown in the target line
    installed: Callable[[ToolEntry], bool]              # is the skill there already?
    install: Callable[[ToolEntry], Tuple[bool, str]]    # (ok, output)
    uninstall: Callable[[ToolEntry], Tuple[bool, str]]  # (ok, output)
    stale: Callable[[ToolEntry], bool] = lambda tool: False  # reinstall even if present


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

    @property
    def has_skill(self) -> bool:
        return bool(self.tool.skill_name)


def default_rows(tools: List[ToolEntry], preselect: Optional[bool] = None,
                 skill_installed: Optional[Callable[[ToolEntry], bool]] = None) -> List[Row]:
    """Initial tick state.

    ``preselect=None`` ticks everything on a host where none of the tools is
    installed yet (a first run) and otherwise mirrors what is installed.
    ``True`` always ticks everything, ``False`` always mirrors the host.
    """
    installed = [gi.is_installed(t) for t in tools]
    if preselect is None:
        preselect = not any(installed)
    if skill_installed is None:
        skill_installed = lambda tool: gi._skill_installed(tool.skill_name)  # noqa: E731
    rows = []
    for tool, is_in in zip(tools, installed):
        want = True if preselect else is_in
        skill = bool(tool.skill_name) and (want if preselect else skill_installed(tool))
        rows.append(Row(tool, want, skill))
    return rows


class Step(NamedTuple):
    kind: str                           # install | update | remove | skill_install | skill_remove
    tool: ToolEntry
    target: Optional[SkillTarget] = None


def plan(rows: List[Row], targets: List[SkillTarget], active: Set[str]) -> List[Step]:
    """What Apply would do, in order: tools first, then skills per active target."""
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
    for step in steps:
        tool, target = step.tool, step.target
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
        else:  # pragma: no cover - plan() never emits anything else
            continue
        counts[step.kind if ok else "errors"] += 1
        log(f"  {'ok' if ok else 'FAILED'}")
        for line in (out or "").splitlines():
            log(f"    {line}")
        if ok and out:
            hint = _hint(out) or hint
    gi.refresh_desktop_database()
    result: Dict[str, object] = dict(counts)
    result["hint"] = hint
    return result


def summary(result: Dict[str, object]) -> str:
    parts = []
    for kind, label in (("install", "installed"), ("update", "updated"),
                        ("remove", "removed"), ("skill_install", "skills written"),
                        ("skill_remove", "skills removed"), ("errors", "errors")):
        if result.get(kind):
            parts.append(f"{result[kind]} {label}")
    return ", ".join(parts) if parts else "nothing to do"


# --- headless apply -----------------------------------------------------------

def _matches(tool: ToolEntry, name: str) -> bool:
    name = name.strip().lower()
    return name in {tool.alias.lower(), tool.name.lower(), tool.skill_name.lower(),
                    tool.desktop_file.lower().removesuffix(".desktop"),
                    os.path.basename(os.path.dirname(tool.script_path)).lower()}


def apply_headless(tools: List[ToolEntry], names: str, target_keys: str = "claude",
                   targets: Optional[List[SkillTarget]] = None) -> int:
    """``--apply NAMES [--skill-target KEYS]``: install the named tools and their
    skills without a screen. Tools not named are left as they are. Returns 0,
    1 on errors, 2 on an unknown name."""
    targets = list(targets) if targets else [claude_target()]
    wanted = [n.strip() for n in names.split(",") if n.strip()]
    if wanted == ["all"]:
        chosen = list(tools)
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
    rows = [Row(t, True, bool(t.skill_name) and bool(keys)) for t in chosen]
    steps = plan(rows, targets, keys)
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
    cursor: int = 0
    status: Dict[int, str] = field(default_factory=dict)   # row index -> label
    log: List[str] = field(default_factory=list)
    busy: bool = False
    result: Optional[Dict[str, object]] = None
    events: "queue.Queue" = field(default_factory=queue.Queue)

    def refresh_status(self) -> None:
        for i, row in enumerate(self.rows):
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
    _put(scr, 1, 1, "Space install   s skill   1-9 skill target   a all   n none   "
                    "Enter apply   q quit", curses.A_DIM)
    tline = "Skills go to:"
    for i, target in enumerate(st.targets, start=1):
        tick = "x" if target.key in st.active else " "
        tline += f"   [{tick}] {i} {target.label}"
    _put(scr, 2, 1, tline)
    _put(scr, 3, 1, "─" * (width - 2))
    name_w = max(12, min(28, max((len(r.tool.name) for r in st.rows), default=12)))
    header = f"  Inst  Skill  {'Tool':<{name_w}}  {'Alias / shortcut':<20}  Status"
    _put(scr, 4, 1, header, curses.A_UNDERLINE)

    log_lines = max(3, min(10, height - 8 - len(st.rows)))
    list_top, list_height = 5, max(1, height - 5 - log_lines - 2)
    first = max(0, st.cursor - list_height + 1) if st.cursor >= list_height else 0
    for shown, i in enumerate(range(first, min(len(st.rows), first + list_height))):
        row = st.rows[i]
        tool = row.tool
        inst = "[x]" if row.install else "[ ]"
        if row.has_skill:
            skill = "[x]" if row.skill else "[ ]"
        else:
            skill = "   "
        handle = tool.alias if "Icon" not in tool.tags else tool.desktop_file
        line = (f"{inst}   {skill}    {_fit(tool.name, name_w):<{name_w}}  "
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
        elif ch == ord("s") and st.rows:
            row = st.rows[st.cursor]
            if row.install and row.has_skill:
                row.skill = not row.skill
        elif ord("1") <= ch <= ord("9"):
            idx = ch - ord("1")
            if idx < len(st.targets):
                key = st.targets[idx].key
                st.active.symmetric_difference_update({key})
        elif ch == ord("a"):
            for row in st.rows:
                row.install, row.skill = True, row.has_skill
        elif ch == ord("n"):
            for row in st.rows:
                row.install = row.skill = False
        elif ch in (curses.KEY_ENTER, 10, 13, ord("i")):
            steps = plan(st.rows, st.targets, st.active)
            if not steps:
                st.log.append("Nothing to do.")
                continue
            st.result = None
            st.busy = True
            threading.Thread(target=_worker, args=(steps, st.events), daemon=True).start()


def run_tui(tools: List[ToolEntry], *, targets: Optional[List[SkillTarget]] = None,
            preselect: Optional[bool] = None, title: str = "Tools installer") -> int:
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
    targets = list(targets) if targets else [claude_target()]
    rows = default_rows(tools, preselect, skill_installed=targets[0].installed)
    st = _State(rows=rows, targets=targets, active={targets[0].key}, title=title)
    st.refresh_status()
    curses.wrapper(lambda scr: _loop(scr, st))
    if st.result is not None:
        print(f"Done: {summary(st.result)}.")
        hint = st.result.get("hint")
        if hint:
            print(f"Run: {hint}")
        elif st.result.get("install") or st.result.get("update"):
            print(host.shell_hint())
        return 1 if st.result.get("errors") else 0
    return 0
