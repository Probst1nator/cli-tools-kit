"""Tests for tui_installer — screen choice, initial ticks, and the Apply plan.

The curses drawing itself is not tested; everything below it is plain data.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

from cli_tools_kit import gui_installer as gi
from cli_tools_kit import tui_installer as tui
from cli_tools_kit.gui_installer import ToolEntry


def _tool(name: str, skill: str = "", script: str = "") -> ToolEntry:
    return ToolEntry(
        name=name, desktop_file=f"{name}.desktop", script_path=script or f"/x/{name}/main.py",
        args=[], icon="utilities-terminal", description=f"{name} desc", terminal=True,
        category="", capability="cli", tags=["CLI"], alias=name, skill_name=skill,
    )


def _fake_host(monkeypatch: pytest.MonkeyPatch, installed: List[str], stale: Optional[List[str]] = None):
    stale = stale or []
    monkeypatch.setattr(gi, "is_installed", lambda t: t.name in installed)
    monkeypatch.setattr(gi, "needs_update", lambda t: t.name in stale)


def _target(key: str, have: List[str]) -> tui.SkillTarget:
    return tui.SkillTarget(
        key=key, label=key,
        installed=lambda t: t.skill_name in have,
        install=lambda t: (True, "Run: source ~/.bashrc"),
        uninstall=lambda t: (True, ""),
    )


# --- prefer_tui -------------------------------------------------------------

def test_flags_win_over_environment() -> None:
    assert tui.prefer_tui(force_tui=True, environ={"DISPLAY": ":0"})
    assert not tui.prefer_tui(force_gui=True, environ={})


def test_no_display_means_tui(linux_host) -> None:
    assert tui.prefer_tui(environ={})
    assert not tui.prefer_tui(environ={"DISPLAY": ":0"})
    assert not tui.prefer_tui(environ={"WAYLAND_DISPLAY": "wayland-0"})


def test_missing_tkinter_means_tui_even_with_display(linux_host) -> None:
    assert tui.prefer_tui(have_tk=False, environ={"DISPLAY": ":0"})


# --- default_rows -----------------------------------------------------------

def test_fresh_host_ticks_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=[])
    rows = tui.default_rows([_tool("a", skill="a"), _tool("b")], skill_installed=lambda t: False)
    assert [(r.install, r.skill) for r in rows] == [(True, True), (True, False)]


def test_host_with_installs_mirrors_them(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=["a"])
    rows = tui.default_rows([_tool("a", skill="a"), _tool("b", skill="b")],
                            skill_installed=lambda t: t.skill_name == "a")
    assert [(r.install, r.skill) for r in rows] == [(True, True), (False, False)]


def test_preselect_forces_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=["a"])
    rows = tui.default_rows([_tool("a"), _tool("b")], preselect=True)
    assert all(r.install for r in rows)
    rows = tui.default_rows([_tool("a"), _tool("b")], preselect=False,
                            skill_installed=lambda t: False)
    assert [r.install for r in rows] == [True, False]


# --- plan -------------------------------------------------------------------

def test_plan_installs_updates_and_removes(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=["b", "c"], stale=["b"])
    rows = [tui.Row(_tool("a"), True, False), tui.Row(_tool("b"), True, False),
            tui.Row(_tool("c"), False, False), tui.Row(_tool("d"), False, False)]
    steps = tui.plan(rows, [], set())
    assert [(s.kind, s.tool.name) for s in steps] == [
        ("install", "a"), ("update", "b"), ("remove", "c")]


def test_plan_writes_skills_only_to_active_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=[])
    claude, fau = _target("claude", have=[]), _target("fauclaude", have=[])
    rows = [tui.Row(_tool("a", skill="a"), True, True)]
    steps = tui.plan(rows, [claude, fau], {"fauclaude"})
    assert [(s.kind, s.target.key) for s in steps if s.target] == [("skill_install", "fauclaude")]


def test_plan_skips_present_skill_unless_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=["a"])
    present = _target("claude", have=["a"])
    rows = [tui.Row(_tool("a", skill="a"), True, True)]
    assert tui.plan(rows, [present], {"claude"}) == []
    stale = present._replace(stale=lambda t: True)
    assert [s.kind for s in tui.plan(rows, [stale], {"claude"})] == ["skill_install"]


def test_plan_removes_skill_when_unticked(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=["a"])
    present = _target("claude", have=["a"])
    rows = [tui.Row(_tool("a", skill="a"), True, False)]
    assert [s.kind for s in tui.plan(rows, [present], {"claude"})] == ["skill_remove"]


# --- execute ----------------------------------------------------------------

def test_execute_counts_and_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gi, "install_tool", lambda t, skip_deps=False: (True, "Run: source ~/.bashrc"))
    monkeypatch.setattr(gi, "remove_tool", lambda t: (False, "boom"))
    monkeypatch.setattr(gi, "is_autostart_enabled", lambda t: False)
    monkeypatch.setattr(gi, "refresh_desktop_database", lambda: None)
    target = _target("claude", have=[])
    steps = [tui.Step("install", _tool("a")), tui.Step("remove", _tool("b")),
             tui.Step("skill_install", _tool("a", skill="a"), target)]
    log: List[str] = []
    result = tui.execute(steps, log.append)
    assert result["install"] == 1 and result["skill_install"] == 1 and result["errors"] == 1
    assert result["hint"] == "source ~/.bashrc"
    assert "boom" in " ".join(log)
    assert tui.summary(result) == "1 installed, 1 skills written, 1 errors"


def test_execute_writes_no_skill_for_a_tool_that_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gi, "install_tool",
                        lambda t, skip_deps=False: (t.name != "bad", "" if t.name != "bad" else "boom"))
    monkeypatch.setattr(gi, "refresh_desktop_database", lambda: None)
    written: List[str] = []
    target = tui.SkillTarget(key="claude", label="claude", installed=lambda t: False,
                             install=lambda t: (written.append(t.name), (True, ""))[1],
                             uninstall=lambda t: (True, ""))
    steps = [tui.Step("install", _tool("bad", skill="bad")),
             tui.Step("install", _tool("good", skill="good")),
             tui.Step("skill_install", _tool("bad", skill="bad"), target),
             tui.Step("skill_install", _tool("good", skill="good"), target)]
    log: List[str] = []
    result = tui.execute(steps, log.append)
    assert written == ["good"]
    assert result["errors"] == 1 and result["skill_install"] == 1
    assert "Skill bad skipped: bad did not install" in log


# --- apply_headless ---------------------------------------------------------

def test_apply_headless_installs_named_tools(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _fake_host(monkeypatch, installed=[])
    done: List[str] = []
    monkeypatch.setattr(gi, "install_tool", lambda t, skip_deps=False: (done.append(t.name), (True, ""))[1])
    monkeypatch.setattr(gi, "refresh_desktop_database", lambda: None)
    fau = _target("fauclaude", have=[])
    tools = [_tool("a", skill="a"), _tool("b", skill="b"), _tool("c")]
    rc = tui.apply_headless(tools, "a, c", "fauclaude", targets=[_target("claude", have=[]), fau])
    assert rc == 0 and done == ["a", "c"]
    assert "Skill a -> fauclaude" in capsys.readouterr().out


def test_apply_headless_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_host(monkeypatch, installed=[])
    assert tui.apply_headless([_tool("a")], "zzz") == 2
    assert tui.apply_headless([_tool("a")], "a", "nowhere") == 2


def test_apply_headless_none_skips_skills(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    _fake_host(monkeypatch, installed=[])
    monkeypatch.setattr(gi, "install_tool", lambda t, skip_deps=False: (True, ""))
    monkeypatch.setattr(gi, "refresh_desktop_database", lambda: None)
    assert tui.apply_headless([_tool("a", skill="a")], "all", "none", targets=[_target("claude", have=[])]) == 0
    assert "Skill" not in capsys.readouterr().out
