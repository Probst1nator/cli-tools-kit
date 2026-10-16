"""What ``--startup-time`` measures: how long each part of the installer's start takes.

``--startup-time [RUNS]`` opens no screen itself. The installer starts itself
RUNS times (default 1), each time as a new process with the same command line
and ``ENV_REPORT`` set. Each of those runs records the start and end of every
startup phase, stops once the window is drawn, writes the phases to the file
``ENV_REPORT`` names and exits. Everything up to the first frame runs as in a
normal start, including a pre-discovery hook that clones a missing repo or
refreshes an org listing. The work the window schedules for after it is shown
(orphan warning, startup auto-update, upgrade check) never runs, so a
measurement rewrites no shortcut and makes no upgrade. The first process then prints one
table: the first run on its own, since it may have read from a cold disk cache,
and the median and range of the others.

Times count from the moment the first process starts a run, so the
interpreter's own start and every import are included.

The engine records phases with :func:`phase` (a ``with`` block, nestable) and
:func:`total` (a running sum, for work spread over many calls such as loading
icons). Both cost one ``if`` when no measurement is running. This module
imports nothing from the kit at load time, because every engine module imports
it.
"""

from __future__ import annotations

import atexit
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional

ENV_REPORT = "CLI_TOOLS_KIT_STARTUP_REPORT"
ENV_T0 = "CLI_TOOLS_KIT_STARTUP_T0"

# Taken out of the environment at import, so the processes a measured run
# starts (each tool's --advertise among them) are not measured runs themselves
# and do not overwrite the report.
_REPORT_PATH = os.environ.pop(ENV_REPORT, "")
ACTIVE = bool(_REPORT_PATH)


def _start_time() -> float:
    try:
        return float(os.environ.pop(ENV_T0))
    except (KeyError, ValueError):
        return time.time()


T0 = _start_time()
_LOADED = time.time()          # when the kit began loading: __init__ imports this module first
_phases: List[dict] = []       # in the order they started, so parents precede children
_stack: List[str] = []         # labels of the phases open right now
_totals: Dict[str, dict] = {}
_info: Dict[str, object] = {}
_probes: List[tuple] = []     # (tool directory, seconds), one per --advertise
_SEP = " > "                   # joins the labels of nested phases into one key
_written = False


def _key(label: str) -> str:
    return _SEP.join(_stack + [label])


def _record(label: str, start: float, end: float) -> None:
    _phases.append({"key": _key(label), "label": label, "depth": len(_stack),
                    "start": start - T0, "end": end - T0})


@contextmanager
def phase(label: str) -> Iterator[None]:
    """Time the ``with`` block as one phase, nested under the phases open around it."""
    if not ACTIVE:
        yield
        return
    entry = {"key": _key(label), "label": label, "depth": len(_stack),
             "start": time.time() - T0, "end": None}
    _phases.append(entry)
    _stack.append(label)
    try:
        yield
    finally:
        _stack.pop()
        entry["end"] = time.time() - T0


@contextmanager
def total(label: str) -> Iterator[None]:
    """Add the ``with`` block's time to a running sum shown under the open phase."""
    if not ACTIVE:
        yield
        return
    key = _key(label)
    started = time.time()
    try:
        yield
    finally:
        entry = _totals.get(key)
        if entry is None:
            entry = {"key": key, "label": label, "depth": len(_stack), "seconds": 0.0,
                     "calls": 0, "total": True}
            _totals[key] = entry
            _phases.append(entry)
        entry["seconds"] += time.time() - started
        entry["calls"] += 1


def to_main() -> None:
    """Record the time before main(): interpreter start, imports, the wrapper's own setup."""
    if ACTIVE:
        _record("python start and wrapper, until the kit loads", T0, _LOADED)
        _record("kit imports (tkinter, Pillow) and wrapper setup", _LOADED, time.time())


@contextmanager
def probe(script: str) -> Iterator[None]:
    """Note how long one tool's ``--advertise`` takes. Used from the probe threads."""
    if not ACTIVE:
        yield
        return
    started = time.time()
    try:
        yield
    finally:
        _probes.append((os.path.basename(os.path.dirname(script)) or script,
                        time.time() - started))


def info(**values) -> None:
    """Facts for the report header, such as the tool count."""
    if ACTIVE:
        _info.update(values)


def write_report() -> None:
    """Write what was recorded to the file ENV_REPORT names. Runs once."""
    global _written
    if not ACTIVE or _written:
        return
    _written = True
    with open(_REPORT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"phases": _phases, "info": dict(_info, probes=_probes)}, fh)


if ACTIVE:
    # A run that ends another way (an error, a headless action) still reports
    # what it measured up to there.
    atexit.register(write_report)


def stop_after_first_draw(root) -> None:
    """Draw the window once, report and exit without entering the main loop."""
    with phase("first draw"):
        root.update()
    write_report()
    sys.stdout.flush()
    sys.stderr.flush()
    root.destroy()
    os._exit(0)


def stop(reason: str) -> None:
    """End a measured run before a screen opens, saying why."""
    if ACTIVE:
        _info["stopped"] = reason
        write_report()
        sys.exit(0)


# --- the first process: start the runs and print the table -------------------

