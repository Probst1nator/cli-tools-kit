"""Sources — one installer offering tools that live in several repos.

An organisation's tools rarely sit in one checkout. This module lets the
installer read a list of repos from a TOML file next to it, put each one on
disk, and hand the engine one discovery root per repo.

``installer.toml`` is tracked and shared by everyone:

    [[source]]
    name = "acme/tools"
    path = "."                                    # relative to this file

    [[source]]
    name = "acme/lab"
    url = "https://github.com/acme/lab-tools"     # cloned into <root>/acme/lab

``installer.local.toml`` next to it is optional and belongs to one machine, so
it is gitignored by convention. It sets ``root`` and adds or replaces a ``path``
for a source matched by ``name``:

    root = "/home/me/checkouts"

    [[source]]
    name = "acme/lab"
    path = "/home/me/work/lab-tools"

The root is never guessed from where the config file happens to sit.
``run_installer`` takes ``--root``, else the ``root`` of the local file, else it
asks the user, and writes the answer back into the local file so the question is
asked once.

A source resolves in this order: the path from the local file, then the ``path``
from the tracked file, then an existing ``<root>/<name>``, then a clone of
``url`` into ``<root>/<name>``. Only ``https://`` URLs are cloned, the clone is
full rather than shallow, and a clone that fails prints one line and drops that
source, so the other repos' tools still install.

A resolved repo may carry its own ``installer.toml``. Its ``[[source]]`` entries
are resolved too, one nested level deep and no further. Paths in a nested file
are relative to that file, clones still go under the same root, a path already
resolved is not visited twice, and duplicates are dropped.

The whole feature is three calls:

    sources = load_sources("installer.toml")
    roots = resolve_sources(sources, root="~/acme-tools")

or, for a wrapper that just wants the installer:

    run_installer(os.path.join(HERE, "installer.toml"),
                  identity=InstallerIdentity(slug="acme-tools"),
                  entry_script=__file__)
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence

__all__ = ["Source", "load_sources", "resolve_sources", "run_installer",
           "local_root", "save_local_root", "default_root"]

# How far below the top-level installer.toml a nested one is still read.
MAX_NESTING = 1

# Neutralise the ext:: and file:// transports and any hook, so fetching a repo
# cannot turn into running code from it.
GIT_SAFE = ("-c", "protocol.ext.allow=never",
            "-c", "protocol.file.allow=never",
            "-c", "core.hooksPath=/dev/null")


@dataclass(frozen=True)
class Source:
    """One repo an installer offers tools from.

    ``path`` is already absolute: ``load_sources`` resolves it against the TOML
    file it was written in. ``url`` is used only when no path is on disk.
    """

    name: str
    url: Optional[str] = None
    path: Optional[str] = None


# --- TOML -------------------------------------------------------------------

def _toml_module():
    """``tomllib`` (Python 3.11+), ``tomli`` if it is installed, else None."""
    try:
        import tomllib  # noqa: PLC0415
        return tomllib
    except ImportError:
        pass
    try:
        import tomli  # noqa: PLC0415
        return tomli
    except ImportError:
        return None


def _read_toml(path: Path, log: Callable = print) -> dict:
    """One TOML file as a dict, empty if it is absent or does not parse."""
    toml = _toml_module()
    if toml is None:
        log(f"{path.name}: not read (needs Python 3.11 or `pip install tomli`)")
        return {}
    if not path.is_file():
        return {}
    try:
        with open(path, "rb") as fh:
            return toml.load(fh)
    except (OSError, ValueError) as exc:
        log(f"{path.name}: not read ({exc})")
        return {}


def _local_path_for(config_path: Path) -> Path:
    """``installer.toml`` -> ``installer.local.toml`` in the same directory."""
    return config_path.with_name(config_path.stem + ".local" + config_path.suffix)


def _absolute(base: Path, raw: str) -> str:
    return os.path.abspath(os.path.join(str(base), os.path.expanduser(raw)))


def local_root(config_path, local_path=None) -> Optional[str]:
    """The top-level ``root`` of the local file next to ``config_path``, or None."""
    config_path = Path(config_path)
    local = Path(local_path) if local_path is not None else _local_path_for(config_path)
    value = _read_toml(local).get("root")
    if isinstance(value, str) and value:
        return str(Path(os.path.expanduser(value)).absolute())
    return None


def save_local_root(config_path, root, local_path=None, log: Callable = print) -> bool:
    """Write ``root`` into the local file next to ``config_path``.

    Returns False and changes nothing when the file already sets a ``root``: a
    value someone put there by hand is never overwritten. Existing
    ``[[source]]`` entries are kept, and the key is written above them, because
    a top-level key written after a table would belong to that table.
    """
    config_path = Path(config_path)
    local = Path(local_path) if local_path is not None else _local_path_for(config_path)
    if local_root(config_path, local) is not None:
        return False
    existing = ""
    if local.is_file():
        try:
            existing = local.read_text(encoding="utf-8")
        except OSError as exc:
            log(f"{local.name}: not updated ({exc})")
            return False
    # A TOML literal string, because a Windows root is full of backslashes and
    # a basic string would read them as escapes: "C:\Users\..." dies on \U and
    # takes the whole file with it, so the question would be asked again on
    # every launch. A path holding a single quote falls back to a basic string
    # with the two characters TOML needs escaped there.
    if "'" in root:
        line = 'root = "{}"\n'.format(root.replace("\\", "\\\\").replace('"', '\\"'))
    else:
        line = f"root = '{root}'\n"
    try:
        local.write_text(line + ("\n" + existing.lstrip("\n") if existing.strip() else ""),
                         encoding="utf-8")
    except OSError as exc:
        log(f"{local.name}: not written ({exc})")
        return False
    return True


def load_sources(config_path, local_path=None, log: Callable = print) -> List[Source]:
    """The ``[[source]]`` entries of one TOML file, local overrides applied.

    ``local_path`` defaults to ``installer.local.toml`` next to ``config_path``.
    A ``path`` is taken relative to the file it is written in. An entry without
    a name is reported and dropped.
    """
    config_path = Path(config_path)
    local_path = Path(local_path) if local_path is not None else _local_path_for(config_path)
    data = _read_toml(config_path, log)
    local = _read_toml(local_path, log)

    overrides = {entry["name"]: entry
                 for entry in local.get("source") or []
                 if isinstance(entry, dict) and entry.get("name")}

    sources: List[Source] = []
    for entry in data.get("source") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            log(f"{config_path.name}: a [[source]] without a name, skipped")
            continue
        override = overrides.get(name, {}).get("path")
        raw = override or entry.get("path")
        base = local_path.parent if override else config_path.parent
        path = _absolute(base, raw) if raw else None
        sources.append(Source(name=name, url=entry.get("url"), path=path))
    return sources


# --- resolution -------------------------------------------------------------

def _git(*args):
    try:
        result = subprocess.run(["git", *GIT_SAFE, *args], capture_output=True, text=True)
    except OSError as exc:
        return False, str(exc)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def _git_reason(output: str, fallback: str = "git failed") -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return next((line for line in lines if line.startswith("fatal:")),
                lines[-1] if lines else fallback)


def _clone_reason(output: str) -> str:
    return _git_reason(output, "git clone failed")


def _resolve_one(source: Source, root: Path, refresh: bool, log: Callable,
                 clone: bool) -> Optional[Path]:
    """Where one source sits on disk, cloning it if that is the only way."""
    if source.path and Path(source.path).is_dir():
        return Path(source.path).resolve()

    target = root.joinpath(*source.name.split("/"))
    if target.is_dir():
        if refresh and clone and (target / ".git").is_dir() and source.url:
            ok, out = _git("-C", str(target), "pull", "--ff-only")
            # A pull can fail for reasons the user cannot fix here: the remote
            # is gone, the network is down, the history diverged. One line, and
            # the checkout that is already on disk is used as it stands.
            log(f"{source.name}: pulled" if ok else
                f"{source.name}: not updated, using the checkout as it is"
                f" ({_git_reason(out)})")
        return target.resolve()

    if not source.url:
        if clone:
            log(f"{source.name}: no checkout at {target} and no url, skipped")
        return None
    if not source.url.lower().startswith("https://"):
        if clone:
            log(f"{source.name}: {source.url} is not an https:// URL, skipped")
        return None
    if not clone:
        return None
    if not (root.is_dir() and os.access(str(root), os.W_OK)):
        log(f"{source.name}: not cloned, skipped ({root} does not exist or cannot be"
            " written to; pass --root DIR to clone somewhere else)")
        return None

    log(f"Cloning {source.url} -> {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    ok, out = _git("clone", source.url, str(target))
    if not ok:
        log(f"{source.name}: not cloned, skipped ({_clone_reason(out)})")
        return None
    return target.resolve()


def _resolve_level(sources: Sequence[Source], root: Path, refresh: bool, log: Callable,
                   clone: bool, config_name: str, depth: int,
                   found: List[Path], seen: set) -> None:
    for source in sources:
        path = _resolve_one(source, root, refresh, log, clone)
        if path is None or path in seen:
            continue
        seen.add(path)
        found.append(path)
        if depth >= MAX_NESTING:
            continue
        nested = path / config_name
        if nested.is_file():
            _resolve_level(load_sources(nested, log=log), root, refresh, log, clone,
                           config_name, depth + 1, found, seen)


def resolve_sources(sources: Sequence[Source], root, refresh: bool = False,
                    log: Callable = print, clone: bool = True) -> List[Path]:
    """Put every source on disk and return one discovery root per repo.

    Clones the sources that are given by a URL and are not on disk yet. With
    ``refresh`` the clones are also brought up to date with ``git pull
    --ff-only``; a checkout given by ``path`` is never pulled. A source that
    cannot be resolved prints one line and is left out.

    A resolved repo that holds its own ``installer.toml`` contributes its
    sources too, one nested level deep. Clones from a nested file go under the
    same root. A path that is already in the result is not visited again, so a
    file that points back at its parent cannot loop.

    ``clone=False`` resolves from the filesystem alone and never reaches the
    network, which is what the login check needs.
    """
    root = Path(os.path.expanduser(str(root))).absolute()
    found: List[Path] = []
    _resolve_level(sources, root, refresh, log, clone, "installer.toml", 0, found, set())
    return found


# --- the convenience wrapper ------------------------------------------------

def _take_root(argv: List[str]):
    """Pull ``--root DIR`` out of ``argv``. The engine owns every other flag."""
    rest, root = [], None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--root" and i + 1 < len(argv):
            root = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--root="):
            root = arg[len("--root="):]
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, root


def default_root(name: str = "tools") -> str:
    """The suggested install location: ``<current directory>/<name>``.

    Absolute, because the user is shown this string and has to recognise where
    it points. It is only ever a suggestion: what the user types in the dialog
    or on stdin wins, and ``--root`` wins over both.
    """
    return str(Path.cwd().joinpath(name).absolute())


# Flags that mean nobody is watching the screen, so nothing may block on input.
HEADLESS_FLAGS = ("--list", "--check", "--apply")


def _is_headless(argv: Sequence[str]) -> bool:
    return any(arg in HEADLESS_FLAGS or arg.startswith("--apply=") for arg in argv)


def _wants_gui(argv: Sequence[str]) -> bool:
    """True when the tkinter window is the screen this run will open."""
    from . import tui_installer  # noqa: PLC0415 — pulls in the engine, keep it lazy
    from .gui_installer import _HAVE_TK  # noqa: PLC0415
    return not tui_installer.prefer_tui(force_tui="--tui" in argv,
                                        force_gui="--gui" in argv,
                                        have_tk=_HAVE_TK)


def _ask_root_gui(default: str) -> Optional[str]:
    """A small window asking where the tools go. None when the user cancels."""
    import tkinter as tk  # noqa: PLC0415
    from tkinter import filedialog  # noqa: PLC0415

    win = tk.Tk()
    win.title("Install location")
    chosen: List[str] = []
    value = tk.StringVar(value=default)

    tk.Label(win, text="Where should the tools be installed?",
             font=("", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 6))
    tk.Label(win, text="The folder is created if it does not exist.",
             justify="left").pack(anchor="w", padx=16)

    row = tk.Frame(win)
    row.pack(fill="x", padx=16, pady=12)
    entry = tk.Entry(row, textvariable=value, width=54)
    entry.pack(side="left", fill="x", expand=True)

    def browse():
        picked = filedialog.askdirectory(parent=win, title="Install location",
                                         initialdir=os.path.dirname(value.get()) or "/")
        if picked:
            value.set(str(Path(picked).absolute()))

    tk.Button(row, text="Browse…", command=browse).pack(side="left", padx=(8, 0))

    buttons = tk.Frame(win)
    buttons.pack(fill="x", padx=16, pady=(0, 16))

    def ok(*_):
        text = value.get().strip()
        if text:
            chosen.append(text)
        win.destroy()

    tk.Button(buttons, text="OK", command=ok, width=10).pack(side="right")
    tk.Button(buttons, text="Cancel", command=win.destroy, width=10).pack(side="right", padx=8)
    win.bind("<Return>", ok)
    win.protocol("WM_DELETE_WINDOW", win.destroy)
    entry.focus_set()
    entry.icursor("end")
    win.mainloop()
    return chosen[0] if chosen else None


def _ask_root_stdin(default: str) -> Optional[str]:
    """One line on stdin, before any curses screen opens. None on Ctrl-D."""
    try:
        answer = input(f"Where should the tools be installed? [{default}] ")
    except EOFError:
        return None
    return answer.strip() or default


def _resolve_root(config_path: Path, root_arg: Optional[str], argv: Sequence[str],
                  default_root_name: str, log: Callable = print) -> str:
    """Settle the clone root: the flag, the local file, the user, the default.

    Asks only when the first two miss. The tkinter dialog is used when this run
    opens the tkinter window, the stdin prompt when it opens the text screen on
    a terminal. A headless run (``--list``, ``--apply``, ``--check``, or a text
    screen without a terminal) never asks: it takes the default and says so.
    """
    if root_arg:
        return str(Path(os.path.expanduser(root_arg)).absolute())
    stored = local_root(config_path)
    if stored:
        return stored

    default = default_root(default_root_name)
    if _is_headless(argv):
        log(f"root: {default} (pass --root to change)")
        return default

    if _wants_gui(argv):
        answer = _ask_root_gui(default)
    elif sys.stdin is not None and sys.stdin.isatty():
        answer = _ask_root_stdin(default)
    else:
        log(f"root: {default} (pass --root to change)")
        return default

    if answer is None:
        print("No install location chosen, nothing was installed.")
        raise SystemExit(0)
    chosen = str(Path(os.path.expanduser(answer)).absolute())
    save_local_root(config_path, chosen, log=log)
    return chosen


def run_installer(config_path, argv=None, default_root_name: str = "tools", **run_kwargs):
    """Read a sources file, wire the engine to it, and run the installer.

    ``--root DIR`` is taken from ``argv`` (``sys.argv[1:]`` by default) and the
    rest is left to the engine, so ``--list``, ``--apply``, ``--skill-target``,
    ``--check``, ``--tui`` and ``--gui`` keep working. ``--refresh`` stays the
    engine's flag: it reaches the pre-discovery hook, which then pulls every
    clone.

    The root is ``--root`` if given, else the ``root`` of
    ``installer.local.toml``, else the user's answer to a question, else — on a
    headless run only — ``<current directory>/<default_root_name>``. The answer
    is written into ``installer.local.toml``, so the question is asked once.
    The root directory is created if it does not exist. Cloning happens in the
    pre-discovery hook, which the engine skips on the ``--check`` path, so that
    check stays network-free and sees whatever is already on disk.

    ``default_root_name`` is the folder name the suggestion ends in, so an
    organisation's installer can suggest ``<cwd>/WW3-tools`` rather than
    ``<cwd>/tools``.

    Every other keyword goes to :func:`cli_tools_kit.gui_installer.run`.
    ``discovery_roots`` and ``pre_discovery`` are this function's to set.
    ``prune`` reaches the walker that way, so a wrapper can name directories
    its repos keep that hold no tools.
    """
    for reserved in ("discovery_roots", "pre_discovery"):
        if reserved in run_kwargs:
            raise TypeError(f"run_installer sets {reserved} itself")

    config_path = Path(config_path).absolute()
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, root_arg = _take_root(argv)
    sys.argv = [sys.argv[0]] + argv

    root = _resolve_root(config_path, root_arg, argv, default_root_name)
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        print(f"{root}: not created ({exc})")
    sources = load_sources(config_path)

    # The engine reads DISCOVERY_ROOTS after the hook has run, so the hook fills
    # this list in place with what it resolved. It is pre-filled with what is on
    # disk already, for the --check path that never calls the hook.
    roots = [str(p) for p in resolve_sources(sources, root, clone=False,
                                             log=lambda *_: None)]

    def pre_discovery(refresh):
        roots[:] = [str(p) for p in resolve_sources(sources, root, refresh=refresh)]

    from . import gui_installer  # noqa: PLC0415 — imports tkinter, keep it lazy
    run_kwargs.setdefault("root_dir", root)
    return gui_installer.run(discovery_roots=roots, pre_discovery=pre_discovery,
                             **run_kwargs)
