"""The icon theme lookup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli_tools_kit import icons


@pytest.mark.parametrize("kdeglobals, expected", [
    ("[Sounds]\nTheme=ocean\n", "hicolor"),
    ("[Sounds]\nTheme=ocean\n\n[Icons]\nTheme=breeze-dark\n", "breeze-dark"),
])
def test_kde_icon_theme_comes_from_the_icons_group(kdeglobals: str, expected: str,
                                                   sandbox_home: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    def no_gsettings(*args, **kwargs):
        raise FileNotFoundError("gsettings")
    monkeypatch.setattr(subprocess, "run", no_gsettings)
    (sandbox_home / ".config").mkdir(exist_ok=True)
    (sandbox_home / ".config" / "kdeglobals").write_text(kdeglobals, encoding="utf-8")
    icons.clear_cache()
    try:
        assert icons._get_current_icon_theme() == expected
    finally:
        icons.clear_cache()


def test_an_icon_in_a_status_or_applets_directory_is_found(sandbox_home: Path,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(icons, "_get_current_icon_theme", lambda: "kit-test-theme")
    found = {}
    for category in ("status", "applets"):
        icon = (sandbox_home / ".local" / "share" / "icons" / "kit-test-theme" / category
                / "48" / f"kit-test-{category}.svg")
        icon.parent.mkdir(parents=True)
        icon.write_text("<svg/>", encoding="utf-8")
        found[category] = icon
    try:
        for category, icon in found.items():
            assert icons.find_icon_path(f"kit-test-{category}", 24) == str(icon)
    finally:
        icons._is_icon_dir.cache_clear()


def test_an_svg_is_rendered_once_until_it_changes(sandbox_home: Path, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    Image = pytest.importorskip("PIL.Image")
    svg = tmp_path / "icon.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    renders = []
    renderer = {"name": "rsvg-convert"}

    def render(path, size):
        renders.append(path)
        buf = io.BytesIO()
        Image.new("RGBA", (size, size), (255, 0, 0, 255)).save(buf, "PNG")
        return buf.getvalue(), renderer["name"]

    monkeypatch.setattr(icons, "_render_svg", render)
    first = icons._cached_svg(str(svg), 24)
    second = icons._cached_svg(str(svg), 24)
    assert len(renders) == 1
    assert second.size == first.size == (24, 24)
    assert second.getpixel((0, 0)) == (255, 0, 0, 255)

    svg.write_text("<svg></svg>", encoding="utf-8")   # a changed file is a new key
    icons._cached_svg(str(svg), 24)
    assert len(renders) == 2

    # ImageMagick's colours can be off, so its output is rendered again next time.
    renderer["name"] = "convert"
    icons._cached_svg(str(svg), 32)
    icons._cached_svg(str(svg), 32)
    assert len(renders) == 4


def test_a_missing_svg_renderer_falls_through_to_the_next(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    import sys

    Image = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    Image.new("RGBA", (8, 8)).save(buf, "PNG")
    tried = []

    def run(cmd, **kwargs):
        tried.append(cmd[0])
        if cmd[0] == "rsvg-convert":
            raise FileNotFoundError(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, buf.getvalue(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setitem(sys.modules, "cairosvg", None)   # import cairosvg raises
    img = icons._rasterize_svg("/nowhere/icon.svg", 8)
    assert img is not None and img.size == (8, 8)
    assert tried == ["rsvg-convert", "convert"]
