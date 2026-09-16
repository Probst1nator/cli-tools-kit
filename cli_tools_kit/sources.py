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

An entry may name a GitHub organisation instead of one repo. The installer
lists the org's repos, keeps the ones carrying a topic, and turns each into an
ordinary source, so everything after that step is unchanged:

    [[source]]
    org     = "AutomatedAlchemy"
    topic   = "cli-tool-kit"                  # the default when omitted
    exclude = ["alchemy-installer"]           # repo names to skip
    include = ["manim-kit"]                   # allowlist; wins over exclude

``org`` is mutually exclusive with ``url`` and ``path``. The listing is cached
for a day, a network error falls back to the cached list and then to the
directories already under the root, and an explicit ``[[source]]`` with the
same ``name`` always wins over an org-derived one.

The whole feature is three calls:

    sources = load_sources("installer.toml")
    roots = resolve_sources(sources, root="~/acme-tools")

or, for a wrapper that just wants the installer:

    run_installer(os.path.join(HERE, "installer.toml"),
                  identity=InstallerIdentity(slug="acme-tools"),
                  entry_script=__file__)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

__all__ = ["Source", "OrgSource", "load_sources", "expand_org_sources",
           "resolve_sources", "run_installer", "local_root", "save_local_root",
           "default_root"]

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


# The default GitHub topic an org's repos are tagged with to be offered.
DEFAULT_ORG_TOPIC = "cli-tool-kit"

# A GitHub org or user name, and a topic: both are pasted into a URL path, so
# they stay to the characters GitHub itself allows. \Z, not $, so a trailing
# newline cannot smuggle a second path segment in.
_ORG_RE = re.compile(r"^[A-Za-z0-9-]{1,39}\Z")
_TOPIC_RE = re.compile(r"^[A-Za-z0-9-]{1,50}\Z")

# The one host this module talks to, hard-coded so a config file cannot point
# the listing at somewhere else.
GITHUB_API = "https://api.github.com"

# How long a cached listing is used without asking GitHub again.
ORG_CACHE_TTL = 24 * 60 * 60

# Enough for 500 repos; a listing longer than that is a config mistake.
ORG_MAX_PAGES = 5

ORG_TIMEOUT = 10
GH_TOKEN_TIMEOUT = 5


@dataclass(frozen=True)
class OrgSource:
    """One GitHub organisation whose topic-tagged repos become sources.

    Expanded into ordinary :class:`Source` entries by
    :func:`expand_org_sources`, which is the only place in this module that
    reaches the network.
    """

    org: str
    topic: str = DEFAULT_ORG_TOPIC
    include: Tuple[str, ...] = ()
    exclude: Tuple[str, ...] = ()


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


def _names(entry: dict, key: str) -> Tuple[str, ...]:
    """One of the ``include`` / ``exclude`` lists, as a tuple of strings."""
    raw = entry.get(key)
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, str) and item)


def _org_source(entry: dict, config_name: str, log: Callable) -> Optional[OrgSource]:
    """One ``[[source]]`` table with an ``org``, or None when it is unusable.

    Reported and dropped the same way an https-only violation is: one line
    naming what is wrong, and the other sources still install.
    """
    org = entry.get("org")
    if not isinstance(org, str) or not _ORG_RE.match(org):
        log(f"{config_name}: a [[source]] with an unusable org "
            f"({org!r}), skipped")
        return None
    if entry.get("url") or entry.get("path"):
        log(f"{org}: a [[source]] cannot have both org and url/path, skipped")
        return None
    topic = entry.get("topic", DEFAULT_ORG_TOPIC)
    if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
        log(f"{org}: {topic!r} is not a usable topic, skipped")
        return None
    return OrgSource(org=org, topic=topic,
                     include=_names(entry, "include"),
                     exclude=_names(entry, "exclude"))


def load_sources(config_path, local_path=None, log: Callable = print) -> List:
    """The ``[[source]]`` entries of one TOML file, local overrides applied.

    ``local_path`` defaults to ``installer.local.toml`` next to ``config_path``.
    A ``path`` is taken relative to the file it is written in. An entry without
    a name is reported and dropped.

    An entry that carries an ``org`` instead of a ``name`` becomes an
    :class:`OrgSource` in the returned list. :func:`expand_org_sources` turns
    those into ordinary :class:`Source` entries; :func:`resolve_sources` ignores
    any that are left, so a caller that does not expand simply gets no tools
    from the org rather than an error.
    """
    config_path = Path(config_path)
    local_path = Path(local_path) if local_path is not None else _local_path_for(config_path)
    data = _read_toml(config_path, log)
    local = _read_toml(local_path, log)

    overrides = {entry["name"]: entry
                 for entry in local.get("source") or []
                 if isinstance(entry, dict) and entry.get("name")}

    sources: List = []
    for entry in data.get("source") or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not name:
            if "org" in entry:
                org_source = _org_source(entry, config_path.name, log)
                if org_source is not None:
                    sources.append(org_source)
                continue
            log(f"{config_path.name}: a [[source]] without a name, skipped")
            continue
        if entry.get("org"):
            log(f"{name}: a [[source]] cannot have both org and url/path, skipped")
            continue
        override = overrides.get(name, {}).get("path")
        raw = override or entry.get("path")
        base = local_path.parent if override else config_path.parent
        path = _absolute(base, raw) if raw else None
        sources.append(Source(name=name, url=entry.get("url"), path=path))
    return sources


