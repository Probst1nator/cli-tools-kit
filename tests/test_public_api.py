"""The surface consumers use, pinned by name.

From 1.0 on these names follow SemVer: removing or renaming one is a major
release. The list is what the installers and tools in the wild actually call
(WW3 tools-installer, AutomatedAlchemy, tools/, about 55 tool main.py files),
not everything the modules happen to define. A failure here means a consumer
breaks; bump the major version or put the name back.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

import cli_tools_kit
from cli_tools_kit import gui_installer, sources, tui_installer
from cli_tools_kit.taxonomy import corpus, groups


def _params(obj) -> set:
    return set(inspect.signature(obj).parameters)


def test_top_level_exports() -> None:
    assert set(cli_tools_kit.__all__) >= {
        "ToolInstaller", "ToolMetadata", "InstallerIdentity", "LEGACY_IDENTITY",
        "CronInstaller", "advertise", "skill_status", "skill_payload_hash",
        "installed_skill_hash", "read_installed_skill",
    }
    for name in cli_tools_kit.__all__:
        assert hasattr(cli_tools_kit, name), name
    assert isinstance(cli_tools_kit.__version__, str)


def test_tool_side() -> None:
    assert _params(cli_tools_kit.ToolInstaller) >= {"script_path", "metadata", "identity"}
    for method in ("install", "remove", "variants"):
        assert callable(getattr(cli_tools_kit.ToolInstaller, method)), method
    assert _params(cli_tools_kit.ToolMetadata) >= {
        "name", "desktop_file", "icon", "desc", "terminal", "args", "categories",
        "tags", "alias", "alias_args", "capability", "domain", "category",
        "skill_name", "skill_status", "autostart_conditions",
    }
    assert _params(cli_tools_kit.CronInstaller) >= {"marker"}
    for method in ("install", "remove", "is_installed", "installed_lines", "remove_unmarked"):
        assert callable(getattr(cli_tools_kit.CronInstaller, method)), method
    assert _params(cli_tools_kit.advertise) >= {"metadata"}


def test_installer_side() -> None:
    assert _params(gui_installer.run) >= {
        "identity", "root_dir", "entry_script", "window_title", "discovery_roots",
        "discoverer", "prune", "group_by", "pre_discovery", "check_reconcile_shortcuts",
        "skill_targets", "tui_preselect", "autostart_check_desktop_name",
        "check_log_name", "check_state_name", "self_desktop_file", "self_desktop_name",
        "self_desktop_icon", "wm_class", "notify_app", "hooks",
    }
    assert set(gui_installer.InstallHooks._fields) == {
        "install_tool", "remove_tool", "install_skill", "uninstall_skill"}
    # Wrappers from before 1.0 rebind these by assignment; they must stay
    # module attributes that every screen looks up at call time.
    for name in ("install_tool", "remove_tool", "install_skill_for_tool",
                 "uninstall_skill_for_tool", "discover_tools", "cli_check"):
        assert callable(getattr(gui_installer, name)), name
    assert set(gui_installer.ToolEntry._fields) >= {
        "name", "script_path", "args", "alias", "tags", "skill_name",
        "autostart_conditions"}
    assert _params(sources.run_installer) >= {"config_path", "argv", "default_root_name"}
    assert _params(tui_installer.SkillTarget) >= {
        "key", "label", "installed", "install", "uninstall"}
    assert callable(tui_installer.claude_target)


def test_testing_helper() -> None:
    from cli_tools_kit import testing
    assert _params(testing.assert_advertises) >= {"script", "python", "timeout"}
    assert _params(testing.validate_advertise) >= {"data"}


def test_grouping_side() -> None:
    assert _params(groups.ensure_groups) >= {"root"}
    assert isinstance(groups.UNGROUPED, str)
    assert callable(groups.groups_path) and callable(groups.read_groups_file)
    assert callable(corpus.tool_dirs)


@pytest.mark.parametrize("flag", [
    "--list", "--apply", "--skill-target", "--refresh", "--update-all", "--install",
    "--uninstall", "--cleanup", "--check", "--tui", "--gui",
    "--enable-autostart-check", "--disable-autostart-check",
])
def test_cli_flags(flag: str, installer_help: str) -> None:
    assert flag in installer_help


@pytest.fixture(scope="module")
def installer_help() -> str:
    example = Path(__file__).resolve().parent.parent / "examples" / "org-installer"
    result = subprocess.run([sys.executable, str(example / "installer.py"), "--help"],
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=60)
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_the_engine_globals_are_reachable_through_gui_installer(engine_state) -> None:
    """The names moved out of gui_installer still read and assign through it."""
    for module in gui_installer._ENGINE_MODULES:
        for name, value in list(vars(module).items()):
            if name.isupper() and not name.startswith("_"):
                assert getattr(gui_installer, name) is value, name
                setattr(gui_installer, name, "sentinel")
                assert getattr(module, name) == "sentinel", name
