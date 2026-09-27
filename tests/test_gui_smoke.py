"""Build the tkinter window once with every kind of row.

The rest of the suite never constructs InstallerApp, so a NameError in its
row rendering shipped in 0.8.0 through 0.8.2 and crashed the installer at
startup. This test needs a display; CI runs it under xvfb, and it skips
where tkinter or a display is missing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

tkinter = pytest.importorskip("tkinter")

import cli_tools_kit.gui_installer as gi  # noqa: E402  (after the tkinter skip)


@pytest.fixture
def tk_root():
    try:
        root = tkinter.Tk()
    except tkinter.TclError as exc:
        pytest.skip(f"no display: {exc}")
    errors: list = []
    # Errors inside Tk callbacks are printed, not raised; collect them instead.
    root.report_callback_exception = lambda *exc: errors.append(exc)
    root.errors = errors
    yield root
    root.destroy()


def _entry(script: Path, name: str, **fields) -> gi.ToolEntry:
    base: dict = dict(name=name, desktop_file=f"{name.lower()}.desktop",
                      script_path=str(script), args=[], icon="utilities-terminal",
                      description=f"{name} for the smoke test", terminal=False,
                      category="Smoke", capability="misc")
    base.update(fields)
    return gi.ToolEntry(**base)


def test_window_builds_with_every_row_kind(sandbox_home: Path, tmp_path: Path, tk_root) -> None:
    def script(name: str) -> Path:
        path = tmp_path / name / "main.py"
        path.parent.mkdir()
        path.write_text("", encoding="utf-8")
        return path

    grouped = script("voice")
    tools = [
        _entry(script("plain"), "Plain", tags=["CLI"], alias="plain"),
        _entry(script("skilled"), "Skilled", tags=["CLI"], alias="skilled", skill_name="skilled"),
        _entry(script("cron"), "Cron", tags=["CLI"], alias="cron", cron_schedule="@reboot"),
        _entry(script("iconic"), "Iconic", tags=["GUI", "Icon"],
               autostart_conditions=["time_window"]),
        # Two entries on one script make a group: a parent row with a child.
        # The 0.8.0 crash was in exactly this path, for a parent with an
        # Auto-Start box and autostart conditions.
        _entry(grouped, "Voice", tags=["GUI", "Icon"], autostart_conditions=["network"]),
        _entry(grouped, "Voice (Private)", tags=["GUI", "Icon"], args=["--private"]),
    ]

    app = gi.InstallerApp(tk_root, tools)
    tk_root.update()

    assert tk_root.errors == []
    assert len(app.tools_by_key) == len(tools)
