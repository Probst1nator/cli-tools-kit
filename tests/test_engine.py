"""The engine's per-tool logic, driven against a real tool tree.

These pin what gui_installer does today (discover, install, detect drift,
remove, skills, autostart, orphans, the --update-all/--cleanup/--check
paths) so the logic can move out of that file without changing behaviour.
Each tool's own --install runs for real, in a subprocess, inside a sandbox
home. The GUI is covered separately by test_gui_smoke.py.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from cli_tools_kit import host

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="crontab exists only on POSIX")


def _tools(gi) -> dict:
    return {t.alias or t.name.lower(): t for t in gi.discover_tools(run_pre=False)}


def test_discovery_reads_every_tool(engine) -> None:
    tools = _tools(engine)
    assert set(tools) == {"clitool", "guitool", "crontool", "skilltool"}
    assert tools["guitool"].tags == ["GUI", "Icon"]
    assert tools["crontool"].cron_schedule == "@reboot"
    assert tools["skilltool"].skill_name == "skilltool"


@pytest.mark.parametrize("alias", ["clitool", "guitool"])
def test_install_then_remove(engine, alias: str) -> None:
    tool = _tools(engine)[alias]
    assert not engine.is_installed(tool)

    ok, output = engine.install_tool(tool, skip_deps=True)
    assert ok, output
    assert engine.is_installed(tool)
    assert not engine.needs_update(tool)

    ok, output = engine.remove_tool(tool)
    assert ok, output
    assert not engine.is_installed(tool)


def test_a_renamed_alias_needs_an_update(engine) -> None:
    tool = _tools(engine)["clitool"]
    assert engine.install_tool(tool, skip_deps=True)[0]
    assert engine.needs_update(tool._replace(alias="clitool2"))


@pytest.mark.skipif(os.name == "nt", reason="a .lnk is binary; needs_update skips it")
def test_a_moved_script_needs_an_update(engine) -> None:
    tool = _tools(engine)["guitool"]
    assert engine.install_tool(tool, skip_deps=True)[0]
    assert engine.needs_update(tool._replace(script_path="/elsewhere/main.py"))


def test_skill_install_and_uninstall(engine, sandbox_home: Path) -> None:
    tool = _tools(engine)["skilltool"]
    skill = sandbox_home / ".claude" / "skills" / "skilltool" / "SKILL.md"
    assert engine.install_skill_for_tool(tool)[0]
    assert skill.exists()
    assert engine.uninstall_skill_for_tool(tool)[0]
    assert not skill.exists()


def test_icon_autostart_on_and_off(engine) -> None:
    tool = _tools(engine)["guitool"]
    assert engine.install_tool(tool, skip_deps=True)[0]
    ok, msg = engine.enable_autostart(tool)
    assert ok, msg
    assert engine.is_autostart_enabled(tool)
    assert os.path.exists(engine.get_autostart_path(tool))
    ok, msg = engine.disable_autostart(tool)
    assert ok, msg
    assert not engine.is_autostart_enabled(tool)


@POSIX_ONLY
def test_cron_autostart_on_and_off(engine, fake_crontab: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", str(fake_crontab.parent) + os.pathsep + os.environ["PATH"])
    fake_crontab.write_text("0 3 * * * backup.sh\n")
    tool = _tools(engine)["crontool"]

    ok, msg = engine.enable_autostart(tool)
    assert ok, msg
    assert engine.is_autostart_enabled(tool)
    assert "0 3 * * * backup.sh" in fake_crontab.read_text()

    ok, msg = engine.disable_autostart(tool)
    assert ok, msg
    assert not engine.is_autostart_enabled(tool)
    assert fake_crontab.read_text().strip() == "0 3 * * * backup.sh"


@POSIX_ONLY
def test_a_cron_line_quotes_paths_with_spaces(engine, fake_crontab: Path, monkeypatch) -> None:
    """cron runs the line through /bin/sh, so a space must not split a path.
    A plain path stays unquoted, so lines already in a crontab still match."""
    import shlex
    import sys

    from cli_tools_kit import autostart

    monkeypatch.setenv("PATH", str(fake_crontab.parent) + os.pathsep + os.environ["PATH"])
    tool = _tools(engine)["crontool"]
    monkeypatch.setattr(sys, "executable", "/usr/bin/python3")
    plain = tool._replace(script_path="/opt/tools/crontool/main.py")
    assert autostart._cron_line_for_tool(plain) == "@reboot /usr/bin/python3 /opt/tools/crontool/main.py"

    monkeypatch.setattr(sys, "executable", "/opt/my env/bin/python3")
    spaced = tool._replace(script_path="/home/u/My Tools/crontool/main.py")
    ok, msg = engine.enable_autostart(spaced)
    assert ok, msg
    assert shlex.split(fake_crontab.read_text()) == [
        "@reboot", "/opt/my env/bin/python3", "/home/u/My Tools/crontool/main.py"]
    assert engine.is_autostart_enabled(spaced)
    ok, msg = engine.disable_autostart(spaced)
    assert ok, msg
    assert fake_crontab.read_text().strip() == ""


@POSIX_ONLY
def test_conditions_turn_the_autostart_entry_into_a_gated_copy(engine) -> None:
    tool = _tools(engine)["guitool"]._replace(autostart_conditions=["time_window"])
    assert engine.install_tool(tool, skip_deps=True)[0]
    engine.set_autostart_conditions(tool, {"time_window": {"from": "06:00", "to": "12:00"}})
    ok, msg = engine.enable_autostart(tool)
    assert ok, msg
    entry = engine.get_autostart_path(tool)
    assert not os.path.islink(entry)
    assert "cli-tools-kit-autostart-gate" in Path(entry).read_text()


def test_a_deleted_tool_leaves_an_orphan_that_cleanup_removes(engine, fake_tree: Path) -> None:
    tools = _tools(engine)
    for alias in ("clitool", "guitool"):
        assert engine.install_tool(tools[alias], skip_deps=True)[0]
    shutil.rmtree(fake_tree / "clitool")
    shutil.rmtree(fake_tree / "guitool")

    assert [o.name for o in engine.find_orphan_aliases()] == ["clitool"]
    if not host.IS_WINDOWS:   # Windows shortcuts are .lnk; the sweep reads .desktop
        assert [o.filename for o in engine.find_orphan_desktop_files()] == ["guitool.desktop"]

    engine.cli_cleanup(dry_run=True)
    assert engine.find_orphan_aliases()            # a dry run changes nothing

    engine.cli_cleanup(dry_run=False)
    assert engine.find_orphan_aliases() == []
    assert engine.find_orphan_desktop_files() == []


def test_update_all_reinstalls_what_is_installed(engine, monkeypatch) -> None:
    tools = _tools(engine)
    assert engine.install_tool(tools["clitool"], skip_deps=True)[0]
    calls = []
    real = engine.install_tool
    monkeypatch.setattr(engine, "install_tool",
                        lambda t, skip_deps=False: calls.append(t.alias) or real(t, True))
    monkeypatch.setattr(engine, "cli_install_self", lambda quiet=False: (True, ""))
    engine.cli_update_all(list(tools.values()))
    assert calls == ["clitool"]
    assert engine.is_installed(tools["clitool"])


def test_check_reconciles_a_drifted_shortcut(engine, monkeypatch) -> None:
    """--check reinstalls a shortcut whose alias drifted, network-free."""
    monkeypatch.setattr(engine, "_notify_send", lambda *a, **k: None)
    tool = _tools(engine)["clitool"]
    assert engine.install_tool(tool, skip_deps=True)[0]
    if host.IS_WINDOWS:   # the shims are the aliases there
        shim_dir = Path(engine.IDENTITY.shim_path)
        for suffix in (".cmd", ""):
            (shim_dir / f"clitool{suffix}").rename(shim_dir / f"oldname{suffix}")
    else:
        aliases = engine._load_aliases()
        aliases["oldname"] = aliases.pop("clitool")
        engine._save_aliases(aliases)
    assert engine.needs_update(tool)

    assert engine.cli_check() == 0
    assert not engine.needs_update(tool)


@POSIX_ONLY
def test_a_failing_crontab_read_never_wipes_the_crontab(engine, tmp_path: Path,
                                                        monkeypatch) -> None:
    """crontab -l can fail for reasons other than "no crontab" (permissions, a
    locked spool). Reading that as empty and writing back would replace the
    user's whole crontab with one line."""
    spool = tmp_path / "spool"
    spool.write_text("0 3 * * * backup.sh\n")
    shim = tmp_path / "bin" / "crontab"
    shim.parent.mkdir()
    shim.write_text(f'#!/bin/sh\nif [ "$1" = "-l" ]; then echo "crontab: permission denied" >&2; '
                    f'exit 1; fi\ncat > "{spool}"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", str(shim.parent) + os.pathsep + os.environ["PATH"])

    ok, msg = engine.enable_autostart(_tools(engine)["crontool"])
    assert not ok
    assert "permission denied" in msg
    assert spool.read_text() == "0 3 * * * backup.sh\n"
