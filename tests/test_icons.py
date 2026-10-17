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
