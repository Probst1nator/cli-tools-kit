# todo — cli-tools-kit

Follow-ups from moving studon-client onto `CronInstaller` + alias (studon-client `380cece`, 2026-09-22):

- `CronInstaller` has no public way to adopt or remove unmarked legacy cron lines. studon-client's migration calls the private `_read`/`_write`. Add a public method if a second consumer needs it.
- The `CHECK_RECONCILE_SHORTCUTS` comment in `gui_installer.py` and the `installer.py` docstring still describe studon-client's "~/.bashrc function". Update both.
- studon-client's `--install` still asks questions interactively (it ends in the `--map-lectures` wizard). The GUI parent runs it without a terminal.
