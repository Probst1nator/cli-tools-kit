"""Tests for the Gemini tool grouping: prompts, reply parsing, repair."""

import json

import pytest

from cli_tools_kit.taxonomy import llm_groups

BLURBS = {
    "audio_speak": "directory: audio_speak\nadvertises: Audio Speak | tts",
    "voice_typer": "directory: voice_typer\nadvertises: Voice Typer | stt",
    "image_gen": "directory: image_gen\nadvertises: Image Gen | image-gen",
    "LMChat": "directory: LMChat\nadvertises: LMChat | llm-chat",
}
NAMES = sorted(BLURBS)
CATEGORIES = [
    {"name": "Voice & Audio", "scope": "speech in and out"},
    {"name": "Images", "scope": "pictures"},
]


def _assignment(mapping):
    return json.dumps({"assignment": mapping})


# --- prompts --------------------------------------------------------------

def test_the_model_is_the_latest_flash_lite():
    assert llm_groups.MODEL == "gemini-3.5-flash-lite"


def test_naming_prompt_lists_every_tool_and_the_count():
    prompt = llm_groups.naming_prompt(BLURBS, 2)
    for name in NAMES:
        assert name in prompt
    assert "exactly 2 category names" in prompt
    assert "Misc" in prompt  # the no-catch-all rule survives


def test_naming_prompt_uses_the_advertised_line_not_the_whole_blurb():
    prompt = llm_groups.naming_prompt(BLURBS, 2)
    assert "directory: audio_speak" not in prompt
    assert "Audio Speak | tts" in prompt


def test_assignment_prompt_carries_the_categories_and_the_blurbs():
    prompt = llm_groups.assignment_prompt(BLURBS, CATEGORIES, 2)
    assert "Voice & Audio: speech in and out" in prompt
    assert "directory: audio_speak" in prompt


def test_size_band_widens_with_the_collection():
    assert llm_groups.size_band(37, 6) == (3, 10)
    assert llm_groups.size_band(4, 2) == (2, 5)


# --- names ----------------------------------------------------------------

def test_long_name_is_cut_at_a_word_boundary():
    assert llm_groups._clean_name("System & Development Tools") == "System & Development"


def test_catch_all_names_are_rejected():
    for name in ("Misc", "other", "Utilities", "Tools"):
        assert llm_groups._clean_name(name) == ""


def test_name_keeps_a_normal_label_intact():
    assert llm_groups._clean_name('  "Voice & Audio" ') == "Voice & Audio"


# --- reply parsing --------------------------------------------------------

def test_categories_parse_from_a_fenced_reply():
    reply = '```json\n{"categories": [{"name": "Voice & Audio"}, {"name": "Images"}]}\n```'
    cats = llm_groups.parse_categories(reply, 2)
    assert [c["name"] for c in cats] == ["Voice & Audio", "Images"]


def test_wrong_number_of_categories_is_an_error():
    with pytest.raises(ValueError):
        llm_groups.parse_categories('{"categories": [{"name": "Only One"}]}', 2)


def test_assignment_matches_ids_and_categories_case_insensitively():
    reply = _assignment({
        "lmchat": "voice & audio", "audio_speak": "Voice & Audio",
        "voice_typer": "Voice & Audio", "image_gen": "Images",
    })
    labels, complaints = llm_groups.parse_assignment(reply, NAMES, CATEGORIES)
    assert labels["Voice & Audio"] == ["LMChat", "audio_speak", "voice_typer"]
    assert complaints == []


def test_a_misspelt_id_is_reported_and_the_tool_still_placed():
    # Observed: flash-lite answered "llmchat" for "LMChat" — not a case
    # difference, so it cannot be matched. It must not vanish either.
    reply = _assignment({
        "llmchat": "Voice & Audio", "audio_speak": "Voice & Audio",
        "voice_typer": "Voice & Audio", "image_gen": "Images",
    })
    labels, complaints = llm_groups.parse_assignment(reply, NAMES, CATEGORIES)
    assert sorted(t for group in labels.values() for t in group) == NAMES
    assert any("llmchat" in c for c in complaints)
    assert any("unassigned: LMChat" in c for c in complaints)


def test_forgotten_tool_lands_in_the_smallest_category():
    reply = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "image_gen": "Images",
    })
    labels, complaints = llm_groups.parse_assignment(reply, NAMES, CATEGORIES)
    assert labels["Images"] == ["LMChat", "image_gen"]
    assert any("unassigned" in c for c in complaints)


