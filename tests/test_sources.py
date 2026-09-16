"""Tests for the sources feature (load_sources / resolve_sources / run_installer)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cli_tools_kit import sources
from cli_tools_kit.sources import Source, load_sources, resolve_sources


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _repo(path: Path) -> Path:
    """A directory that looks like a checkout."""
    (path / ".git").mkdir(parents=True, exist_ok=True)
    return path


def _fake_git(record: list, ok: bool = True):
    """A subprocess.run stand-in that records the command and creates the clone."""
    class Result:
        returncode = 0 if ok else 128
        stdout = ""
        stderr = "" if ok else "fatal: could not read Username\n"

    def run(cmd, **kwargs):
        record.append(cmd)
        if ok and "clone" in cmd:
            _repo(Path(cmd[-1]))
        return Result()

    return run


# --- loading and layering ---------------------------------------------------

def test_loads_name_url_and_relative_path(tmp_path: Path) -> None:
    config = _write(tmp_path / "installer.toml", """
[[source]]
name = "org/tools"
path = "."

[[source]]
name = "org/lab"
url = "https://example.invalid/lab.git"
""")
    loaded = load_sources(config)
    assert [s.name for s in loaded] == ["org/tools", "org/lab"]
    assert loaded[0].path == str(tmp_path)
    assert loaded[0].url is None
    assert loaded[1].path is None
    assert loaded[1].url == "https://example.invalid/lab.git"


def test_local_file_adds_and_replaces_paths(tmp_path: Path) -> None:
    config = _write(tmp_path / "installer.toml", """
[[source]]
name = "org/tools"
path = "checkout"

[[source]]
name = "org/lab"
url = "https://example.invalid/lab.git"
""")
    _write(tmp_path / "installer.local.toml", f"""
root = "{tmp_path / 'elsewhere'}"

[[source]]
name = "org/tools"
path = "other"

[[source]]
name = "org/lab"
path = "{tmp_path / 'lab'}"
""")
    loaded = load_sources(config)
    assert loaded[0].path == str(tmp_path / "other")   # replaced
    assert loaded[1].path == str(tmp_path / "lab")     # added
    assert loaded[1].url == "https://example.invalid/lab.git"  # url is kept
    assert sources.local_root(config) == str(tmp_path / "elsewhere")


def test_missing_and_malformed_files_are_empty(tmp_path: Path) -> None:
    assert load_sources(tmp_path / "absent.toml") == []
    broken = _write(tmp_path / "installer.toml", "[[source]\nname =")
    assert load_sources(broken) == []


def test_source_without_a_name_is_dropped(tmp_path: Path) -> None:
    config = _write(tmp_path / "installer.toml", """
[[source]]
url = "https://example.invalid/x.git"

