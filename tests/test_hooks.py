"""run(hooks=...) replaces how one tool is installed, for every screen."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

import cli_tools_kit.gui_installer as gi
from cli_tools_kit import InstallerIdentity

EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "org-installer"


def test_apply_goes_through_the_hooks(sandbox_home: Path, tmp_path: Path,
                                      monkeypatch: pytest.MonkeyPatch, engine_state) -> None:
    tree = tmp_path / "org"
    shutil.copytree(EXAMPLE, tree)
    calls: list = []

    def install(tool, skip_deps=False):
        calls.append(("install", tool.alias))
        return True, "hooked"

    def install_skill(tool):
        calls.append(("skill", tool.alias))
        return True, "hooked"

    monkeypatch.setattr(sys, "argv", ["installer.py", "--apply", "greeter"])
    with pytest.raises(SystemExit) as done:
        gi.run(identity=InstallerIdentity(slug="acme-tools", title="Acme Tools"),
               root_dir=str(tree), entry_script=str(tree / "installer.py"),
               hooks=gi.InstallHooks(install_tool=install, install_skill=install_skill))
    assert done.value.code == 0
    assert ("install", "greeter") in calls
    # The kit's own install never ran: it would have written the alias file.
    assert not (sandbox_home / ".acme_tools_aliases").exists()


def test_unset_hooks_keep_the_kits_own(engine_state) -> None:
    own = gi.remove_tool
    gi._apply_hooks(gi.InstallHooks(install_tool=lambda t, skip_deps=False: (True, "")))
    assert gi.remove_tool is own
