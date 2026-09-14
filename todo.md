# todo — cli-tools-kit

## Roll the autostart contract out to the consumers (opened 2026-09-14)

§ Autostart in [`PROTOCOL.md`](PROTOCOL.md) and § "Working on a consumer" in
[`CLAUDE.md`](CLAUDE.md) landed today. They document what was already true in
`gui_installer.py` — the contract simply had never been written down, which is
why `lernclaude` hand-rolled a login entry before reverting it.

Nothing is broken in the consumers; this is alignment work, one repo at a time,
each its own commit in its own repo.

### Triage

Seven consumers advertise and install. Autostart state as of 2026-09-14:

| Consumer | Tags | `default_autostart` | CLAUDE.md | What it needs |
|---|---|---|---|---|
| `lernclaude` | CLI+Icon | yes (symlink) | yes | done — the reference case |
| `quizhub-client` | CLI | yes (cron) | yes | check its two `@reboot` lines go through `CronInstaller`, not a hand-rolled crontab edit |
| `studon-client` | CLI | no | yes | registers two `@reboot` crons **and** writes a `studon-client()` function straight into `~/.bashrc` — the known parallel-install wart in the umbrella CLAUDE.md. Decide: fold the helper into the kit's alias mechanism, or record why it cannot be |
| `bloggen` | — | no | yes | skill registration only; confirm it follows `skill_name` + `--install-skill` |
| `manim-kit` | — | no | yes | same check as bloggen |
| `belegungen-watcher` | CLI | no | **none** | a `notify-send` watcher, so a plausible `cron_schedule` candidate — ask before adding one |
| `fau-courses` | CLI | no | **none** | search tool, no autostart wanted; nothing to do beyond the pointer if we add one |

### Order

1. `studon-client` — the only real divergence, and the one the umbrella already
   flags. Worth doing first because it is the precedent the others cite.
2. `quizhub-client` — verify the cron path, cheap.
3. `bloggen`, `manim-kit` — skill-registration check, cheap.
4. `belegungen-watcher`, `fau-courses` — only if we decide doc-light repos
   should carry a pointer at all. The user deliberately left them without a
   CLAUDE.md; creating one is a decision, not a cleanup.

### Notes

- A pointer belongs only in genuine consumers. `FAU-Studium-References`,
  `LatexLabworksTemplate`, `lecture-recorder` and `ssh_help` have a CLAUDE.md
  but do not use the kit — leave them alone.
- Keep the pointer short and identical wherever it goes: one line naming
  `PROTOCOL.md` as the contract. The detail stays here, in one place.
- The kit's own `origin` is GitHub, so its commits wait for an explicit push.

### Handoff (2026-09-14, session ended here)

Docs are committed in this repo, unpushed (`origin` is GitHub, push is the
user's call). Nothing else was touched today.

**Unverified claims in the triage table above — check before acting on them:**

- `quizhub-client`: the "CLI / cron / yes" row came from a single grep hit for
  `default_autostart`. Its tags and whether its two `@reboot` lines go through
  `CronInstaller` or a raw crontab edit were never confirmed. Verify first.
- `bloggen` / `manim-kit` tags are likewise unconfirmed (shown as "—").
- The kit's pytest suite was NOT run after these markdown edits.

**Already true and verified:** `lernclaude` is the reference case — it advertises
`default_autostart: True`, the kit's `enable_autostart` symlinked
`~/.config/autostart/lernclaude.desktop` → the installed `.desktop`, 18 tests
green, pushed to its `gitea` remote (`1ea4f63`). Its own CLAUDE.md records the
reverted hand-rolled attempt so nobody repeats it.
