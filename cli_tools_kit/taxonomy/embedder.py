"""Text embeddings with a local backend first and Gemini as fallback.

Two backends behind one function. The local OpenAI-compatible
``/v1/embeddings`` endpoint (LM Studio on this fleet) is tried first because it
is free and offline; Gemini is used when it is not reachable. If neither works
the caller gets :class:`EmbeddingUnavailable` rather than a silent empty list.
"""

import json
import os
import urllib.error
import urllib.request
from typing import List

_DEFAULT_HOST = "http://localhost:11434"
_DEFAULT_MODEL = "text-embedding-nomic-embed-text-v1.5"
_TIMEOUT = 60.0

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(_HERE))


class EmbeddingUnavailable(RuntimeError):
    """No embedding backend could produce vectors."""


def _load_env() -> None:
    """Load the repo-root .env so GEMINI_API_KEY is present."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(os.path.join(ROOT_DIR, ".env"))


def load_tree_key(root: str) -> None:
    """Set GEMINI_API_KEY from ``<root>/.env`` when the environment has none.

    The key sits in the .env of the tree being grouped. _load_env does not
    reach it once the kit is installed as a package, because ROOT_DIR is then
    the package's parent directory. Only this one variable is read, so the
    file's other secrets stay out of the environment the installer's tools
    inherit.
    """
    if os.getenv("GEMINI_API_KEY", "").strip():
        return
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if not sep or name.strip() != "GEMINI_API_KEY":
            continue
        value = value.strip()
        if value[:1] in ("'", '"'):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()
        if value:
            os.environ["GEMINI_API_KEY"] = value
        return


def local_host() -> str:
    """Base URL of the local OpenAI-compatible server."""
    host = (
        os.getenv("TOOLS_EMBED_HOST", "")
        or os.getenv("LMSTUDIO_HOST", "")
        or os.getenv("OLLAMA_HOST", "")
        or _DEFAULT_HOST
    )
    if not host.startswith("http"):
        host = f"http://{host}"
    return host.rstrip("/")


def local_model() -> str:
    """Name of the local embedding model."""
    return os.getenv("TOOLS_EMBED_MODEL", "") or _DEFAULT_MODEL


def _embed_local(texts: List[str]) -> List[List[float]]:
    """Call the local /v1/embeddings endpoint. Returns [] on any failure."""
    payload = json.dumps({"model": local_model(), "input": texts}).encode()
    req = urllib.request.Request(
        f"{local_host()}/v1/embeddings",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return []

    rows = body.get("data")
    if not isinstance(rows, list) or len(rows) != len(texts):
        return []
    rows = sorted(rows, key=lambda r: r.get("index", 0))
    vectors = []
    for row in rows:
        vec = row.get("embedding")
        if not isinstance(vec, list) or not vec:
            return []
        vectors.append([float(x) for x in vec])
    return vectors


def _embed_gemini_client(texts: List[str]) -> List[List[float]]:
    """Try the shared GeminiClient's own embed(). Returns [] if unusable."""
    try:
        from _shared.gemini import GeminiClient
    except ImportError:
        return []
    try:
        client = GeminiClient()
        result = client.embed(texts)
    except Exception:
        return []
    if not isinstance(result, list) or len(result) != len(texts):
        return []
    if not all(isinstance(v, list) and v for v in result):
        return []
    return [[float(x) for x in v] for v in result]


def _embed_gemini_rest(texts: List[str]) -> List[List[float]]:
    """Direct REST call to the Gemini embedding endpoint. [] on failure."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        return []
    model = "models/gemini-embedding-001"
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/{model}"
        f":batchEmbedContents?key={api_key}"
    )
    payload = json.dumps(
        {
            "requests": [
                {"model": model, "content": {"parts": [{"text": t}]}}
                for t in texts
            ]
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            body = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return []

    rows = body.get("embeddings")
    if not isinstance(rows, list) or len(rows) != len(texts):
        return []
    vectors = []
    for row in rows:
        vec = (row or {}).get("values")
        if not isinstance(vec, list) or not vec:
            return []
        vectors.append([float(x) for x in vec])
    return vectors


def embed_texts(texts: List[str]) -> List[List[float]]:
    """Embed a list of texts, local backend first, Gemini second.

    Raises EmbeddingUnavailable if no backend answers.
    """
    texts = [t if t else " " for t in texts]
    if not texts:
        return []

    vectors = _embed_local(texts)
    if vectors:
        return vectors

    _load_env()
    for backend in (_embed_gemini_client, _embed_gemini_rest):
        vectors = backend(texts)
        if vectors:
            return vectors

    raise EmbeddingUnavailable(
        "No embedding backend answered: neither the local "
        f"/v1/embeddings server at {local_host()} nor Gemini."
    )


def backend_name() -> str:
    """Name of the backend a fresh embed_texts call would reach first."""
    try:
        if _embed_local([" "]):
            return f"local:{local_model()}"
    except Exception:
        pass
    return "gemini"