def test_unknown_tool_and_category_are_reported():
    reply = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "image_gen": "Images", "LMChat": "Nonexistent", "ghost_tool": "Images",
    })
    _, complaints = llm_groups.parse_assignment(reply, NAMES, CATEGORIES)
    assert any("ghost_tool" in c for c in complaints)
    assert any("Nonexistent" in c for c in complaints)


def test_oversized_category_is_reported():
    names = [f"tool_{i}" for i in range(12)]  # band for 12 in 2 is (2, 9)
    reply = _assignment({n: "Voice & Audio" for n in names})
    _, complaints = llm_groups.parse_assignment(reply, names, CATEGORIES)
    assert any("more than" in c for c in complaints)


def test_a_reply_without_an_assignment_is_an_error():
    with pytest.raises(ValueError):
        llm_groups.parse_assignment('{"groups": []}', NAMES, CATEGORIES)


# --- review patch ---------------------------------------------------------

LABELS = {"Voice & Audio": ["audio_speak", "voice_typer"], "Images": ["LMChat", "image_gen"]}


def test_review_applies_a_move():
    reply = '{"moves": {"LMChat": "Voice & Audio"}, "renames": {}}'
    labels, notes = llm_groups.parse_review(reply, LABELS)
    assert labels["Voice & Audio"] == ["LMChat", "audio_speak", "voice_typer"]
    assert labels["Images"] == ["image_gen"]
    assert notes == ["moved LMChat: Images -> Voice & Audio"]


def test_review_applies_a_rename_and_keeps_the_members():
    reply = '{"renames": {"Images": "Vision & Creation"}}'
    labels, notes = llm_groups.parse_review(reply, LABELS)
    assert labels["Vision & Creation"] == ["LMChat", "image_gen"]
    assert "Images" not in labels
    assert notes == ["renamed Images -> Vision & Creation"]


def test_review_never_empties_a_category():
    single = {"Voice & Audio": ["audio_speak", "voice_typer"], "Images": ["image_gen"]}
    reply = '{"moves": {"image_gen": "Voice & Audio"}}'
    labels, notes = llm_groups.parse_review(reply, single)
    assert labels["Images"] == ["image_gen"]
    assert notes == []


def test_review_ignores_an_unknown_tool_or_destination():
    reply = '{"moves": {"ghost": "Images", "LMChat": "Nowhere"}}'
    labels, notes = llm_groups.parse_review(reply, LABELS)
    assert labels == {k: sorted(v) for k, v in LABELS.items()}
    assert notes == []


def test_review_refuses_a_rename_onto_an_existing_name():
    reply = '{"renames": {"Images": "Voice & Audio"}}'
    labels, notes = llm_groups.parse_review(reply, LABELS)
    assert set(labels) == set(LABELS)
    assert notes == []


def test_review_refuses_a_catch_all_rename():
    labels, notes = llm_groups.parse_review('{"renames": {"Images": "Misc"}}', LABELS)
    assert "Images" in labels
    assert notes == []


def test_an_empty_patch_changes_nothing():
    labels, notes = llm_groups.parse_review('{"moves": {}, "renames": {}}', LABELS)
    assert labels == {k: sorted(v) for k, v in LABELS.items()}
    assert notes == []


def test_review_prompt_shows_sizes_and_scopes():
    prompt = llm_groups.review_prompt(LABELS, BLURBS, CATEGORIES)
    assert "Voice & Audio (2) — meant for: speech in and out" in prompt
    assert "audio_speak" in prompt


# --- the three-step call --------------------------------------------------

def _stub_ask(monkeypatch, replies):
    """Feed _ask a script of replies, and pin the backend to gemini so the
    pipeline takes the single-shot path rather than the local batching one."""
    monkeypatch.setattr(llm_groups, "_ACTIVE", None)
    monkeypatch.setattr(llm_groups, "active_backend", lambda: "gemini")
    seen = []

    def fake(prompt, schema=None, fresh=False):
        seen.append(prompt)
        return replies[min(len(seen) - 1, len(replies) - 1)]

    monkeypatch.setattr(llm_groups, "_ask", fake)
    return seen


