"""Ask Gemini to sort the tools into named categories.

k-means over document embeddings produced defensible clusters with
indefensible names: the label was the most distinctive *token* in a cluster,
so a band holding arxiv_summary, scrape and extract_pdf_annotations came out
called "youtube". Naming a category is a language task, so a language model
does it — and it assigns the members at the same time, which also fixes the
clusters that were only held together by shared boilerplate.

Three steps, not one. Asked to name and fill six categories in a single call,
flash-lite reliably produced five plausible ones and swept the remaining third
of the collection into a "System & Utility" bin. The pipeline instead:

1. name — propose the k categories from one-line advertised summaries;
2. assign — file every tool into those fixed names, using the full blurbs;
3. review — show the model what each category ended up holding, and let it
   move the misfits and rename a category to match its actual contents.

Step 3 is a patch, not a rewrite: only valid moves and renames are applied, so
a confused reply degrades to the step-2 result rather than replacing it.

A rebuild only happens when the corpus fingerprint moves (see
groups.ensure_groups), so this is about three cheap calls per documentation
edit. When it cannot run, build.py falls back to the offline embedding path.
"""

import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Sequence, Tuple

MODEL = "gemini-3.5-flash-lite"
# Tried in order on the local OpenAI-compatible server (LM Studio). The small
# one first: this is a classification job, not a writing job.
LOCAL_MODELS = ("google/gemma-4-e2b", "qwen/qwen3.6-35b-a3b")
GEMINI_TIMEOUT = 60.0
LOCAL_TIMEOUT = 300.0
# A local model is small and its context is short (the default gemma-4-e2b load
# offers 4096 tokens, and the assignment prompt for 36 tools is 4145). On that
# backend the tools are described by their one-line summary and filed in
# batches that fit this budget, rather than all at once with full blurbs.
LOCAL_TOKEN_BUDGET = 2400

# LM Studio rejects response_format "json_object" ("must be 'json_schema' or
# 'text'"), and a small model holds its shape far better with a schema than
# with an instruction, so each step declares one.
_STRING_MAP = {"type": "object", "additionalProperties": {"type": "string"}}
CATEGORIES_SCHEMA = {
    "type": "object",
    "properties": {
        "categories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "scope": {"type": "string"},
                },
                "required": ["name"],
            },
        }
    },
    "required": ["categories"],
}
ASSIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {"assignment": _STRING_MAP},
    "required": ["assignment"],
}
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"moves": _STRING_MAP, "renames": _STRING_MAP},
}
MAX_NAME_CHARS = 24
_RETRIES = 2
_BANNED_NAMES = {
    "misc", "miscellaneous", "other", "others", "utilities", "utility",
    "tools", "general", "various", "assorted", "extras",
}


class LLMGroupingUnavailable(RuntimeError):
    """The model could not be reached, or would not answer usably."""


def model_name() -> str:
    """Gemini model used for the grouping; TOOLS_GROUPS_MODEL overrides."""
    return os.getenv("TOOLS_GROUPS_MODEL", "") or MODEL


def local_models() -> List[str]:
    """Local models to try, in order; TOOLS_GROUPS_LOCAL_MODEL overrides."""
    pinned = os.getenv("TOOLS_GROUPS_LOCAL_MODEL", "").strip()
    return [pinned] if pinned else list(LOCAL_MODELS)


def _have_api_key() -> bool:
    """True when a Gemini key is reachable (the repo .env counts)."""
    try:
        from .embedder import _load_env
        _load_env()
    except ImportError:  # pragma: no cover - depends on the venv
        pass
    return bool(_gemini_key())


def backend_order() -> List[str]:
    """Which backends to try, in order.

    TOOLS_GROUPS_BACKEND pins one ("local" or "gemini"). The default is to use
    Gemini when a key is present and the local LM Studio server otherwise —
    so this fleet keeps the better model, and a checkout without a key still
    gets a real grouping instead of the capability fallback.
    """
    pinned = os.getenv("TOOLS_GROUPS_BACKEND", "").strip().lower()
    if pinned in ("local", "gemini"):
        return [pinned]
    return ["gemini", "local"] if _have_api_key() else ["local", "gemini"]