[[source]]
name = "keep"
path = "."
""")
    assert [s.name for s in load_sources(config)] == ["keep"]


# --- resolution order -------------------------------------------------------

def test_path_wins_over_root_checkout(tmp_path: Path) -> None:
    given = _repo(tmp_path / "given")
    _repo(tmp_path / "root" / "org" / "lab")
    source = Source(name="org/lab", url="https://example.invalid/lab.git",
                    path=str(given))
    assert resolve_sources([source], tmp_path / "root") == [given]


def test_existing_root_checkout_is_used_without_cloning(tmp_path: Path,
                                                        monkeypatch) -> None:
    target = _repo(tmp_path / "root" / "org" / "lab")
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    source = Source(name="org/lab", url="https://example.invalid/lab.git")
    assert resolve_sources([source], tmp_path / "root") == [target]
    assert calls == []


def test_missing_path_falls_through_to_the_clone(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "root").mkdir()
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    source = Source(name="org/lab", url="https://example.invalid/lab.git",
                    path=str(tmp_path / "gone"))
    assert resolve_sources([source], tmp_path / "root") == [tmp_path / "root" / "org" / "lab"]
    assert calls[0][:2] == ["git", "-c"]
    assert "clone" in calls[0]
    # The transports that turn a fetch into code execution are off.
    assert "protocol.ext.allow=never" in calls[0]
    assert "protocol.file.allow=never" in calls[0]
    assert "core.hooksPath=/dev/null" in calls[0]


def test_refresh_pulls_a_clone_ff_only(tmp_path: Path, monkeypatch) -> None:
    _repo(tmp_path / "root" / "org" / "lab")
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    source = Source(name="org/lab", url="https://example.invalid/lab.git")
    resolve_sources([source], tmp_path / "root", refresh=True)
    assert calls[0][-4:] == ["-C", str(tmp_path / "root" / "org" / "lab"),
                             "pull", "--ff-only"]
    assert "clone" not in calls[0]


def test_a_failed_pull_keeps_the_clone_as_a_discovery_root(tmp_path: Path,
                                                           monkeypatch) -> None:
    """A remote that is gone, a network that is down, a diverged history.

    One line about it, and the checkout that is already on disk is still used.
    """
    target = _repo(tmp_path / "root" / "org" / "lab")

    class Result:
        returncode = 128
        stdout = ""
        stderr = ("fatal: repository 'https://example.invalid/lab.git/' not found\n")

    monkeypatch.setattr(sources.subprocess, "run", lambda cmd, **kw: Result())
    lines: list = []
    source = Source(name="org/lab", url="https://example.invalid/lab.git")
    assert resolve_sources([source], tmp_path / "root", refresh=True,
                           log=lines.append) == [target]
    assert target.is_dir()
    assert len(lines) == 1
    assert "not found" in lines[0]


def test_a_checkout_given_by_path_is_never_pulled(tmp_path: Path, monkeypatch) -> None:
    given = _repo(tmp_path / "given")
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    source = Source(name="org/lab", url="https://example.invalid/lab.git", path=str(given))
    resolve_sources([source], tmp_path / "root", refresh=True)
    assert calls == []


def test_clone_false_never_reaches_git(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "root").mkdir()
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    source = Source(name="org/lab", url="https://example.invalid/lab.git")
    assert resolve_sources([source], tmp_path / "root", clone=False) == []
    assert calls == []


# --- refusals ---------------------------------------------------------------

@pytest.mark.parametrize("url", ["http://example.invalid/lab.git",
                                 "git@example.invalid:org/lab.git",
                                 "file:///tmp/lab",
                                 "ext::sh -c whoami"])
def test_only_https_urls_are_cloned(tmp_path: Path, monkeypatch, url: str) -> None:
    (tmp_path / "root").mkdir()
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    lines: list = []
    assert resolve_sources([Source(name="lab", url=url)], tmp_path / "root",
                           log=lines.append) == []
    assert calls == []
    assert any("not an https:// URL" in line for line in lines)


def test_a_failed_clone_skips_only_that_source(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "root").mkdir()
    good = _repo(tmp_path / "good")
    monkeypatch.setattr(sources.subprocess, "run", _fake_git([], ok=False))
    lines: list = []
    resolved = resolve_sources(
        [Source(name="private", url="https://example.invalid/private.git"),
         Source(name="good", path=str(good))],
        tmp_path / "root", log=lines.append)
    assert resolved == [good]
    assert any("fatal: could not read Username" in line for line in lines)


def test_no_clone_into_a_root_that_does_not_exist(tmp_path: Path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    lines: list = []
    source = Source(name="lab", url="https://example.invalid/lab.git")
    assert resolve_sources([source], tmp_path / "absent", log=lines.append) == []
    assert calls == []
    assert any("--root DIR" in line for line in lines)


def test_no_clone_into_a_root_that_cannot_be_written(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o500)
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    try:
        source = Source(name="lab", url="https://example.invalid/lab.git")
        assert resolve_sources([source], root, log=lambda *_: None) == []
        assert calls == []
    finally:
        root.chmod(0o700)


# --- recursion --------------------------------------------------------------

def test_a_nested_installer_toml_contributes_its_sources(tmp_path: Path) -> None:
    inner = _repo(tmp_path / "inner")
    deep = _repo(tmp_path / "deep")
    outer = _repo(tmp_path / "outer")
    _write(outer / "installer.toml", f"""
[[source]]
name = "inner"
path = "{inner}"
""")
    _write(inner / "installer.toml", f"""
[[source]]
name = "deep"
path = "{deep}"
""")
    config = _write(tmp_path / "installer.toml", f"""
