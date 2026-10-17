"""Build one text document per installable tool.

A tool's document is its CLAUDE.md plus its README.md. When it has neither,
the tool is asked for its --advertise metadata and the name/desc from that is
used instead.

:func:`corpus_fingerprint` hashes those same inputs without running anything,
so a caller can tell whether a stored grouping is still current.
"""

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Tuple

MAX_CHARS = 6000
BLURB_MAX_CHARS = 700
BLURB_PARAGRAPHS = 2
BLURB_HEADINGS = 8
DOC_FILES = ("CLAUDE.md", "README.md")
ADVERTISE_TIMEOUT = 5
PROBE_WORKERS = 16
SKIP_PREFIXES = ("_", ".")
SKIP_NAMES = {"dev"}


def tool_dirs(root: str) -> List[str]:
    """Names of the top-level directories that are installable tools."""
    found = []
    for name in sorted(os.listdir(root)):
        if name.startswith(SKIP_PREFIXES) or name in SKIP_NAMES:
            continue
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        if os.path.isfile(os.path.join(path, "main.py")) and os.path.isfile(
            os.path.join(path, "requirements.txt")
        ):
            found.append(name)
    return found


def _advertise_entries(tool_path: str) -> List[dict]:
    """Run the tool's --advertise and return its metadata entries."""
    main_py = os.path.join(tool_path, "main.py")
    try:
        proc = subprocess.run(
            [sys.executable, main_py, "--advertise"],
            capture_output=True,
            text=True,
            timeout=ADVERTISE_TIMEOUT,
            cwd=tool_path,
        )
    except (subprocess.SubprocessError, OSError):
        return []

    for line in reversed((proc.stdout or "").strip().splitlines()):
        line = line.strip()
        if not line.startswith(("[", "{")):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries = data if isinstance(data, list) else [data]
        return [e for e in entries if isinstance(e, dict)]
    return []


def _advertise_text(tool_path: str) -> str:
    """The tool's advertised names, descriptions and taxonomy words."""
    parts = []
    for entry in _advertise_entries(tool_path):
        for key in ("name", "desc", "capability", "domain"):
            value = entry.get(key)
            if value:
                parts.append(str(value))
    return "\n".join(parts)


def tool_capabilities(root: str) -> Dict[str, str]:
    """Map each tool to its advertised capability word, "" when it has none.

    The capability is hand-written, validated against CAPABILITY_VOCAB, and
    needs no network to read — which is what makes it usable as the last-resort
    grouping on a host with no API key.
    """
    caps = {}
    for name in tool_dirs(root):
        capability = ""
        for entry in _advertise_entries(os.path.join(root, name)):
            value = str(entry.get("capability") or "").strip()
            if value:
                capability = value
                break
        caps[name] = capability
    return caps


def tool_documents(root: str) -> Dict[str, str]:
    """Map each tool directory name to its document text."""
    docs = {}
    for name in tool_dirs(root):
        path = os.path.join(root, name)
        chunks = [name.replace("_", " ")]
        for doc_name in DOC_FILES:
            doc_path = os.path.join(path, doc_name)
            if os.path.isfile(doc_path):
                try:
                    with open(doc_path, encoding="utf-8", errors="replace") as fh:
                        chunks.append(fh.read())
                except OSError:
                    pass
        if len(chunks) == 1:
            advertised = _advertise_text(path)
            if advertised:
                chunks.append(advertised)
        docs[name] = "\n\n".join(chunks)[:MAX_CHARS]
    return docs


