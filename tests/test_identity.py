"""InstallerIdentity, and the engine wiring that consumes it.

Two things are covered here. First that an identity derives the names it
promises, and that LEGACY_IDENTITY still produces the exact pre-0.2.2
first-party ones — an existing install must not be renamed out from under
somebody. Second a smoke test over the engine's reuse surface (import, the
run() kwargs, the default discoverer), which had no tests at all: a consumer
could previously only find out that an upgrade broke their wrapper by
launching the GUI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_tools_kit import InstallerIdentity, LEGACY_IDENTITY, ToolInstaller, ToolMetadata


# --- the identity value ----------------------------------------------------

def test_slug_derives_every_name():
    ident = InstallerIdentity(slug="acme-tools", title="Acme Tools")

    assert ident.self_desktop_file == "acme-tools-installer.desktop"
    assert ident.self_wm_class == "acme_tools_installer"
    assert ident.self_desktop_name == "Acme Tools"
    assert ident.notify_label == "Acme Tools"
    assert ident.desktop_keywords == "acme-tools;ai;tool;"
    assert ident.marker_token == "acme-tools"
    assert ident.aliases_path.replace(os.sep, "/").endswith("/.acme_tools_aliases")
    assert ident.config_path.replace(os.sep, "/").endswith("/.config/acme-tools")
    assert ident.config_file.replace(os.sep, "/").endswith("/.config/acme-tools/config.json")
    assert ident.icons_dir.replace(os.sep, "/").endswith("/.config/acme-tools/icons")
    assert ident.cache_path.replace(os.sep, "/").endswith("/.cache/acme-tools")
    assert ident.check_desktop == "acme-tools-check.desktop"
    assert ident.check_log == "acme-tools-check.log"
    assert ident.check_state == "acme-tools-check.json"
    # Windows shims: %LOCALAPPDATA%\acme-tools\bin, derived from the slug too.
    assert ident.shim_path.replace(os.sep, "/").endswith("/acme-tools/bin")


def test_title_defaults_to_slug():
    assert InstallerIdentity(slug="acme").display_title == "acme"


def test_explicit_fields_override_derived_ones():
    ident = InstallerIdentity(
        slug="acme", desktop_file="custom.desktop", wm_class="custom_wm",
        marker="acme.example.com",
    )
    assert ident.self_desktop_file == "custom.desktop"
    assert ident.self_wm_class == "custom_wm"
    assert ident.desktop_keywords == "acme.example.com;ai;tool;"


@pytest.mark.parametrize("bad", ["", "Acme", "acme tools", "-acme", "acme/tools", "acme\n"])
def test_bad_slug_rejected(bad):
    # The slug lands in a path, a filename and a .desktop key; none of these
    # tolerate spaces, separators or newlines.
    with pytest.raises(ValueError):
        InstallerIdentity(slug=bad)


def test_legacy_identity_reproduces_the_historical_names(sandbox_home: Path):
    # Pinned literally: these are on real machines. Changing one orphans the
    # shortcuts, aliases and icon overrides already installed under it.
    assert LEGACY_IDENTITY.self_desktop_file == "ai_tools_manager.desktop"
    assert LEGACY_IDENTITY.self_desktop_name == "Tools Installer"
    assert LEGACY_IDENTITY.self_wm_class == "tools_installer"
    assert LEGACY_IDENTITY.notify_label == "Tools Installer"
    assert LEGACY_IDENTITY.display_title == "probable.work - Tools Installer"
    assert LEGACY_IDENTITY.desktop_keywords == "probable.work;ai;tool;"
    assert LEGACY_IDENTITY.check_desktop == "tools-installer-check.desktop"
    assert LEGACY_IDENTITY.check_log == "tools-installer-check.log"
    assert LEGACY_IDENTITY.check_state == "tools-installer-check.json"
    assert LEGACY_IDENTITY.aliases_path == str(sandbox_home / ".tools_aliases")
    assert LEGACY_IDENTITY.config_path == str(sandbox_home / ".config" / "tools-installer")
    assert LEGACY_IDENTITY.cache_path == str(sandbox_home / ".cache" / "tools-installer")


def test_paths_follow_home_rather_than_freezing_it(sandbox_home: Path):
    # Resolving "~" when the identity is constructed would pin whatever HOME
    # held at import time and send a sandboxed run back to the real home.
    assert InstallerIdentity(slug="acme").aliases_path == str(sandbox_home / ".acme_aliases")


def test_two_identities_share_no_per_host_path():
    a = InstallerIdentity(slug="acme-tools")
    b = InstallerIdentity(slug="globex")
    for field in ("aliases_path", "config_path", "cache_path", "self_desktop_file",
                  "self_wm_class", "marker_token", "check_desktop", "check_log",
                  "check_state"):
        assert getattr(a, field) != getattr(b, field), field


def test_env_round_trip(monkeypatch):
    ident = InstallerIdentity(slug="acme-tools")
    for key, value in ident.env().items():
        monkeypatch.setenv(key, value)
    assert InstallerIdentity.from_env().slug == "acme-tools"


def test_from_env_falls_back_when_unset_or_malformed(monkeypatch):
    monkeypatch.delenv(InstallerIdentity.ENV_VAR, raising=False)
    assert InstallerIdentity.from_env() is LEGACY_IDENTITY
    monkeypatch.setenv(InstallerIdentity.ENV_VAR, "Not A Slug")
    assert InstallerIdentity.from_env() is LEGACY_IDENTITY


# --- the artifacts a tool writes ------------------------------------------

def _tool(tmp_path: Path) -> Path:
    script = tmp_path / "main.py"
    script.write_text("print('hi')\n")
    (tmp_path / "requirements.txt").write_text("")
    return script


def test_tool_artifacts_carry_the_identity(sandbox_home: Path, tmp_path: Path,
                                          monkeypatch, linux_host):
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    script = _tool(tmp_path)
    ident = InstallerIdentity(slug="acme-tools", title="Acme Tools")
    inst = ToolInstaller(
        script_path=str(script),
        metadata=ToolMetadata(name="Greeter", desktop_file="greeter.desktop",
                              icon="dialog-information", desc="Say hello",
                              tags=["GUI", "Icon"], alias="greeter"),
        identity=ident,
    )
    inst.install()

    desktop = sandbox_home / ".local/share/applications/greeter.desktop"
    assert "Keywords=acme-tools;ai;tool;" in desktop.read_text()
    # The alias goes to this org's file, and the first-party one is untouched.
    assert "alias greeter=" in (sandbox_home / ".acme_tools_aliases").read_text()
    assert not (sandbox_home / ".tools_aliases").exists()


def test_tool_takes_the_identity_of_the_installer_that_spawned_it(
    sandbox_home: Path, tmp_path: Path, monkeypatch, linux_host
):
    # The engine installs a tool by running its --install in a subprocess, so
    # the identity travels through the environment.
    monkeypatch.setenv(InstallerIdentity.ENV_VAR, "acme-tools")
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    inst = ToolInstaller(
        script_path=str(_tool(tmp_path)),
        metadata=ToolMetadata(name="Greeter", desktop_file="greeter.desktop",
                              icon="dialog-information", desc="Say hello",
                              tags=["CLI"], alias="greeter"),
    )
    assert inst.identity.slug == "acme-tools"
    inst.install()
    assert (sandbox_home / ".acme_tools_aliases").exists()
    assert not (sandbox_home / ".tools_aliases").exists()


def test_tool_spawned_by_the_legacy_installer_keeps_the_legacy_names(
    sandbox_home: Path, tmp_path: Path, monkeypatch, linux_host
):
    # A slug alone rebuilt InstallerIdentity(slug="probable.work"), whose
    # aliases file is ~/.probable_work_aliases, not the legacy ~/.tools_aliases.
    for key, value in LEGACY_IDENTITY.env().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    assert InstallerIdentity.from_env(InstallerIdentity(slug="acme-tools")) is LEGACY_IDENTITY
    inst = ToolInstaller(
        script_path=str(_tool(tmp_path)),
        metadata=ToolMetadata(name="Greeter", desktop_file="greeter.desktop",
                              icon="dialog-information", desc="Say hello",
                              tags=["CLI"], alias="greeter"),
    )
    assert inst.identity is LEGACY_IDENTITY
    inst.install()
    assert "alias greeter=" in (sandbox_home / ".tools_aliases").read_text()
    assert not (sandbox_home / ".probable_work_aliases").exists()


def test_tool_run_by_hand_keeps_the_legacy_names(sandbox_home: Path, tmp_path: Path,
                                                 monkeypatch, linux_host):
    monkeypatch.delenv(InstallerIdentity.ENV_VAR, raising=False)
    monkeypatch.setenv("TOOLS_INSTALLER_SKIP_DEPS", "1")
    inst = ToolInstaller(
        script_path=str(_tool(tmp_path)),
        metadata=ToolMetadata(name="Greeter", desktop_file="greeter.desktop",
                              icon="dialog-information", desc="Say hello",
                              tags=["CLI"], alias="greeter"),
    )
    inst.install()
    assert (sandbox_home / ".tools_aliases").exists()


# --- the engine's reuse surface -------------------------------------------

def test_run_is_importable():
    # The one entry point a third-party wrapper calls. It was never exercised
    # by a test before 0.2.2.
    from cli_tools_kit.gui_installer import run
    assert callable(run)


def test_apply_identity_repoints_every_global(sandbox_home: Path):
    import cli_tools_kit.gui_installer as gi

    before = gi.IDENTITY
    try:
        gi._apply_identity(InstallerIdentity(slug="acme-tools", title="Acme Tools"))
        assert gi.WINDOW_TITLE == "Acme Tools"
        assert gi.SELF_DESKTOP_FILE == "acme-tools-installer.desktop"
        assert gi.WM_CLASS == "acme_tools_installer"
        assert gi.NOTIFY_APP == "Acme Tools"
        assert gi.ALIASES_FILE.replace(os.sep, "/").endswith("/.acme_tools_aliases")
        assert gi.CONFIG_FILE.replace(os.sep, "/").endswith("/.config/acme-tools/config.json")
        assert gi.CUSTOM_ICONS_DIR.replace(os.sep, "/").endswith("/.config/acme-tools/icons")
        # Derived paths are recomputed, not just the names they come from.
        assert gi.CHECK_LOG.replace(os.sep, "/").endswith("/acme-tools-check.log")
        assert gi.CHECK_STATE.replace(os.sep, "/").endswith("/acme-tools-check.json")
        # .desktop on Linux, a .lnk in the Startup folder on Windows.
        check_entry = os.path.basename(gi.AUTOSTART_CHECK_DESKTOP)
        assert os.path.splitext(check_entry)[0] == "acme-tools-check"
    finally:
        gi._apply_identity(before)


def test_engine_defaults_are_the_legacy_identity():
    import cli_tools_kit.gui_installer as gi
    assert gi.IDENTITY is LEGACY_IDENTITY


def test_group_by_is_validated():
    from cli_tools_kit.gui_installer import run
    with pytest.raises(ValueError, match="group_by"):
        run(identity=InstallerIdentity(slug="acme"), group_by="nonsense")


def test_default_discoverer_finds_flat_and_nested_layouts(tmp_path: Path):
    from cli_tools_kit.gui_installer import _default_tools_discoverer

    def make(*parts: str) -> None:
        d = tmp_path.joinpath(*parts)
        d.mkdir(parents=True)
        (d / "main.py").write_text("")
        (d / "requirements.txt").write_text("")

    make("greeter")                      # flat
    make("tools_research", "arxiv")      # nested, category from the folder
    (tmp_path / "_shared").mkdir()       # skipped: underscore prefix
    (tmp_path / "_shared" / "main.py").write_text("")
    (tmp_path / "_shared" / "requirements.txt").write_text("")
    (tmp_path / "docs").mkdir()          # skipped: no main.py

    found = {os.path.relpath(p, tmp_path).replace(os.sep, "/"): cat
             for p, cat in _default_tools_discoverer(str(tmp_path))}
    assert found == {
        "greeter/main.py": "",
        "tools_research/arxiv/main.py": "Research",
    }


def test_custom_discoverer_is_honoured(tmp_path: Path, monkeypatch):
    import cli_tools_kit.gui_installer as gi

    script = tmp_path / "greeter" / "main.py"
    script.parent.mkdir()
    script.write_text(
        "import sys, json\n"
        "if '--advertise' in sys.argv:\n"
        "    print(json.dumps([{'name': 'Greeter', 'capability': 'notify',\n"
        "        'desktop_file': 'greeter.desktop', 'icon': 'x', 'desc': 'hi',\n"
        "        'tags': ['CLI'], 'alias': 'greeter'}]))\n"
        "    sys.exit(0)\n"
    )
    calls = []

    def discoverer(root):
        calls.append(root)
        return [(str(script), "Custom")]

    monkeypatch.setattr(gi, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(gi, "DISCOVERY_ROOTS", [str(tmp_path)])
    monkeypatch.setattr(gi, "DISCOVERER", discoverer)
    tools = gi.discover_tools()

    assert calls == [str(tmp_path)]
    assert [t.name for t in tools] == ["Greeter"]
    assert tools[0].category == "Custom"


# --- the wider walk used when discovery_roots is set ------------------------

def _tool(directory: Path, script: str = "main.py") -> Path:
    """A directory that looks like a tool: an entry point and requirements.txt."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / script).write_text("")
    (directory / "requirements.txt").write_text("")
    return directory


