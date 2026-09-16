"""Tests for autostart_gate — time-window and network conditions on autostart."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest

from cli_tools_kit import ToolMetadata, advertise
from cli_tools_kit import autostart_gate as gate


# ------------------------------------------------------------- time_window

@pytest.mark.parametrize("hour,expected", [
    (5, False), (6, True), (9, True), (11, True), (12, False), (23, False),
])
def test_time_window_half_open(hour: int, expected: bool) -> None:
    """06:00-12:00 includes its start and excludes its end."""
    window = {"from": "06:00", "to": "12:00"}
    now = datetime(2026, 9, 16, hour, 0)
    assert gate.time_window_open(window, now) is expected


@pytest.mark.parametrize("hour,expected", [
    (21, False), (22, True), (23, True), (0, True), (1, True), (2, False),
])
def test_time_window_wraps_midnight(hour: int, expected: bool) -> None:
    """An end not after the start reads as a window across midnight."""
    window = {"from": "22:00", "to": "02:00"}
    now = datetime(2026, 9, 16, hour, 0)
    assert gate.time_window_open(window, now) is expected


def test_time_window_unparseable_does_not_restrict() -> None:
    """A malformed window must not silently disable a tool forever."""
    assert gate.time_window_open({"from": "oops", "to": "12:00"}) is True
    assert gate.time_window_open({}) is True
    assert gate.time_window_open({"from": "06:00", "to": "06:00"}) is True


# ----------------------------------------------------------------- network

def test_network_matches_active_ssid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "current_ssids", lambda: ["b_wifi"])
    assert gate.network_matches({"ssids": ["b_wifi", "b2_wifi"]}, wait=False) is True


def test_network_rejects_other_ssid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "current_ssids", lambda: ["cafe_guest"])
    assert gate.network_matches({"ssids": ["b_wifi"]}, wait=False) is False


def test_network_empty_list_does_not_restrict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "current_ssids", lambda: [])
    assert gate.network_matches({"ssids": []}, wait=False) is True


def test_network_waits_for_late_association(monkeypatch: pytest.MonkeyPatch) -> None:
    """The link usually associates after login, so the gate polls for it."""
    seen = {"calls": 0}

    def ssids():
        seen["calls"] += 1
        return ["b_wifi"] if seen["calls"] >= 3 else []

    monkeypatch.setattr(gate, "current_ssids", ssids)
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    assert gate.network_matches({"ssids": ["b_wifi"], "grace_seconds": 60}) is True
    assert seen["calls"] >= 3


def test_network_gives_up_after_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "current_ssids", lambda: [])
    monkeypatch.setattr(gate.time, "sleep", lambda _s: None)
    assert gate.network_matches({"ssids": ["b_wifi"], "grace_seconds": 1}) is False


def test_current_ssids_reads_active_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stdout = "no:neighbour_wifi\nyes:b_wifi\nno:other\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: Result())
    assert gate.current_ssids() == ["b_wifi"]


def test_current_ssids_survives_missing_nmcli(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise FileNotFoundError("nmcli")

    monkeypatch.setattr(gate.subprocess, "run", boom)
    assert gate.current_ssids() == []


# -------------------------------------------------------------- evaluation

def test_conditions_pass_with_nothing_configured() -> None:
    assert gate.conditions_pass({}) == (True, "")


def test_conditions_report_the_failing_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "time_window_open", lambda *_a, **_k: False)
    passed, reason = gate.conditions_pass({"time_window": {"from": "06:00", "to": "12:00"}})
    assert passed is False
    assert "06:00" in reason


def test_unknown_condition_is_skipped_not_failed() -> None:
    """Config from a newer kit must not shut the gate on an older one."""
    assert gate.conditions_pass({"phase_of_moon": {"full": True}}) == (True, "")


# ------------------------------------------------------------------ config

def test_legacy_slug_uses_the_installers_own_config_dir(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The legacy identity overrides config_dir, and the gate must follow it.

    Deriving the path from the slug alone would look in ~/.config/probable.work
    while the installer writes ~/.config/tools-installer, so every gated tool
    would read an empty config and start unconditionally.
    """
    from cli_tools_kit.identity import LEGACY_IDENTITY

    monkeypatch.setenv("HOME", str(tmp_path))
    assert gate.config_path(LEGACY_IDENTITY.slug) == os.path.join(
        LEGACY_IDENTITY.config_path, "autostart.json")
    assert "tools-installer" in gate.config_path(LEGACY_IDENTITY.slug)


