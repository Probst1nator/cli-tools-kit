"""MCP servers that tools bring along (the installer's MCP column).

A tool that advertises ``mcp_name`` and ``mcp_command`` comes with a stdio MCP
server, the way a tool with ``skill_name`` comes with a skill. The installer
shows an MCP checkbox on the tool's row and registers that command under that
name with the agent harnesses on the host. The tool itself needs no MCP code.

Each harness is an :class:`McpTarget`. Two are built in:

- **Claude Code**, user scope: the ``mcpServers`` of ``~/.claude.json``.
  Changes go through ``claude mcp add-json`` and ``claude mcp remove`` with
  ``-s user``, from an empty working directory and without an inherited
  ``CLAUDE_CONFIG_DIR``. ``claude mcp list`` and ``claude mcp get`` start every
  registered server to check its health, so they are never used.
- **opencode**: the ``mcp`` object of its global config,
  ``$XDG_CONFIG_HOME/opencode/opencode.json`` (or ``.jsonc``). Adding goes
  through ``opencode mcp add``, which keeps the file's comments. opencode has no
  remove command, so removing deletes the entry in the file (:mod:`.jsonc`).

Reading either config takes only each server's name and command; account data
and the servers' environment stay untouched. A wrapper adds harnesses with
``run(mcp_targets=[...])``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import jsonc


class McpError(Exception):
    pass


class McpTarget(NamedTuple):
    """One agent harness an MCP server can be registered with."""
    key: str                                                # "claude", "opencode"
    label: str                                              # shown to the user
    available: Callable[[], bool]                           # is the harness on this host?
    registered: Callable[[], Dict[str, List[str]]]          # name -> argv of its servers
    install: Callable[[str, List[str]], Tuple[bool, str]]   # (name, argv) -> (ok, output)
    uninstall: Callable[[str], Tuple[bool, str]]            # name -> (ok, output)


def state(target: McpTarget, name: str, argv: Sequence[str]) -> str:
    """``"absent"``, ``"current"``, or ``"other"`` (the name runs another command)."""
    have = target.registered().get(name)
    if have is None:
        return "absent"
    return "current" if have == list(argv) else "other"


def _message(result: subprocess.CompletedProcess) -> str:
    return (result.stdout + result.stderr).strip() or f"exit code {result.returncode}"


def _run(argv: List[str], env: Optional[Dict[str, str]] = None,
         timeout: int = 60) -> subprocess.CompletedProcess:
    # An empty working directory: both harnesses read project config from it.
    with tempfile.TemporaryDirectory(prefix="cli-tools-kit-") as cwd:
        return subprocess.run(argv, cwd=cwd, env=env, capture_output=True,
                              encoding="utf-8", errors="replace", timeout=timeout)


# The last readable answer per file: Claude Code rewrites .claude.json often,
# and a read in the middle of that must not look like "nothing registered".
_last: Dict[str, Dict[str, List[str]]] = {}


def _read(path: Path, parse: Callable[[str], Dict[str, List[str]]]) -> Dict[str, List[str]]:
    for attempt in range(2):
        try:
            servers = parse(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            servers = {}
        except (OSError, ValueError):
            if attempt == 0:
                time.sleep(0.05)
                continue
            return _last.get(str(path), {})
        _last[str(path)] = servers
        return servers
    return {}


# --- Claude Code ---------------------------------------------------------------

def claude_config_file() -> Path:
    """Where Claude Code keeps the user-scope MCP servers (outside ~/.claude)."""
    return Path.home() / ".claude.json"


def _claude_parse(text: str) -> Dict[str, List[str]]:
    data = json.loads(text)
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    out: Dict[str, List[str]] = {}
    for name, entry in (servers.items() if isinstance(servers, dict) else []):
        entry = entry if isinstance(entry, dict) else {}
        command, args = entry.get("command"), entry.get("args") or []
        out[str(name)] = ([str(command)] + [str(a) for a in args]
                          if isinstance(command, str) and isinstance(args, list) else [])
    return out


def _claude_registered() -> Dict[str, List[str]]:
    return _read(claude_config_file(), _claude_parse)


def _claude(args: List[str]) -> subprocess.CompletedProcess:
    exe = shutil.which("claude")
    if not exe:
        raise McpError("the `claude` command is not on the PATH; install Claude Code first")
    env = os.environ.copy()
    env.pop("CLAUDE_CONFIG_DIR", None)
    return _run([exe, "mcp", *args], env)


def _claude_install(name: str, argv: List[str]) -> Tuple[bool, str]:
    current = _claude_registered().get(name)
    if current == argv:
        return True, f"{name} is registered"
    if current is not None:
        result = _claude(["remove", "-s", "user", name])
        if result.returncode != 0:
            return False, f"could not replace {name}: {_message(result)}"
    spec = json.dumps({"type": "stdio", "command": argv[0], "args": argv[1:]})
    result = _claude(["add-json", "-s", "user", name, spec])
    return result.returncode == 0 and _claude_registered().get(name) == argv, _message(result)


def _claude_uninstall(name: str) -> Tuple[bool, str]:
    if name not in _claude_registered():
        return True, f"{name} is not registered"
    result = _claude(["remove", "-s", "user", name])
    return result.returncode == 0, _message(result)


def claude_code_target() -> McpTarget:
    return McpTarget(
        key="claude", label="Claude Code",
        available=lambda: shutil.which("claude") is not None,
        registered=_claude_registered, install=_claude_install, uninstall=_claude_uninstall)


# --- opencode ------------------------------------------------------------------

def opencode_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "opencode"


def opencode_config_file() -> Path:
    """The file ``opencode mcp add`` writes: ``opencode.json``, else ``.jsonc``."""
    folder = opencode_config_dir()
    for name in ("opencode.json", "opencode.jsonc"):
        if (folder / name).exists():
            return folder / name
    return folder / "opencode.json"


def _opencode_exe() -> Optional[str]:
    found = shutil.which("opencode")
    if found:
        return found
    # The opencode installer puts it here and adds it to PATH only in shell rc files.
    fallback = Path.home() / ".opencode" / "bin" / "opencode"
    return str(fallback) if fallback.is_file() else None


def _opencode_parse(text: str) -> Dict[str, List[str]]:
    data = jsonc.loads(text)
    servers = data.get("mcp") if isinstance(data, dict) else None
    out: Dict[str, List[str]] = {}
    for name, entry in (servers.items() if isinstance(servers, dict) else []):
        command = entry.get("command") if isinstance(entry, dict) else None
        local = isinstance(entry, dict) and entry.get("type") == "local"
        out[str(name)] = ([str(a) for a in command]
                          if local and isinstance(command, list) else [])
    return out


def _opencode_registered() -> Dict[str, List[str]]:
    return _read(opencode_config_file(), _opencode_parse)


def _opencode_install(name: str, argv: List[str]) -> Tuple[bool, str]:
    if _opencode_registered().get(name) == argv:
        return True, f"{name} is registered"
    exe = _opencode_exe()
    if not exe:
        raise McpError("the `opencode` command is not on the PATH; install opencode first")
    # `mcp add` replaces an entry of the same name. --pure skips plugins.
    result = _run([exe, "mcp", "add", "--pure", name, "--", *argv])
    return result.returncode == 0 and _opencode_registered().get(name) == argv, _message(result)


def _opencode_uninstall(name: str) -> Tuple[bool, str]:
    path = opencode_config_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return True, f"{name} is not registered"
    new = jsonc.remove_member(text, ["mcp", name])
    if new is None:
        return True, f"{name} is not registered"
    tmp = path.with_name(path.name + ".cli-tools-kit.tmp")
    tmp.write_text(new, encoding="utf-8")
    os.chmod(tmp, path.stat().st_mode & 0o7777)
    os.replace(tmp, path)
    return True, f"removed {name} from {path}"


def opencode_target() -> McpTarget:
    return McpTarget(
        key="opencode", label="opencode",
        available=lambda: _opencode_exe() is not None or opencode_config_dir().is_dir(),
        registered=_opencode_registered, install=_opencode_install,
        uninstall=_opencode_uninstall)


# --- what the screens use --------------------------------------------------------

def default_targets() -> List[McpTarget]:
    return [claude_code_target(), opencode_target()]


def targets() -> List[McpTarget]:
    """The wrapper's ``run(mcp_targets=...)``, else the built-in ones."""
    from . import state as kit_state
    chosen = kit_state.MCP_TARGETS
    return list(chosen) if chosen is not None else default_targets()


