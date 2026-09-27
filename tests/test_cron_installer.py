"""Tests for CronInstaller — atomic, idempotent, marker-scoped cron management."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_tools_kit import CronInstaller


def test_install_writes_lines_with_tag(fake_crontab: Path) -> None:
    cron = CronInstaller("my-tool")
    cron.install(["@reboot echo hi"])
    content = fake_crontab.read_text()
    assert "@reboot echo hi" in content
    assert "# cli-tool-kit:my-tool" in content


def test_install_is_idempotent(fake_crontab: Path) -> None:
    cron = CronInstaller("my-tool")
    cron.install(["@reboot echo hi"])
    first = fake_crontab.read_text()
    cron.install(["@reboot echo hi"])
    second = fake_crontab.read_text()
    assert first == second, "second install should not duplicate the line"


def test_install_replaces_old_lines_for_same_marker(fake_crontab: Path) -> None:
    cron = CronInstaller("my-tool")
    cron.install(["@reboot echo OLD"])
    cron.install(["@reboot echo NEW"])
    content = fake_crontab.read_text()
    assert "OLD" not in content
    assert "NEW" in content


def test_remove_leaves_other_tools_alone(fake_crontab: Path) -> None:
    # Pre-seed the crontab with unrelated entries.
    fake_crontab.write_text(
        "# user's own entry\n"
        "0 6 * * * /home/user/backup.sh\n"
        "@reboot /opt/other-tool/run.sh  # cli-tool-kit:other-tool\n"
    )
    cron = CronInstaller("my-tool")
    cron.install(["@reboot echo mine"])
    # remove only "my-tool"'s lines
    cron.remove()
    content = fake_crontab.read_text()
    assert "# user's own entry" in content
    assert "/home/user/backup.sh" in content
    assert "/opt/other-tool/run.sh" in content
    assert "cli-tool-kit:other-tool" in content
    assert "echo mine" not in content
    assert "cli-tool-kit:my-tool" not in content


def test_multiple_markers_coexist(fake_crontab: Path) -> None:
    a = CronInstaller("tool-a")
    b = CronInstaller("tool-b")
    a.install(["@reboot run-a"])
    b.install(["@reboot run-b"])
    content = fake_crontab.read_text()
    assert "run-a" in content
    assert "run-b" in content
    assert "cli-tool-kit:tool-a" in content
    assert "cli-tool-kit:tool-b" in content


def test_remove_one_marker_keeps_the_other(fake_crontab: Path) -> None:
    a = CronInstaller("tool-a")
    b = CronInstaller("tool-b")
    a.install(["@reboot run-a"])
    b.install(["@reboot run-b"])
    a.remove()
    content = fake_crontab.read_text()
    assert "run-a" not in content
    assert "run-b" in content


def test_remove_on_empty_crontab_is_noop(fake_crontab: Path) -> None:
    cron = CronInstaller("my-tool")
    cron.remove()  # must not raise
    # file should still not exist or be empty
    assert not fake_crontab.exists() or fake_crontab.read_text() == ""


def test_install_multiple_lines(fake_crontab: Path) -> None:
    cron = CronInstaller("multi")
    cron.install([
        "@reboot /path/script.py --daemon",
        "0 6 * * * /path/script.py --daily",
    ])
    content = fake_crontab.read_text()
    assert "--daemon" in content
    assert "--daily" in content
    # both lines should bear the tag
    tagged_lines = [
        l for l in content.splitlines() if "# cli-tool-kit:multi" in l
    ]
    assert len(tagged_lines) == 2


def test_is_installed_reflects_state(fake_crontab: Path) -> None:
    cron = CronInstaller("watcher")
    assert not cron.is_installed()
    cron.install(["@reboot watch"])
    assert cron.is_installed()
    cron.remove()
    assert not cron.is_installed()


def test_installed_lines_strips_tag(fake_crontab: Path) -> None:
    cron = CronInstaller("reader")
    cron.install(["@reboot do-thing", "*/5 * * * * other-thing"])
    lines = cron.installed_lines()
    assert lines == ["@reboot do-thing", "*/5 * * * * other-thing"]


def test_marker_rejects_newline() -> None:
    with pytest.raises(ValueError):
        CronInstaller("bad\nmarker")


def test_marker_rejects_hash() -> None:
    with pytest.raises(ValueError):
        CronInstaller("bad#marker")


def test_marker_rejects_empty() -> None:
    with pytest.raises(ValueError):
        CronInstaller("")


def test_install_rejects_line_with_existing_tag(fake_crontab: Path) -> None:
    cron = CronInstaller("self")
    with pytest.raises(ValueError):
        cron.install(["@reboot foo  # cli-tool-kit:self"])


def test_remove_unmarked_takes_only_untagged_matching_lines(fake_crontab: Path) -> None:
    fake_crontab.write_text(
        "@reboot python3 /old/studon_client.py --daily-sync\n"
        "0 3 * * * backup.sh\n"
    )
    cron = CronInstaller("studon-client")
    cron.install(["@reboot python3 /new/studon_client.py --daily-sync"])

    removed = cron.remove_unmarked(lambda line: "studon_client.py" in line)

    assert removed == 1
    lines = fake_crontab.read_text().splitlines()
    assert "0 3 * * * backup.sh" in lines
    # The tagged line matches the predicate too, and stays.
    assert cron.installed_lines() == ["@reboot python3 /new/studon_client.py --daily-sync"]


def test_remove_unmarked_leaves_the_crontab_alone_when_nothing_matches(fake_crontab: Path) -> None:
    fake_crontab.write_text("0 3 * * * backup.sh\n")
    assert CronInstaller("x").remove_unmarked(lambda line: False) == 0
    assert fake_crontab.read_text() == "0 3 * * * backup.sh\n"