# --- GitHub org listings ----------------------------------------------------

def _gh_token() -> Optional[str]:
    """The token ``gh auth token`` prints, or None.

    Opportunistic: with the GitHub CLI logged in, the listing also sees the
    org's private repos. Without it the public listing is used. The token is
    never logged.
    """
    if not shutil.which("gh"):
        return None
    try:
        result = subprocess.run(["gh", "auth", "token"], capture_output=True,
                                text=True, timeout=GH_TOKEN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def _api_version() -> str:
    from . import __version__  # noqa: PLC0415 — avoids an import cycle at module load
    return __version__


def _fetch_org_repos(org: str, token: Optional[str]) -> List[dict]:
    """Every repo of one org, over as many pages as GitHub needs.

    Raises ``OSError`` (which ``urllib`` errors are) on anything that goes
    wrong, so the one caller can fall back in a single place.
    """
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": f"cli-tools-kit/{_api_version()}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repos: List[dict] = []
    for page in range(1, ORG_MAX_PAGES + 1):
        query = f"per_page=100&page={page}"
        if not token:
            # Without a token only public repos are visible anyway; asking for
            # them explicitly keeps the response small.
            query += "&type=public"
        request = urllib.request.Request(  # noqa: S310 — the host is hard-coded above
            f"{GITHUB_API}/orgs/{org}/repos?{query}", headers=headers)
        with urllib.request.urlopen(request, timeout=ORG_TIMEOUT) as response:
            batch = json.loads(response.read().decode("utf-8"))
        if not isinstance(batch, list):
            raise OSError("the listing was not a JSON array")
        repos.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return repos


def _keep_fields(repos: Sequence[dict]) -> List[dict]:
    """Only the fields this module uses, so the cache stays small and readable."""
    keep = ("name", "clone_url", "topics", "archived", "default_branch",
            "description")
    return [{field_name: repo.get(field_name) for field_name in keep}
            for repo in repos if repo.get("name")]


def _cache_file(cache_dir, org: str) -> Path:
    return Path(os.path.expanduser(str(cache_dir))) / f"org-{org}.json"


def _read_org_cache(cache_dir, org: str) -> Optional[dict]:
    """The cached listing for one org, or None when there is none to read."""
    try:
        with open(_cache_file(cache_dir, org), encoding="utf-8") as fh:
            cached = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cached, dict) or not isinstance(cached.get("repos"), list):
        return None
    return cached


def _write_org_cache(cache_dir, org: str, topic: str, repos: Sequence[dict],
                     log: Callable) -> None:
    path = _cache_file(cache_dir, org)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": time.time(), "topic": topic,
                                    "repos": list(repos)}, indent=1),
                        encoding="utf-8")
    except OSError as exc:
        log(f"{org}: the listing was not cached ({exc})")


def _age(seconds: float) -> str:
    """"2h ago", "3 days ago" — how long ago a listing was fetched."""
    delta = max(0.0, time.time() - seconds)
    if delta < 90 * 60:
        return f"{int(delta // 60)}min ago"
    if delta < 36 * 3600:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)} days ago"


def _selected(entry: OrgSource, repos: Sequence[dict]) -> List[dict]:
    """The repos of one listing this entry offers.

    ``include`` is an allowlist and wins over ``exclude``; the topic is still
    required either way, so a repo that lost its tag stops being offered
    without anyone having to edit the config.
    """
    chosen = []
    for repo in repos:
        name = repo.get("name")
        if not name or repo.get("archived"):
            continue
        topics = repo.get("topics") or []
        if entry.topic not in topics:
            continue
        if entry.include:
            if name not in entry.include:
                continue
        elif name in entry.exclude:
            continue
        chosen.append(repo)
    return chosen


def _from_root(entry: OrgSource, root, log: Callable) -> List[Source]:
    """The last fallback: the org's checkouts that are already under the root.

    No listing and no cache, so what is on disk is all this run knows about.
    A path-only source, because without a listing there is no clone URL.
    """
    base = Path(os.path.expanduser(str(root)))
    found = []
    try:
        entries = sorted(item for item in base.iterdir() if item.is_dir())
    except OSError:
        entries = []
    for item in entries:
        if entry.include:
            if item.name not in entry.include:
                continue
        elif item.name in entry.exclude:
            continue
        found.append(Source(name=item.name, path=str(item.resolve())))
    log(f"{entry.org}: no listing and no cache, using the "
        f"{len(found)} checkout(s) already under {base}")
    return found


