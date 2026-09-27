"""cli_tools_kit.testing: the protocol check tools run in their own tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_tools_kit.testing import advertise_errors, assert_advertises, validate_advertise

EXAMPLE_TOOL = (Path(__file__).resolve().parent.parent
                / "examples" / "org-installer" / "greeter" / "main.py")

GOOD = {"name": "Greeter", "desktop_file": "greeter.desktop", "icon": "x",
        "desc": "Say hello", "tags": ["CLI"], "alias": "greeter"}


def test_the_example_tool_advertises_correctly() -> None:
    assert_advertises(str(EXAMPLE_TOOL))


def test_a_valid_entry_has_no_errors() -> None:
    assert validate_advertise([GOOD]) == []
    assert validate_advertise(GOOD) == []   # a single object is tolerated
    assert validate_advertise([{**GOOD, "future_field": 1}]) == []


@pytest.mark.parametrize("entry, needle", [
    ({k: v for k, v in GOOD.items() if k != "desc"}, "missing required field 'desc'"),
    ({**GOOD, "alias": ""}, "'alias' is required"),
    ({**GOOD, "tags": ["CLI", "Tray"]}, "unknown tags ['Tray']"),
    ({**GOOD, "terminal": "yes"}, "'terminal' must be bool"),
    ({**GOOD, "args": ["--a", 3]}, "'args' must be a list of strings"),
    ({**GOOD, "skill_status": "fresh"}, "skill_status must be one of"),
    ({**GOOD, "autostart_conditions": ["battery"]}, "unknown autostart_conditions"),
])
def test_each_protocol_break_is_named(entry: dict, needle: str) -> None:
    errors = validate_advertise([entry])
    assert any(needle in e for e in errors), errors


def test_an_empty_list_is_an_error() -> None:
    assert validate_advertise([]) != []


def test_a_slow_probe_is_reported(tmp_path: Path) -> None:
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    errors = advertise_errors(str(script), timeout=0.5)
    assert errors and "longer than 0.5s" in errors[0]


def test_output_besides_the_json_is_reported(tmp_path: Path) -> None:
    script = tmp_path / "chatty.py"
    script.write_text("print('loading...')\nprint('[]')\n", encoding="utf-8")
    errors = advertise_errors(str(script))
    assert errors and "no valid JSON" in errors[0]