def test_llm_groups_runs_all_three_steps(monkeypatch):
    names_reply = '{"categories": [{"name": "Voice & Audio"}, {"name": "Images"}]}'
    assign_reply = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "LMChat": "Images", "image_gen": "Images",
    })
    review_reply = '{"moves": {}, "renames": {"Images": "Vision"}}'
    seen = _stub_ask(monkeypatch, [names_reply, assign_reply, review_reply])
    labels = llm_groups.llm_groups(BLURBS, 2)
    assert labels == {
        "Voice & Audio": ["audio_speak", "voice_typer"],
        "Vision": ["LMChat", "image_gen"],
    }
    assert len(seen) == 3


def test_a_complaint_triggers_one_retry(monkeypatch):
    names_reply = '{"categories": [{"name": "Voice & Audio"}, {"name": "Images"}]}'
    sloppy = _assignment({"audio_speak": "Voice & Audio"})
    good = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "LMChat": "Images", "image_gen": "Images",
    })
    review_reply = '{"moves": {}, "renames": {}}'
    seen = _stub_ask(monkeypatch, [names_reply, sloppy, good, review_reply])
    labels = llm_groups.llm_groups(BLURBS, 2)
    assert labels["Images"] == ["LMChat", "image_gen"]
    assert len(seen) == 4
    assert "was rejected" in seen[2]


def test_a_failed_review_keeps_the_assignment(monkeypatch):
    names_reply = '{"categories": [{"name": "Voice & Audio"}, {"name": "Images"}]}'
    assign_reply = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "LMChat": "Images", "image_gen": "Images",
    })
    _stub_ask(monkeypatch, [names_reply, assign_reply, "not json at all"])
    labels = llm_groups.llm_groups(BLURBS, 2)
    assert labels == {
        "Voice & Audio": ["audio_speak", "voice_typer"],
        "Images": ["LMChat", "image_gen"],
    }


def test_unnameable_categories_raise(monkeypatch):
    _stub_ask(monkeypatch, ['{"categories": [{"name": "Only One"}]}'])
    with pytest.raises(llm_groups.LLMGroupingUnavailable):
        llm_groups.llm_groups(BLURBS, 2)


def test_a_dead_backend_raises(monkeypatch):
    def boom(prompt, schema=None, fresh=False):
        raise llm_groups.LLMGroupingUnavailable("no key")

    monkeypatch.setattr(llm_groups, "_ask", boom)
    with pytest.raises(llm_groups.LLMGroupingUnavailable):
        llm_groups.llm_groups(BLURBS, 2)


# --- local backend --------------------------------------------------------

def test_backend_order_prefers_gemini_when_a_key_exists(monkeypatch):
    monkeypatch.setattr(llm_groups, "_have_api_key", lambda: True)
    monkeypatch.delenv("TOOLS_GROUPS_BACKEND", raising=False)
    assert llm_groups.backend_order() == ["gemini", "local"]


def test_backend_order_prefers_local_without_a_key(monkeypatch):
    monkeypatch.setattr(llm_groups, "_have_api_key", lambda: False)
    monkeypatch.delenv("TOOLS_GROUPS_BACKEND", raising=False)
    assert llm_groups.backend_order() == ["local", "gemini"]


def test_backend_can_be_pinned(monkeypatch):
    monkeypatch.setenv("TOOLS_GROUPS_BACKEND", "local")
    assert llm_groups.backend_order() == ["local"]


def test_local_models_are_the_documented_pair(monkeypatch):
    monkeypatch.delenv("TOOLS_GROUPS_LOCAL_MODEL", raising=False)
    assert llm_groups.local_models() == [
        "google/gemma-4-e2b", "qwen/qwen3.6-35b-a3b",
    ]


def test_a_pinned_local_model_wins(monkeypatch):
    monkeypatch.setenv("TOOLS_GROUPS_LOCAL_MODEL", "some/other-model")
    assert llm_groups.local_models() == ["some/other-model"]


def test_batches_stay_inside_the_token_budget():
    blurbs = {f"tool_{i}": "x" * 400 for i in range(20)}   # ~100 tokens each
    batches = llm_groups.batch_for_budget(blurbs, overhead=200, budget=1000)
    assert len(batches) > 1
    assert sorted(t for batch in batches for t in batch) == sorted(blurbs)
    for batch in batches:
        assert 200 + sum(llm_groups._estimate_tokens(blurbs[t]) + 8
                         for t in batch) <= 1000 + 108  # one item may straddle


