"""MCP servers and skills per agent harness: the JSONC edit, the two built-in
MCP targets, the opencode skill target, and the screens' MCP rules.

No real harness runs: conftest's no_real_harnesses moves opencode's config to
a temp dir and replaces `claude` with a fake that edits a temp .claude.json.
"""

from __future__ import annotations

import json
import os
import queue
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import pytest

from cli_tools_kit import discovery, jsonc, mcp_servers
from cli_tools_kit import gui_installer as gi
from cli_tools_kit import tui_installer as tui
from cli_tools_kit.gui_installer import ToolEntry
from cli_tools_kit.testing import validate_advertise

ARGV = ["/venv/bin/python", "/repo/mcp_server/server.py"]


def _tool(name: str = "esc", mcp: str = "esc", argv=tuple(ARGV), skill: str = "") -> ToolEntry:
    return ToolEntry(
        name=name, desktop_file=f"{name}.desktop", script_path=f"/x/{name}/main.py", args=[],
        icon="system-run", description="", terminal=False, category="", capability="cli",
        tags=["CLI"], alias=name, skill_name=skill, mcp_name=mcp, mcp_command=tuple(argv))


# --- jsonc ---------------------------------------------------------------------

OPENCODE = """{
  // my settings
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "other": {"type": "local", "command": ["x"]}, /* keep me */
    "esc": {
      "type": "local",
      "command": ["/venv/bin/python", "/repo/mcp_server/server.py"]
    }
  },
}
"""


def test_jsonc_reads_comments_and_trailing_commas() -> None:
    data = jsonc.loads(OPENCODE)
    assert data["mcp"]["esc"]["command"] == ARGV
    assert jsonc.loads('{"url": "http://a//b", "s": "/* no */"}') == {
        "url": "http://a//b", "s": "/* no */"}


@pytest.mark.parametrize("text, expected", [
    ('{"mcp": {"esc": 1, "b": 2}}', {"mcp": {"b": 2}}),
    ('{"mcp": {"a": 1, "esc": [1, {"x": 2}]}}', {"mcp": {"a": 1}}),
    ('{"mcp": {"a": 1, "esc": 2,}}', {"mcp": {"a": 1}}),
    ('{"mcp": {"esc": {"type": "local"}}}', {"mcp": {}}),
])
def test_remove_member_keeps_the_rest_valid(text: str, expected: dict) -> None:
    assert jsonc.loads(jsonc.remove_member(text, ["mcp", "esc"])) == expected


def test_remove_member_keeps_comments_and_drops_whole_lines() -> None:
    out = jsonc.remove_member(OPENCODE, ["mcp", "esc"])
    assert "// my settings" in out and "/* keep me */" in out
    assert '"esc"' not in out and "server.py" not in out
    assert jsonc.loads(out)["mcp"] == {"other": {"type": "local", "command": ["x"]}}
    assert jsonc.remove_member(OPENCODE, ["mcp", "absent"]) is None
    assert jsonc.remove_member('{"x": 1}', ["mcp", "esc"]) is None


# --- opencode MCP target ---------------------------------------------------------

@pytest.fixture
def opencode_dir(no_real_harnesses: Path) -> Path:
    folder = no_real_harnesses / "opencode"
    folder.mkdir()
    return folder


def test_opencode_reads_the_file_opencode_writes(opencode_dir: Path) -> None:
    assert mcp_servers.opencode_config_file() == opencode_dir / "opencode.json"
    (opencode_dir / "opencode.jsonc").write_text(OPENCODE)
    assert mcp_servers.opencode_config_file() == opencode_dir / "opencode.jsonc"
    target = mcp_servers.opencode_target()
    assert target.registered() == {"other": ["x"], "esc": ARGV}
    assert mcp_servers.state(target, "esc", ARGV) == "current"
    assert mcp_servers.state(target, "esc", ["/other"]) == "other"
    assert mcp_servers.state(target, "nope", ARGV) == "absent"


