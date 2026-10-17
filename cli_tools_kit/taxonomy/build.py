"""Compute the tool grouping and write data/tool_groups.json.

The one place that decides the bands and writes the file. regroup.py calls it
to rebuild on demand; groups.ensure_groups calls it when the installer starts
and the stored grouping no longer matches the corpus.

Three ways to get there, tried in order:

1. **gemini** — reads a condensed blurb per tool and returns named categories
   with their members. The normal path: naming a category is a language task.
2. **capability**, when a grouping is already stored — keep those bands and
   their names, and file only the tools missing from them by the capability
   word they advertise. No network. Preferred over re-embedding because it
   preserves names a model chose over names tf-idf would invent.
3. **embed** — no stored grouping to extend, so cluster the full documents and
   name each cluster after its most distinctive token. Needs an embedding
   backend; if that is missing too, fall back to capability words alone.

Only the first needs an API key, and only steps 1 and 3 need a network. A
distributed checkout ships with data/tool_groups.json already written, so it
never reaches any of them until its owner adds a tool of their own.
"""

import datetime
import json
import os
import tempfile
from typing import Dict, List, Optional, Tuple

from .capability import capability_groups
from .cluster import cluster_documents
from .corpus import (
    corpus_fingerprint,
    tool_blurbs,
    tool_capabilities,
    tool_documents,
)
from .embedder import EmbeddingUnavailable, backend_name, embed_texts, load_tree_key
from .groups import groups_path, read_groups_file
from .llm_groups import (
    LLMGroupingUnavailable,
    active_label,
    llm_groups,
)

DEFAULT_K = 6
DEFAULT_SEED = 0


def build_groups(
    root: str,
    k: int = DEFAULT_K,
    seed: int = DEFAULT_SEED,
    write: bool = True,
    method: str = "auto",
    budget: Optional[float] = None,
    rename: bool = False,
) -> dict:
    """Decide the grouping and (unless write=False) store it.

    ``budget`` caps the LLM tier in seconds — the installer passes one, a
    deliberate ``regroup.py`` run does not. ``rename`` throws the stored
    category names away and asks for six fresh ones.

    ``method`` is "auto" (the chain described above), or one tier by name:
    "llm", "embed" or "capability". Raises ValueError when the tree holds no
    tools, and EmbeddingUnavailable / LLMGroupingUnavailable when a single tier
    was asked for and could not run. Existing overrides are carried over
    untouched.
    """
    from .corpus import tool_dirs

    names = tool_dirs(root)
    if not names:
        raise ValueError("no tools found")
    load_tree_key(root)
    previous = read_groups_file(root)
    stored = previous.get("labels") or {}
    categories = None if rename else previous.get("categories")
    if not isinstance(categories, list) or len(categories) != k:
        categories = None

    labels: Dict[str, List[str]] = {}
    used = ""
    if method in ("auto", "llm"):
        try:
            labels = llm_groups(
                tool_blurbs(root), k, budget=budget, categories=categories
            )
            used = active_label() or "llm"
        except LLMGroupingUnavailable:
            if method == "llm":
                raise

    if not labels and method in ("auto", "capability"):
        if stored or method == "capability":
            labels = capability_groups(tool_capabilities(root), stored or None)
            used = "capability" + (":extended" if stored else "")

    if not labels and method in ("auto", "embed"):
        try:
            labels = _embed_groups(root, names, k, seed)
            used = f"embed-kmeans:{backend_name()}"
        except EmbeddingUnavailable:
            if method == "embed":
                raise

    if not labels:
        labels = capability_groups(tool_capabilities(root), stored or None)
        used = "capability"

    payload = {
        "generated": datetime.date.today().isoformat(),
        "k": k,
        "seed": seed,
        "method": used,
        "categories": _carry_scopes(labels, categories),
        "fingerprint": corpus_fingerprint(root),
        "labels": labels,
        "overrides": read_groups_file(root).get("overrides") or {},
    }
    if write:
        write_groups_file(root, payload)
    return payload


def _carry_scopes(
    labels: Dict[str, List[str]], categories: Optional[List[dict]]
) -> List[dict]:
    """The band names to reuse next time, keeping the scope of each survivor.

    A band the review step renamed keeps its members but loses its scope; that
    is fine, the scope only steers the next assignment.
    """
    scopes = {c.get("name"): c.get("scope", "") for c in (categories or [])}
    return [{"name": name, "scope": scopes.get(name, "")} for name in labels]


def _embed_groups(
    root: str, names: List[str], k: int, seed: int
) -> Dict[str, List[str]]:
    """Offline fallback: embed the full documents and k-means them."""
    docs = tool_documents(root)
    ordered = sorted(docs)
    vectors = embed_texts([docs[n] for n in ordered])
    groups: List[Tuple[str, List[str]]] = cluster_documents(
        ordered, vectors, docs, k, seed=seed
    )
    return {label: members for label, members in groups}


def write_groups_file(root: str, payload: dict) -> str:
    """Write the payload atomically so a concurrent reader sees whole JSON."""
    path = groups_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


__all__ = ["DEFAULT_K", "DEFAULT_SEED", "build_groups", "write_groups_file",
           "EmbeddingUnavailable", "LLMGroupingUnavailable"]