def present(targets_: Optional[List[McpTarget]] = None) -> List[McpTarget]:
    """The targets whose harness this host has."""
    return [t for t in (targets() if targets_ is None else targets_) if _safe(t.available, False)]


def _safe(fn: Callable, default):
    try:
        return fn()
    except Exception:  # a broken harness config must not take a screen down
        return default


def registered_with(name: str, targets_: List[McpTarget]) -> List[McpTarget]:
    """The targets that have a server of that name, whatever its command."""
    return [t for t in targets_ if name in _safe(t.registered, {})]


def _print(msg: str, tag: str = "info") -> None:
    print(msg)


def apply(name: str, argv: Sequence[str], want: bool, targets_: List[McpTarget], *,
          add_missing: bool = True, log: Callable[..., None] = _print) -> Dict[str, int]:
    """Bring each target in line with *want*; returns counts of what happened.

    With *want*, a target without the server gets it only when *add_missing*;
    one that runs another command under that name is always updated. Without
    *want*, every target that has the name loses it. *log* gets each line and
    a tag (``"success"`` or ``"error"``), the GUI log's signature.
    """
    argv = [str(a) for a in argv]
    counts = {"installed": 0, "removed": 0, "errors": 0}
    for target in targets_:
        try:
            have = target.registered().get(name)
            if want:
                if have == argv or (have is None and not add_missing):
                    continue
                ok, out = target.install(name, argv)
                kind = "installed"
            else:
                if have is None:
                    continue
                ok, out = target.uninstall(name)
                kind = "removed"
        except (McpError, OSError, ValueError, subprocess.TimeoutExpired) as exc:
            ok, out, kind = False, str(exc), "errors"
        counts[kind if ok else "errors"] += 1
        tag = "success" if ok else "error"
        verb = "registered with" if want else "removed from"
        log(f"MCP server {name} {verb} {target.label}: {'ok' if ok else 'FAILED'}", tag)
        for line in (out or "").splitlines():
            log(f"    {line}", tag)
    return counts
