"""Claude Code plugins as installable tools.

A tool that advertises ``claude_plugin`` (a plugin id, ``name@marketplace``)
and ``claude_marketplace`` (what ``claude plugin install --marketplace`` takes:
a GitHub ``owner/repo``, a git or https URL, or a local directory) is
installed by installing and enabling that plugin in Claude Code, not by a
shortcut or an alias. The installer asks ``claude plugin list --json`` whether
it is there; the tool's own ``--install`` and ``--remove`` call :func:`main`
here, so a wrapper that runs tools its own way (a per-tool venv) still works.

Only one plugin of a name is enabled at a time: installing ``clawd@b``
disables ``clawd@a``, since two copies of one plugin would register the same
commands and hooks.

A row talks to one Claude Code config directory. ``""`` is the default
(``~/.claude``): ``CLAUDE_CONFIG_DIR`` is removed from the child's environment,
so an installer started from a shell that set it still writes to the default.
A wrapper offers further directories through ``run(plugin_targets=[...])``;
discovery then lists each plugin once per target, and the row passes its
directory to the tool as ``--claude-config-dir DIR``.

``claude`` itself contacts Anthropic on every call, so the login check
(``--check``) skips plugin rows. ``claude plugin install`` is skipped when
``TOOLS_INSTALLER_SKIP_DEPS`` is set (``--update-all``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from typing import Dict, List, NamedTuple, Optional, Tuple

CONFIG_DIR_ARG = "--claude-config-dir"


class PluginTarget(NamedTuple):
    """One Claude Code config directory a plugin can be installed into."""
    key: str          # "fauclaude": appended to the row's name and desktop_file
    label: str        # shown in the row's description
    config_dir: str   # "" for the default ~/.claude; "~" is expanded


class PluginError(Exception):
    pass


def default_target() -> PluginTarget:
    return PluginTarget(key="claude", label="Claude Code (~/.claude)", config_dir="")


def _config_dir(config_dir: str) -> str:
    return os.path.expanduser(config_dir or os.path.join("~", ".claude"))


def _env(config_dir: str) -> Dict[str, str]:
    env = os.environ.copy()
    env.pop("CLAUDE_CONFIG_DIR", None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    return env


def _claude(args: List[str], config_dir: str, timeout: int = 60) -> subprocess.CompletedProcess:
    exe = shutil.which("claude")
    if not exe:
        raise PluginError("the `claude` command is not on the PATH; install Claude Code first")
    # An empty working directory: Claude Code reads <cwd>/.claude/settings.json
    # as project settings, and those switch plugins on and off in the listing
    # too. In ~ that file is the user settings of ~/.claude.
    with tempfile.TemporaryDirectory(prefix="cli-tools-kit-") as cwd:
        return subprocess.run([exe, "plugin", *args], cwd=cwd,
                              env=_env(config_dir), capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout)


# --- reading -----------------------------------------------------------------

_lock = threading.Lock()
_cache: Dict[str, Tuple[tuple, List[dict]]] = {}


def _signature(config_dir: str) -> tuple:
    """Changes whenever Claude Code records an install, removal or toggle."""
    base = _config_dir(config_dir)
    sig = []
    for rel in (("plugins", "installed_plugins.json"), ("settings.json",)):
        try:
            st = os.stat(os.path.join(base, *rel))
            sig.append((st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append(None)
    return tuple(sig)


def installed_plugins(config_dir: str = "") -> List[dict]:
    """``claude plugin list --json`` for one config dir; [] when it fails.

    Cached until Claude Code rewrites its plugin files, because one call takes
    most of a second and a screen asks once per row and redraw. Without an
    ``installed_plugins.json`` nothing is installed there, and ``claude`` is
    not started: it would create the directory.
    """
    base = _config_dir(config_dir)
    if not os.path.isfile(os.path.join(base, "plugins", "installed_plugins.json")):
        return []
    sig = _signature(config_dir)
    with _lock:
        hit = _cache.get(config_dir)
        if hit and hit[0] == sig:
            return hit[1]
        try:
            result = _claude(["list", "--json"], config_dir)
            plugins = json.loads(result.stdout) if result.returncode == 0 else []
        except (PluginError, OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            plugins = []
        if not isinstance(plugins, list):
            plugins = []
        _cache[config_dir] = (sig, plugins)
        return plugins


def _user_entries(config_dir: str) -> List[dict]:
    return [p for p in installed_plugins(config_dir)
            if isinstance(p, dict) and p.get("scope") == "user"]


def is_enabled(plugin_id: str, config_dir: str = "") -> bool:
    """True when *plugin_id* is installed in the user scope and enabled."""
    return any(p.get("id") == plugin_id and p.get("enabled")
               for p in _user_entries(config_dir))


def _siblings(plugin_id: str, config_dir: str) -> List[str]:
    """Other enabled user-scope plugins with the same name as *plugin_id*."""
    name = plugin_id.split("@", 1)[0]
    return sorted({p["id"] for p in _user_entries(config_dir)
                   if p.get("enabled") and p.get("id") != plugin_id
                   and str(p.get("id", "")).split("@", 1)[0] == name})


# --- changing ----------------------------------------------------------------

def _message(result: subprocess.CompletedProcess) -> str:
    """The human line of a ``--json`` result, else the raw output."""
    for line in reversed(result.stdout.splitlines()):
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    return (result.stdout + result.stderr).strip() or f"exit code {result.returncode}"


def install(plugin_id: str, marketplace: str, config_dir: str = "", log=print) -> bool:
    """Install and enable *plugin_id*, then disable its same-name siblings.

    A config directory other than the default must have been set up by the
    program that uses it: a launcher such as fauclaude seeds its directory
    only while ``settings.json`` is missing, and installing would write one.
    """
    if config_dir and not os.path.isfile(os.path.join(_config_dir(config_dir), "settings.json")):
        log(f"{config_dir} is not set up yet: start the program that uses it once, "
            "then install again")
        return False
    if not is_enabled(plugin_id, config_dir):
        if os.environ.get("TOOLS_INSTALLER_SKIP_DEPS") == "1":
            log(f"Skipped {plugin_id}: installing a plugin needs the network, "
                "and TOOLS_INSTALLER_SKIP_DEPS is set")
            return True
        name = plugin_id.split("@", 1)[0]
        result = _claude(["install", name, "--marketplace", marketplace,
                          "--scope", "user", "--json"], config_dir, timeout=600)
        log(_message(result))
        if result.returncode != 0:
            return False
        if not is_enabled(plugin_id, config_dir):
            log(f"{marketplace} did not provide {plugin_id}; check the "
                "marketplace name in its .claude-plugin/marketplace.json")
            return False
    else:
        log(f"{plugin_id} is installed and enabled")
    ok = True
    for other in _siblings(plugin_id, config_dir):
        result = _claude(["disable", other, "--scope", "user", "--json"], config_dir)
        if result.returncode == 0:
            log(f"Disabled {other}: only one {other.split('@', 1)[0]} plugin runs at a time")
        else:
            ok = False
            log(f"Could not disable {other}: {_message(result)}")
    return ok


def uninstall(plugin_id: str, config_dir: str = "", log=print) -> bool:
    """Uninstall *plugin_id* from the user scope. The marketplace stays added."""
    if not any(p.get("id") == plugin_id for p in _user_entries(config_dir)):
        log(f"{plugin_id} is not installed")
        return True
    result = _claude(["uninstall", plugin_id, "--scope", "user", "--json"], config_dir)
    log(_message(result))
    return result.returncode == 0


def main(plugin_id: str, marketplace: str, argv: Optional[List[str]] = None) -> int:
    """``--install`` / ``--remove`` for a tool that installs a Claude Code plugin.

    Call it from the tool's main script after the ``--advertise`` guard::

        from cli_tools_kit import plugins
        sys.exit(plugins.main("clawd@clawd", "Probst1nator/clawd"))

    ``--claude-config-dir DIR`` picks a config directory other than ``~/.claude``.
    """
    import sys
    args = list(sys.argv[1:] if argv is None else argv)
    config_dir = ""
    if CONFIG_DIR_ARG in args:
        i = args.index(CONFIG_DIR_ARG)
        if i + 1 >= len(args):
            print(f"{CONFIG_DIR_ARG} needs a directory", file=sys.stderr)
            return 2
        config_dir = args[i + 1]
        del args[i:i + 2]
    where = f" in {config_dir}" if config_dir else ""
    try:
        if "--install" in args:
            return 0 if install(plugin_id, marketplace, config_dir) else 1
        if "--remove" in args:
            return 0 if uninstall(plugin_id, config_dir) else 1
    except (PluginError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"{plugin_id}{where}: {exc}", file=sys.stderr)
        return 1
    print(f"usage: --install | --remove [{CONFIG_DIR_ARG} DIR]  ({plugin_id})", file=sys.stderr)
    return 2
