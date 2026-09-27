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
    # os.path.expanduser caches its lookups via os.environ['HOME']; that's
    # fine because monkeypatch.setenv updates os.environ in-place.
    return home
