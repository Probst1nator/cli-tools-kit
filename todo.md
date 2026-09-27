# todo — cli-tools-kit

Follow-ups from moving studon-client onto `CronInstaller` + alias (studon-client `380cece`, 2026-09-22):

- studon-client's legacy cron migration still calls the private `CronInstaller._read`/`_write`; switch it to `remove_unmarked(match)` (public since the 1.0 work).
- The `CHECK_RECONCILE_SHORTCUTS` comment in `gui_installer.py` and the `installer.py` docstring still describe studon-client's "~/.bashrc function". Update both.
- studon-client's `--install` still asks questions interactively (it ends in the `--map-lectures` wizard). The GUI parent runs it without a terminal.
