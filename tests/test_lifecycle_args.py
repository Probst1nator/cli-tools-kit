"""A tool's launch args must never reach its lifecycle commands.

studon-client launches with ``--gui``. The engine used to run
``main.py --uninstall-skill --gui``; the tool handled ``--gui`` first, opened
its window and exited 0, so the installer reported "removed" while the skill
file stayed. A tool that opens a window, starts a server or a REPL for those
args behaves the same way. These tests use a tool whose launch arg wins over
every other flag, as the real one did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_tools_kit import plugins

_TRAP = '''\
import json, os, sys
if "--gui" in sys.argv:
    print("window opened")          # what the real tool did, and it exited 0
    sys.exit(0)
with open(os.environ["TRAP_LOG"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
'''


@pytest.fixture
def trap(engine, tmp_path: Path, monkeypatch):
    tool = {t.name.lower(): t for t in engine.discover_tools(run_pre=False)}["skilltool"]
    script = tmp_path / "trap.py"
    script.write_text(_TRAP, encoding="utf-8")
    log = tmp_path / "argv.json"
    monkeypatch.setenv("TRAP_LOG", str(log))
    entry = tool._replace(script_path=str(script), args=["--gui"])

    def ran() -> list:
        return json.loads(log.read_text(encoding="utf-8")) if log.exists() else []

    return engine, entry, ran


@pytest.mark.parametrize("function, flag", [
    ("install_tool", "--install"),
    ("remove_tool", "--remove"),
    ("install_skill_for_tool", "--install-skill"),
    ("uninstall_skill_for_tool", "--uninstall-skill"),
])
def test_launch_args_do_not_follow_a_lifecycle_flag(trap, function: str, flag: str) -> None:
    engine, tool, ran = trap
    fn = getattr(engine, function)
    ok, output = fn(tool, skip_deps=True) if function == "install_tool" else fn(tool)
    assert ok, output
    assert "window opened" not in output
    assert ran() == [flag]


@pytest.mark.parametrize("function, flag", [
    ("install_skill_for_tool", "--install-skill"),
    ("uninstall_skill_for_tool", "--uninstall-skill"),
])
def test_the_config_dir_of_a_plugin_row_is_still_passed(trap, function: str, flag: str) -> None:
    engine, tool, ran = trap
    tool = tool._replace(args=["--gui", plugins.CONFIG_DIR_ARG, "/x/cfg"])
    assert getattr(engine, function)(tool)[0]
    assert ran() == [flag, plugins.CONFIG_DIR_ARG, "/x/cfg"]
