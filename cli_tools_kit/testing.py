"""Check a tool's ``--advertise`` answer against PROTOCOL.md, in the tool's own tests.

    from cli_tools_kit.testing import assert_advertises

    def test_advertise():
        assert_advertises("main.py")

A tool that breaks the protocol does not crash anything: the parent installer
skips the entry or the whole script, and the tool silently disappears from the
list. This turns that into a failing test in the tool's repo.

Fields the table below does not know are allowed, because the schema only
grows (README § Stability).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import List, Optional

from . import host

# field -> (type, required). Mirrors PROTOCOL.md § Metadata schema.
FIELDS = {
    "name": (str, True),
    "desktop_file": (str, True),
    "icon": (str, True),
    "desc": (str, True),
    "terminal": (bool, False),
    "args": (list, False),
    "tags": (list, False),
    "alias": (str, False),
    "categories": (str, False),
    "skill_name": (str, False),
    "skill_status": (str, False),
    "alias_args": (list, False),
    "capability": (str, False),
    "domain": (str, False),
    "category": (str, False),
    "default_autostart": (bool, False),
    "cron_schedule": (str, False),
    "cron_args": (list, False),
    "autostart_conditions": (list, False),
}
TAGS = {"GUI", "CLI", "Icon"}
SKILL_STATUSES = {"absent", "current", "stale"}
AUTOSTART_CONDITIONS = {"time_window", "network"}
PROBE_TIMEOUT = 5.0   # what the parent installer allows


def validate_advertise(data) -> List[str]:
    """Every way ``data`` (the parsed JSON) breaks the protocol; empty if none."""
    if isinstance(data, dict):
        data = [data]   # parents tolerate a single object
    if not isinstance(data, list) or not data:
        return ["the answer must be a JSON list of one or more objects"]
    errors: List[str] = []
    for i, entry in enumerate(data):
        where = f"entry {i}"
        if not isinstance(entry, dict):
            errors.append(f"{where}: not an object")
            continue
        where = f"entry {i} ({entry.get('name', '?')})"
        for field, (kind, required) in FIELDS.items():
            if field not in entry:
                if required:
                    errors.append(f"{where}: missing required field '{field}'")
                continue
            value = entry[field]
            if not isinstance(value, kind):
                errors.append(f"{where}: '{field}' must be {kind.__name__}, "
                              f"got {type(value).__name__}")
            elif kind is list and not all(isinstance(v, str) for v in value):
                errors.append(f"{where}: '{field}' must be a list of strings")
            elif kind is str and required and not value.strip():
                errors.append(f"{where}: '{field}' is empty")
        tags = entry.get("tags", ["GUI", "Icon"])
        if isinstance(tags, list):
            unknown = sorted(set(tags) - TAGS)
            if unknown:
                errors.append(f"{where}: unknown tags {unknown}; allowed {sorted(TAGS)}")
            if "Icon" not in tags and not entry.get("alias"):
                errors.append(f"{where}: 'alias' is required when 'Icon' is not in tags")
        status = entry.get("skill_status")
        if isinstance(status, str) and status and status not in SKILL_STATUSES:
            errors.append(f"{where}: skill_status must be one of {sorted(SKILL_STATUSES)}")
        conditions = entry.get("autostart_conditions")
        if isinstance(conditions, list):
            unknown = sorted(set(conditions) - AUTOSTART_CONDITIONS)
            if unknown:
                errors.append(f"{where}: unknown autostart_conditions {unknown}")
    return errors


def advertise_errors(script: str, python: Optional[str] = None,
                     timeout: float = PROBE_TIMEOUT) -> List[str]:
    """Run ``python script --advertise`` the way a parent does; list what is wrong."""
    script = os.path.abspath(script)
    try:
        result = subprocess.run(
            [python or sys.executable, script, "--advertise"],
            cwd=os.path.dirname(script), capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout, env=host.child_env(),
        )
    except subprocess.TimeoutExpired:
        return [f"--advertise took longer than {timeout:g}s; the parent gives up "
                "at 5s, so answer before any heavy import"]
    if result.returncode != 0:
        return [f"--advertise exited {result.returncode}: {result.stderr.strip()[-500:]}"]
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return [f"--advertise printed no valid JSON ({exc}); stdout must hold only "
                f"the JSON: {result.stdout.strip()[:200]!r}"]
    return validate_advertise(data)


def assert_advertises(script: str, python: Optional[str] = None,
                      timeout: float = PROBE_TIMEOUT) -> None:
    """Fail with every protocol problem of ``script``'s ``--advertise``."""
    errors = advertise_errors(script, python=python, timeout=timeout)
    if errors:
        raise AssertionError(f"{script} --advertise:\n  " + "\n  ".join(errors))
