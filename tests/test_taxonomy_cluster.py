"""Tests for the taxonomy clustering, labelling, groups file and embedder."""

import json
import math

import pytest

from cli_tools_kit.taxonomy import cluster, corpus, embedder, groups


def _toy_vectors():
    """Three well-separated blobs of three points each, in 3-D."""
    blobs = [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)]
    vectors = []
    for bx, by, bz in blobs:
        for jitter in (0.0, 0.02, -0.02):
            vectors.append([bx + jitter, by + jitter / 2, bz - jitter / 2])
    return vectors


def _same_partition(a, b):
    """True when two label lists describe the same grouping."""
    def canon(labels):
        buckets = {}
        for i, lab in enumerate(labels):
            buckets.setdefault(lab, []).append(i)
        return sorted(tuple(v) for v in buckets.values())

    return canon(a) == canon(b)


# --- kmeans ---------------------------------------------------------------

def test_kmeans_is_deterministic():
    vectors = _toy_vectors()
    first = cluster.kmeans(vectors, k=3, seed=0)
    for _ in range(3):
        assert cluster.kmeans(vectors, k=3, seed=0) == first


def test_kmeans_recovers_the_blobs():
    labels = cluster.kmeans(_toy_vectors(), k=3, seed=0)
    assert _same_partition(labels, [0, 0, 0, 1, 1, 1, 2, 2, 2])


def test_kmeans_seed_is_used():
    vectors = _toy_vectors()
    runs = {tuple(cluster.kmeans(vectors, k=3, seed=s)) for s in range(5)}
    # Every seed must still produce three groups of three.
    for run in runs:
        sizes = sorted(run.count(lab) for lab in set(run))
        assert sizes == [3, 3, 3]


def test_balanced_caps_cluster_size():
    # 12 points, 11 of them identical: unbalanced k-means would put them all
    # in one cluster.
    vectors = [[1.0, 0.0]] * 11 + [[0.0, 1.0]]
    k = 3
    labels = cluster.kmeans(vectors, k=k, seed=0, balanced=True)
    cap = math.ceil(len(vectors) / k) + 1
    for lab in set(labels):
        assert labels.count(lab) <= cap


def test_unbalanced_may_exceed_the_cap():
    vectors = [[1.0, 0.0]] * 11 + [[0.0, 1.0]]
    labels = cluster.kmeans(vectors, k=3, seed=0, balanced=False)
    assert max(labels.count(lab) for lab in set(labels)) > math.ceil(12 / 3) + 1


def test_kmeans_handles_k_larger_than_n():
    labels = cluster.kmeans([[1.0, 0.0], [0.0, 1.0]], k=9, seed=0)
    assert len(labels) == 2


def test_kmeans_on_empty_input():
    assert cluster.kmeans([], k=3) == []


def test_normalize_gives_unit_vectors():
    out = cluster.normalize([[3.0, 4.0], [0.0, 0.0]])
    assert out[0] == pytest.approx([0.6, 0.8])
    assert out[1] == [0.0, 0.0]


# --- labelling ------------------------------------------------------------

def test_tokenize_drops_stopwords_and_short_tokens():
    tokens = cluster.tokenize("The tool is a Python audio Transcriber, ok 12")
    assert "the" not in tokens
    assert "tool" not in tokens
    assert "python" not in tokens
    assert "ok" not in tokens
    assert "12" not in tokens
    assert "audio" in tokens
    assert "transcriber" in tokens


def test_label_group_picks_distinctive_tokens():
    inside = [
        "audio transcription with whisper",
        "audio recording and whisper models",
        "whisper audio pipeline",
    ]
    outside = [
        "gemini image generation",
        "image resizing and cropping",
        "diffusion image models",
    ]
    assert cluster.label_group(inside, outside) in {"audio", "whisper"}
    two = cluster.label_group(inside, outside, top=2)
    assert set(two.split(" · ")) == {"audio", "whisper"}


def test_label_group_on_empty_input():
    assert cluster.label_group([], ["something"]) == "Ungrouped"