[[source]]
name = "outer"
path = "{outer}"
""")
    # outer -> inner is one level; inner -> deep is one level too far.
    assert resolve_sources(load_sources(config), tmp_path / "root") == [outer, inner]


def test_recursion_does_not_loop_back(tmp_path: Path) -> None:
    a = _repo(tmp_path / "a")
    b = _repo(tmp_path / "b")
    _write(a / "installer.toml", f'[[source]]\nname = "b"\npath = "{b}"\n')
    _write(b / "installer.toml", f'[[source]]\nname = "a"\npath = "{a}"\n')
    config = _write(tmp_path / "installer.toml", f'[[source]]\nname = "a"\npath = "{a}"\n')
    assert resolve_sources(load_sources(config), tmp_path / "root") == [a, b]


def test_the_same_path_is_listed_once(tmp_path: Path) -> None:
    shared = _repo(tmp_path / "shared")
    resolved = resolve_sources([Source(name="one", path=str(shared)),
                                Source(name="two", path=str(shared))],
                               tmp_path / "root")
    assert resolved == [shared]


# --- run_installer ----------------------------------------------------------

@pytest.fixture
def engine(monkeypatch):
    """Capture the keyword arguments run_installer hands to the engine."""
    from cli_tools_kit import gui_installer

    captured: dict = {}

    def fake_run(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(gui_installer, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["installer.py"])
    return captured


def _tree(tmp_path: Path) -> Path:
    """<tmp>/org/tools/installer.toml, so the default root is <tmp>."""
    tools = _repo(tmp_path / "org" / "tools")
    return _write(tools / "installer.toml",
                  '[[source]]\nname = "org/tools"\npath = "."\n')


def test_run_installer_wires_roots_and_the_hook(tmp_path: Path, engine,
                                                monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    sources.run_installer(config, argv=["--list", "--root", str(tmp_path)])
    assert engine["root_dir"] == str(tmp_path)
    assert engine["discovery_roots"] == [str(tmp_path / "org" / "tools")]
    assert callable(engine["pre_discovery"])
    assert sys.argv[1:] == ["--list"]          # the engine's own flags survive


def test_root_flag_wins_and_is_consumed(tmp_path: Path, engine) -> None:
    config = _tree(tmp_path)
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    sources.run_installer(config, argv=["--root", str(chosen), "--apply", "all"])
    assert engine["root_dir"] == str(chosen)
    assert sys.argv[1:] == ["--apply", "all"]

    sources.run_installer(config, argv=[f"--root={chosen}", "--check"])
    assert engine["root_dir"] == str(chosen)
    assert sys.argv[1:] == ["--check"]


def test_local_root_is_the_default_when_no_flag(tmp_path: Path, engine,
                                                never_asks) -> None:
    config = _tree(tmp_path)
    _write(config.with_name("installer.local.toml"), f'root = "{tmp_path / "here"}"\n')
    sources.run_installer(config, argv=[])
    assert engine["root_dir"] == str(tmp_path / "here")


def test_the_hook_resolves_and_refills_the_roots(tmp_path: Path, engine,
                                                 monkeypatch) -> None:
    config = _tree(tmp_path)
    _write(config, f"""
[[source]]
name = "org/tools"
path = "."

