"""Shared pytest fixtures."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


@pytest.fixture
def fake_crontab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a Path that acts as the user's crontab.

    Writes a shell shim named `crontab` into a tmp dir, points
    CronInstaller.CRONTAB_BIN at it, and returns the file the shim
    reads from / writes to. The shim mirrors real `crontab` semantics:
        crontab -l     → cat the file (rc=0), or rc=1 if missing
        crontab -      → read stdin, overwrite the file
    """
    if os.name == "nt":
        pytest.skip("crontab exists only on POSIX; CronInstaller is Linux-only")
    crontab_file = tmp_path / "user.crontab"
    shim = tmp_path / "crontab"
    shim.write_text(
        f"""#!/bin/sh
set -e
FILE="{crontab_file}"
case "$1" in
  -l)
    if [ -s "$FILE" ]; then
      cat "$FILE"
    else
      echo "no crontab for testuser" >&2
      exit 1
    fi
    ;;
  -)
    cat > "$FILE"
    ;;
  *)
    echo "unsupported: $@" >&2
    exit 2
    ;;
esac
"""
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    from cli_tools_kit import cron_installer
    monkeypatch.setattr(cron_installer.CronInstaller, "CRONTAB_BIN", str(shim))
    return crontab_file


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect $HOME (and thereby ~/.local, ~/.tools_aliases, ~/.bashrc) to tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # The Windows equivalents, so the shim directory and the Start Menu also
    # land under the sandbox when a test patches host.IS_WINDOWS.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    # A tool's --install on Windows writes the user PATH in the registry; a
    # test must never do that to the machine it runs on.
    monkeypatch.setenv("CLI_TOOLS_KIT_SKIP_USER_PATH", "1")
    # os.path.expanduser caches its lookups via os.environ['HOME']; that's
    # fine because monkeypatch.setenv updates os.environ in-place.
    return home


@pytest.fixture
def linux_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the Linux branches on any host.

    Every module reads ``host.IS_WINDOWS`` at call time, so a test of the
    .desktop, alias-file or display logic runs the same on the Windows CI
    runner. The Windows branches are covered by test_host.py.
    """
    from cli_tools_kit import host
    monkeypatch.setattr(host, "IS_WINDOWS", False)


# --- a small tool tree for the engine ------------------------------------------

_TOOL_TEMPLATE = '''\
import inspect, json, os, sys
META = {meta!r}
if "--advertise" in sys.argv:
    print(json.dumps([META]))
    sys.exit(0)
from cli_tools_kit import ToolInstaller, ToolMetadata
fields = inspect.signature(ToolMetadata).parameters
installer = ToolInstaller(__file__, ToolMetadata(**{{k: v for k, v in META.items() if k in fields}}))
skill = os.path.join(os.path.expanduser("~"), ".claude", "skills", META["name"].lower(), "SKILL.md")
if "--install" in sys.argv:
    installer.install()
elif "--remove" in sys.argv:
    installer.remove()
elif "--install-skill" in sys.argv:
    os.makedirs(os.path.dirname(skill), exist_ok=True)
    with open(skill, "w", encoding="utf-8") as fh:
        fh.write("---\\nname: x\\n---\\n")
elif "--uninstall-skill" in sys.argv:
    if os.path.exists(skill):
        os.remove(skill)
'''

FAKE_TOOLS = {
    "clitool": {"name": "Clitool", "desktop_file": "clitool.desktop", "icon": "x",
                "desc": "A CLI tool", "tags": ["CLI"], "alias": "clitool",
                "capability": "misc"},
    "guitool": {"name": "Guitool", "desktop_file": "guitool.desktop", "icon": "x",
                "desc": "A GUI tool", "tags": ["GUI", "Icon"], "capability": "misc"},
    "crontool": {"name": "Crontool", "desktop_file": "crontool.desktop", "icon": "x",
                 "desc": "A scheduled tool", "tags": ["CLI"], "alias": "crontool",
                 "cron_schedule": "@reboot", "capability": "misc"},
    "skilltool": {"name": "Skilltool", "desktop_file": "skilltool.desktop", "icon": "x",
                  "desc": "A tool with a skill", "tags": ["CLI"], "alias": "skilltool",
                  "skill_name": "skilltool", "capability": "misc"},
}


@pytest.fixture
def fake_tree(tmp_path: Path) -> Path:
    """Four tools in the flat layout, each installing itself via ToolInstaller."""
    root = tmp_path / "tree"
    for folder, meta in FAKE_TOOLS.items():
        tool = root / folder
        tool.mkdir(parents=True)
        (tool / "main.py").write_text(_TOOL_TEMPLATE.format(meta=meta), encoding="utf-8")
        (tool / "requirements.txt").write_text("# none\n", encoding="utf-8")
    return root


@pytest.fixture
def engine(sandbox_home: Path, fake_tree: Path, monkeypatch: pytest.MonkeyPatch):
    """gui_installer pointed at the sandbox and the fake tree, restored afterwards.

    Some of the engine's paths are fixed when the module is imported, so they
    are set here explicitly rather than left to follow $HOME.
    """
    import cli_tools_kit.gui_installer as gi
    from cli_tools_kit import InstallerIdentity, host

    saved = dict(gi.__dict__)
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    gi._apply_identity(InstallerIdentity(slug="acme-tools", title="Acme Tools"))
    if host.IS_WINDOWS:
        gi.APPS_DIR = host.start_menu_dir()
        gi.AUTOSTART_DIR = host.startup_dir()
    else:
        gi.APPS_DIR = str(sandbox_home / ".local" / "share" / "applications")
        gi.AUTOSTART_DIR = str(sandbox_home / ".config" / "autostart")
    gi.CLAUDE_SKILLS_DIR = str(sandbox_home / ".claude" / "skills")
    gi.ROOT_DIR = str(fake_tree)
    gi.DISCOVERY_ROOTS = []
    gi.DISCOVERER = None
    yield gi
    for name in set(gi.__dict__) - set(saved):
        delattr(gi, name)
    gi.__dict__.update(saved)