def test_cluster_documents_returns_unique_labels_and_all_members():
    names = ["a", "b", "c", "d", "e", "f", "g", "h", "i"]
    docs = {
        "a": "audio whisper speech", "b": "audio whisper microphone",
        "c": "audio speech recording", "d": "image diffusion render",
        "e": "image diffusion pixels", "f": "image render canvas",
        "g": "commit git branch", "h": "commit git rebase",
        "i": "git branch merge",
    }
    result = cluster.cluster_documents(names, _toy_vectors(), docs, k=3, seed=0)
    assert len(result) == 3
    labels = [label for label, _ in result]
    assert len(set(labels)) == 3
    members = sorted(m for _, group in result for m in group)
    assert members == names


# --- groups file ----------------------------------------------------------

def _write_groups(tmp_path, payload):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "tool_groups.json").write_text(json.dumps(payload))


def test_load_groups_reads_labels(tmp_path):
    _write_groups(tmp_path, {"labels": {"Audio": ["audio_speak", "voice_typer"]}})
    mapping = groups.load_groups(str(tmp_path))
    assert mapping == {"audio_speak": "Audio", "voice_typer": "Audio"}


def test_overrides_beat_the_computed_label(tmp_path):
    _write_groups(tmp_path, {
        "labels": {"Audio": ["audio_speak", "voice_typer"]},
        "overrides": {"voice_typer": "Input"},
    })
    mapping = groups.load_groups(str(tmp_path))
    assert mapping["audio_speak"] == "Audio"
    assert mapping["voice_typer"] == "Input"


def test_override_can_name_a_tool_not_in_labels(tmp_path):
    _write_groups(tmp_path, {"labels": {}, "overrides": {"scrape": "Web"}})
    assert groups.load_groups(str(tmp_path))["scrape"] == "Web"


def test_missing_file_gives_no_groups(tmp_path):
    assert groups.load_groups(str(tmp_path)) == {}
    assert groups.group_of(str(tmp_path), "scrape") == groups.UNGROUPED


def test_broken_file_gives_no_groups(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "tool_groups.json").write_text("{not json")
    assert groups.load_groups(str(tmp_path)) == {}


def test_unknown_tool_is_ungrouped(tmp_path):
    _write_groups(tmp_path, {"labels": {"Audio": ["audio_speak"]}})
    assert groups.group_of(str(tmp_path), "nope") == groups.UNGROUPED


# --- corpus fingerprint ---------------------------------------------------

def _write_tool(tmp_path, name, docs=None, main_py="print(1)\n"):
    """Create a minimal tool dir; docs maps filename -> text."""
    tool = tmp_path / name
    tool.mkdir()
    (tool / "main.py").write_text(main_py)
    (tool / "requirements.txt").write_text("")
    for doc_name, text in (docs or {}).items():
        (tool / doc_name).write_text(text)
    return tool


def test_fingerprint_is_stable_across_calls(tmp_path):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    first = corpus.corpus_fingerprint(str(tmp_path))
    assert corpus.corpus_fingerprint(str(tmp_path)) == first


def test_fingerprint_follows_an_edited_doc(tmp_path):
    tool = _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    before = corpus.corpus_fingerprint(str(tmp_path))
    (tool / "README.md").write_text("images now")
    assert corpus.corpus_fingerprint(str(tmp_path)) != before


def test_fingerprint_follows_a_new_tool(tmp_path):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    before = corpus.corpus_fingerprint(str(tmp_path))
    _write_tool(tmp_path, "beta", {"README.md": "images"})
    assert corpus.corpus_fingerprint(str(tmp_path)) != before


def test_fingerprint_follows_main_py_when_the_tool_has_no_docs(tmp_path):
    tool = _write_tool(tmp_path, "alpha")
    before = corpus.corpus_fingerprint(str(tmp_path))
    (tool / "main.py").write_text("print(2)\n")
    assert corpus.corpus_fingerprint(str(tmp_path)) != before


def test_fingerprint_ignores_an_unrelated_file(tmp_path):
    tool = _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    before = corpus.corpus_fingerprint(str(tmp_path))
    (tool / "notes.txt").write_text("scratch")
    assert corpus.corpus_fingerprint(str(tmp_path)) == before


# --- staleness and ensure_groups ------------------------------------------

def _write_current_groups(tmp_path, k=6, **extra):
    payload = {
        "k": k,
        "fingerprint": corpus.corpus_fingerprint(str(tmp_path)),
        "labels": {"audio": ["alpha"]},
    }
    payload.update(extra)
    _write_groups(tmp_path, payload)


