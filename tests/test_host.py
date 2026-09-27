"""Tests for the Windows paths in cli_tools_kit.host.

These run on Linux: ``host.IS_WINDOWS`` is patched where a Windows decision is
under test, and the registry is a fake object, so nothing here needs a Windows
machine or the ``winreg`` module.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_tools_kit import host, InstallerIdentity, ToolInstaller, ToolMetadata


# --- shims -------------------------------------------------------------------

def test_write_shims_writes_both_launchers(tmp_path: Path) -> None:
    shim_dir = tmp_path / "bin"
    paths = host.write_shims(
        str(shim_dir), "mytool",
        r"C:\Python\python.exe", r"C:\tools\mytool\main.py", ["--clip"],
    )
    assert [os.path.basename(p) for p in paths] == ["mytool.cmd", "mytool"]

    cmd = (shim_dir / "mytool.cmd").read_text()
    assert cmd.splitlines()[0] == "@echo off"
    assert r'"C:\Python\python.exe" "C:\tools\mytool\main.py" "--clip" %*' \
        in cmd.splitlines()[1]

    sh = (shim_dir / "mytool").read_text()
    assert sh.splitlines()[0] == "#!/bin/sh"
    # Git Bash reads a backslash as an escape, so the paths carry forward slashes.
    assert sh.splitlines()[1] == (
        'exec "C:/Python/python.exe" "C:/tools/mytool/main.py" --clip "$@"'
    )
    assert "\\" not in sh


def test_write_shims_without_args(tmp_path: Path) -> None:
    host.write_shims(str(tmp_path), "plain", "/usr/bin/python3", "/opt/t/main.py")
    assert (tmp_path / "plain.cmd").read_text().splitlines()[1] == \
        '"/usr/bin/python3" "/opt/t/main.py" %*'
    assert (tmp_path / "plain").read_text().splitlines()[1] == \
        'exec "/usr/bin/python3" "/opt/t/main.py" "$@"'


def test_remove_shims_is_idempotent(tmp_path: Path) -> None:
    host.write_shims(str(tmp_path), "gone", "python", "main.py")
    assert len(host.remove_shims(str(tmp_path), "gone")) == 2
    assert host.remove_shims(str(tmp_path), "gone") == []
    assert host.remove_shims(str(tmp_path), "never-existed") == []


# --- the user PATH -----------------------------------------------------------

class FakeKey:
    def __init__(self, store: dict) -> None:
        self.store = store


class FakeRegistry:
    """Just enough of winreg for ensure_user_path."""

    HKEY_CURRENT_USER = 1
    KEY_READ = 0x20019
    KEY_WRITE = 0x20006
    REG_EXPAND_SZ = 2

    def __init__(self, path_value=None) -> None:
        self.store = {} if path_value is None else {"Path": (path_value, 2)}
        self.closed = 0

    def OpenKey(self, root, sub, reserved, access):
        assert (root, sub) == (self.HKEY_CURRENT_USER, "Environment")
        return FakeKey(self.store)

    def QueryValueEx(self, key, name):
        if name not in key.store:
            raise OSError("not found")
        return key.store[name]

    def SetValueEx(self, key, name, reserved, type_, value):
        key.store[name] = (value, type_)

    def CloseKey(self, key):
        self.closed += 1


def test_ensure_user_path_appends_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", r"C:\Windows")
    reg = FakeRegistry(r"C:\Windows;C:\Windows\System32")
    target = r"C:\Users\me\AppData\Local\acme\bin"

    assert host.ensure_user_path(target, reg=reg) is True
    stored, kind = reg.store["Path"]
    assert stored.split(";")[-1] == target
    assert kind == FakeRegistry.REG_EXPAND_SZ
    assert os.environ["PATH"].startswith(target)

    # A second call finds it already there and leaves the registry alone.
    assert host.ensure_user_path(target, reg=reg) is False
    assert reg.store["Path"][0] == stored
    assert os.environ["PATH"].startswith(target)


def test_ensure_user_path_compares_case_insensitively() -> None:
    reg = FakeRegistry(r"C:\Users\Me\AppData\Local\Acme\Bin")
    assert host.ensure_user_path(r"c:\users\me\appdata\local\acme\bin", reg=reg) is False


def test_ensure_user_path_with_no_existing_value() -> None:
    reg = FakeRegistry()
    assert host.ensure_user_path(r"C:\shims", reg=reg) is True
    assert reg.store["Path"][0] == r"C:\shims"
    assert reg.closed == 1


# --- hints and directories ---------------------------------------------------

def test_shell_hint_per_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    assert host.shell_hint() == "Open a new terminal"
    monkeypatch.setattr(host, "IS_WINDOWS", False)
    assert host.shell_hint() == "Open a new shell, or run: source ~/.bashrc"


def test_shim_path_comes_from_the_slug(sandbox_home: Path) -> None:
    ident = InstallerIdentity(slug="acme-tools")
    assert Path(ident.shim_path) == sandbox_home / "AppData/Local/acme-tools/bin"
    assert host.shim_dir(ident) == ident.shim_path


def test_start_menu_and_startup_dirs(sandbox_home: Path) -> None:
    programs = sandbox_home / "AppData/Roaming/Microsoft/Windows/Start Menu/Programs"
    assert Path(host.start_menu_dir()) == programs
    assert Path(host.startup_dir()) == programs / "Startup"


def test_prefer_tui_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli_tools_kit import tui_installer

    monkeypatch.setattr(host, "IS_WINDOWS", True)
    # No DISPLAY on Windows, and tkinter ships with Python: only --tui forces
    # the text screen.
    assert tui_installer.prefer_tui(environ={}) is False
    assert tui_installer.prefer_tui(force_tui=True, environ={}) is True
    assert tui_installer.prefer_tui(force_gui=True, environ={}) is False

    monkeypatch.setattr(host, "IS_WINDOWS", False)
    assert tui_installer.prefer_tui(environ={}) is True


# --- the installer on Windows ------------------------------------------------

def test_cli_tool_installs_shims_on_windows(sandbox_home: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host, "IS_WINDOWS", True)
    added: list = []
    monkeypatch.setattr(host, "ensure_user_path",
                        lambda directory, reg=None: added.append(directory) or False)

    script_dir = sandbox_home / "fake_tool"
    script_dir.mkdir()
    script = script_dir / "main.py"
    script.write_text("# dummy\n")

    identity = InstallerIdentity(slug="acme-tools")
    installer = ToolInstaller(
        script_path=str(script),
        metadata=ToolMetadata(
            name="Fake Tool", desktop_file="fake_tool.desktop",
            icon="utilities-terminal", desc="A fake tool for tests",
            tags=["CLI"],
        ),
        identity=identity,
    )
    installer.install()

    shim_dir = Path(identity.shim_path)
    assert (shim_dir / "fake_tool.cmd").exists()
    assert (shim_dir / "fake_tool").exists()
    assert str(script) in (shim_dir / "fake_tool.cmd").read_text()
    assert added == [str(shim_dir)]
    # No alias file and no ~/.bashrc line on Windows.
    assert not (sandbox_home / ".acme_tools_aliases").exists()
    assert not (sandbox_home / ".bashrc").exists()

    installer.remove()
    assert not (shim_dir / "fake_tool.cmd").exists()
    assert not (shim_dir / "fake_tool").exists()


# --- text encoding -----------------------------------------------------------

def test_harden_stdio_turns_unencodable_output_into_question_marks() -> None:
    """A Windows pipe is cp1252, which has no ✓; --list used to die on it."""
    import io

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")
    host.harden_stdio([stream])
    print(" [✓] Greeter", file=stream)
    stream.flush()
    assert raw.getvalue() == b" [?] Greeter\n"


def test_tool_output_reaches_the_kit_whatever_the_parent_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool printing an emoji must not die in its own print, and the kit
    must read the text back intact. PYTHONIOENCODING=cp1252 stands in for a
    Windows pipe on any host."""
    import cli_tools_kit.gui_installer as gi

    script = tmp_path / "tool" / "main.py"
    script.parent.mkdir()
    script.write_text("import sys\nprint('✅ installed')\nsys.exit(0)\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    tool = gi.ToolEntry(name="Tool", desktop_file="tool.desktop", script_path=str(script),
                        args=[], icon="", description="", terminal=False, category="")
    ok, output = gi.install_tool(tool, skip_deps=True)
    assert ok, output
    assert output == "✅ installed"