def test_opencode_install_runs_mcp_add_and_uninstall_edits_the_file(
        opencode_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = opencode_dir / "opencode.jsonc"
    config.write_text(OPENCODE.replace('"esc"', '"old"'))
    os.chmod(config, 0o600)
    calls: List[List[str]] = []

    def fake_run(argv, env=None, timeout=60):
        calls.append(argv)
        data = jsonc.loads(config.read_text())
        name, command = argv[4], argv[6:]
        data["mcp"][name] = {"type": "local", "command": command}
        config.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0, stdout="added", stderr="")
    monkeypatch.setattr(mcp_servers, "_opencode_exe", lambda: "/bin/opencode")
    monkeypatch.setattr(mcp_servers, "_run", fake_run)
    target = mcp_servers.opencode_target()

    assert target.install("esc", ARGV) == (True, "added")
    assert calls == [["/bin/opencode", "mcp", "add", "--pure", "esc", "--", *ARGV]]
    assert target.install("esc", ARGV)[0] and len(calls) == 1   # already current

    config.write_text(OPENCODE)
    ok, _ = target.uninstall("esc")
    assert ok and "esc" not in target.registered()
    assert "// my settings" in config.read_text()
    assert config.stat().st_mode & 0o777 == 0o600
    assert target.uninstall("esc") == (True, "esc is not registered")


# --- Claude Code MCP target --------------------------------------------------------

@pytest.fixture
def fake_claude_mcp(no_real_harnesses: Path, monkeypatch: pytest.MonkeyPatch):
    """`claude mcp add-json|remove -s user` against the temp .claude.json."""
    config = no_real_harnesses / "claude.json"
    config.write_text(json.dumps({"numStartups": 3, "mcpServers": {}}))
    calls: List[List[str]] = []

    def fake(args):
        calls.append(args)
        data = json.loads(config.read_text())
        if args[0] == "add-json":
            data["mcpServers"][args[3]] = json.loads(args[4])
        elif args[0] == "remove":
            del data["mcpServers"][args[3]]
        config.write_text(json.dumps(data))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(mcp_servers, "_claude", fake)
    return config, calls


def test_claude_target_adds_replaces_and_removes(fake_claude_mcp) -> None:
    config, calls = fake_claude_mcp
    target = mcp_servers.claude_code_target()
    assert target.install("esc", ARGV)[0]
    assert calls[-1][:4] == ["add-json", "-s", "user", "esc"]
    assert json.loads(calls[-1][4]) == {"type": "stdio", "command": ARGV[0], "args": ARGV[1:]}
    assert target.install("esc", ARGV)[0] and len(calls) == 1
    assert target.install("esc", ["/new/python", "s.py"])[0]
    assert [c[0] for c in calls] == ["add-json", "remove", "add-json"]
    assert target.uninstall("esc")[0]
    assert target.registered() == {}
    assert json.loads(config.read_text())["numStartups"] == 3


def test_apply_adds_new_harnesses_only_when_asked() -> None:
    have: Dict[str, Dict[str, List[str]]] = {"a": {"esc": ARGV}, "b": {}, "c": {"esc": ["/old"]}}

    def target(key: str) -> mcp_servers.McpTarget:
        def install(name, argv):
            have[key][name] = argv
            return True, ""

        def uninstall(name):
            del have[key][name]
            return True, ""
        return mcp_servers.McpTarget(key, key, lambda: True, lambda: dict(have[key]),
                                     install, uninstall)
    targets = [target(k) for k in "abc"]
    quiet = lambda *_a: None  # noqa: E731

    done = mcp_servers.apply("esc", ARGV, True, targets, add_missing=False, log=quiet)
    assert done == {"installed": 1, "removed": 0, "errors": 0}   # only c, which ran /old
    assert "esc" not in have["b"]
    mcp_servers.apply("esc", ARGV, True, targets, log=quiet)
    assert have["b"] == {"esc": ARGV}
    done = mcp_servers.apply("esc", ARGV, False, targets, log=quiet)
    assert done["removed"] == 3 and not any(have.values())


# --- advertise and discovery -------------------------------------------------------

def test_mcp_name_needs_mcp_command() -> None:
    record = {"name": "x", "desktop_file": "x.desktop", "icon": "system-run", "desc": "x",
              "tags": ["CLI"], "alias": "x", "mcp_name": "x", "mcp_command": ARGV}
    assert validate_advertise([record]) == []
    del record["mcp_command"]
    assert validate_advertise([record]) == [
        "entry 0 (x): 'mcp_name' and 'mcp_command' go together"]


