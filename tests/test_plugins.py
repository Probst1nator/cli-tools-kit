"""Tests for plugins — Claude Code plugins as tools, against a fake `claude`.

The fake keeps its plugins in <config dir>/plugins/installed_plugins.json, the
file whose mtime and size the kit's cache watches, and logs every call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cli_tools_kit import discovery, install, plugins, tui_installer
from cli_tools_kit.discovery import ToolEntry
from cli_tools_kit.tool_installer import ToolMetadata

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake claude is a POSIX script")

FAKE_CLAUDE = r'''#!PYTHON
import json, os, sys
cfg = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.environ["HOME"], ".claude")
path = os.path.join(cfg, "plugins", "installed_plugins.json")
plugins = json.load(open(path)) if os.path.exists(path) else []
args = sys.argv[2:]
with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
    log.write(json.dumps({"cfg": cfg, "args": args, "cwd": os.getcwd()}) + "\n")
cmd, rest = args[0], args[1:]
def find(pid):
    return next((p for p in plugins if p["id"] == pid and p["scope"] == "user"), None)
if cmd == "list":
    print(json.dumps(plugins))
    sys.exit(0)
if cmd == "install":
    pid = rest[0] + "@" + rest[rest.index("--marketplace") + 1].rstrip("/").split("/")[-1]
    entry = find(pid)
    if entry:
        entry["enabled"] = True
    else:
        plugins.append({"id": pid, "scope": "user", "enabled": True})
elif cmd == "disable":
    find(rest[0])["enabled"] = False
elif cmd == "uninstall":
    plugins.remove(find(rest[0]))
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(plugins, open(path, "w"))
print(json.dumps({"command": cmd, "message": cmd + " ok"}))
'''


@pytest.fixture
def fake_claude(sandbox_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE.replace("PYTHON", sys.executable, 1))
    exe.chmod(0o755)
    log = tmp_path / "claude.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("TOOLS_INSTALLER_SKIP_DEPS", raising=False)
    monkeypatch.setattr(plugins, "_cache", {})

    def seed(entries, config_dir=sandbox_home / ".claude"):
        target = Path(config_dir) / "plugins" / "installed_plugins.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(entries))

    def calls():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    return seed, calls


def _row(plugin_id: str, config_dir: str = "") -> ToolEntry:
    return ToolEntry(
        name=plugin_id.split("@")[1], desktop_file=f"{plugin_id.split('@')[1]}.desktop",
        script_path="/x/clawd/main.py", args=[], icon="", description="Clawd.",
        terminal=False, category="", capability="claude-mod", tags=["Plugin"],
        claude_plugin=plugin_id, claude_marketplace="owner/" + plugin_id.split("@")[1],
        claude_config_dir=config_dir,
    )


def test_install_enables_the_plugin_and_disables_its_namesakes(fake_claude, capsys):
    seed, calls = fake_claude
    seed([{"id": "clawd@prob-tools", "scope": "user", "enabled": True},
          {"id": "clawd@other", "scope": "project", "enabled": True}])
    assert install.is_installed(_row("clawd@prob-tools"))

    assert plugins.main("clawd@clawd-matsci", "AutomatedAlchemy/clawd-matsci", ["--install"]) == 0

    assert install.is_installed(_row("clawd@clawd-matsci"))
    assert not install.is_installed(_row("clawd@prob-tools"))
    assert "Disabled clawd@prob-tools" in capsys.readouterr().out
    # A project-scope entry belongs to that project and is left alone.
    assert not any(c["args"][:2] == ["disable", "clawd@other"] for c in calls())


def test_a_disabled_plugin_is_not_installed_and_install_enables_it(fake_claude):
    seed, _ = fake_claude
    seed([{"id": "clawd@clawd", "scope": "user", "enabled": False}])
    assert not install.is_installed(_row("clawd@clawd"))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--install"]) == 0
    assert install.is_installed(_row("clawd@clawd"))


def test_config_dir_targets_that_dir_and_the_default_drops_an_inherited_one(
        fake_claude, sandbox_home, tmp_path, monkeypatch):
    _, calls = fake_claude
    fau = tmp_path / "claude-fau"
    # Not set up yet: nothing is installed, claude is not started, install refuses.
    assert not install.is_installed(_row("clawd@clawd", str(fau)))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd",
                        ["--install", plugins.CONFIG_DIR_ARG, str(fau)]) == 1
    assert calls() == [] and not fau.exists()
    fau.mkdir()
    (fau / "settings.json").write_text("{}")
    assert plugins.main("clawd@clawd", "Probst1nator/clawd",
                        ["--install", plugins.CONFIG_DIR_ARG, str(fau)]) == 0
    assert install.is_installed(_row("clawd@clawd", str(fau)))
    assert not install.is_installed(_row("clawd@clawd"))

    # Started from a shell that set CLAUDE_CONFIG_DIR, the default row still
    # writes to ~/.claude.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(fau))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--install"]) == 0
    assert install.is_installed(_row("clawd@clawd"))
    assert calls()[-1]["cfg"] == str(sandbox_home / ".claude")
    # In ~, ~/.claude/settings.json would be read as project settings too.
    assert all(Path(c["cwd"]) != sandbox_home for c in calls())


def test_skip_deps_never_installs(fake_claude, monkeypatch, capsys):
    _, calls = fake_claude
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--install"]) == 0
    assert "Skipped" in capsys.readouterr().out
    assert not any(c["args"][0] == "install" for c in calls())


def test_remove_uninstalls_and_needs_update_stays_false(fake_claude):
    seed, _ = fake_claude
    seed([{"id": "clawd@clawd", "scope": "user", "enabled": True}])
    assert not install.needs_update(_row("clawd@clawd"))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--remove"]) == 0
    assert not install.is_installed(_row("clawd@clawd"))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--remove"]) == 0


def test_the_listing_is_cached_until_claude_writes_its_files(fake_claude):
    seed, calls = fake_claude
    seed([{"id": "clawd@clawd", "scope": "user", "enabled": True}])
    for _ in range(5):
        assert install.is_installed(_row("clawd@clawd"))
    assert len(calls()) == 1


@pytest.mark.usefixtures("sandbox_home")
def test_without_claude_nothing_is_installed_and_install_says_why(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(plugins, "_cache", {})
    assert not install.is_installed(_row("clawd@clawd"))
    assert plugins.main("clawd@clawd", "Probst1nator/clawd", ["--install"]) == 1
    assert "not on the PATH" in capsys.readouterr().err


def test_targets_give_one_row_per_config_dir():
    plain = ToolEntry(name="x", desktop_file="x.desktop", script_path="/x/x/main.py",
                      args=[], icon="", description="", terminal=True, category="",
                      tags=["CLI"], alias="x")
    targets = [plugins.default_target(),
               plugins.PluginTarget("fauclaude", "fauclaude (~/.claude-fau)", "~/.claude-fau")]
    rows = discovery.expand_plugin_targets([plain, _row("clawd@clawd")], targets)
    assert [r.name for r in rows] == ["x", "clawd", "clawd (fauclaude)"]
    fau = rows[2]
    assert fau.desktop_file == "clawd-fauclaude.desktop"
    assert fau.args == [plugins.CONFIG_DIR_ARG, "~/.claude-fau"]
    assert fau.claude_config_dir == "~/.claude-fau"
    assert discovery.expand_plugin_targets([_row("clawd@clawd")], None)[0].args == []


def test_plugin_rows_are_never_preselected_nor_matched_by_their_directory(monkeypatch):
    rows_in = [_row("clawd@prob-tools"), _row("clawd@clawd-matsci")]
    monkeypatch.setattr(tui_installer.gi, "is_installed", lambda _tool: False)
    assert [r.install for r in tui_installer.default_rows(rows_in)] == [False, False]
    assert not tui_installer._matches(rows_in[0], "clawd")
    assert tui_installer._matches(rows_in[1], "clawd-matsci")

    applied = []
    monkeypatch.setattr(tui_installer, "execute", lambda steps, log: applied.extend(steps) or {})
    monkeypatch.setattr(tui_installer.gi, "needs_update", lambda _tool: False)
    assert tui_installer.apply_headless(rows_in, "all") == 0
    assert applied == []
    assert tui_installer.apply_headless(rows_in, "clawd-matsci") == 0
    assert [s.tool.claude_plugin for s in applied] == ["clawd@clawd-matsci"]


def test_advertise_and_discovery_carry_the_plugin(tmp_path):
    meta = ToolMetadata(name="clawd-matsci", desktop_file="clawd-matsci.desktop", icon="",
                        desc="Clawd.", capability="claude-mod",
                        claude_plugin="clawd@clawd-matsci",
                        claude_marketplace="AutomatedAlchemy/clawd-matsci")
    script = tmp_path / "main.py"
    script.write_text(
        "import sys\n"
        "from cli_tools_kit import ToolMetadata, advertise\n"
        f"advertise(ToolMetadata(**{meta.__dict__!r}))\n")
    (entry,) = discovery.get_metadata_native(str(script), "")
    assert entry.tags == ["Plugin"]
    assert entry.alias == ""
    assert entry.claude_plugin == "clawd@clawd-matsci"
    assert entry.claude_marketplace == "AutomatedAlchemy/clawd-matsci"


def test_gui_apply_lets_the_newly_ticked_plugin_win(fake_claude, monkeypatch):
    """Both rows ticked, one installed: the other installs and stays the one enabled."""
    pytest.importorskip("tkinter")
    import queue
    from types import SimpleNamespace
    from cli_tools_kit import gui_installer as gi

    seed, _ = fake_claude
    seed([{"id": "clawd@clawd-matsci", "scope": "user", "enabled": True}])
    rows = [_row("clawd@prob-tools"), _row("clawd@clawd-matsci")]
    monkeypatch.setattr(install, "install_tool", lambda tool, skip_deps=False: (
        plugins.install(tool.claude_plugin, tool.claude_marketplace, tool.claude_config_dir,
                        log=lambda _msg: None), ""))
    app = SimpleNamespace(tools=rows, _extract_hint_from_output=lambda _out: None)
    ticked = {f"{r.category}_{r.name}": True for r in rows}
    gi.InstallerApp._apply_worker(app, ticked, {}, {}, queue.Queue())

    assert install.is_installed(rows[0])
    assert not install.is_installed(rows[1])


def test_the_login_check_never_starts_claude(engine, fake_claude, monkeypatch):
    seed, _ = fake_claude
    seed([{"id": "clawd@clawd", "scope": "user", "enabled": True}])
    monkeypatch.setattr(engine, "_notify_send", lambda *a, **k: None)
    monkeypatch.setattr(discovery, "discover_tools", lambda run_pre=True: [_row("clawd@clawd")])

    def no_claude(*_a, **_k):
        raise AssertionError("the login check started claude")
    monkeypatch.setattr(plugins, "_claude", no_claude)
    assert engine.cli_check() == 0