def benchmark(runs: int, argv: Optional[List[str]] = None, timeout: float = 180.0,
              out=None) -> int:
    """Start the installer ``runs`` times as new processes and print their phases.

    ``argv`` is the command to start; by default the one this installer was
    started with, so a wrapper's own arguments such as ``--root`` stay.
    """
    from . import __version__, upgrade  # noqa: PLC0415 — this module imports no kit module at load

    out = out or sys.stdout
    cmd = list(argv) if argv is not None else upgrade.restart_argv()
    load = _load()
    reports = []
    with tempfile.TemporaryDirectory(prefix="startup-time-") as tmp:
        for n in range(1, max(1, runs) + 1):
            path = os.path.join(tmp, f"run{n}.json")
            env = dict(os.environ)
            env[ENV_REPORT] = path
            env[ENV_T0] = repr(time.time())
            try:
                code = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                                      timeout=timeout).returncode
            except subprocess.TimeoutExpired:
                print(f"run {n}: no window after {timeout:.0f} s, stopped it.", file=sys.stderr)
                return 1
            try:
                with open(path, encoding="utf-8") as fh:
                    reports.append(json.load(fh))
            except (OSError, json.JSONDecodeError):
                print(f"run {n} exited with code {code} and wrote no timings.", file=sys.stderr)
                return 1
    header = [f"cli-tools-kit {__version__}, Python {sys.version.split()[0]}, "
              f"{os.cpu_count()} CPUs, load {load} before the first run"]
    out.write(format_report(reports, header) + "\n")
    return 0


def _load() -> str:
    try:
        return "%.1f" % os.getloadavg()[0]
    except (AttributeError, OSError):   # Windows has no load average
        return "n/a"


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.2f}"


def format_report(reports: List[dict], header: Optional[List[str]] = None) -> str:
    """The table ``--startup-time`` prints, from the reports of one or more runs."""
    info = reports[0].get("info", {})
    lines = [f"Startup of {info.get('title') or 'the installer'}: "
             f"{len(reports)} run(s), each a new process."]
    lines.extend(header or [])
    if "tools" in info:
        lines.append(f"{info['tools']} rows in the window.")
    if info.get("stopped"):
        lines.append(f"Stopped before a window: {info['stopped']}")
    lines.append("")

    # One row per phase key, in the order of the first run that has it.
    order: List[str] = []
    rows: Dict[str, dict] = {}
    values: Dict[str, List[Optional[float]]] = {}
    for n, report in enumerate(reports):
        for entry in report.get("phases", []):
            key = entry["key"]
            if key not in rows:
                order.append(key)
                rows[key] = entry
                values[key] = [None] * len(reports)
            if entry.get("total"):
                values[key][n] = entry["seconds"]
            elif entry.get("end") is not None:
                values[key][n] = entry["end"] - entry["start"]
    children: Dict[Optional[str], List[str]] = {None: []}
    for key in order:
        children[key] = []
        parent = key.rsplit(_SEP, 1)[0] if _SEP in key else None
        children[parent if parent in children else None].append(key)

    later_runs = len(reports) > 1
    width = max([len("  " * rows[k]["depth"] + _label(rows[k])) for k in order] + [36]) + 2

    def row(label: str, depth: int, vals: List[Optional[float]]) -> str:
        text = f"{'  ' * depth + label:<{width}}{_fmt(vals[0]):>7}"
        later = [v for v in vals[1:] if v is not None]
        if later:
            span = f"{min(later):.2f}-{max(later):.2f}"
            text += f"{statistics.median(later):>9.2f}{span:>13}"
        elif later_runs:
            text += f"{'-':>9}{'-':>13}"
        return text

    head = f"{'phase (seconds)':<{width}}{'run 1':>7}"
    if later_runs:
        head += f"{'median':>9}{'min-max':>13}  of runs 2-{len(reports)}"
    lines.append(head)

    def emit(key: str) -> None:
        entry = rows[key]
        lines.append(row(_label(entry), entry["depth"], values[key]))
        kids = children[key]
        for kid in kids:
            emit(kid)
        if kids:
            # The time inside this phase that none of its parts accounts for.
            other: List[Optional[float]] = []
            for n in range(len(reports)):
                parts = [values[k][n] for k in kids]
                whole = values[key][n]
                other.append(None if whole is None or None in parts
                             else whole - sum(p for p in parts if p is not None))
            if any(v is not None and v >= 0.05 for v in other):
                lines.append(row("other", entry["depth"] + 1, other))

    for key in children[None]:
        emit(key)

    ends = []
    for report in reports:
        times = [e["end"] for e in report.get("phases", []) if e.get("end") is not None]
        ends.append(max(times) if times else None)
    lines.append(row("total, start to first drawn window" if not info.get("stopped")
                     else "total, start to stop", 0, ends))
    lines.extend(_probe_lines(reports))
    return "\n".join(lines)


def _label(entry: dict) -> str:
    if entry.get("total"):
        return f"{entry['label']} ({entry['calls']}x, summed)"
    return entry["label"]


def _probe_lines(reports: List[dict]) -> List[str]:
    """The --advertise probes behind the discovery row: count, parallelism, slowest."""
    first = reports[0].get("info", {})
    probes = first.get("probes") or []
    if not probes:
        return []
    summed = sum(seconds for _, seconds in probes)
    lines = ["", f"--advertise, run 1: {len(probes)} probes, {first.get('probe_workers', '?')} "
                 f"at a time, {summed:.2f} s summed over all probes."]
    by_tool: Dict[str, List[float]] = {}
    for report in reports:
        for name, seconds in report.get("info", {}).get("probes") or []:
            by_tool.setdefault(name, []).append(seconds)
    slowest = sorted(by_tool.items(), key=lambda kv: -statistics.median(kv[1]))[:5]
    lines.append("Slowest probes (median over all runs): "
                 + ", ".join(f"{name} {statistics.median(s):.2f}" for name, s in slowest))
    return lines