def _file_digest(path: str) -> str:
    """sha256 of a file's bytes, "" when it cannot be read."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def corpus_fingerprint(root: str) -> str:
    """Hash the inputs :func:`tool_documents` reads, without running them.

    Covers the set of tool directories and the contents of every CLAUDE.md and
    README.md. A tool with neither is represented by its main.py instead, since
    that is what the --advertise fallback reads. Content hashes rather than
    mtimes: a Syncthing checkout restamps mtimes fleet-wide without changing a
    byte.
    """
    digest = hashlib.sha256()
    for name in tool_dirs(root):
        path = os.path.join(root, name)
        digest.update(name.encode())
        documented = False
        for doc_name in DOC_FILES:
            doc_path = os.path.join(path, doc_name)
            if os.path.isfile(doc_path):
                documented = True
                digest.update(doc_name.encode())
                digest.update(_file_digest(doc_path).encode())
        if not documented:
            digest.update(b"main.py")
            digest.update(_file_digest(os.path.join(path, "main.py")).encode())
    return digest.hexdigest()


# --- condensed blurbs -----------------------------------------------------
#
# tool_documents() feeds an embedder, which tolerates bulk. The LLM grouping
# wants the opposite: a short, comparable description of what each tool is for.
# A tool's CLAUDE.md is mostly operational prose — commit policy, install
# mechanics, fleet paths — that says nothing about its subject, and the six
# tools with the longest docs would otherwise drown out the fourteen with none.

_SKIP_LINE_PREFIXES = ("|", ">", "<!--", "<", "!", "```", "---", "===", "*Note")
_DOC_STOP_HEADINGS = {
    "installation", "install", "usage", "requirements", "dependencies",
    "license", "licence", "testing", "tests", "development", "changelog",
    "troubleshooting", "configuration", "config", "files", "layout",
    "architecture overview", "environment variables", "cli protocol",
}


def _clean_markdown(text: str) -> Tuple[List[str], List[str]]:
    """Split a markdown doc into (prose paragraphs, heading names).

    Code fences, tables, block quotes, badges and raw HTML are dropped: they
    carry syntax, not subject.
    """
    paragraphs: List[str] = []
    headings: List[str] = []
    buffer: List[str] = []
    in_fence = False

    def flush():
        if buffer:
            para = " ".join(buffer).strip()
            # A paragraph of mostly punctuation or paths is not a description.
            if len(para) >= 40 and sum(c.isalpha() for c in para) > len(para) / 2:
                paragraphs.append(para)
            buffer.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_fence = not in_fence
            flush()
            continue
        if in_fence:
            continue
        if not line:
            flush()
            continue
        if line.startswith("#"):
            flush()
            name = line.lstrip("#").strip().strip("*_`")
            if name and name.lower() not in _DOC_STOP_HEADINGS:
                headings.append(name)
            continue
        if line.startswith(_SKIP_LINE_PREFIXES):
            flush()
            continue
        buffer.append(line.lstrip("-*+ ").strip())
    flush()
    return paragraphs, headings


def _advertise_fields(tool_path: str) -> List[str]:
    """Advertised name/desc/capability/domain lines, deduplicated in order."""
    text = _advertise_text(tool_path)
    seen = set()
    fields = []
    for line in text.splitlines():
        line = line.strip()
        if line and line.lower() not in seen:
            seen.add(line.lower())
            fields.append(line)
    return fields


def tool_blurb(root: str, name: str) -> str:
    """A short, comparable description of one tool, for the LLM grouping.

    Built from, in order: the advertised name/desc/capability/domain, the first
    couple of real prose paragraphs of CLAUDE.md or README.md, and that doc's
    section headings (which name features in very few tokens). Capped at
    BLURB_MAX_CHARS so a heavily documented tool cannot outweigh a bare one.
    """
    path = os.path.join(root, name)
    parts = [f"directory: {name}"]

    fields = _advertise_fields(path)
    if fields:
        parts.append("advertises: " + " | ".join(fields))

    for doc_name in DOC_FILES:
        doc_path = os.path.join(path, doc_name)
        if not os.path.isfile(doc_path):
            continue
        try:
            with open(doc_path, encoding="utf-8", errors="replace") as fh:
                text = fh.read(MAX_CHARS * 2)
        except OSError:
            continue
        paragraphs, headings = _clean_markdown(text)
        if paragraphs:
            parts.append(" ".join(paragraphs[:BLURB_PARAGRAPHS]))
        if headings:
            parts.append("sections: " + "; ".join(headings[:BLURB_HEADINGS]))
        break

    return "\n".join(parts)[:BLURB_MAX_CHARS]


def tool_blurbs(root: str) -> Dict[str, str]:
    """One condensed blurb per tool.

    Each blurb runs the tool's --advertise, so they are built up to
    PROBE_WORKERS at a time, as discovery runs its probes.
    """
    names = tool_dirs(root)
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(names), PROBE_WORKERS)) as pool:
        return dict(zip(names, pool.map(lambda name: tool_blurb(root, name), names)))
