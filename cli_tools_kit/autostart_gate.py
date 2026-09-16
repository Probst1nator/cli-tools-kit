"""Conditional autostart — run a tool at login only when a condition holds.

An autostart entry is all-or-nothing: the `.desktop` symlink in
`~/.config/autostart` either launches the tool at every login or never. Some
tools only make sense some of the time — a study launcher during study hours,
a voice assistant on the home network and nowhere else.

This module is the generic half of that. A tool declares WHICH conditions it
supports (`autostart_conditions` in its metadata); the user fills in the VALUES
through the installer's ⚙ dialog, which stores them per host in the installer's
own config directory. Nothing personal — no hours, no network names — belongs
in a tool's source.

The gate runs as a wrapper. When a tool has conditions configured, the
installer writes the autostart `.desktop` with an Exec line of

    cli-tools-kit-autostart-gate --slug <installer> --tool <key> -- <real command>

The gate evaluates the stored conditions, execs the real command when they all
pass, and exits 0 silently when any fails. Exit 0 matters: a non-zero exit from
an XDG autostart entry makes systemd log a failed unit at every login.

Two conditions ship here:

`time_window`
    Wall-clock window, `{"from": "06:00", "to": "12:00"}`. A window whose end
    is not after its start wraps midnight, so `22:00`–`02:00` is four hours
    around midnight rather than an empty set.

`network`
    Wi-Fi SSID, `{"ssids": [...], "grace_seconds": 120}`. At login the wireless
    link is usually still associating, so a bare check would lose the race and
    the tool would never start. The gate polls until one of the named SSIDs
    appears or the grace period runs out, and fires at most once per run.

Both are evaluated at launch only. Neither watches for later changes: leaving
the network does not stop a tool the gate already started.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, time as dtime
from typing import Dict, List, Optional, Sequence

from .identity import InstallerIdentity, LEGACY_IDENTITY

# Conditions a tool may name in `autostart_conditions`. A tool advertising a
# word outside this set is ignored rather than rejected, so a tool written for
# a newer kit still installs against an older one.
KNOWN_CONDITIONS = ("time_window", "network")

# Poll interval while waiting for the wireless link to associate.
_NETWORK_POLL_SECONDS = 3.0

# Cap on `grace_seconds`, so a typo cannot leave a gate process resident for
# the rest of the session.
_MAX_GRACE_SECONDS = 900


def identity_for_slug(slug: str) -> InstallerIdentity:
    """The identity a slug names.

    A slug alone cannot reconstruct an identity that overrides its directories,
    so the legacy one is matched by name: its config lives in
    ``~/.config/tools-installer``, not in ``~/.config/probable.work``, and the
    gate has to read the same file the installer wrote.
    """
    if slug == LEGACY_IDENTITY.slug:
        return LEGACY_IDENTITY
    return InstallerIdentity(slug=slug)


def config_path(slug: str) -> str:
    """Where one installer's autostart conditions live.

    Beside that installer's other config, keyed by slug so several installers
    on one host keep separate files.
    """
    return os.path.join(identity_for_slug(slug).config_path, "autostart.json")


def load_conditions(slug: str) -> Dict[str, dict]:
    """Read every tool's configured conditions for one installer.

    Returns `{}` when the file is missing or unreadable — an absent config
    means "no conditions", which gates open rather than shut.
    """
    try:
        with open(config_path(slug), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_tool_conditions(slug: str, tool_key: str) -> dict:
    """The configured conditions for one tool, or `{}` if it has none."""
    entry = load_conditions(slug).get(tool_key)
    return entry if isinstance(entry, dict) else {}


def save_tool_conditions(slug: str, tool_key: str, conditions: Optional[dict]) -> None:
    """Store (or, with *conditions* empty/None, drop) one tool's conditions."""
    data = load_conditions(slug)
    if conditions:
        data[tool_key] = conditions
    else:
        data.pop(tool_key, None)

    path = config_path(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------- conditions

def _parse_hhmm(value: str) -> Optional[dtime]:
    """Parse `"HH:MM"`. Returns None on anything unparseable."""
    try:
        hh, _, mm = str(value).partition(":")
        return dtime(int(hh), int(mm))
    except (TypeError, ValueError):
        return None


def time_window_open(config: dict, now: Optional[datetime] = None) -> bool:
    """True when *now* falls inside the configured window.

    A window whose end is not after its start wraps midnight. An unparseable
    or half-specified window is treated as no restriction.
    """
    start = _parse_hhmm(config.get("from", ""))
    end = _parse_hhmm(config.get("to", ""))
    if start is None or end is None:
        return True

    current = (now or datetime.now()).time()
    if start < end:
        return start <= current < end
    if start == end:
        # A zero-width window would never open; read it as "no restriction"
        # rather than silently disabling the tool forever.
        return True
    # Wraps midnight: inside means after the start OR before the end.
    return current >= start or current < end


def current_ssids() -> List[str]:
    """SSIDs of the currently active Wi-Fi connections.

    Uses NetworkManager, the only mechanism present on the KDE/GNOME hosts this
    kit targets. Returns `[]` when nmcli is missing or reports nothing, which
    is indistinguishable from "not on Wi-Fi" — deliberately, since both mean
    the network condition cannot be satisfied.
    """
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "ACTIVE,SSID", "device", "wifi"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []

    found = []
    for line in result.stdout.splitlines():
        active, _, ssid = line.partition(":")
        if active == "yes" and ssid:
            found.append(ssid)
    return found


def network_matches(config: dict, *, wait: bool = True) -> bool:
    """True when one of the configured SSIDs is active.

    With *wait* set, polls for up to `grace_seconds` so a login that outruns
    the wireless association still sees the network. An empty SSID list is no
    restriction.
    """
    wanted = [s for s in config.get("ssids", []) if s]
    if not wanted:
        return True

    try:
        grace = float(config.get("grace_seconds", 120))
    except (TypeError, ValueError):
        grace = 120.0
    grace = max(0.0, min(grace, _MAX_GRACE_SECONDS)) if wait else 0.0

    deadline = time.monotonic() + grace
    while True:
        if any(ssid in wanted for ssid in current_ssids()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(_NETWORK_POLL_SECONDS, max(0.1, deadline - time.monotonic())))


def conditions_pass(conditions: dict, *, wait: bool = True) -> tuple[bool, str]:
    """Evaluate every configured condition. Returns (passed, reason).

    *reason* names the first condition that failed, for the log line; it is
    empty when everything passed. An unknown condition name is skipped rather
    than failed, so a config written by a newer kit does not shut the gate.
    """
    window = conditions.get("time_window")
    if isinstance(window, dict) and not time_window_open(window):
        return False, (
            f"outside time window {window.get('from', '?')}–{window.get('to', '?')}"
        )

    network = conditions.get("network")
    if isinstance(network, dict) and not network_matches(network, wait=wait):
        wanted = ", ".join(network.get("ssids", [])) or "?"
        return False, f"network not one of [{wanted}]"

    return True, ""


# --------------------------------------------------------------------- entry

def build_exec_prefix(slug: str, tool_key: str) -> List[str]:
    """The argv prefix that wraps a gated tool's own command.

    Prefers the installed `cli-tools-kit-autostart-gate` console script, which
    survives the interpreter that wrote the entry moving or being rebuilt. Only
    when that is not on PATH does it pin the current interpreter, since a
    `python -m` line is the one form guaranteed to resolve the package.
    """
    script = shutil.which("cli-tools-kit-autostart-gate")
    head = [script] if script else [sys.executable, "-m", "cli_tools_kit.autostart_gate"]
    return head + ["--slug", slug, "--tool", tool_key, "--"]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli-tools-kit-autostart-gate",
        description="Run a command only when its configured autostart conditions hold.",
    )
    parser.add_argument("--slug", required=True,
                        help="Installer slug whose autostart.json holds the conditions")
    parser.add_argument("--tool", required=True,
                        help="Tool key within that config")
    parser.add_argument("--no-wait", action="store_true",
                        help="Skip the network grace period; evaluate once and exit")
    parser.add_argument("--check", action="store_true",
                        help="Report whether the gate would open, run nothing")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="-- followed by the command to run when the gate opens")
    args = parser.parse_args(argv)

    conditions = load_tool_conditions(args.slug, args.tool)
    passed, reason = conditions_pass(conditions, wait=not args.no_wait)

    if args.check:
        print("open" if passed else f"closed: {reason}")
        return 0 if passed else 1

    if not passed:
        # Exit 0 on a closed gate: a non-zero exit from an XDG autostart entry
        # surfaces as a failed systemd unit at every login.
        print(f"autostart-gate: {args.tool} not started ({reason})", file=sys.stderr)
        return 0

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("autostart-gate: no command given", file=sys.stderr)
        return 2

    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print(f"autostart-gate: cannot run {command[0]}: {exc}", file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