def test_one_oversized_tool_still_gets_a_batch():
    blurbs = {"huge": "x" * 100_000}
    assert llm_groups.batch_for_budget(blurbs, overhead=10, budget=100) == [["huge"]]


def test_a_batch_prompt_says_not_to_balance_itself():
    prompt = llm_groups.assignment_prompt(BLURBS, CATEGORIES, 2, total=40)
    assert "one batch of 40 tools" in prompt
    assert "do not try to balance the batch" in prompt


def test_a_whole_collection_prompt_keeps_the_size_band():
    prompt = llm_groups.assignment_prompt(BLURBS, CATEGORIES, 2, total=len(BLURBS))
    assert "roughly" in prompt
    assert "one batch" not in prompt


def test_local_batches_are_merged_and_repaired(monkeypatch):
    monkeypatch.setattr(llm_groups, "_ACTIVE", ("local", "google/gemma-4-e2b"))
    replies = iter([
        _assignment({"audio_speak": "Voice & Audio"}),
        "not json",                                    # a batch that fails
        _assignment({"image_gen": "Images"}),
        '{"moves": {}, "renames": {}}',                # the review
    ])
    monkeypatch.setattr(
        llm_groups, "_ask", lambda prompt, schema=None, fresh=False: next(replies))
    monkeypatch.setattr(llm_groups, "batch_for_budget",
                        lambda blurbs, overhead, budget=0: [["audio_speak"], ["voice_typer"], ["image_gen", "LMChat"]])
    labels = llm_groups._assign_in_batches(BLURBS, CATEGORIES, 2, NAMES)
    placed = sorted(t for group in labels.values() for t in group)
    assert placed == NAMES          # the failed batch's tools are still filed
    assert "audio_speak" in labels["Voice & Audio"]


def test_every_batch_failing_is_an_error(monkeypatch):
    monkeypatch.setattr(llm_groups, "_ACTIVE", ("local", "m"))
    monkeypatch.setattr(
        llm_groups, "_ask", lambda prompt, schema=None, fresh=False: "not json")
    with pytest.raises(llm_groups.LLMGroupingUnavailable):
        llm_groups._assign_in_batches(BLURBS, CATEGORIES, 2, NAMES)


def test_the_local_call_retries_without_the_schema(monkeypatch):
    sent = []

    class _Response:
        def __init__(self, body):
            self._body = body

        def read(self):
            return json.dumps(self._body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request, timeout=0):
        body = json.loads(request.data.decode())
        sent.append("response_format" in body)
        if "response_format" in body:
            raise OSError("must be 'json_schema' or 'text'")
        return _Response({"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(llm_groups.urllib.request, "urlopen", fake_urlopen)
    assert llm_groups._chat_local("p", "m", {"type": "object"}) == '{"ok": true}'
    assert sent == [True, False]


def test_the_budget_stops_the_pipeline(monkeypatch):
    monkeypatch.setattr(llm_groups, "_DEADLINE", llm_groups.time.monotonic() - 1)
    with pytest.raises(llm_groups.LLMGroupingUnavailable):
        llm_groups._ask("anything")


def test_no_budget_means_no_deadline(monkeypatch):
    monkeypatch.setattr(llm_groups, "_DEADLINE", None)
    assert llm_groups._remaining() is None


def test_stored_categories_skip_the_naming_step(monkeypatch):
    seen = _stub_ask(monkeypatch, [
        _assignment({
            "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
            "LMChat": "Images", "image_gen": "Images",
        }),
        '{"moves": {}, "renames": {}}',
    ])
    labels = llm_groups.llm_groups(BLURBS, 2, categories=CATEGORIES)
    assert set(labels) == {"Voice & Audio", "Images"}
    assert len(seen) == 2                      # assign + review, no naming
    assert "Propose exactly" not in seen[0]


def test_a_stored_set_of_the_wrong_size_is_ignored(monkeypatch):
    names_reply = '{"categories": [{"name": "Voice & Audio"}, {"name": "Images"}]}'
    assign_reply = _assignment({
        "audio_speak": "Voice & Audio", "voice_typer": "Voice & Audio",
        "LMChat": "Images", "image_gen": "Images",
    })
    seen = _stub_ask(monkeypatch, [names_reply, assign_reply, '{"moves": {}}'])
    llm_groups.llm_groups(BLURBS, 2, categories=[{"name": "Only One"}])
    assert "Propose exactly 2 category names" in seen[0]
