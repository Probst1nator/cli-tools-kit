"""Atomic, idempotent cron-line management with marker comments.

Each CronInstaller instance owns a unique marker string. `install()` rewrites
the crontab so any pre-existing lines bearing this marker are stripped, then
the new lines are appended with the marker as a trailing comment. Other tools'
cron lines (with different markers or none at all) are left untouched.

Example:

    from cli_tools_kit import CronInstaller

    cron = CronInstaller("studon-client")
    cron.install([
        f"@reboot cd {script_dir} && {python} {script} --daily-sync",
        f"@reboot cd {script_dir} && {python} {script} --lecture-sync",
    ])
    # ... later
    cron.remove()
"""

from __future__ import annotations

import subprocess
from typing import Callable, List

try:
    from termcolor import colored
except ImportError:
    def colored(text: str, *_args, **_kwargs) -> str:
        return text


# The marker on real crontabs since the first release. It keeps the old
# project name on purpose: renaming it would leave every already-installed
# cron line unmatched by remove().
_TAG_PREFIX = "# cli-tool-kit:"


class CronInstaller:
    """Manage a tool's cron lines via a unique marker.

    A trailing comment of the form `# cli-tool-kit:<marker>` is appended to
    each managed line. install() and remove() match on this tag, so lines
    added by other tools (or hand-edited entries) survive unchanged.
    """

    # Class attribute so tests can swap in a fake crontab shim without
    # monkeypatching subprocess.
    CRONTAB_BIN: str = "crontab"

    def __init__(self, marker: str) -> None:
        if not marker or "\n" in marker or "#" in marker:
            raise ValueError(
                "marker must be non-empty and contain no '\\n' or '#' characters"
            )
        self.marker = marker
        self.tag = f"{_TAG_PREFIX}{marker}"

    # ---- crontab I/O ----

    def _read(self) -> str:
        """Return current crontab contents, or '' if there is no crontab.

        `crontab -l` exits 1 with "no crontab for <user>" on stderr when the
        user simply has no crontab — that maps to ''. Other non-zero exits
        (binary missing, permission denied, transient failure) must NOT be
        silently coerced to '', because that would cause a subsequent
        `install([...])` to write a fresh crontab containing only the new
        lines, wiping any pre-existing entries.
        """
        try:
            result = subprocess.run(
                [self.CRONTAB_BIN, "-l"], capture_output=True, text=True
            )
        except FileNotFoundError as e:
            raise RuntimeError(f"crontab binary not found: {self.CRONTAB_BIN}") from e
        if result.returncode == 0:
            return result.stdout
        if "no crontab" in (result.stderr or "").lower():
            return ""
        raise RuntimeError(
            f"crontab -l failed (rc={result.returncode}): {result.stderr.strip()}"
        )

    def _write(self, content: str) -> None:
        """Replace the crontab with the given content."""
        if content and not content.endswith("\n"):
            content += "\n"
        subprocess.run(
            [self.CRONTAB_BIN, "-"], input=content, text=True, check=True
        )

    def _strip_marked(self, crontab: str) -> List[str]:
        """Return crontab lines not bearing this installer's tag."""
        return [line for line in crontab.splitlines() if self.tag not in line]

    # ---- public API ----

    def install(self, lines: List[str]) -> None:
        """Install (or replace) cron lines for this marker.

        Strips any existing lines bearing our tag, then appends each given line
        suffixed with the tag. Calling install() twice with the same lines
        leaves the crontab in the same final state.
        """
        for line in lines:
            if "\n" in line:
                raise ValueError("cron lines must not contain '\\n'")
            if self.tag in line:
                raise ValueError(
                    "cron lines must not include the tag (it is appended automatically)"
                )

        existing = self._strip_marked(self._read())
        # Trim trailing blanks so our block sits cleanly at the end.
        while existing and not existing[-1].strip():
            existing.pop()

        tagged = [f"{line}  {self.tag}" for line in lines]
        self._write("\n".join(existing + tagged))

        for line in tagged:
            print(colored(f"Cron line installed: {line}", "green"))

    def remove(self) -> None:
        """Remove all cron lines bearing this installer's tag."""
        original = self._read()
        if not original:
            print(colored(f"No crontab present (marker '{self.marker}').", "yellow"))
            return

        kept = self._strip_marked(original)
        removed_count = len(original.splitlines()) - len(kept)
        if removed_count == 0:
            print(colored(
                f"No cron lines to remove for marker '{self.marker}'.", "yellow"
            ))
            return

        self._write("\n".join(kept))
        print(colored(
            f"Removed {removed_count} cron line(s) for marker '{self.marker}'.",
            "green",
        ))

    def remove_unmarked(self, match: Callable[[str], bool]) -> int:
        """Remove cron lines that carry no kit tag and satisfy ``match``.

        For a tool migrating from hand-written cron lines to this class: pass a
        predicate that recognises its old lines (script name, flags). Lines
        tagged by any CronInstaller are never touched, whatever ``match`` says.
        A failing ``crontab -l`` raises instead of being read as empty, so the
        crontab is never rewritten from nothing. Returns how many lines went.
        """
        lines = self._read().splitlines()
        kept = [line for line in lines if _TAG_PREFIX in line or not match(line)]
        if len(kept) == len(lines):
            return 0
        self._write("\n".join(kept))
        return len(lines) - len(kept)

    def is_installed(self) -> bool:
        """Return True iff any crontab line bears this installer's tag."""
        return self.tag in self._read()

    def installed_lines(self) -> List[str]:
        """Return the currently-installed lines (without the trailing tag)."""
        out: List[str] = []
        for line in self._read().splitlines():
            if self.tag not in line:
                continue
            # Strip the tag and any whitespace that preceded it.
            body = line.split(self.tag)[0].rstrip()
            out.append(body)
        return out