def test_conditions_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    conditions = {"network": {"ssids": ["b_wifi"], "grace_seconds": 120}}
    gate.save_tool_conditions("acme-tools", "jarvis", conditions)
    assert gate.load_tool_conditions("acme-tools", "jarvis") == conditions


def test_conditions_are_per_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    gate.save_tool_conditions("acme-tools", "jarvis", {"network": {"ssids": ["b_wifi"]}})
    gate.save_tool_conditions("acme-tools", "lernclaude",
                              {"time_window": {"from": "06:00", "to": "12:00"}})
    assert gate.load_tool_conditions("acme-tools", "jarvis") == {
        "network": {"ssids": ["b_wifi"]}}
    assert gate.load_tool_conditions("acme-tools", "lernclaude") == {
        "time_window": {"from": "06:00", "to": "12:00"}}


def test_saving_empty_clears_a_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    gate.save_tool_conditions("acme-tools", "jarvis", {"network": {"ssids": ["b_wifi"]}})
    gate.save_tool_conditions("acme-tools", "jarvis", {})
    assert gate.load_tool_conditions("acme-tools", "jarvis") == {}


def test_missing_config_reads_as_no_conditions(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert gate.load_tool_conditions("acme-tools", "nothing-here") == {}


def test_corrupt_config_gates_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable config must not stop every gated tool from starting."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = Path(gate.config_path("acme-tools"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert gate.load_tool_conditions("acme-tools", "jarvis") == {}


# ------------------------------------------------------------------- entry

def test_closed_gate_exits_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                capsys: pytest.CaptureFixture) -> None:
    """A non-zero exit would surface as a failed systemd unit every login."""
    monkeypatch.setenv("HOME", str(tmp_path))
    gate.save_tool_conditions("acme-tools", "lernclaude",
                              {"time_window": {"from": "06:00", "to": "06:01"}})
    monkeypatch.setattr(gate, "time_window_open", lambda *_a, **_k: False)
    rc = gate.main(["--slug", "acme-tools", "--tool", "lernclaude", "--", "/bin/true"])
    assert rc == 0
    assert "not started" in capsys.readouterr().err


def test_open_gate_execs_the_command(tmp_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    execed = {}

    def fake_exec(binary, argv):
        execed["binary"] = binary
        execed["argv"] = list(argv)
        raise SystemExit(0)

    monkeypatch.setattr(gate.os, "execvp", fake_exec)
    with pytest.raises(SystemExit):
        gate.main(["--slug", "acme-tools", "--tool", "anything",
                   "--", "/usr/bin/python3", "main.py"])
    assert execed["binary"] == "/usr/bin/python3"
    assert execed["argv"] == ["/usr/bin/python3", "main.py"]


def test_check_reports_without_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                       capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    rc = gate.main(["--slug", "acme-tools", "--tool", "unconfigured", "--check"])
    assert rc == 0
    assert "open" in capsys.readouterr().out


# --------------------------------------------------------------- advertise

def test_advertise_emits_conditions(capsys: pytest.CaptureFixture) -> None:
    import json

    with pytest.raises(SystemExit):
        advertise(ToolMetadata(
            name="Jarvis", desktop_file="jarvis.desktop", icon="mic",
            desc="Voice assistant", autostart_conditions=["network"],
        ))
    record = json.loads(capsys.readouterr().out)[0]
    assert record["autostart_conditions"] == ["network"]


def test_advertise_omits_conditions_when_unset(capsys: pytest.CaptureFixture) -> None:
    """A tool that never touched the field emits a pre-0.8.0 record."""
    import json

    with pytest.raises(SystemExit):
        advertise(ToolMetadata(
            name="Plain", desktop_file="plain.desktop", icon="x", desc="y",
        ))
    assert "autostart_conditions" not in json.loads(capsys.readouterr().out)[0]
