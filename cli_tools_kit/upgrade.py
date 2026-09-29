"""Is there anything newer than what this installer runs, and bring it up to date.

Three things can fall behind: the installer's own checkout, the tool repos the
installer cloned (``state.UPGRADE_REPOS``), and cli-tools-kit in the Python the
installer runs on. :func:`check` lists them, :func:`upgrade` pulls and
reinstalls. Neither touches tkinter, so the window, the text screen and
``--upgrade`` share them.

The network is used at most once a day: one ``git fetch`` per repo and one
``pip install --dry-run``. What was fetched is compared with the checkout on
every call, so a repo pulled by hand stops showing at once. The login check
(``--check``) never calls this module.

A checkout pinned by ``path`` in ``installer.toml`` is a development tree and
is never pulled. cli-tools-kit is left alone when it runs from a development
checkout or outside a virtual environment, where pip would replace the editable
install or write into the system Python.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import discovery, install, state
from .sources import GIT_SAFE

KIT = "cli-tools-kit"
CHECK_INTERVAL = 24 * 3600   # seconds between two network checks
CACHE_NAME = "upgrade-check.json"


class Item(NamedTuple):
    """One thing that can be upgraded."""

    kind: str     # "installer", "source" or "kit"
    name: str
    path: str     # the repo; for "kit", the interpreter
    detail: str   # "3 commits", "1.0.0 → 1.1.0"

    def label(self) -> str:
        return f"{self.name} ({self.detail})"


# --- git ---------------------------------------------------------------------------

def _git(path, *args, timeout: int = 60) -> Tuple[bool, str]:
    # No credential prompt: a private repo without stored credentials must fail,
    # not wait on a terminal or open a password dialog at every start. An empty
    # GIT_ASKPASS also stops git from falling back to SSH_ASKPASS (ksshaskpass).
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="",
               GCM_INTERACTIVE="never")
    try:
        result = subprocess.run(["git", *GIT_SAFE, "-C", str(path), *args],
                                capture_output=True, encoding="utf-8", errors="replace",
                                timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _reason(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return next((line for line in lines if line.startswith(("fatal:", "error:"))),
                lines[-1] if lines else "git failed")


def repo_root(path) -> Optional[Path]:
    """The top of the git checkout ``path`` is in, or None."""
    ok, out = _git(path, "rev-parse", "--show-toplevel")
    return Path(out).resolve() if ok and out else None


def behind(path) -> Optional[int]:
    """How many fetched commits ``path`` does not have yet; None without an upstream."""
    ok, out = _git(path, "rev-list", "--count", "HEAD..@{u}")
    return int(out) if ok and out.isdigit() else None


def fetch(path) -> bool:
    return _git(path, "fetch", "--quiet", timeout=120)[0]


def _repos() -> List[Tuple[str, str, Path]]:
    """(kind, name, path) of every checkout the upgrade may pull."""
    found: List[Tuple[str, str, Path]] = []
    seen = set()
    entry_dir = os.path.dirname(os.path.abspath(state.ENTRY_SCRIPT))
    own = repo_root(entry_dir) if os.path.isdir(entry_dir) else None
    if own is not None and own != _kit_checkout():
        found.append(("installer", own.name, own))
        seen.add(own)
    for name, path in state.UPGRADE_REPOS:
        path = Path(path).resolve()
        if path not in seen and (path / ".git").exists():
            found.append(("source", name, path))
            seen.add(path)
    return found


# --- cli-tools-kit -------------------------------------------------------------------

def kit_version() -> str:
    import cli_tools_kit  # noqa: PLC0415 — the package imports this module's siblings
    return cli_tools_kit.__version__


def _kit_checkout() -> Optional[Path]:
    """The kit's own development checkout, when it runs from one."""
    import cli_tools_kit  # noqa: PLC0415
    top = Path(cli_tools_kit.__file__).resolve().parent.parent
    return top if (top / "pyproject.toml").is_file() else None


def kit_blocker() -> Optional[str]:
    """Why pip must not upgrade the kit here, or None when it may."""
    if _kit_checkout() is not None:
        return "cli-tools-kit runs from a development checkout"
    if sys.prefix == sys.base_prefix:
        return "the installer does not run in a virtual environment"
    return None


def kit_requirement() -> List[str]:
    """pip arguments that ask for the kit the way this installer pins it.

    The installer's ``requirements.txt`` when it names the kit, so pip stays
    inside the wrapper's range; otherwise any version below the next major.
    """
    requirements = Path(os.path.abspath(state.ENTRY_SCRIPT)).parent / "requirements.txt"
    try:
        text = requirements.read_text(encoding="utf-8").lower().replace("_", "-")
    except OSError:
        text = ""
    if KIT in text:
        return ["-r", str(requirements)]
    major = _version_key(kit_version())[0]
    return [f"{KIT}<{major + 1}"]


