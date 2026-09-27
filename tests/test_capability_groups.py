"""Tests for the network-free capability grouping tier."""


from cli_tools_kit.taxonomy.capability import DEFAULT_BANDS, capability_groups


def test_cold_start_files_by_the_default_table():
    labels = capability_groups({
        "audio_speak": "tts", "voice_typer": "stt", "jarvis": "agent",
        "image_gen": "image-gen",
    })
    assert labels["Audio & Speech"] == ["audio_speak", "voice_typer"]
    assert labels["AI & Chat"] == ["jarvis"]
    assert labels["Vision & Creation"] == ["image_gen"]


def test_every_vocabulary_word_has_a_band():
    # Mirrors validate_structure.CAPABILITY_VOCAB; a new word needs a home.
    vocab = {
        "agent", "summarize", "tts", "stt", "image-gen", "finetune", "ocr",
        "image-edit", "scrape", "download", "llm-chat", "commit-gen",
        "dep-check", "context-picker", "pdf-extract", "vnc-display",
        "audio-visualizer", "media-launcher", "vision-search", "smarthome",
        "forge", "chat", "publish", "repo-hygiene",
    }
    placed = {c for capabilities in DEFAULT_BANDS.values() for c in capabilities}
    assert vocab - placed == set()


def test_an_unknown_capability_lands_in_the_smallest_band():
    labels = capability_groups({
        "a": "tts", "b": "tts", "c": "agent", "d": "brand-new-thing",
    })
    assert labels["AI & Chat"] == ["c", "d"]


def test_a_tool_without_a_capability_is_still_placed():
    labels = capability_groups({"a": "tts", "b": ""})
    assert sorted(t for group in labels.values() for t in group) == ["a", "b"]


def test_existing_bands_and_names_survive():
    existing = {"Sound Stuff": ["audio_speak"], "Agents": ["jarvis"]}
    labels = capability_groups(
        {"audio_speak": "tts", "jarvis": "agent", "voice_typer": "stt"}, existing
    )
    assert set(labels) == {"Sound Stuff", "Agents"}
    assert labels["Sound Stuff"] == ["audio_speak", "voice_typer"]


def test_a_newcomer_joins_the_band_that_holds_its_capability():
    existing = {"One": ["a"], "Two": ["b", "c"]}
    capabilities = {"a": "tts", "b": "agent", "c": "agent", "d": "agent"}
    assert capability_groups(capabilities, existing)["Two"] == ["b", "c", "d"]


def test_a_capability_split_across_bands_follows_the_majority():
    existing = {"One": ["a"], "Two": ["b", "c"]}
    capabilities = {"a": "agent", "b": "agent", "c": "agent", "d": "agent"}
    assert capability_groups(capabilities, existing)["Two"] == ["b", "c", "d"]


def test_a_removed_tool_is_dropped_from_the_stored_bands():
    existing = {"One": ["a", "gone"], "Two": ["b"]}
    labels = capability_groups({"a": "tts", "b": "agent"}, existing)
    assert labels["One"] == ["a"]
    assert "gone" not in [t for group in labels.values() for t in group]


def test_a_band_left_empty_by_removals_disappears():
    existing = {"One": ["gone"], "Two": ["b"]}
    labels = capability_groups({"b": "agent"}, existing)
    assert set(labels) == {"Two"}


def test_no_tools_gives_no_bands():
    assert capability_groups({}) == {}
    assert capability_groups({}, {"One": ["a"]}) == {}