def test_walk_discoverer_finds_the_three_layouts(tmp_path: Path):
    from cli_tools_kit.gui_installer import _walk_tools_discoverer

    root = tmp_path / "manim-kit"
    _tool(root, "manim_kit.py")          # the root itself is the tool
    _tool(root / "greeter")              # one level down
    _tool(root / "tools" / "faullm")     # two levels down
    (root / "docs").mkdir()              # no entry point

    found = {os.path.relpath(p, root).replace(os.sep, "/"): cat for p, cat in _walk_tools_discoverer(str(root))}
    assert found == {
        "manim_kit.py": "manim-kit",     # category is the root's own name
        "greeter/main.py": "",           # parent is the root, so no category
        "tools/faullm/main.py": "tools",  # category is the parent directory
    }


def test_walk_discoverer_needs_requirements_next_to_the_entry_point(tmp_path: Path):
    from cli_tools_kit.gui_installer import _walk_tools_discoverer

    (tmp_path / "script-only").mkdir()
    (tmp_path / "script-only" / "main.py").write_text("")
    (tmp_path / "deps-only").mkdir()
    (tmp_path / "deps-only" / "requirements.txt").write_text("")
    assert _walk_tools_discoverer(str(tmp_path)) == []


def test_walk_discoverer_takes_the_directory_name_with_underscores(tmp_path: Path):
    from cli_tools_kit.gui_installer import _walk_tools_discoverer

    _tool(tmp_path / "manim-kit", "manim_kit.py")     # dashes become underscores
    _tool(tmp_path / "other-kit", "other-kit.py")     # the dashed spelling is not it
    _tool(tmp_path / "third-kit", "run.py")           # some other name is not it

    found = [os.path.relpath(p, tmp_path).replace(os.sep, "/") for p, _ in _walk_tools_discoverer(str(tmp_path))]
    assert found == ["manim-kit/manim_kit.py"]