def latest_kit() -> Optional[str]:
    """The kit version pip would install within the pin; None when pip could not say."""
    cmd = [sys.executable, "-m", "pip", "install", "--dry-run", "--upgrade", "--quiet",
           "--disable-pip-version-check", "--report", "-", *kit_requirement()]
    try:
        result = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                                errors="replace", timeout=180)
        report = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if not isinstance(report, dict):
        return None
    for entry in report.get("install", []):
        meta = entry.get("metadata", {}) if isinstance(entry, dict) else {}
        if re.sub(r"[-_.]+", "-", str(meta.get("name", ""))).lower() == KIT:
            return meta.get("version")
    return kit_version()   # pip would install nothing: this is the newest in range


def _version_key(version: str) -> Tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3]) or (0,)


# --- the check ----------------------------------------------------------------------

def _cache_path() -> Path:
    return Path(state.IDENTITY.cache_path) / CACHE_NAME


def _load_cache() -> Dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(data: Dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass   # the next start checks again; nothing else depends on the file


def check(force: bool = False, now: Optional[float] = None) -> List[Item]:
    """What is newer than what this installer runs.

    Fetches and asks pip only when ``force`` is set or the last successful
    check is a day old. When every fetch failed, or pip did, the date is left
    alone, so an offline start is retried on the next one. One repo that
    cannot be fetched (no credentials for a private repo) does not count as
    offline, or every start would go to the network again.
    """
    now = time.time() if now is None else now
    cache = _load_cache()
    online = force or now - float(cache.get("checked", 0)) >= CHECK_INTERVAL
    repos = _repos()
    network_ok = not repos   # with repos, at least one fetch has to succeed
    items: List[Item] = []

    for kind, name, path in repos:
        if online and fetch(path):
            network_ok = True
        count = behind(path)
        if count:
            items.append(Item(kind, name, str(path),
                              f"{count} commit{'' if count == 1 else 's'}"))

    if kit_blocker() is None:
        cached = cache.get("kit") if cache.get("kit_python") == sys.executable else None
        latest = cached
        if online:
            latest = latest_kit()
            if latest is None:
                network_ok, latest = False, cached
            else:
                cache.update(kit=latest, kit_python=sys.executable)
        current = kit_version()
        if latest and _version_key(latest) > _version_key(current):
            items.append(Item("kit", KIT, sys.executable, f"{current} → {latest}"))

    if online and network_ok:
        cache["checked"] = now
        _save_cache(cache)
    return items


# --- the upgrade --------------------------------------------------------------------

Log = Callable[..., None]   # log(message, tag="info"); tags: info, success, error, header


def upgrade(items: Sequence[Item], tools: Sequence[discovery.ToolEntry],
            log: Log) -> Dict[str, object]:
    """Pull the repos in ``items``, upgrade the kit, reinstall what changed.

    Every installed tool whose repo was pulled runs its ``--install`` again
    (through ``install.install_tool``, so a wrapper's hooks apply), because its
    code and its requirements may have changed. The kit is upgraded when it is
    in ``items`` or the installer's own checkout was pulled, since that can
    change the installer's pin.

    Returns ``{"changed": bool, "errors": int}``. ``changed`` means the running
    installer is out of date and should restart.
    """
    changed, errors = False, 0
    pulled: List[str] = []
    for item in items:
        if item.kind == "kit":
            continue
        log(f"Pulling {item.name} ({item.detail})", "header")
        ok, out = _git(item.path, "pull", "--ff-only", timeout=300)
        if ok:
            changed = True
            pulled.append(item.path)
            log(f"  ✓ {item.name} is up to date", "success")
        else:
            errors += 1
            log(f"  ✗ {item.name} not pulled: {_reason(out)}", "error")

    installer_pulled = any(i.kind == "installer" and i.path in pulled for i in items)
    if any(i.kind == "kit" for i in items) or installer_pulled:
        blocker = kit_blocker()
        if blocker:
            log(f"cli-tools-kit left as it is: {blocker}", "info")
        else:
            log("Upgrading cli-tools-kit", "header")
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade",
                   "--disable-pip-version-check", *kit_requirement()]
            try:
                result = subprocess.run(cmd, capture_output=True, encoding="utf-8",
                                        errors="replace", timeout=600)
                ok, out = result.returncode == 0, result.stdout + result.stderr
            except (OSError, subprocess.TimeoutExpired) as exc:
                ok, out = False, str(exc)
            if ok:
                changed = True
                log("  ✓ cli-tools-kit upgraded", "success")
            else:
                errors += 1
                tail = [line for line in out.splitlines() if line.strip()][-3:]
                log("  ✗ pip failed: " + (" | ".join(tail) or "no output"), "error")

    roots = [Path(p) for p in pulled]
    for tool in tools:
        script = Path(tool.script_path).resolve()
        if not any(root in script.parents for root in roots):
            continue
        if not install.is_installed(tool):
            continue
        log(f"Reinstalling {tool.name}")
        ok, out = install.install_tool(tool)
        if ok:
            log(f"  ✓ {tool.name} reinstalled", "success")
        else:
            errors += 1
            log(f"  ✗ {tool.name}: {out}", "error")

    return {"changed": changed, "errors": errors}


def restart_argv() -> List[str]:
    """The command that starts this installer again the way it was started."""
    return [sys.executable, *(state.LAUNCH_ARGV or sys.argv)]
