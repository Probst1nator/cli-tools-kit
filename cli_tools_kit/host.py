"""Platform decisions, in one place.

Everything the engine does differently on Windows lives here, so the rest of
the package can stay written for one host. On Linux a CLI tool becomes a bash
alias in ``~/.tools_aliases`` sourced from ``~/.bashrc``; Windows has no such
file, so the tool becomes a pair of small launcher scripts (shims) in a
directory that is added to the user's PATH once.

``IS_WINDOWS`` is a module constant rather than a call so tests can patch it
(``monkeypatch.setattr(host, "IS_WINDOWS", True)``) and exercise the Windows
paths on Linux. Read it as ``host.IS_WINDOWS`` at call time, never
``from .host import IS_WINDOWS``, or the patch will not be seen.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from typing import Iterable, Optional

IS_WINDOWS = os.name == "nt"


# --- text encoding --------------------------------------------------------------

def harden_stdio(streams=None) -> None:
    """Print a character the output cannot encode as ``?`` instead of crashing.

    On Windows, output that goes to a pipe or a file is encoded in the ANSI
    code page (cp1252), which has no ✓. ``--list`` died with
    UnicodeEncodeError as soon as one tool was installed, whenever a script or
    a coding agent read its output.
    """
    for stream in streams if streams is not None else (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(errors="replace")
        except (ValueError, OSError):
            pass


def symbol(fancy: str, plain: str, stream=None) -> str:
    """``fancy`` where ``stream`` can encode it, else the ASCII ``plain``.

    harden_stdio() stops the crash, but a ✓ that arrives as ``?`` reads as
    "unknown". A status column should say ``x`` on a cp1252 pipe instead.
    """
    encoding = getattr(stream if stream is not None else sys.stdout, "encoding", None)
    try:
        fancy.encode(encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return plain
    return fancy


def child_env(env: Optional[dict] = None) -> dict:
    """Environment for a tool's subprocess: its stdout and stderr in UTF-8.

    The kit reads that output as UTF-8, so a tool printing an emoji no longer
    dies under cp1252 before the kit sees a word of it.
    """
    env = dict(os.environ if env is None else env)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


# --- restarting -------------------------------------------------------------------

def restart(argv: Iterable[str]) -> None:
    """Replace this process with a new one running ``argv``; does not return.

    After an upgrade the old code is still loaded, so the installer starts
    again. Windows has no real exec (``os.execv`` there spawns a child and
    leaves the console behind), so it starts the new process and exits.
    """
    argv = list(argv)
    if IS_WINDOWS:
        subprocess.Popen(argv)
        os._exit(0)
    os.execv(argv[0], argv)


# --- where the shims live -----------------------------------------------------

def shim_dir(identity) -> str:
    """The directory this installer's Windows shims go in.

    Derived from the identity's slug, like every other per-host name, so two
    organisations on one host do not share a shim directory.
    """
    return identity.shim_path


def _quote_cmd(value: str) -> str:
    """Quote one argument for a .cmd batch file."""
    return f'"{value}"' if value else '""'


def write_shims(directory: str, alias: str, python: str, script: str,
                args: Iterable[str] = ()) -> list:
    """Write the two launcher files for one CLI tool. Returns their paths.

    ``<alias>.cmd`` is what cmd.exe and PowerShell run. The extensionless
    ``<alias>`` is a /bin/sh script for Git Bash, which is the shell Claude
    Code uses on Windows; Git Bash ignores the .cmd file, and cmd.exe ignores
    the extensionless one, so both can sit in the same directory.
    """
    os.makedirs(directory, exist_ok=True)
    args = list(args or [])

    cmd_line = " ".join([_quote_cmd(python), _quote_cmd(script)]
                        + [_quote_cmd(a) for a in args])
    cmd_path = os.path.join(directory, alias + ".cmd")
    with open(cmd_path, "w", newline="\r\n") as fh:
        fh.write("@echo off\n")
        fh.write(cmd_line + " %*\n")

    # Git Bash wants forward slashes; a backslash there is an escape character.
    sh_python = python.replace("\\", "/")
    sh_script = script.replace("\\", "/")
    sh_args = " ".join(shlex.quote(a) for a in args)
    sh_line = f'exec "{sh_python}" "{sh_script}"'
    if sh_args:
        sh_line += " " + sh_args
    sh_line += ' "$@"'
    sh_path = os.path.join(directory, alias)
    with open(sh_path, "w", newline="\n") as fh:
        fh.write("#!/bin/sh\n")
        fh.write(sh_line + "\n")
    try:
        os.chmod(sh_path, 0o755)
    except OSError:
        pass

    return [cmd_path, sh_path]


def remove_shims(directory: str, alias: str) -> list:
    """Delete both shim files for one tool. Returns the ones that were there."""
    removed = []
    for name in (alias + ".cmd", alias):
        path = os.path.join(directory, name)
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    return removed


# --- the user PATH ------------------------------------------------------------

def _default_registry():
    """The stdlib ``winreg`` module, or None off Windows."""
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def _broadcast_setting_change() -> None:
    """Tell running programs the environment changed. Best effort."""
    try:
        import ctypes
        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(  # type: ignore[attr-defined]
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, None,
        )
    except Exception:
        pass


def _same_dir(a: str, b: str) -> bool:
    """Compare two Windows directory paths: case-insensitive, either slash,
    trailing separator ignored. Spelled out rather than left to os.path.normcase
    so the comparison is the Windows one even when the tests run on Linux."""
    def norm(value: str) -> str:
        return value.replace("/", "\\").rstrip("\\").lower()
    return norm(a) == norm(b)


def ensure_user_path(directory: str, reg=None) -> bool:
    """Add ``directory`` to the user's PATH, once.

    Writes ``HKCU\\Environment\\Path`` as REG_EXPAND_SZ, broadcasts the change
    so newly started programs pick it up, and prepends the directory to this
    process's PATH. Returns True if the registry value was changed, False if
    the directory was already listed (or there is no registry to write).

    ``reg`` is the registry module to use; it defaults to ``winreg`` and is
    what the tests replace with a fake. A tool's ``--install`` runs in a
    subprocess the tests cannot hand a fake to, so ``CLI_TOOLS_KIT_SKIP_USER_PATH``
    set to anything non-empty leaves the registry alone there; a test run must
    not rewrite the developer's own PATH.
    """
    if reg is None and os.environ.get("CLI_TOOLS_KIT_SKIP_USER_PATH"):
        return False
    reg = reg if reg is not None else _default_registry()

    changed = False
    if reg is not None:
        key = reg.OpenKey(reg.HKEY_CURRENT_USER, "Environment", 0,
                          reg.KEY_READ | reg.KEY_WRITE)
        try:
            try:
                current = reg.QueryValueEx(key, "Path")[0] or ""
            except OSError:
                current = ""
            parts = [p for p in current.split(";") if p.strip()]
            if not any(_same_dir(p, directory) for p in parts):
                parts.append(directory)
                reg.SetValueEx(key, "Path", 0, reg.REG_EXPAND_SZ, ";".join(parts))
                changed = True
        finally:
            reg.CloseKey(key)
        if changed:
            _broadcast_setting_change()

    live = os.environ.get("PATH", "")
    if not any(_same_dir(p, directory) for p in live.split(os.pathsep) if p):
        os.environ["PATH"] = directory + os.pathsep + live if live else directory
    return changed


def shell_hint() -> str:
    """What to tell the user so a freshly installed command is found."""
    if IS_WINDOWS:
        return "Open a new terminal"
    return "Open a new shell, or run: source ~/.bashrc"


# --- Start Menu ---------------------------------------------------------------

def _appdata() -> str:
    return os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")


def start_menu_dir() -> str:
    """``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs``."""
    return os.path.join(_appdata(), "Microsoft", "Windows", "Start Menu", "Programs")


def startup_dir() -> str:
    """The Startup folder inside the Start Menu — entries here run at login."""
    return os.path.join(start_menu_dir(), "Startup")


def write_shortcut(lnk_path: str, target: str, args: str = "",
                   icon: Optional[str] = None, terminal: bool = False,
                   workdir: Optional[str] = None) -> bool:
    """Write a .lnk shortcut through PowerShell's WScript.Shell.

    This avoids a pywin32 dependency for the one thing it would be needed for.
    Best effort: returns False instead of raising when PowerShell is missing or
    the call fails.
    """
    try:
        os.makedirs(os.path.dirname(lnk_path) or ".", exist_ok=True)
    except OSError:
        return False

    def ps(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = [
        "$s = New-Object -ComObject WScript.Shell",
        f"$l = $s.CreateShortcut({ps(lnk_path)})",
        f"$l.TargetPath = {ps(target)}",
    ]
    if args:
        script.append(f"$l.Arguments = {ps(args)}")
    if workdir:
        script.append(f"$l.WorkingDirectory = {ps(workdir)}")
    if icon:
        script.append(f"$l.IconLocation = {ps(icon)}")
    # 1 = normal window. `terminal` is already expressed in the target
    # (cmd.exe /k ...), so it does not change the window style here.
    script.append("$l.WindowStyle = 1")
    script.append("$l.Save()")

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
        return result.returncode == 0
    except Exception:
        return False


def remove_shortcut(lnk_path: str) -> bool:
    """Delete a .lnk. True if one was there."""
    try:
        os.remove(lnk_path)
        return True
    except OSError:
        return False
