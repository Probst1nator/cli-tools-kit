"""upgrade.check() and upgrade.upgrade() against real git repositories."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from cli_tools_kit import discovery, install, state, upgrade

DAY = upgrade.CHECK_INTERVAL
T = 1_800_000_000.0   # a plausible time.time(), well past the first interval


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.org",
                    "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine_state):
    """A bare remote, a clone of it, and a way to push a new commit to it."""
    # The kit forbids local transports for fetching; these repos are local.
    monkeypatch.setattr(upgrade, "GIT_SAFE", ())
    monkeypatch.setattr(upgrade, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(upgrade, "kit_blocker", lambda: "not under test")
    state.ENTRY_SCRIPT = str(tmp_path / "elsewhere" / "installer.py")

    bare, seed, clone = tmp_path / "remote.git", tmp_path / "seed", tmp_path / "clone"
    _git(tmp_path, "init", "--bare", str(bare))
    _git(tmp_path, "clone", str(bare), str(seed))
    (seed / "tool").mkdir()
    (seed / "tool" / "main.py").write_text("print('v1')\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "v1")
    _git(seed, "push", "origin", "HEAD:main")
    _git(tmp_path, "clone", str(bare), str(clone))

    def push(n: int = 1) -> None:
        for i in range(n):
            (seed / "tool" / "main.py").write_text(f"print('v{i + 2}')\n")
            _git(seed, "commit", "-am", f"v{i + 2}")
        _git(seed, "push", "origin", "HEAD:main")

    return clone, push


def test_a_clone_behind_its_remote_is_found_and_pulled(remote) -> None:
    clone, push = remote
    state.UPGRADE_REPOS = [("tools", str(clone))]
    assert upgrade.check(force=True) == []

    push(2)
    items = upgrade.check(force=True)
    assert [(i.kind, i.name, i.detail) for i in items] == [("source", "tools", "2 commits")]

    lines = []
    result = upgrade.upgrade(items, [], lambda msg, tag="info": lines.append(msg))
    assert result == {"changed": True, "errors": 0}
    assert (clone / "tool" / "main.py").read_text() == "print('v3')\n"
    assert upgrade.check(force=True) == []


def test_the_network_is_used_once_a_day(remote, monkeypatch: pytest.MonkeyPatch) -> None:
    clone, push = remote
    state.UPGRADE_REPOS = [("tools", str(clone))]
    fetches = []
    real_fetch = upgrade.fetch
    monkeypatch.setattr(upgrade, "fetch", lambda path: fetches.append(path) or real_fetch(path))

    upgrade.check(now=T)
    push()
    # Within the day nothing is fetched, so the new commit is not seen yet.
    assert upgrade.check(now=T + DAY - 1) == []
    assert len(fetches) == 1
    assert [i.detail for i in upgrade.check(now=T + DAY)] == ["1 commit"]
    assert len(fetches) == 2


def test_only_an_offline_start_is_retried(remote, tmp_path, monkeypatch) -> None:
    clone, _ = remote
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(clone), str(other))
    state.UPGRADE_REPOS = [("tools", str(clone)), ("other", str(other))]
    monkeypatch.setattr(upgrade, "fetch", lambda path: False)
    upgrade.check(now=T)
    assert not upgrade._cache_path().exists()
    # One repo without credentials is not "offline": the day still counts.
    monkeypatch.setattr(upgrade, "fetch", lambda path: Path(path) == clone.resolve())
    upgrade.check(now=T)
    assert upgrade._cache_path().exists()


def test_the_installer_checkout_is_checked_and_its_tools_reinstalled(
        remote, monkeypatch: pytest.MonkeyPatch) -> None:
    clone, push = remote
    state.ENTRY_SCRIPT = str(clone / "installer.py")
    push()
    items = upgrade.check(force=True)
    assert [(i.kind, i.name) for i in items] == [("installer", "clone")]

    tool = discovery.ToolEntry(
        name="tool", desktop_file="tool.desktop", script_path=str(clone / "tool" / "main.py"),
        args=[], icon="", description="", terminal=False, category="", alias="tool")
    elsewhere = tool._replace(name="other", script_path="/somewhere/else/main.py")
    reinstalled = []
    monkeypatch.setattr(install, "is_installed", lambda t: True)
    monkeypatch.setattr(install, "install_tool",
                        lambda t, skip_deps=False: reinstalled.append(t.name) or (True, ""))
    lines = []
    result = upgrade.upgrade(items, [tool, elsewhere], lambda msg, tag="info": lines.append(msg))
    assert result == {"changed": True, "errors": 0}
    assert reinstalled == ["tool"]
    # The installer was pulled, so the kit step ran, and said why it did nothing.
    assert any("cli-tools-kit left as it is: not under test" in line for line in lines)


@pytest.mark.parametrize("pinned, pip_runs", [("0.7.1", False), (None, False), ("99.0.0", True)])
def test_a_pulled_installer_never_downgrades_the_kit(
        remote, monkeypatch: pytest.MonkeyPatch, pinned, pip_runs) -> None:
    clone, push = remote
    state.ENTRY_SCRIPT = str(clone / "installer.py")
    push()
    items = upgrade.check(force=True)
    # pinned None: the installer's pin matches the running kit exactly.
    monkeypatch.setattr(upgrade, "kit_blocker", lambda: None)
    monkeypatch.setattr(upgrade, "latest_kit", lambda: pinned or upgrade.kit_version())
    pip_calls = []
    real_run = upgrade.subprocess.run

    def run(cmd, **kwargs):
        if "pip" in cmd:
            pip_calls.append(cmd)
            return _Result(0, "")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    lines = []
    result = upgrade.upgrade(items, [], lambda msg, tag="info": lines.append(msg))
    assert result == {"changed": True, "errors": 0}
    assert bool(pip_calls) == pip_runs
    if not pip_runs:
        assert any("stays at" in line for line in lines)


def test_a_pull_that_cannot_fast_forward_is_an_error(remote) -> None:
    clone, push = remote
    state.UPGRADE_REPOS = [("tools", str(clone))]
    push()
    (clone / "tool" / "main.py").write_text("print('local')\n")
    _git(clone, "commit", "-am", "local")
    items = upgrade.check(force=True)
    lines = []
    result = upgrade.upgrade(items, [], lambda msg, tag="info": lines.append(msg))
    assert result == {"changed": False, "errors": 1}
    assert any("not pulled" in line for line in lines)


def test_a_newer_kit_within_the_pin_is_offered(tmp_path, monkeypatch, engine_state) -> None:
    monkeypatch.setattr(upgrade, "_cache_path", lambda: tmp_path / "cache.json")
    monkeypatch.setattr(upgrade, "kit_blocker", lambda: None)
    monkeypatch.setattr(upgrade, "latest_kit", lambda: "99.0.0")
    state.UPGRADE_REPOS = []
    state.ENTRY_SCRIPT = str(tmp_path / "installer.py")
    items = upgrade.check(force=True)
    assert [(i.kind, i.detail) for i in items] == [
        ("kit", f"{upgrade.kit_version()} → 99.0.0")]
    # The answer is kept for the day, per interpreter.
    monkeypatch.setattr(upgrade, "latest_kit", lambda: pytest.fail("asked pip again"))
    assert [i.kind for i in upgrade.check(now=json.loads(
        (tmp_path / "cache.json").read_text())["checked"] + 60)] == ["kit"]


class _Result:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode, self.stdout, self.stderr = returncode, stdout, ""


def test_latest_kit_reads_the_pip_report(tmp_path, monkeypatch, engine_state) -> None:
    state.ENTRY_SCRIPT = str(tmp_path / "installer.py")
    calls = []

    def fake_run(report, returncode=0):
        def run(cmd, **kwargs):
            calls.append(cmd)
            return _Result(returncode, json.dumps(report))
        return run

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run(
        {"install": [{"metadata": {"name": "Pillow", "version": "12.0"}},
                     {"metadata": {"name": "cli_tools_kit", "version": "1.2.3"}}]}))
    assert upgrade.latest_kit() == "1.2.3"
    assert calls[-1][:3] == [sys.executable, "-m", "pip"] and "--dry-run" in calls[-1]

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run({"install": []}))
    assert upgrade.latest_kit() == upgrade.kit_version()

    monkeypatch.setattr(upgrade.subprocess, "run", fake_run({}, returncode=1))
    assert upgrade.latest_kit() is None


def test_the_kit_requirement_follows_the_installer(tmp_path, engine_state) -> None:
    state.ENTRY_SCRIPT = str(tmp_path / "installer.py")
    major = int(upgrade.kit_version().split(".")[0])
    assert upgrade.kit_requirement() == [f"cli-tools-kit<{major + 1}"]
    (tmp_path / "requirements.txt").write_text("cli-tools-kit[gui]>=1.0,<2\n")
    assert upgrade.kit_requirement() == ["-r", str(tmp_path / "requirements.txt")]


def test_a_development_checkout_of_the_kit_is_not_upgraded(monkeypatch) -> None:
    monkeypatch.setattr(upgrade, "_kit_checkout", lambda: Path("/src/cli-tools-kit"))
    assert "development checkout" in upgrade.kit_blocker()
    monkeypatch.setattr(upgrade, "_kit_checkout", lambda: None)
    monkeypatch.setattr(sys, "base_prefix", sys.prefix)
    assert "virtual environment" in upgrade.kit_blocker()


def test_restart_uses_the_command_line_the_installer_was_started_with(engine_state) -> None:
    state.LAUNCH_ARGV = ["installer.py", "--root", "/x"]
    assert upgrade.restart_argv() == [sys.executable, "installer.py", "--root", "/x"]