def size_band(n: int, k: int) -> Tuple[int, int]:
    """Smallest and largest sensible category size for n tools in k groups."""
    return max(2, (n // k) // 2), math.ceil(n / k) + 3


# --- prompts --------------------------------------------------------------

def _one_liner(name: str, blurb: str) -> str:
    """The advertised summary line of a blurb, for the naming call."""
    for line in blurb.splitlines():
        if line.startswith("advertises:"):
            return f"{name}: {line[len('advertises:'):].strip()[:120]}"
    return f"{name}: {blurb.splitlines()[-1][:120] if blurb else ''}"


def naming_prompt(blurbs: Dict[str, str], k: int) -> str:
    """Stage one: propose the k category names from one-line summaries."""
    n = len(blurbs)
    low, high = size_band(n, k)
    listing = "\n".join(_one_liner(name, blurbs[name]) for name in sorted(blurbs))
    return (
        f"Here are {n} personal command-line and GUI tools, one per line as "
        "'id: advertised name | description | capability word'.\n\n"
        f"{listing}\n\n"
        f"Propose exactly {k} category names to file them under in a graphical "
        "installer's sidebar. Each name is one or two words, title case, at "
        f"most {MAX_NAME_CHARS} characters, and names a subject a user would "
        "look under. No catch-all ('Misc', 'Other', 'Utilities', 'Tools', "
        f"'General'). Together the {k} must cover all {n} tools with roughly "
        f"{low}-{high} tools each, so choose boundaries that split the "
        "collection evenly rather than one broad name that would absorb "
        "everything left over.\n"
        'Answer as JSON: {"categories": [{"name": "...", "scope": "one line '
        'on what belongs here"}]}'
    )


def assignment_prompt(
    blurbs: Dict[str, str],
    categories: Sequence[dict],
    k: int,
    total: Optional[int] = None,
) -> str:
    """Step two: file every tool under one of the fixed category names.

    ``total`` is the size of the whole collection when this prompt covers only
    a batch of it, which changes the size guidance: a batch must not be
    balanced against itself.
    """
    n = len(blurbs)
    block = "\n".join(
        f"- {c['name']}: {c.get('scope', '')}" for c in categories
    )
    bodies = "\n\n".join(f"### {name}\n{blurbs[name]}" for name in sorted(blurbs))
    if total and total != n:
        sizing = (
            f"These {n} are one batch of {total} tools being filed into the "
            "same categories, so do not try to balance the batch: file each "
            "tool where it belongs, even if that fills one category."
        )
    else:
        low, high = size_band(n, k)
        sizing = f"Each category ends up with roughly {low}-{high} tools."
    return (
        f"File each of these {n} tools under exactly one of the {k} "
        f"categories.\n\nCategories:\n{block}\n\n"
        f"{sizing}\n"
        "Rules: every tool id appears exactly once; use the ids verbatim, "
        "with their original capitalisation; file by subject and ignore the "
        "install, git and sync prose a description may carry.\n"
        'Answer as JSON: {"assignment": {"tool_id": "Category Name", ...}}\n\n'
        f"{bodies}"
    )


def _estimate_tokens(text: str) -> int:
    """Rough token count — four characters per token is close enough here."""
    return len(text) // 4 + 1


def batch_for_budget(
    blurbs: Dict[str, str], overhead: int, budget: int = LOCAL_TOKEN_BUDGET
) -> List[List[str]]:
    """Split tool names into batches whose prompts fit the token budget."""
    batches: List[List[str]] = [[]]
    used = overhead
    for name in sorted(blurbs):
        cost = _estimate_tokens(blurbs[name]) + 8
        if batches[-1] and used + cost > budget:
            batches.append([])
            used = overhead
        batches[-1].append(name)
        used += cost
    return [batch for batch in batches if batch]


# --- reply handling -------------------------------------------------------

def _clean_name(raw) -> str:
    """Tidy a category name and keep it inside MAX_NAME_CHARS.

    An over-long name is cut back to a whole word: "System & Development Tools"
    truncated blind reads "System & Development Too".
    """
    name = str(raw or "").strip().strip('"').strip()
    if not name or name.lower() in _BANNED_NAMES:
        return ""
    if len(name) > MAX_NAME_CHARS:
        cut = name[:MAX_NAME_CHARS + 1]
        if " " in cut.strip():
            cut = cut[:cut.rstrip().rfind(" ")]
        name = cut.strip()
    return name.strip(" &-,/·").strip()


def _extract_json(text: str) -> dict:
    """Parse the reply, tolerating a code fence or prose around the JSON."""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty reply")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def parse_categories(reply: str, k: int) -> List[dict]:
    """Stage-one reply -> [{"name", "scope"}], names cleaned and deduplicated."""
    data = _extract_json(reply)
    raw = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raise ValueError("no 'categories' list in the reply")

    out: List[dict] = []
    seen = set()
    for entry in raw:
        if isinstance(entry, str):
            entry = {"name": entry}
        if not isinstance(entry, dict):
            continue
        name = _clean_name(entry.get("name"))
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append({"name": name, "scope": str(entry.get("scope") or "").strip()})
    if len(out) != k:
        raise ValueError(f"got {len(out)} category names, need exactly {k}")
    return out


def parse_assignment(
    reply: str, names: Sequence[str], categories: Sequence[dict]
) -> Tuple[Dict[str, List[str]], List[str]]:
    """Stage-two reply -> {category: [tool, ...]} plus a list of complaints.

    Never raises on a merely sloppy answer. Tool ids and category names are
    matched case-insensitively (flash-lite writes "llmchat" for "LMChat"),
    unknown ids are dropped, and a tool the model forgot lands in the smallest
    category. The complaints are what a retry is told about.
    """
    data = _extract_json(reply)
    raw = data.get("assignment") if isinstance(data, dict) else None
    if not isinstance(raw, dict) or not raw:
        raise ValueError("no 'assignment' object in the reply")

    by_tool = {n.casefold(): n for n in names}
    by_category = {c["name"].casefold(): c["name"] for c in categories}
    labels: Dict[str, List[str]] = {c["name"]: [] for c in categories}
    complaints: List[str] = []
    assigned = set()

    for tool, category in raw.items():
        real = by_tool.get(str(tool).strip().casefold())
        if real is None:
            complaints.append(f"unknown tool {tool!r}")
            continue
        if real in assigned:
            complaints.append(f"{real} assigned twice")
            continue
        label = by_category.get(str(category).strip().casefold())
        if label is None:
            complaints.append(f"{real} put in unknown category {category!r}")
            continue
        labels[label].append(real)
        assigned.add(real)

    missing = [n for n in names if n not in assigned]
    if missing:
        complaints.append("unassigned: " + ", ".join(missing))
        smallest = min(labels, key=lambda label: (len(labels[label]), label))
        labels[smallest].extend(missing)

    _, high = size_band(len(names), len(categories))
    for label, members in labels.items():
        if len(members) > high:
            complaints.append(
                f"category {label!r} holds {len(members)} tools, more than {high}"
            )

    labels = {label: sorted(members) for label, members in labels.items() if members}
    if not labels:
        raise ValueError("no tool was assigned")
    return labels, complaints


def review_prompt(
    labels: Dict[str, List[str]],
    blurbs: Dict[str, str],
    categories: Sequence[dict] = (),
) -> str:
    """Step three: show the finished bands and invite a patch.

    Each band is shown with the scope it was created for, so a move is judged
    against what the category was meant to hold and not only its name.
    """
    scopes = {c["name"]: c.get("scope", "") for c in categories}
    blocks = []
    for label in sorted(labels):
        members = "\n".join(
            "  " + _one_liner(name, blurbs.get(name, "")) for name in labels[label]
        )
        scope = scopes.get(label)
        header = f"{label} ({len(labels[label])})"
        if scope:
            header += f" — meant for: {scope}"
        blocks.append(f"{header}:\n{members}")
    return (
        "These are the finished categories of a tool installer's sidebar, each "
        "with the tools filed under it.\n\n" + "\n\n".join(blocks) + "\n\n"
        "Review the result and correct it. Look for a tool whose subject does "
        "not match the category it sits in, and for a category whose name no "
        "longer describes what it actually holds. Judge a tool by what it does "
        "for its user: reading a chat service is not the same subject as "
        "running an AI agent, even though both involve conversations.\n"
        "Rules: move a tool only when another existing category is clearly a "
        "better home — do not shuffle borderline cases; keep every category "
        "non-empty; a new name is one or two words, title case, at most "
        f"{MAX_NAME_CHARS} characters, and must not be a catch-all ('Misc', "
        "'Other', 'Utilities', 'Tools', 'General').\n"
        "Change nothing you are not confident about. An empty patch is a valid "
        "answer.\n"
        'Answer as JSON: {"moves": {"tool_id": "Destination Category"}, '
        '"renames": {"Old Name": "New Name"}}'
    )


def parse_review(
    reply: str, labels: Dict[str, List[str]]
) -> Tuple[Dict[str, List[str]], List[str]]:
    """Apply a review patch to the labels, returning (labels, applied notes).

    Everything is checked before it is applied: an unknown tool or destination,
    a move that would empty a category, a rename onto an existing name or to a
    banned one, are all ignored. The step-2 result is the floor.
    """
    data = _extract_json(reply)
    if not isinstance(data, dict):
        raise ValueError("review reply is not an object")

    result = {label: list(members) for label, members in labels.items()}
    home = {tool: label for label, members in result.items() for tool in members}
    notes: List[str] = []

    moves = data.get("moves")
    if isinstance(moves, dict):
        by_tool = {t.casefold(): t for t in home}
        by_label = {label.casefold(): label for label in result}
        for tool, destination in moves.items():
            real = by_tool.get(str(tool).strip().casefold())
            target = by_label.get(str(destination).strip().casefold())
            if real is None or target is None or target == home[real]:
                continue
            source = home[real]
            if len(result[source]) <= 1:
                continue  # never empty a category
            result[source].remove(real)
            result[target].append(real)
            home[real] = target
            notes.append(f"moved {real}: {source} -> {target}")

    renames = data.get("renames")
    if isinstance(renames, dict):
        by_label = {label.casefold(): label for label in result}
        for old, new in renames.items():
            source = by_label.get(str(old).strip().casefold())
            name = _clean_name(new)
            if source is None or not name or name == source:
                continue
            if name.casefold() in {label.casefold() for label in result}:
                continue
            result[name] = result.pop(source)
            by_label.pop(source.casefold(), None)
            by_label[name.casefold()] = name
            notes.append(f"renamed {source} -> {name}")

    return {label: sorted(members) for label, members in result.items()}, notes


# --- the call ------------------------------------------------------------

def _gemini_rest(prompt: str) -> str:
    """One generateContent call over plain HTTP. "" when it cannot be made.

    The fallback for anyone without the first-party GeminiClient: it needs
    nothing but GEMINI_API_KEY and the standard library, which is what makes
    the LLM tier available to a third-party checkout at all.
    """
    api_key = _gemini_key()
    if not api_key:
        return ""
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{model_name()}:generateContent?key={api_key}")
    payload = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=GEMINI_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return ""
    try:
        parts = data["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _gemini_key() -> str:
    try:
        from .embedder import gemini_key
    except ImportError:  # pragma: no cover - defensive
        return os.getenv("GEMINI_API_KEY", "").strip()
    return gemini_key()


def _ask_gemini(prompt: str, fresh: bool = False) -> str:
    """One Gemini call. Raises LLMGroupingUnavailable if it cannot be made.

    Prefers the first-party GeminiClient when it is importable — it brings the
    response cache and the model rotation — and falls back to a direct REST
    call, so a checkout without it still gets the LLM grouping.
    """
    try:
        from .embedder import _load_env
        _load_env()  # GEMINI_API_KEY lives in the tree's .env
    except ImportError:  # pragma: no cover - defensive
        pass

    try:
        from _shared.gemini import GeminiClient
    except ImportError:
        GeminiClient = None

    if GeminiClient is not None:
        try:
            client = GeminiClient(api_key=_gemini_key() or None, models=[model_name()])
            reply = client.generate(prompt, json_mode=True, label="", skip_cache=fresh)
            if reply:
                return reply
        except Exception:
            pass  # fall through to REST rather than losing the tier

    reply = _gemini_rest(prompt)
    if not reply:
        raise LLMGroupingUnavailable("Gemini returned nothing")
    return reply


def _local_host() -> str:
    """Base URL of the local OpenAI-compatible server."""
    try:
        from .embedder import local_host
        return local_host()
    except ImportError:  # pragma: no cover - depends on the venv
        host = os.getenv("TOOLS_EMBED_HOST", "") or "http://localhost:11434"
        return (host if host.startswith("http") else f"http://{host}").rstrip("/")


def _chat_local(prompt: str, model: str, schema: Optional[dict] = None) -> str:  # noqa: C901
    """One /v1/chat/completions call. Returns "" on any failure.

    Retries once without the schema: an older server, or one whose model has no
    grammar support, rejects response_format rather than ignoring it.
    """
    for use_schema in (bool(schema), False):
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        if use_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "tool_grouping", "strict": True, "schema": schema,
                },
            }
        request = urllib.request.Request(
            f"{_local_host()}/v1/chat/completions",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        left = _remaining()
        timeout = LOCAL_TIMEOUT if left is None else min(LOCAL_TIMEOUT, max(1.0, left))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
        except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
            continue
        try:
            content = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            continue
        if content:
            return content
    return ""


# The backend and model that answered first; the rest of the pipeline stays on
# it rather than re-probing before every step.
_ACTIVE: Optional[Tuple[str, str]] = None
# Wall-clock deadline for the whole pipeline, or None for no limit. The
# installer sets one because it calls this on the way to opening a window; a
# local model on a busy host can take minutes per batch, and a GUI that waits
# for it is worse than a grouping filed by capability words.
_DEADLINE: Optional[float] = None


def _remaining() -> Optional[float]:
    """Seconds left of the deadline, or None when there is none."""
    return None if _DEADLINE is None else _DEADLINE - time.monotonic()


def active_backend() -> str:
    """Which backend answered ("local", "gemini"), or "" before the first call."""
    return _ACTIVE[0] if _ACTIVE else ""


def active_label() -> str:
    """What produced the current grouping, e.g. "local:google/gemma-4-e2b"."""
    if _ACTIVE is None:
        return ""
    backend, model = _ACTIVE
    return f"{backend}:{model}"


def _ask(prompt: str, schema: Optional[dict] = None, fresh: bool = False) -> str:
    """Ask whichever backend is available, and stay on the one that answers.

    ``fresh`` bypasses the response cache. The naming step uses it: an
    identical prompt otherwise replays one answer forever, so a mediocre set of
    names could never be shaken off by asking again.
    """
    global _ACTIVE

    left = _remaining()
    if left is not None and left <= 0:
        raise LLMGroupingUnavailable("out of time")

    if _ACTIVE is not None:
        backend, model = _ACTIVE
        if backend == "gemini":
            return _ask_gemini(prompt, fresh)
        reply = _chat_local(prompt, model, schema)
        if reply:
            return reply
        raise LLMGroupingUnavailable(f"local model {model} stopped answering")

    problems = []
    for backend in backend_order():
        if backend == "gemini":
            try:
                reply = _ask_gemini(prompt, fresh)
            except LLMGroupingUnavailable as exc:
                problems.append(str(exc))
                continue
            _ACTIVE = ("gemini", model_name())
            return reply
        for model in local_models():
            reply = _chat_local(prompt, model, schema)
            if reply:
                _ACTIVE = ("local", model)
                return reply
            problems.append(f"local model {model} did not answer")
    raise LLMGroupingUnavailable("; ".join(problems) or "no backend configured")


def llm_groups(
    blurbs: Dict[str, str],
    k: int,
    budget: Optional[float] = None,
    categories: Optional[Sequence[dict]] = None,
) -> Dict[str, List[str]]:
    """Category name -> members, decided by the model.

    ``categories`` skips the naming step and files into names that already
    exist. The installer passes the stored ones: a rebuild after an edited
    README should not rename every band, and asking for six fresh names each
    time produced overlapping ones ("Artificial Intelligence" beside "AI
    Agents"). The review step can still rename one whose contents drifted.

    ``budget`` caps the whole pipeline in seconds; past it the next call raises
    rather than starting. Raises LLMGroupingUnavailable when the model cannot
    be reached, its answers stay unusable, or the budget runs out.
    """
    global _ACTIVE, _DEADLINE
    _ACTIVE = None
    _DEADLINE = None if budget is None else time.monotonic() + budget

    names = sorted(blurbs)
    if not names:
        raise ValueError("no tools to group")

    if categories and len(categories) == k:
        return _fill(blurbs, list(categories), k, names)
    return _fill(blurbs, None, k, names)


def _fill(
    blurbs: Dict[str, str],
    categories: Optional[List[dict]],
    k: int,
    names: Sequence[str],
) -> Dict[str, List[str]]:
    """Name the categories if needed, then assign and review."""
    problem = ""
    for _ in range(_RETRIES):
        if categories is not None:
            break
        prompt = naming_prompt(blurbs, k)
        if problem:
            prompt += f"\n\nYour previous answer was rejected: {problem}."
        try:
            reply = _ask(prompt, CATEGORIES_SCHEMA, fresh=True)
            categories = parse_categories(reply, k)
            break
        except (ValueError, json.JSONDecodeError) as exc:
            problem = str(exc)
    if categories is None:
        raise LLMGroupingUnavailable(f"could not name {k} categories: {problem}")

    if active_backend() == "local":
        labels = _assign_in_batches(blurbs, categories, k, names)
        return _review(labels, _compact(blurbs), categories)

    problem = ""
    best: Dict[str, List[str]] = {}
    for attempt in range(_RETRIES):
        prompt = assignment_prompt(blurbs, categories, k)
        if problem:
            prompt += (
                f"\n\nYour previous answer was rejected: {problem}. Answer "
                "again, covering every tool exactly once and keeping the "
                "categories to a sensible size."
            )
        try:
            reply = _ask(prompt, ASSIGNMENT_SCHEMA)
            labels, complaints = parse_assignment(reply, names, categories)
        except (ValueError, json.JSONDecodeError) as exc:
            problem = str(exc)
            continue
        if not complaints:
            return _review(labels, blurbs, categories)
        best = best or labels
        if attempt == _RETRIES - 1:
            # repaired: usable, if not pristine
            return _review(best, blurbs, categories)
        problem = "; ".join(complaints[:5])
    raise LLMGroupingUnavailable(f"unusable assignment: {problem}")


def _compact(blurbs: Dict[str, str]) -> Dict[str, str]:
    """One line per tool — what a short-context model gets instead of blurbs."""
    return {name: _one_liner(name, blurb) for name, blurb in blurbs.items()}


def _assign_in_batches(
    blurbs: Dict[str, str],
    categories: Sequence[dict],
    k: int,
    names: Sequence[str],
) -> Dict[str, List[str]]:
    """Step two for a short-context backend: compact input, several batches.

    A batch that fails is not fatal — its tools come back unassigned and the
    usual repair files them, so one bad reply costs a few placements rather
    than the whole grouping.
    """
    compact = _compact(blurbs)
    overhead = _estimate_tokens(assignment_prompt({}, categories, k, total=len(names)))
    merged: Dict[str, str] = {}
    for batch in batch_for_budget(compact, overhead):
        subset = {name: compact[name] for name in batch}
        prompt = assignment_prompt(subset, categories, k, total=len(names))
        try:
            data = _extract_json(_ask(prompt, ASSIGNMENT_SCHEMA))
        except (LLMGroupingUnavailable, ValueError, json.JSONDecodeError):
            continue
        assignment = data.get("assignment") if isinstance(data, dict) else None
        if isinstance(assignment, dict):
            merged.update({str(t): str(c) for t, c in assignment.items()})

    if not merged:
        raise LLMGroupingUnavailable("no batch of the assignment step answered")
    labels, _ = parse_assignment(json.dumps({"assignment": merged}), names, categories)
    return labels


def _review(
    labels: Dict[str, List[str]],
    blurbs: Dict[str, str],
    categories: Sequence[dict] = (),
) -> Dict[str, List[str]]:
    """Step three, best effort: a failed review keeps the step-2 result."""
    try:
        reply = _ask(review_prompt(labels, blurbs, categories), REVIEW_SCHEMA)
        reviewed, notes = parse_review(reply, labels)
    except (LLMGroupingUnavailable, ValueError, json.JSONDecodeError):
        return labels
    for note in notes:
        print(f"  review: {note}")
    return reviewed
