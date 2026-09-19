"""Tests for venvs — one venv per upstream, shared across installers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli_tools_kit import venvs


def _pip_calls(monkeypatch: pytest.MonkeyPatch, tool: Path) -> list:
    """Run ensure_venv with pip stubbed out, returning the pip commands it ran.

    Only pip is intercepted: the git calls that resolve the upstream key have
    to keep working, or the venv would not resolve to the shared location the
    test just prepared.
    """
    real_run = subprocess.run
    calls = []

    def fake_run(cmd, *a, **k):
        if "pip" in cmd:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(venvs.subprocess, "run", fake_run)
    venvs.ensure_venv(str(tool))
    return calls


def _checkout(home: Path, name: str, origin: str | None,
              requirements: str = "somedep==1.0\n") -> Path:
    """A tool directory, optionally a git checkout with an origin remote."""
    tool_dir = home / name
    tool_dir.mkdir(parents=True)
    (tool_dir / "requirements.txt").write_text(requirements)
    if origin is not None:
        subprocess.run(["git", "init", "-q", str(tool_dir)], check=True)
        subprocess.run(["git", "-C", str(tool_dir), "remote", "add",
                        "origin", origin], check=True)
    return tool_dir


# --- key derivation ----------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("git@github.com:Org/tool.git", "github.com/org/tool"),
    ("git@github.com:Org/tool", "github.com/org/tool"),
    ("https://github.com/Org/tool.git", "github.com/org/tool"),
    ("https://github.com/Org/tool", "github.com/org/tool"),
    ("https://user@github.com/Org/tool/", "github.com/org/tool"),
    ("ssh://git@github.com/Org/tool.git", "github.com/org/tool"),
])
def test_normalise_remote_agrees_across_spellings(url: str, expected: str) -> None:
    assert venvs.normalise_remote(url) == expected


@pytest.mark.parametrize("url", ["", "   ", "not a url"])
def test_normalise_remote_rejects_non_remotes(url: str) -> None:
    assert venvs.normalise_remote(url) is None


def test_local_path_remotes_agree(tmp_path: Path) -> None:
    """A bare repo on disk is an upstream too — two clones of it should share."""
    bare = tmp_path / "upstream.git"
    bare.mkdir()
    direct = venvs.normalise_remote(str(bare))
    assert direct is not None
    assert venvs.normalise_remote(f"file://{bare}") == direct
    assert venvs.normalise_remote(str(tmp_path / "." / "upstream.git")) == direct


def test_different_repos_get_different_keys(sandbox_home: Path) -> None:
    a = _checkout(sandbox_home, "a", "git@github.com:Org/one.git")
    b = _checkout(sandbox_home, "b", "git@github.com:Org/two.git")
    assert venvs.upstream_key(str(a)) != venvs.upstream_key(str(b))


def test_key_is_none_without_origin(sandbox_home: Path) -> None:
    plain = _checkout(sandbox_home, "plain", None)
    assert venvs.upstream_key(str(plain)) is None


# --- the point of the module -------------------------------------------------------

def test_two_checkouts_of_one_upstream_share_a_venv(sandbox_home: Path) -> None:
    """The whole reason this module exists: install once, reuse everywhere."""
    first = _checkout(sandbox_home, "installer-a/manim-kit",
                      "git@github.com:Org/manim-kit.git")
    second = _checkout(sandbox_home, "installer-b/manim-kit",
                       "https://github.com/Org/manim-kit")
    assert venvs.venv_for(str(first)) == venvs.venv_for(str(second))


def test_checkout_without_origin_keeps_a_private_venv(sandbox_home: Path) -> None:
    plain = _checkout(sandbox_home, "plain", None)
    assert venvs.venv_for(str(plain)) == str(plain / ".venv")


def test_shared_venv_lives_outside_both_checkouts(sandbox_home: Path) -> None:
    tool = _checkout(sandbox_home, "installer-a/tool", "git@github.com:Org/tool.git")
    assert not venvs.venv_for(str(tool)).startswith(str(tool))


# --- requirements --------------------------------------------------------------

def test_requirement_lines_skip_comments_and_blanks(tmp_path: Path) -> None:
    req = tmp_path / "requirements.txt"
    req.write_text("# a comment\n\nfoo==1.0\n  bar>=2  \n\n# trailing\n")
    assert venvs.requirement_lines(str(req)) == ["foo==1.0", "bar>=2"]


def test_requirement_lines_of_missing_file_is_empty(tmp_path: Path) -> None:
    assert venvs.requirement_lines(str(tmp_path / "nope.txt")) == []


def test_manifest_round_trip(sandbox_home: Path) -> None:
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git")
    venv_dir = sandbox_home / "venv"
    venv_dir.mkdir()
    venvs.write_manifest(str(venv_dir), ["foo==1.0"], str(tool))
    stored = venvs.read_manifest(str(venv_dir))
    assert stored["requirements"] == ["foo==1.0"]
    assert stored["written_by"] == str(tool)


def test_read_manifest_of_unwritten_venv_is_empty(tmp_path: Path) -> None:
    assert venvs.read_manifest(str(tmp_path)) == {}


def test_read_manifest_survives_corruption(tmp_path: Path) -> None:
    (tmp_path / venvs.MANIFEST_NAME).write_text("{not json")
    assert venvs.read_manifest(str(tmp_path)) == {}


# --- provisioning ------------------------------------------------------------------

def test_no_requirements_means_no_venv_is_built(sandbox_home: Path) -> None:
    """A tool with nothing to install gains nothing from a venv."""
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git",
                     requirements="# only a comment\n")
    import sys
    assert venvs.ensure_venv(str(tool)) == sys.executable
    assert not Path(venvs.venv_for(str(tool))).exists()


def test_provision_false_never_creates_anything(sandbox_home: Path) -> None:
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git")
    venvs.ensure_venv(str(tool), provision=False)
    assert not Path(venvs.venv_for(str(tool))).exists()


def test_matching_requirements_skip_reinstall(sandbox_home: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """The second installer must not re-run pip for an already-built venv."""
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git")
    venv_dir = Path(venvs.venv_for(str(tool)))
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("")
    venvs.write_manifest(str(venv_dir), ["somedep==1.0"], str(tool))

    assert _pip_calls(monkeypatch, tool) == []


def test_drifted_requirements_trigger_reinstall(sandbox_home: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkout at a different commit gets its own dependencies installed."""
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git",
                     requirements="somedep==2.0\n")
    venv_dir = Path(venvs.venv_for(str(tool)))
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("")
    venvs.write_manifest(str(venv_dir), ["somedep==1.0"], str(tool))

    assert _pip_calls(monkeypatch, tool) != []
    assert venvs.read_manifest(str(venv_dir))["requirements"] == ["somedep==2.0"]


