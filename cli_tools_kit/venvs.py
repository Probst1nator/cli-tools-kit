"""One venv per tool, shared by every installer that offers that tool.

A host often carries the same tool twice. Two installers each clone
``manim-kit`` from the same GitHub repo into their own tree, and each
provisions its own ``.venv`` next to the checkout — two copies of manim, two
copies of Kokoro, 666M apiece, from one upstream.

This module puts the venv somewhere neither checkout owns:

    <IDENTITY.cache_path>/venvs/<key>/

``key`` comes from the checkout's ``origin`` remote, normalised so that
``git@github.com:Org/tool`` and ``https://github.com/Org/tool.git`` agree
(:func:`upstream_key`). Both checkouts resolve to the same directory, so the
second install finds the environment already built. A checkout with no origin
remote is nobody's duplicate: it keeps a private ``.venv`` in the tool
directory, exactly as before this module existed.

Requirements drift between checkouts
------------------------------------
Two checkouts of one tool sit at different commits, so their
``requirements.txt`` differ. The shared venv records what it last installed in
``.cli-tools-kit.json`` (:func:`read_manifest`) together with the checkout and
commit that wrote it. :func:`ensure_venv` compares the incoming requirement
lines against that record and reinstalls when they differ, so whichever
checkout ran most recently has its dependencies present.

Where one checkout's requirements are a superset of the other's — the usual
case, a tool that grew a dependency — pip converges and both checkouts work.
Where two pins genuinely contradict each other pip fails, and the error names
the checkout that last wrote the venv, because that is the one holding the
environment the current install is fighting.

Adopting what is already on disk
--------------------------------
A per-checkout ``.venv`` from before the move is not deleted. The first
install after the upgrade adopts one (:func:`adopt_existing`) as the shared
venv when no shared venv exists yet; any further ones are left alone on disk
for the user to remove. Deleting a few hundred megabytes is the user's call,
so the installer only reports them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Optional

MANIFEST_NAME = ".cli-tools-kit.json"

# git@host:org/repo.git and ssh://git@host/org/repo both mean the same upstream
# as https://host/org/repo — strip the transport so the key does not.
_SCP_LIKE = re.compile(r"^(?:[\w.-]+@)?([\w.-]+):(.+)$")
_URL_LIKE = re.compile(r"^[a-z][a-z0-9+.-]*://(?:[^@/]+@)?([\w.-]+)/(.+)$", re.I)


def normalise_remote(url: str) -> Optional[str]:
    """``host/path`` for a git remote, or None when it does not look like one.

    Lowercased and stripped of the transport, any credentials, a trailing
    ``.git`` and trailing slashes, so every spelling of one repo gives one
    string::

        git@github.com:Org/tool.git   -> github.com/org/tool
        https://github.com/Org/tool   -> github.com/org/tool

    A remote that is a local path — a bare repo on this machine, or a mirror on
    a mounted share — is resolved to its absolute path under a ``local`` host.
    Two clones of one such repo are as much the same upstream as two clones of
    one GitHub repo, and share a venv on the same terms. Case is preserved
    here: unlike a hostname, a POSIX path distinguishes it.
    """
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("file://"):
        url = url[len("file://"):]
    # A local path first: on Windows `C:\repos\tool` also matches the scp-like
    # pattern, and reading it as host `c` would be wrong.
    if url.startswith(("/", "./", "../", "~")) or os.path.isabs(url) \
            or os.path.isdir(url):
        path = os.path.normpath(os.path.abspath(os.path.expanduser(url)))
        if path.endswith(".git"):
            path = path[: -len(".git")]
        return f"local{path}" if path.startswith("/") else f"local/{path}"
    m = _URL_LIKE.match(url) or _SCP_LIKE.match(url)
    if not m:
        return None
    host, path = m.group(1), m.group(2)
    path = path.strip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    if not path:
        return None
    return f"{host.lower()}/{path.lower()}"


def origin_url(tool_dir: str) -> Optional[str]:
    """The ``origin`` remote of the checkout at ``tool_dir``, if it has one."""
    try:
        result = subprocess.run(
            ["git", "-C", tool_dir, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def head_commit(tool_dir: str) -> Optional[str]:
    """Short HEAD of the checkout, for the manifest's record of who wrote it."""
    try:
        result = subprocess.run(
            ["git", "-C", tool_dir, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def upstream_key(tool_dir: str) -> Optional[str]:
    """Stable directory name for the venv shared by one upstream repo.

    None when the checkout has no usable ``origin``, which means it cannot be
    matched with any other checkout and so keeps a private venv.
    """
    normalised = normalise_remote(origin_url(tool_dir) or "")
    if not normalised:
        return None
    digest = hashlib.sha256(normalised.encode("utf-8")).hexdigest()[:12]
    # The leaf name is readable on purpose: a user looking through the cache
    # should see which tool a directory belongs to without resolving a hash.
    leaf = re.sub(r"[^a-z0-9]+", "-", normalised.rsplit("/", 1)[-1]).strip("-")
    return f"{leaf}-{digest}" if leaf else digest


def venvs_root() -> str:
    """Directory holding every shared venv for this installer's organisation.

    ``gui_installer.IDENTITY`` is rebound when a wrapper calls ``run()``, so it
    is read here at call time rather than imported once — two organisations on
    one host must not share a cache directory.
    """
    from . import gui_installer
    return os.path.join(gui_installer.IDENTITY.cache_path, "venvs")


def venv_python(venv_dir: str) -> str:
    """Interpreter path inside ``venv_dir`` on this platform."""
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python3")


def private_venv(tool_dir: str) -> str:
    """The pre-0.9 location: a ``.venv`` beside the tool's own script."""
    return os.path.join(tool_dir, ".venv")


def venv_for(tool_dir: str) -> str:
    """Where this checkout's venv belongs — shared when it has an upstream."""
    key = upstream_key(tool_dir)
    if not key:
        return private_venv(tool_dir)
    return os.path.join(venvs_root(), key)


def requirement_lines(requirements: str) -> list:
    """Requirement lines of a requirements file, without comments or blanks."""
    try:
        with open(requirements, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def manifest_path(venv_dir: str) -> str:
    return os.path.join(venv_dir, MANIFEST_NAME)


def read_manifest(venv_dir: str) -> dict:
    """What this venv last installed, or ``{}`` when it has no record."""
    try:
        with open(manifest_path(venv_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(venv_dir: str, requirements: list, tool_dir: str) -> None:
    """Record the requirements just installed and the checkout that asked."""
    data = {
        "requirements": list(requirements),
        "written_by": os.path.abspath(tool_dir),
        "commit": head_commit(tool_dir),
    }
    try:
        os.makedirs(venv_dir, exist_ok=True)
        with open(manifest_path(venv_dir), "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except OSError:
        pass  # a venv that works but cannot be annotated still works


def adopt_existing(tool_dir: str, venv_dir: str) -> bool:
    """Move a pre-existing private ``.venv`` into the shared location.

    Only when the shared venv does not exist yet, so the first install after
    the upgrade reuses what is already built instead of downloading it again.
    Returns True when a venv was adopted. Other checkouts' private venvs are
    left where they are; :func:`stale_private_venvs` reports them.
    """
    private = private_venv(tool_dir)
    if os.path.abspath(private) == os.path.abspath(venv_dir):
        return False
    if os.path.exists(venv_dir) or not os.path.isdir(private):
        return False
    if not os.path.exists(venv_python(private)):
        return False
    try:
        os.makedirs(os.path.dirname(venv_dir), exist_ok=True)
        os.rename(private, venv_dir)
    except OSError:
        return False  # across filesystems, or in use: build a fresh one instead
    # A venv records its own location in pyvenv.cfg and in the shebangs of its
    # scripts, so a moved one is only safe through its interpreter, never
    # through `<venv>/bin/<entry-point>`. The kit always calls the interpreter.
    return True


def stale_private_venvs(tool_dirs) -> list:
    """Private ``.venv`` directories made redundant by a shared venv.

    Reported to the user, never deleted: these run to hundreds of megabytes
    and removing them is a decision the installer does not get to make.
    """
    out = []
    for tool_dir in tool_dirs:
        private = private_venv(tool_dir)
        shared = venv_for(tool_dir)
        if os.path.abspath(private) == os.path.abspath(shared):
            continue
        if os.path.isdir(private) and os.path.isdir(shared):
            out.append(private)
    return out


def ensure_venv(tool_dir: str, requirements: Optional[str] = None,
                provision: bool = True) -> str:
    """Interpreter to run this tool with, building the venv when asked.

    Creates the shared venv if it is missing, adopting an existing private one
    first. Installs the checkout's requirements whenever they differ from what
    the manifest records, so a second checkout at a different commit gets its
    own dependencies rather than inheriting a stale set.

    With ``provision=False`` nothing is created or installed: the existing
    interpreter is returned when there is one, else the current one. That is
    the path taken by the login check and by a shortcuts-only refresh.
    """
    venv_dir = venv_for(tool_dir)
    py = venv_python(venv_dir)

    if not provision:
        return py if os.path.exists(py) else sys.executable

    if requirements is None:
        requirements = os.path.join(tool_dir, "requirements.txt")
    wanted = requirement_lines(requirements)
    if not wanted:
        # Nothing to install: a venv would add nothing over the interpreter
        # already running, and building one costs a directory for no gain.
        return py if os.path.exists(py) else sys.executable

    if not os.path.exists(py):
        adopt_existing(tool_dir, venv_dir)
    if not os.path.exists(py):
        try:
            os.makedirs(os.path.dirname(venv_dir), exist_ok=True)
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        except (OSError, subprocess.CalledProcessError):
            return sys.executable  # no venv to be had; the caller still runs

    manifest = read_manifest(venv_dir)
    if manifest.get("requirements") == wanted:
        return py if os.path.exists(py) else sys.executable

    try:
        subprocess.run([py, "-m", "pip", "install", "-r", requirements],
                       check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        owner = manifest.get("written_by")
        if owner and os.path.abspath(owner) != os.path.abspath(tool_dir):
            commit = manifest.get("commit") or "unknown commit"
            raise RuntimeError(
                f"{requirements} could not be installed into the venv shared "
                f"with {owner} (last written from {commit}). The two checkouts "
                f"pin dependencies that cannot both hold. Pip said: {exc}"
            ) from exc
        raise

    write_manifest(venv_dir, wanted, tool_dir)
    return py if os.path.exists(py) else sys.executable
