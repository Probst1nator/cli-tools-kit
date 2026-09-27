"""A whole installer, run the way a user runs it.

A wrapper script over ``sources.run_installer`` (the shape of the WW3 and
AutomatedAlchemy installers) with an ``installer.toml`` that points at the fake
tree, started as a subprocess in a sandbox home. These pin the command-line
contract: the flags, the exit codes, and what lands on disk.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = """\
import os
from cli_tools_kit import InstallerIdentity
from cli_tools_kit.sources import run_installer

HERE = os.path.dirname(os.path.abspath(__file__))
run_installer(os.path.join(HERE, "installer.toml"), default_root_name="acme",
              identity=InstallerIdentity(slug="acme-tools", title="Acme Tools"),
              entry_script=__file__)
"""

INSTALLED = re.compile(r"^ \[(?:✓|x)\] .*\| (\w+)", re.M)


@pytest.fixture
def installer(tmp_path: Path, fake_tree: Path, sandbox_home: Path):
    """Run the wrapper with the given flags; returns the CompletedProcess."""
    org = tmp_path / "org"
    org.mkdir()
    (org / "installer.py").write_text(WRAPPER, encoding="utf-8")
    (org / "installer.toml").write_text(
        f"[[source]]\nname = \"tree\"\npath = '{fake_tree}'\n", encoding="utf-8")
    env = dict(os.environ, TOOLS_INSTALLER_SKIP_DEPS="1")
    if os.name != "nt":   # --check may notify; never on the real desktop
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "notify-send").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "notify-send").chmod(0o755)
        env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(org / "installer.py"), "--root", str(tmp_path / "root"),
             *args],
            cwd=org, env=env, capture_output=True, encoding="utf-8", errors="replace",
            timeout=180)
    return run


def _installed(run) -> set:
    result = run("--list")
    assert result.returncode == 0, result.stderr
    return {name.lower() for name in INSTALLED.findall(result.stdout)}


def _break(tool_dir: Path) -> None:
    """Make the tool's own --install fail from now on."""
    main = tool_dir / "main.py"
    main.write_text(main.read_text(encoding="utf-8").replace(
        'if "--install" in sys.argv:\n',
        'if "--install" in sys.argv:\n    sys.exit("install failed on purpose")\n'),
        encoding="utf-8")


def test_list_shows_the_tree(installer) -> None:
    result = installer("--list")
    assert result.returncode == 0, result.stderr
    for name in ("Clitool", "Guitool", "Crontool", "Skilltool"):
        assert name in result.stdout
    assert _installed(installer) == set()


def test_apply_all_installs_every_tool_and_its_skill(installer, sandbox_home: Path) -> None:
    result = installer("--apply", "all")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed(installer) == {"clitool", "guitool", "crontool", "skilltool"}
    assert (sandbox_home / ".claude" / "skills" / "skilltool" / "SKILL.md").exists()

    again = installer("--apply", "all")
    assert again.returncode == 0
    assert "Nothing to do" in again.stdout


@pytest.mark.parametrize("names", [["clitool,skilltool"], ["clitool", "skilltool"],
                                   ["clitool,", "skilltool"]])
def test_apply_takes_names_by_comma_or_space(installer, names) -> None:
    result = installer("--apply", *names, "--skill-target", "none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _installed(installer) == {"clitool", "skilltool"}


def test_skill_target_none_writes_no_skill(installer, sandbox_home: Path) -> None:
    assert installer("--apply", "skilltool", "--skill-target", "none").returncode == 0
    assert not (sandbox_home / ".claude" / "skills" / "skilltool" / "SKILL.md").exists()


def test_an_unknown_name_exits_2_and_changes_nothing(installer) -> None:
    result = installer("--apply", "clitool,nosuchtool")
    assert result.returncode == 2
    assert "Unknown tool 'nosuchtool'" in result.stderr
    assert _installed(installer) == set()


def test_a_failed_install_fails_the_run_and_writes_no_skill(installer, fake_tree: Path,
                                                            sandbox_home: Path) -> None:
    _break(fake_tree / "skilltool")
    result = installer("--apply", "all")
    assert result.returncode == 1, result.stdout
    assert "FAILED" in result.stdout
    assert "Skill skilltool skipped" in result.stdout
    assert not (sandbox_home / ".claude" / "skills" / "skilltool").exists()
    assert _installed(installer) == {"clitool", "guitool", "crontool"}


def test_update_all_exit_code_follows_the_tools(installer, fake_tree: Path) -> None:
    assert installer("--apply", "clitool,guitool").returncode == 0
    ok = installer("--update-all")
    assert ok.returncode == 0, ok.stdout + ok.stderr

    _break(fake_tree / "clitool")
    failed = installer("--update-all")
    assert failed.returncode == 1
    assert "[ERR]" in failed.stdout


def test_cleanup_lists_then_removes_what_a_deleted_tool_left(installer, fake_tree: Path) -> None:
    assert installer("--apply", "clitool").returncode == 0
    shutil.rmtree(fake_tree / "clitool")

    dry = installer("--cleanup")
    assert dry.returncode == 0
    assert "clitool" in dry.stdout and "Dry run" in dry.stdout

    wet = installer("--cleanup", "--yes")
    assert wet.returncode == 0, wet.stdout
    assert "Removed 1 orphan(s)" in wet.stdout
    assert "All clean" in installer("--cleanup").stdout


def test_check_runs_headless_and_leaves_its_log(installer, sandbox_home: Path) -> None:
    assert installer("--apply", "all", "--skill-target", "none").returncode == 0
    result = installer("--check")
    assert result.returncode == 0, result.stdout + result.stderr
    logs = list((sandbox_home / ".local" / "log").glob("*"))
    assert logs, "the login check writes its log"


def test_the_login_check_entry_turns_on_and_off(installer) -> None:
    on = installer("--enable-autostart-check")
    assert on.returncode == 0, on.stderr
    entry = Path(on.stdout.split(": ", 1)[1].splitlines()[0].strip())
    assert entry.exists()
    off = installer("--disable-autostart-check")
    assert off.returncode == 0
    assert not entry.exists()