def test_discovery_drops_a_server_without_its_command(tmp_path: Path) -> None:
    script = tmp_path / "main.py"
    records = [{"name": "a", "desktop_file": "a.desktop", "alias": "a", "tags": ["CLI"],
                "mcp_name": "a", "mcp_command": ARGV},
               {"name": "b", "desktop_file": "b.desktop", "alias": "b", "tags": ["CLI"],
                "mcp_name": "b"}]
    script.write_text(f"import json\nprint(json.dumps({records!r}))\n")
    a, b = discovery.get_metadata_native(str(script), "")
    assert (a.mcp_name, a.mcp_command) == ("a", tuple(ARGV))
    assert (b.mcp_name, b.mcp_command) == ("", ())


# --- the screens -----------------------------------------------------------------

def _fake_target(have: Dict[str, List[str]]) -> mcp_servers.McpTarget:
    def install(name, argv):
        have[name] = argv
        return True, ""
    return mcp_servers.McpTarget("fake", "Fake", lambda: True, lambda: dict(have),
                                 install, lambda name: (have.pop(name, None) is not None, ""))


def test_tui_plans_mcp_steps_for_active_targets_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gi, "is_installed", lambda t: True)
    monkeypatch.setattr(gi, "needs_update", lambda t: False)
    target = _fake_target({})
    row = tui.Row(_tool(), install=True, skill=False, mcp=True)
    assert tui.plan([row], [], set(), [target], set()) == []
    (step,) = tui.plan([row], [], set(), [target], {"fake"})
    assert step.kind == "mcp_install"
    result = tui.execute([step], lambda _m: None)
    assert result["mcp_install"] == 1 and target.registered() == {"esc": ARGV}
    row.mcp = False
    assert [s.kind for s in tui.plan([row], [], set(), [target], {"fake"})] == ["mcp_remove"]


def test_headless_apply_registers_only_with_named_harnesses(monkeypatch) -> None:
    monkeypatch.setattr(gi, "is_installed", lambda t: True)
    monkeypatch.setattr(gi, "needs_update", lambda t: False)
    have: Dict[str, List[str]] = {}
    target = _fake_target(have)
    assert tui.apply_headless([_tool()], "esc", "none", mcp_targets=[target]) == 0
    assert have == {}
    assert tui.apply_headless([_tool()], "esc", "none", mcp_keys="fake",
                              mcp_targets=[target]) == 0
    assert have == {"esc": ARGV}
    assert tui.apply_headless([_tool()], "esc", "none", mcp_keys="nope",
                              mcp_targets=[target]) == 2


def test_gui_apply_spreads_a_server_only_from_a_box_ticked_now(monkeypatch) -> None:
    pytest.importorskip("tkinter")
    from cli_tools_kit import install
    monkeypatch.setattr(install, "is_installed", lambda t: True)
    monkeypatch.setattr(install, "needs_update", lambda t: False)
    have: Dict[str, List[str]] = {}
    tool = _tool()
    key = f"{tool.category}_{tool.name}"
    app = SimpleNamespace(tools=[tool], _extract_hint_from_output=lambda _o: None,
                          _harnesses=([], [_fake_target(have)]))
    run = lambda state, was: gi.InstallerApp._apply_worker(  # noqa: E731
        app, {key: True}, {}, {}, queue.Queue(), mcp_state={key: state}, mcp_was={key: was})

    run(True, True)     # ticked before, by another harness: nothing new here
    assert have == {}
    run(True, False)    # the user ticked it
    assert have == {"esc": ARGV}
    run(False, True)
    assert have == {}


# --- the opencode skill target -----------------------------------------------------

@pytest.fixture
def skill_dirs(tmp_path: Path, no_real_harnesses: Path, monkeypatch: pytest.MonkeyPatch):
    claude = tmp_path / "claude-skills"
    claude.mkdir()
    monkeypatch.setattr(gi, "CLAUDE_SKILLS_DIR", str(claude))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    def write(tool):
        (claude / tool.skill_name).mkdir(exist_ok=True)
        (claude / tool.skill_name / "SKILL.md").write_text("---\nname: s\n---\nbody\n")
        return True, ""

    def remove(tool):
        import shutil
        shutil.rmtree(claude / tool.skill_name, ignore_errors=True)
        return True, ""
    monkeypatch.setattr(gi, "install_skill_for_tool", write)
    monkeypatch.setattr(gi, "uninstall_skill_for_tool", remove)
    return claude, no_real_harnesses / "opencode" / "skills"


