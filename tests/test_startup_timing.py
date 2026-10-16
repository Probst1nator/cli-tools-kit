"""Tests for --startup-time: phase recording, the report table, the relaunched runs."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cli_tools_kit import startup_timing


@pytest.fixture
def measuring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put this process in the state of a measured run, with a fresh record."""
    report = tmp_path / "report.json"
    monkeypatch.setattr(startup_timing, "_REPORT_PATH", str(report))
    monkeypatch.setattr(startup_timing, "ACTIVE", True)
    monkeypatch.setattr(startup_timing, "_phases", [])
    monkeypatch.setattr(startup_timing, "_stack", [])
    monkeypatch.setattr(startup_timing, "_totals", {})
    monkeypatch.setattr(startup_timing, "_probes", [])
    monkeypatch.setattr(startup_timing, "_info", {})
    monkeypatch.setattr(startup_timing, "_written", False)
    return report


def test_nothing_is_recorded_outside_a_measured_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(startup_timing, "ACTIVE", False)
    monkeypatch.setattr(startup_timing, "_phases", [])
    with startup_timing.phase("discovery"), startup_timing.total("icons"):
        pass
    assert startup_timing._phases == []


def test_phases_nest_and_totals_add_up(measuring: Path) -> None:
    with startup_timing.phase("window"):
        with startup_timing.phase("rows"):
            for _ in range(3):
                with startup_timing.total("icons"):
                    pass
    with startup_timing.probe("/tree/jarvis/main.py"):
        pass
    startup_timing.write_report()

    report = json.loads(measuring.read_text(encoding="utf-8"))
    keys = [(p["key"], p["depth"]) for p in report["phases"]]
    assert keys == [("window", 0), ("window > rows", 1), ("window > rows > icons", 2)]
    assert report["phases"][2]["calls"] == 3
    assert [name for name, _ in report["info"]["probes"]] == ["jarvis"]


def _report(**seconds: float) -> dict:
    """A run with a 'window' phase made of the given parts, back to back."""
    phases, start = [], 0.0
    window = {"key": "window", "label": "window", "depth": 0, "start": 0.0, "end": 0.0}
    phases.append(window)
    for label, length in seconds.items():
        phases.append({"key": f"window > {label}", "label": label, "depth": 1,
                       "start": start, "end": start + length})
        start += length
    window["end"] = start + 0.5          # 0.5 s that no part accounts for
    return {"phases": phases, "info": {"title": "Test installer", "tools": 2,
                                       "probes": [["jarvis", 0.4], ["clip", 0.2]],
                                       "probe_workers": 2}}


def test_the_table_has_the_first_run_the_median_and_the_rest() -> None:
    text = startup_timing.format_report([_report(rows=1.0), _report(rows=2.0),
                                         _report(rows=4.0)])
    assert "Startup of Test installer: 3 run(s)" in text
    rows = next(line for line in text.splitlines() if line.strip().startswith("rows"))
    assert rows.split()[1:] == ["1.00", "3.00", "2.00-4.00"]
    other = next(line for line in text.splitlines() if line.strip().startswith("other"))
    assert other.split()[1] == "0.50"
    assert "Slowest probes (median over all runs): jarvis 0.40, clip 0.20" in text


def test_benchmark_runs_new_processes_and_prints_their_phases() -> None:
    child = ("from cli_tools_kit import startup_timing as st\n"
             "with st.phase('discovery'):\n"
             "    pass\n"
             "st.info(title='Child installer')\n")
    out = io.StringIO()
    assert startup_timing.benchmark(2, argv=[sys.executable, "-c", child], out=out) == 0
    text = out.getvalue()
    assert "Startup of Child installer: 2 run(s)" in text
    assert any(line.startswith("discovery") for line in text.splitlines())


def test_a_measured_run_does_not_pass_the_measurement_on(tmp_path: Path) -> None:
    """The tools' --advertise processes inherit the environment; with the report
    path still in it they wrote an empty report over the installer's."""
    child = ("import os\n"
             "from cli_tools_kit import startup_timing as st\n"
             "print(st.ACTIVE, os.environ.get(st.ENV_REPORT), os.environ.get(st.ENV_T0))\n")
    env = dict(os.environ, **{startup_timing.ENV_REPORT: str(tmp_path / "r.json"),
                              startup_timing.ENV_T0: "1.0"})
    out = subprocess.run([sys.executable, "-c", child], env=env, capture_output=True,
                         text=True, check=True).stdout
    assert out.split() == ["True", "None", "None"]


def test_benchmark_fails_when_a_run_reports_nothing(capsys: pytest.CaptureFixture) -> None:
    assert startup_timing.benchmark(1, argv=[sys.executable, "-c", "pass"],
                                    out=io.StringIO()) == 1
    assert "wrote no timings" in capsys.readouterr().err