def test_conflicting_pins_name_the_other_checkout(sandbox_home: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """When pip cannot satisfy both checkouts, say which one holds the venv."""
    other = _checkout(sandbox_home, "installer-a/tool", "git@github.com:Org/tool.git")
    tool = _checkout(sandbox_home, "installer-b/tool", "git@github.com:Org/tool.git",
                     requirements="somedep==2.0\n")
    venv_dir = Path(venvs.venv_for(str(tool)))
    (venv_dir / "bin").mkdir(parents=True)
    (venv_dir / "bin" / "python3").write_text("")
    venvs.write_manifest(str(venv_dir), ["somedep==1.0"], str(other))

    real_run = subprocess.run

    def fail_pip(cmd, *a, **k):
        if "pip" in cmd:
            raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(venvs.subprocess, "run", fail_pip)
    with pytest.raises(RuntimeError, match=str(other)):
        venvs.ensure_venv(str(tool))


# --- migration ---------------------------------------------------------------------

def test_existing_private_venv_is_adopted(sandbox_home: Path) -> None:
    """The 666M already on disk is reused, not downloaded again."""
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git")
    private = tool / ".venv"
    (private / "bin").mkdir(parents=True)
    (private / "bin" / "python3").write_text("")
    (private / "marker").write_text("original")

    shared = venvs.venv_for(str(tool))
    assert venvs.adopt_existing(str(tool), shared) is True
    assert (Path(shared) / "marker").read_text() == "original"
    assert not private.exists()


def test_adoption_does_not_clobber_an_existing_shared_venv(sandbox_home: Path) -> None:
    tool = _checkout(sandbox_home, "tool", "git@github.com:Org/tool.git")
    private = tool / ".venv"
    (private / "bin").mkdir(parents=True)
    (private / "bin" / "python3").write_text("")

    shared = Path(venvs.venv_for(str(tool)))
    shared.mkdir(parents=True)
    assert venvs.adopt_existing(str(tool), str(shared)) is False
    assert private.exists()


def test_stale_private_venvs_are_reported_not_removed(sandbox_home: Path) -> None:
    first = _checkout(sandbox_home, "installer-a/tool", "git@github.com:Org/tool.git")
    second = _checkout(sandbox_home, "installer-b/tool", "git@github.com:Org/tool.git")
    leftover = second / ".venv"
    leftover.mkdir()
    Path(venvs.venv_for(str(first))).mkdir(parents=True)

    stale = venvs.stale_private_venvs([str(first), str(second)])
    assert stale == [str(leftover)]
    assert leftover.exists()
