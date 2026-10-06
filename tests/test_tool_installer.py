"""Tests for ToolInstaller — desktop file and bash alias install/remove.

These are the Linux branches, so every test here runs with ``linux_host``;
the Windows shims and Start Menu entries are covered in test_host.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_tools_kit import ToolInstaller, ToolMetadata

pytestmark = pytest.mark.usefixtures("linux_host")


def _make_script(home: Path) -> str:
    """Create a dummy entry-point script and return its absolute path."""
    script_dir = home / "fake_tool"
    script_dir.mkdir()
    script = script_dir / "main.py"
    script.write_text("# dummy\nprint('hello')\n")
    return str(script)


def test_install_desktop_file(sandbox_home: Path) -> None:
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Fake Tool",
            desktop_file="fake_tool.desktop",
            icon="utilities-terminal",
            desc="A fake tool for tests",
            tags=["GUI", "Icon"],
        ),
    )
    installer.install()
    desktop_path = sandbox_home / ".local/share/applications/fake_tool.desktop"
    assert desktop_path.exists()
    content = desktop_path.read_text()
    assert "Name=Fake Tool" in content
    assert "Icon=utilities-terminal" in content
    assert script in content


def test_installed_desktop_file_is_not_executable(sandbox_home: Path) -> None:
    """An exec bit here makes systemd-xdg-autostart-generator warn every login
    once the entry is symlinked into ~/.config/autostart."""
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Fake Tool",
            desktop_file="fake_tool.desktop",
            icon="utilities-terminal",
            desc="A fake tool for tests",
            tags=["GUI", "Icon"],
        ),
    )
    installer.install()
    desktop_path = sandbox_home / ".local/share/applications/fake_tool.desktop"
    assert desktop_path.stat().st_mode & 0o111 == 0


def test_reinstall_clears_a_stale_exec_bit(sandbox_home: Path) -> None:
    """Entries installed by older versions were 0755; --install must fix them."""
    script = _make_script(sandbox_home)
    md = ToolMetadata(
        name="Fake Tool",
        desktop_file="fake_tool.desktop",
        icon="utilities-terminal",
        desc="A fake tool for tests",
        tags=["GUI", "Icon"],
    )
    installer = ToolInstaller(script_path=script, metadata=md)
    installer.install()
    desktop_path = sandbox_home / ".local/share/applications/fake_tool.desktop"
    desktop_path.chmod(0o755)
    installer.install()
    assert desktop_path.stat().st_mode & 0o111 == 0


def test_remove_desktop_file(sandbox_home: Path) -> None:
    script = _make_script(sandbox_home)
    md = ToolMetadata(
        name="Fake Tool",
        desktop_file="fake_tool.desktop",
        icon="utilities-terminal",
        desc="A fake tool for tests",
        tags=["GUI", "Icon"],
    )
    installer = ToolInstaller(script_path=script, metadata=md)
    installer.install()
    installer.remove()
    desktop_path = sandbox_home / ".local/share/applications/fake_tool.desktop"
    assert not desktop_path.exists()


def test_install_cli_alias(sandbox_home: Path) -> None:
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Fake CLI",
            desktop_file="fake_cli.desktop",  # unused for CLI-only
            icon="utilities-terminal",
            desc="A fake CLI tool",
            tags=["CLI"],
            alias="faketool",
        ),
    )
    installer.install()
    aliases_file = sandbox_home / ".tools_aliases"
    assert aliases_file.exists()
    content = aliases_file.read_text()
    assert "alias faketool=" in content
    assert script in content
    # bashrc should now source the aliases file
    bashrc = sandbox_home / ".bashrc"
    assert bashrc.exists()
    assert ".tools_aliases" in bashrc.read_text()


def test_remove_cli_alias(sandbox_home: Path) -> None:
    script = _make_script(sandbox_home)
    md = ToolMetadata(
        name="Fake CLI",
        desktop_file="fake_cli.desktop",
        icon="utilities-terminal",
        desc="A fake CLI tool",
        tags=["CLI"],
        alias="faketool",
    )
    installer = ToolInstaller(script_path=script, metadata=md)
    installer.install()
    installer.remove()
    aliases_file = sandbox_home / ".tools_aliases"
    if aliases_file.exists():
        assert "alias faketool=" not in aliases_file.read_text()


def test_install_with_alias_alongside_icon(sandbox_home: Path) -> None:
    """An Icon-tagged tool with an explicit alias gets BOTH a .desktop AND an alias."""
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Dual",
            desktop_file="dual.desktop",
            icon="utilities-terminal",
            desc="Has both",
            tags=["GUI", "CLI", "Icon"],
            alias="dual",
        ),
    )
    installer.install()
    assert (sandbox_home / ".local/share/applications/dual.desktop").exists()
    aliases = (sandbox_home / ".tools_aliases").read_text()
    assert "alias dual=" in aliases


def test_advertise_helper_emits_json(sandbox_home: Path, capsys) -> None:
    """advertise() prints JSON and SystemExits 0."""
    import json
    import pytest
    from cli_tools_kit import advertise

    with pytest.raises(SystemExit) as exc_info:
        advertise(ToolMetadata(
            name="X",
            desktop_file="x.desktop",
            icon="x",
            desc="x",
            tags=["CLI"],
            alias="x",
        ))
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert isinstance(parsed, list)
    assert parsed[0]["name"] == "X"
    assert parsed[0]["alias"] == "x"
    assert parsed[0]["tags"] == ["CLI"]


def test_advertise_default_tags_when_unset(capsys) -> None:
    """When tags is None, advertise() emits the default ['GUI', 'Icon']."""
    import json
    import pytest
    from cli_tools_kit import advertise

    with pytest.raises(SystemExit):
        advertise(ToolMetadata(
            name="Y",
            desktop_file="y.desktop",
            icon="y",
            desc="y",
        ))
    parsed = json.loads(capsys.readouterr().out)
    assert parsed[0]["tags"] == ["GUI", "Icon"]
    # No alias field expected for Icon-only tools without explicit alias
    assert "alias" not in parsed[0]


def test_alias_args_used_only_for_alias_command(sandbox_home: Path) -> None:
    """alias_args takes effect in the bash alias but NOT in get_advertise_data.args."""
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Aliased",
            desktop_file="aliased.desktop",
            icon="x",
            desc="x",
            tags=["CLI"],
            alias="aliased",
            alias_args=["--clip"],
        ),
    )
    installer.install()
    aliases = (sandbox_home / ".tools_aliases").read_text()
    # Alias command must include --clip
    assert "--clip" in aliases
    assert "alias aliased=" in aliases
    # But the advertise data preserves the separation: args stays empty,
    # alias_args carries the --clip flag.
    data = installer.get_advertise_data()[0]
    assert data["args"] == []
    assert data["alias_args"] == ["--clip"]


def test_alias_falls_back_to_args_when_alias_args_unset(sandbox_home: Path) -> None:
    """If alias_args is None, the alias uses args (legacy behavior)."""
    script = _make_script(sandbox_home)
    installer = ToolInstaller(
        script_path=script,
        metadata=ToolMetadata(
            name="Legacy",
            desktop_file="legacy.desktop",
            icon="x",
            desc="x",
            tags=["CLI"],
            alias="legacy",
            args=["--mode", "fast"],
        ),
    )
    installer.install()
    aliases = (sandbox_home / ".tools_aliases").read_text()
    assert "--mode fast" in aliases
    data = installer.get_advertise_data()[0]
    assert "alias_args" not in data  # only surfaced when explicitly set


def test_advertise_emits_taxonomy_and_skill_fields_only_when_set(capsys) -> None:
    """capability/domain/category/skill_name/skill_status ride along in the
    record when set, and are absent (not null) when not."""
    import json
    import pytest
    from cli_tools_kit import advertise

    with pytest.raises(SystemExit):
        advertise(ToolMetadata(
            name="Z",
            desktop_file="z.desktop",
            icon="z",
            desc="z",
            tags=["CLI"],
            alias="z",
            capability="scrape",
            domain="youtube",
            skill_name="search-youtube",
            skill_status="current",
        ))
    rec = json.loads(capsys.readouterr().out)[0]
    assert rec["capability"] == "scrape"
    assert rec["domain"] == "youtube"
    assert rec["skill_name"] == "search-youtube"
    assert rec["skill_status"] == "current"
    assert "category" not in rec

    with pytest.raises(SystemExit):
        advertise(ToolMetadata(name="W", desktop_file="w.desktop", icon="w", desc="w"))
    rec = json.loads(capsys.readouterr().out)[0]
    for key in ("capability", "domain", "category", "skill_name", "skill_status"):
        assert key not in rec


def test_variants_selects_by_desktop_file(sandbox_home: Path) -> None:
    public = ToolMetadata(name="Voice", desktop_file="voice.desktop", icon="x",
                          desc="d", tags=["GUI", "Icon"])
    private = ToolMetadata(name="Voice (Private)", desktop_file="voice-private.desktop",
                           icon="x", desc="d", tags=["GUI", "Icon"])
    installer = ToolInstaller(script_path=_make_script(sandbox_home),
                              metadata=[public, private])
    assert installer.variants() == [public, private]
    assert installer.variants("voice-private.desktop") == [private]
    with pytest.raises(ValueError):
        installer.variants("missing.desktop")


@pytest.mark.parametrize("text, runs_pip", [
    ("# listed so the installer's walker finds the tool\n\n   # indented\n", False),
    ("", False),
    ("# deps\nrequests>=2  # http\n", True),
])
def test_install_dependencies_skips_pip_without_a_requirement(
        sandbox_home: Path, monkeypatch, text: str, runs_pip: bool) -> None:
    """A comment-only requirements.txt made pip warn about an empty file."""
    from cli_tools_kit import tool_installer

    script = _make_script(sandbox_home)
    (Path(script).parent / "requirements.txt").write_text(text)
    calls: list = []
    monkeypatch.setattr(tool_installer.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.delenv("TOOLS_INSTALLER_SKIP_DEPS", raising=False)
    installer = ToolInstaller(script_path=script, metadata=ToolMetadata(
        name="Fake Tool", desktop_file="fake_tool.desktop", icon="x", desc="d"))
    assert installer.install_dependencies()
    assert bool(calls) == runs_pip