[[source]]
name = "org/lab"
url = "https://example.invalid/lab.git"
""")
    calls: list = []
    monkeypatch.setattr(sources.subprocess, "run", _fake_git(calls))
    sources.run_installer(config, argv=["--root", str(tmp_path)])
    roots = engine["discovery_roots"]
    # Before the hook: only what is on disk, so --check never needs the network.
    assert roots == [str(tmp_path / "org" / "tools")]
    engine["pre_discovery"](False)
    assert roots == [str(tmp_path / "org" / "tools"), str(tmp_path / "org" / "lab")]
    assert any("clone" in call for call in calls)


# --- where the tools are installed ------------------------------------------

def _refuse(default):
    raise AssertionError(f"asked for a root, suggesting {default}")


@pytest.fixture
def never_asks(monkeypatch):
    """Fail the test if either prompt is reached."""
    monkeypatch.setattr(sources, "_ask_root_gui", _refuse)
    monkeypatch.setattr(sources, "_ask_root_stdin", _refuse)


def _answers(monkeypatch, answer, gui: bool = True):
    """Route the question to one screen and answer it. Returns the suggestions."""
    seen: list = []

    def ask(default):
        seen.append(default)
        return answer

    monkeypatch.setattr(sources, "_wants_gui", lambda argv: gui)
    monkeypatch.setattr(sources, "_ask_root_gui" if gui else "_ask_root_stdin", ask)
    if not gui:
        monkeypatch.setattr(sources.sys, "stdin",
                            type("Tty", (), {"isatty": staticmethod(lambda: True)})())
    return seen


def test_the_root_flag_is_used_without_asking(tmp_path: Path, engine, never_asks,
                                              monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    sources.run_installer(config, argv=["--root", str(tmp_path / "chosen")])
    assert engine["root_dir"] == str(tmp_path / "chosen")
    assert (tmp_path / "chosen").is_dir()      # created for us
    # Nothing is remembered: the flag is for this run.
    assert not config.with_name("installer.local.toml").exists()


def test_the_answer_is_used_and_remembered(tmp_path: Path, engine, monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    chosen = tmp_path / "picked"
    suggested = _answers(monkeypatch, str(chosen))

    sources.run_installer(config, argv=[])
    assert engine["root_dir"] == str(chosen)
    assert chosen.is_dir()
    assert suggested == [str(tmp_path / "tools")]   # <cwd>/<default_root_name>
    assert sources.local_root(config) == str(chosen)

    # Asked once: the second run reads the file.
    monkeypatch.setattr(sources, "_ask_root_gui", _refuse)
    sources.run_installer(config, argv=[])
    assert engine["root_dir"] == str(chosen)


def test_the_answer_keeps_the_local_files_sources(tmp_path: Path, engine,
                                                  monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    local = _write(config.with_name("installer.local.toml"),
                   f'[[source]]\nname = "org/tools"\npath = "{tmp_path / "other"}"\n')
    _answers(monkeypatch, str(tmp_path / "picked"))
    sources.run_installer(config, argv=[])
    assert sources.local_root(config) == str(tmp_path / "picked")
    assert load_sources(config)[0].path == str(tmp_path / "other")
    assert '[[source]]' in local.read_text(encoding="utf-8")


def test_an_existing_root_in_the_local_file_is_never_overwritten(tmp_path: Path) -> None:
    config = _write(tmp_path / "installer.toml", "")
    _write(config.with_name("installer.local.toml"), f'root = "{tmp_path / "mine"}"\n')
    assert sources.save_local_root(config, str(tmp_path / "other")) is False
    assert sources.local_root(config) == str(tmp_path / "mine")


def test_a_windows_root_survives_the_round_trip(tmp_path: Path) -> None:
    """A backslash path must not be written as a TOML basic string.

    ``root = "C:\\Users\\..."`` reads the \\U as a Unicode escape and kills the
    whole file, so the install location was asked for again on every launch.
    """
    roots = [r"C:\Users\tester\WW3-tools", r"C:\temp\new\table\unicode",
             r"C:\Users\o'brien\tools"]
    for i, root in enumerate(roots):
        config = _write(tmp_path / str(i) / "installer.toml", "")
        assert sources.save_local_root(config, root, log=lambda *a: None)
        local = config.with_name("installer.local.toml")
        assert sources._read_toml(local).get("root") == root


def test_the_default_root_name_names_the_suggested_folder(tmp_path: Path, engine,
                                                          monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    suggested = _answers(monkeypatch, str(tmp_path / "picked"))
    sources.run_installer(config, argv=[], default_root_name="WW3-tools")
    assert suggested == [str(tmp_path / "WW3-tools")]


def test_cancelling_the_question_stops_cleanly(tmp_path: Path, engine,
                                               monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    _answers(monkeypatch, None)
    with pytest.raises(SystemExit) as exit_info:
        sources.run_installer(config, argv=[])
    assert exit_info.value.code == 0
    assert engine == {}                        # the installer never opened


def test_the_text_screen_asks_on_stdin(tmp_path: Path, engine, monkeypatch) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    suggested = _answers(monkeypatch, str(tmp_path / "picked"), gui=False)
    sources.run_installer(config, argv=["--tui"])
    assert suggested == [str(tmp_path / "tools")]
    assert engine["root_dir"] == str(tmp_path / "picked")


@pytest.mark.parametrize("argv", [["--list"], ["--check"], ["--apply", "all"],
                                  ["--apply=all"]])
def test_a_headless_run_takes_the_default_and_says_so(tmp_path: Path, engine,
                                                      never_asks, monkeypatch,
                                                      capsys, argv) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    sources.run_installer(config, argv=list(argv))
    assert engine["root_dir"] == str(tmp_path / "tools")
    assert (f"root: {tmp_path / 'tools'} (pass --root to change)"
            in capsys.readouterr().out)
    # A default is not an answer, so it is not written to the local file.
    assert not config.with_name("installer.local.toml").exists()


def test_no_question_without_a_terminal(tmp_path: Path, engine, never_asks,
                                        monkeypatch, capsys) -> None:
    config = _tree(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sources, "_wants_gui", lambda argv: False)
    monkeypatch.setattr(sources.sys, "stdin",
                        type("Pipe", (), {"isatty": staticmethod(lambda: False)})())
    sources.run_installer(config, argv=[])
    assert engine["root_dir"] == str(tmp_path / "tools")
    assert "pass --root to change" in capsys.readouterr().out


def test_run_installer_refuses_to_have_its_own_arguments_overridden(tmp_path: Path,
                                                                   engine) -> None:
    config = _tree(tmp_path)
    with pytest.raises(TypeError):
        sources.run_installer(config, argv=[], discovery_roots=["/x"])
    with pytest.raises(TypeError):
        sources.run_installer(config, argv=[], pre_discovery=lambda refresh: None)