def _org_listing(entry: OrgSource, *, refresh: bool, clone: bool, cache_dir,
                 log: Callable) -> Optional[Tuple[List[dict], str]]:
    """One org's repo list plus the line describing where it came from.

    None when neither the network nor the cache produced one. ``clone=False``
    is the network-free path, so it never fetches.
    """
    cached = _read_org_cache(cache_dir, entry.org)
    fetched_at = cached.get("fetched_at") if cached else None
    fresh_enough = (isinstance(fetched_at, (int, float))
                    and time.time() - fetched_at < ORG_CACHE_TTL)

    if not clone:
        if cached is None:
            return None
        return cached["repos"], f"cached listing from {_age(fetched_at or 0)}"
    if cached is not None and fresh_enough and not refresh:
        return cached["repos"], f"listed {_age(fetched_at)}"

    try:
        repos = _keep_fields(_fetch_org_repos(entry.org, _gh_token()))
    except (OSError, ValueError, urllib.error.HTTPError) as exc:
        reason = getattr(exc, "reason", None) or exc
        if cached is None:
            return None
        log(f"{entry.org}: not listed, using the cached listing from "
            f"{_age(fetched_at or 0)} ({reason})")
        return cached["repos"], f"cached listing from {_age(fetched_at or 0)}"

    if cached is not None:
        before = {repo.get("name") for repo in _selected(entry, cached["repos"])}
        gone = sorted(before - {repo.get("name") for repo in _selected(entry, repos)})
        if gone:
            log(f"{entry.org}: {len(gone)} repos dropped since the last "
                f"listing: {', '.join(gone)}")
    _write_org_cache(cache_dir, entry.org, entry.topic, repos, log)
    return repos, "listed just now"


def expand_org_sources(sources: Sequence, *, refresh: bool = False, cache_dir,
                       root=None, clone: bool = True,
                       log: Callable = print) -> List[Source]:
    """Replace every :class:`OrgSource` with the repos it stands for.

    Each kept repo becomes an ordinary ``Source(name=<repo>, url=<clone_url>)``,
    so resolution, cloning, nesting, discovery and the ``--advertise`` probe all
    work on it unchanged. A repo that carries the topic but turns out to hold no
    tool clones, advertises nothing, and is dropped by the walker as any other
    directory is.

    An explicit ``[[source]]`` with the same ``name`` wins, so one repo can be
    pinned to a fork or a local checkout while the rest of the org follows the
    listing.

    ``clone=False`` is the network-free path the login check takes: it uses the
    cache, then the directories already under ``root``, and never fetches.
    """
    explicit = {source.name for source in sources if isinstance(source, Source)}
    expanded: List[Source] = []
    for source in sources:
        if isinstance(source, Source):
            expanded.append(source)
            continue
        if not isinstance(source, OrgSource):
            continue
        listing = _org_listing(source, refresh=refresh, clone=clone,
                               cache_dir=cache_dir, log=log)
        if listing is None:
            derived = _from_root(source, root, log) if root is not None else []
        else:
            repos, provenance = listing
            kept = _selected(source, repos)
            log(f"{source.org}: {len(kept)} repos tagged {source.topic} "
                f"({provenance})")
            derived = [Source(name=repo["name"], url=repo.get("clone_url"))
                       for repo in kept]
        for candidate in derived:
            if candidate.name in explicit:
                continue
            url = candidate.url
            if url is not None and not str(url).lower().startswith("https://"):
                log(f"{candidate.name}: {url} is not an https:// URL, skipped")
                continue
            explicit.add(candidate.name)
            expanded.append(candidate)
    return expanded


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
        if not isinstance(source, Source):
            # An OrgSource nobody expanded. Ignored rather than fatal, so a
            # caller that skipped expand_org_sources still installs the rest.
            continue
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

    An ``org`` entry in the sources file is expanded into one source per
    topic-tagged repo before resolution, using the identity's cache directory
    for the listing. That expansion is the only network call this module makes
    besides git, it happens only when such an entry exists, and on the
    ``--check`` path it reads the cache instead of GitHub.

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
    from .identity import LEGACY_IDENTITY  # noqa: PLC0415 — keeps the import graph flat
    identity = run_kwargs.get("identity") or LEGACY_IDENTITY
    cache_dir = identity.cache_path

    # The engine reads DISCOVERY_ROOTS after the hook has run, so the hook fills
    # this list in place with what it resolved. It is pre-filled with what is on
    # disk already, for the --check path that never calls the hook — which is
    # why the expansion here is the network-free one.
    quiet = lambda *_: None  # noqa: E731
    roots = [str(p) for p in resolve_sources(
        expand_org_sources(sources, cache_dir=cache_dir, root=root, clone=False,
                           log=quiet),
        root, clone=False, log=quiet)]

    def pre_discovery(refresh):
        expanded = expand_org_sources(sources, refresh=refresh, cache_dir=cache_dir,
                                      root=root)
        roots[:] = [str(p) for p in resolve_sources(expanded, root, refresh=refresh)]

    from . import gui_installer  # noqa: PLC0415 — imports tkinter, keep it lazy
    run_kwargs.setdefault("root_dir", root)
    return gui_installer.run(discovery_roots=roots, pre_discovery=pre_discovery,
                             **run_kwargs)