def test_walk_discoverer_prunes_the_directories_that_are_not_tools(tmp_path: Path):
    from cli_tools_kit.gui_installer import _walk_tools_discoverer

    _tool(tmp_path / "keeper")
    for skipped in (".venv", "venv", ".git", "node_modules", "__pycache__",
                    "vendor-G2", "out", "cache", "build", "dist", "archive",
                    ".hidden"):
        _tool(tmp_path / skipped)
        _tool(tmp_path / skipped / "inner")

    found = [os.path.relpath(p, tmp_path).replace(os.sep, "/") for p, _ in _walk_tools_discoverer(str(tmp_path))]
    assert found == ["keeper/main.py"]


def test_walk_discoverer_stops_at_four_levels(tmp_path: Path):
    from cli_tools_kit.gui_installer import _walk_tools_discoverer

    _tool(tmp_path / "a" / "b" / "c" / "deep")          # depth 4, found
    _tool(tmp_path / "a" / "b" / "c" / "d" / "deeper")  # depth 5, not found

    found = [os.path.relpath(p, tmp_path).replace(os.sep, "/") for p, _ in _walk_tools_discoverer(str(tmp_path))]
    assert found == ["a/b/c/deep/main.py"]


def test_walk_discoverer_takes_the_names_a_wrapper_prunes(tmp_path: Path, monkeypatch):
    """run(prune=[...]) fills EXTRA_PRUNE, which extends the default set."""
    import cli_tools_kit.gui_installer as gi

    _tool(tmp_path / "keeper")
    _tool(tmp_path / "web")
    _tool(tmp_path / "build")          # a default prune name, still pruned

    monkeypatch.setattr(gi, "EXTRA_PRUNE", {"web"})
    found = [os.path.relpath(p, tmp_path).replace(os.sep, "/")
             for p, _ in gi._walk_tools_discoverer(str(tmp_path))]
    assert found == ["keeper/main.py"]


def test_walk_discoverer_is_the_default_only_with_discovery_roots(tmp_path: Path,
                                                                  monkeypatch):
    import cli_tools_kit.gui_installer as gi

    seen = []
    monkeypatch.setattr(gi, "get_metadata_native",
                        lambda entry, category: seen.append((entry, category)) or [])
    _tool(tmp_path / "tools" / "faullm")

    monkeypatch.setattr(gi, "ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(gi, "DISCOVERY_ROOTS", [])
    gi.discover_tools()
    assert seen == []          # the flat/tools_* walk does not reach that deep

    monkeypatch.setattr(gi, "DISCOVERY_ROOTS", [str(tmp_path)])
    gi.discover_tools()
    assert seen == [(str(tmp_path / "tools" / "faullm" / "main.py"), "tools")]