def test_matching_fingerprint_is_not_stale(tmp_path):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    _write_current_groups(tmp_path)
    assert groups.is_stale(str(tmp_path), 6) is False


def test_edited_doc_makes_it_stale(tmp_path):
    tool = _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    _write_current_groups(tmp_path)
    (tool / "README.md").write_text("images now")
    assert groups.is_stale(str(tmp_path), 6) is True


def test_other_k_makes_it_stale(tmp_path):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    _write_current_groups(tmp_path, k=4)
    assert groups.is_stale(str(tmp_path), 6) is True


def test_missing_file_is_stale(tmp_path):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    assert groups.is_stale(str(tmp_path), 6) is True


def test_ensure_groups_rebuilds_when_stale(tmp_path, monkeypatch):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    calls = []

    def fake_build(root, k=6, seed=0, write=True, budget=None):
        calls.append(k)
        _write_current_groups(tmp_path)
        return {}

    monkeypatch.setattr("cli_tools_kit.taxonomy.build.build_groups", fake_build)
    assert groups.ensure_groups(str(tmp_path)) == {"alpha": "audio"}
    assert calls == [6]


def test_ensure_groups_keeps_the_stored_file_when_the_rebuild_fails(tmp_path, monkeypatch):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    _write_groups(tmp_path, {"k": 6, "labels": {"stale": ["alpha"]}})

    def boom(root, k=6, seed=0, write=True, budget=None):
        raise embedder.EmbeddingUnavailable("no backend")

    monkeypatch.setattr("cli_tools_kit.taxonomy.build.build_groups", boom)
    assert groups.ensure_groups(str(tmp_path)) == {"alpha": "stale"}


def test_ensure_groups_does_not_rebuild_when_current(tmp_path, monkeypatch):
    _write_tool(tmp_path, "alpha", {"README.md": "audio"})
    _write_current_groups(tmp_path)

    def boom(root, k=6, seed=0, write=True, budget=None):
        raise AssertionError("rebuilt a current grouping")

    monkeypatch.setattr("cli_tools_kit.taxonomy.build.build_groups", boom)
    assert groups.ensure_groups(str(tmp_path)) == {"alpha": "audio"}


# --- embedder -------------------------------------------------------------

class _FakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return json.dumps(self._body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_embed_texts_uses_the_local_backend(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["payload"] = json.loads(req.data.decode())
        n = len(seen["payload"]["input"])
        return _FakeResponse(
            {"data": [{"index": i, "embedding": [float(i), 1.0]} for i in range(n)]}
        )

    monkeypatch.setenv("TOOLS_EMBED_HOST", "http://localhost:11434")
    monkeypatch.setenv("TOOLS_EMBED_MODEL", "test-embed")
    monkeypatch.setattr(embedder.urllib.request, "urlopen", fake_urlopen)

    vectors = embedder.embed_texts(["one", "two"])
    assert vectors == [[0.0, 1.0], [1.0, 1.0]]
    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["payload"] == {"model": "test-embed", "input": ["one", "two"]}


def test_embed_texts_raises_when_every_backend_fails(monkeypatch):
    def fail(req, timeout=None):
        raise OSError("no server")

    monkeypatch.setattr(embedder.urllib.request, "urlopen", fail)
    monkeypatch.setattr(embedder, "_load_env", lambda: None)
    monkeypatch.setattr(embedder, "_embed_gemini_client", lambda texts: [])
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(embedder.EmbeddingUnavailable):
        embedder.embed_texts(["one"])


def test_gemini_rest_is_the_fallback(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if "11434" in req.full_url:
            raise OSError("no local server")
        return _FakeResponse({"embeddings": [{"values": [0.5, 0.5]}]})

    monkeypatch.setattr(embedder.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(embedder, "_load_env", lambda: None)
    monkeypatch.setattr(embedder, "_embed_gemini_client", lambda texts: [])
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    assert embedder.embed_texts(["one"]) == [[0.5, 0.5]]
    assert any("batchEmbedContents" in url for url in calls)


def test_embed_texts_on_empty_list():
    assert embedder.embed_texts([]) == []