def test_opencode_uses_the_claude_copy_when_it_reads_it(skill_dirs, monkeypatch) -> None:
    claude, own = skill_dirs
    tool = _tool(skill="s")
    target = tui.opencode_target()
    assert not target.installed(tool)
    gi.install_skill_for_tool(tool)
    assert target.installed(tool)
    assert target.install(tool) == (True, f"opencode reads {claude / 's'}")
    assert not own.exists()
    monkeypatch.setenv("OPENCODE_DISABLE_CLAUDE_CODE_SKILLS", "1")
    assert not target.installed(tool)


def test_opencode_gets_its_own_copy_without_the_claude_one(skill_dirs) -> None:
    claude, own = skill_dirs
    tool = _tool(skill="s")
    target = tui.opencode_target()
    ok, out = target.install(tool)
    assert ok, out
    assert (own / "s" / "SKILL.md").is_file()
    assert not (claude / "s").exists()      # the Claude Code target stays as it was
    assert target.installed(tool) and not target.stale(tool)
    assert target.uninstall(tool)[0] and not (own / "s").exists()


def test_gui_apply_writes_a_skill_to_each_harness_only_from_a_box_ticked_now(monkeypatch):
    pytest.importorskip("tkinter")
    from cli_tools_kit import install
    monkeypatch.setattr(install, "is_installed", lambda t: True)
    monkeypatch.setattr(install, "needs_update", lambda t: False)
    have = {"a": {"s"}, "b": set()}

    def target(key):
        return tui.SkillTarget(key, key, installed=lambda t: t.skill_name in have[key],
                               install=lambda t: (have[key].add(t.skill_name), (True, ""))[1],
                               uninstall=lambda t: (have[key].discard(t.skill_name), (True, ""))[1])
    tool = _tool(mcp="", argv=(), skill="s")
    key = f"{tool.category}_{tool.name}"
    app = SimpleNamespace(tools=[tool], _extract_hint_from_output=lambda _o: None,
                          _harnesses=([target("a"), target("b")], []))
    run = lambda state, was: gi.InstallerApp._apply_worker(  # noqa: E731
        app, {key: True}, {}, {key: state}, queue.Queue(), skill_was={key: was})

    run(True, True)
    assert have == {"a": {"s"}, "b": set()}
    run(True, False)
    assert have == {"a": {"s"}, "b": {"s"}}
    run(False, True)
    assert have == {"a": set(), "b": set()}


def test_window_builds_the_mcp_cell_and_apply_leaves_an_uninstalled_row_alone(
        sandbox_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real window: an MCP row registered elsewhere while its tool is not installed."""
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"no display: {exc}")
    errors: list = []
    root.report_callback_exception = lambda *exc: errors.append(exc)
    from cli_tools_kit import state
    have: Dict[str, List[str]] = {"esc": ARGV}
    monkeypatch.setattr(state, "MCP_TARGETS", [_fake_target(have)])
    script = tmp_path / "esc" / "main.py"
    script.parent.mkdir()
    script.write_text("", encoding="utf-8")
    tool = _tool()._replace(script_path=str(script), category="Smoke")
    plain = _tool(name="plain", mcp="", argv=())._replace(script_path=str(script),
                                                          category="Smoke")
    try:
        app = gi.InstallerApp(root, [tool, plain])
        root.update()
        key = f"{tool.category}_{tool.name}"
        assert errors == []
        assert ("mcp", 55) in gi.RIGHT_COLS
        assert set(app.mcp_vars) == {key}
        assert app.mcp_vars[key].get() and app.mcp_was[key]
        assert not app.check_vars[key].get()          # Install unticked, MCP still ticked
        check = {k: v.get() for k, v in app.check_vars.items()}
        check["Smoke_plain"] = True                   # an Apply for another row
        monkeypatch.setattr(gi, "install_tool", lambda t, skip_deps=False: (True, ""))
        gi.InstallerApp._apply_worker(app, check, {}, {}, queue.Queue(),
                                      mcp_state={key: True}, mcp_was=dict(app.mcp_was))
        assert have == {"esc": ARGV}
    finally:
        root.destroy()
