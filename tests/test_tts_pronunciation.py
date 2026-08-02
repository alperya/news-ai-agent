"""
Tests for Dutch place-name TTS pronunciation (respell → speak → restore).

The English narration voice read "Twente" as "Twenty" (viewer complaint).
Names are respelled phonetically for the TTS engine only; subtitles must
always show the real spelling.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from video.pronunciation import (
    NL_TTS_RESPELL,
    respell_for_tts,
    restore_display_words,
)
from video.tts import SubtitleSegment


def _segs(*texts):
    return [SubtitleSegment(text=t, start=i, end=i + 1) for i, t in enumerate(texts)]


# ── Respelling ───────────────────────────────────────────────────────────────

def test_respell_replaces_with_case_and_punctuation():
    """The reported incident, verbatim shape: 'Twente' mid-sentence."""
    out = respell_for_tts("Ground frost hit Twente on Tuesday.")
    assert out == "Ground frost hit Tventuh on Tuesday."


def test_respell_preserves_case_shapes():
    assert respell_for_tts("TWENTE") == "TVENTUH"
    assert respell_for_tts("twente") == "tventuh"
    assert respell_for_tts("Twente") == "Tventuh"


def test_respell_longest_match_wins():
    """'IJsselmeer' must not be half-replaced via the shorter 'IJssel' entry."""
    assert respell_for_tts("The IJsselmeer and the IJssel river") == \
        "The Eysselmayr and the Eyssel river"


def test_respell_leaves_unknown_text_untouched():
    text = "The cabinet met in parliament on Tuesday."
    assert respell_for_tts(text) == text


def test_respell_word_count_never_changes():
    """Single-token respellings keep word counts aligned with TTS output."""
    text = "Schiphol Vierdaagse Groningen Feyenoord"
    assert len(respell_for_tts(text).split()) == len(text.split())
    for original, respelled in NL_TTS_RESPELL.items():
        assert " " not in respelled and "-" not in respelled, (original, respelled)


def test_respell_does_not_touch_substrings():
    """Word boundaries only — no replacements inside larger words."""
    assert respell_for_tts("goudastraat") == "goudastraat"


# ── Restoration (what subtitles show) ────────────────────────────────────────

def test_restore_roundtrip_on_grouped_segments():
    original = "Frost hit Twente while Schiphol delayed flights"
    spoken = respell_for_tts(original)
    segs = _segs(*spoken.split())
    restored = restore_display_words(segs)
    assert " ".join(s.text for s in restored) == original


def test_restore_handles_multiword_segments():
    """edge-tts sentence boundaries can yield multi-word segments."""
    segs = _segs("Tventuh recorded", "frost near Skippol.")
    restored = restore_display_words(segs)
    assert restored[0].text == "Twente recorded"
    assert restored[1].text == "frost near Schiphol."


def test_restore_is_safe_on_never_respelled_text():
    segs = _segs("Ordinary English sentence.")
    assert restore_display_words(segs)[0].text == "Ordinary English sentence."


# ── Env-var lexicon extension ────────────────────────────────────────────────

def test_env_extension_adds_entry(monkeypatch):
    monkeypatch.setenv("TTS_PRONUNCIATIONS", '{"tilburg": "tilburkh"}')
    assert respell_for_tts("Tilburg festival") == "Tilburkh festival"
    restored = restore_display_words(_segs("Tilburkh festival"))
    assert restored[0].text == "Tilburg festival"


def test_env_extension_invalid_json_is_ignored(monkeypatch):
    monkeypatch.setenv("TTS_PRONUNCIATIONS", "not-json{")
    assert respell_for_tts("Twente") == "Tventuh"  # built-ins still work


# ── Lexicon hygiene ──────────────────────────────────────────────────────────

def test_respellings_are_unique_and_disjoint_from_keys():
    """Restoration is a reverse lookup — values must be unique nonsense tokens
    and never collide with a real lexicon key."""
    values = list(NL_TTS_RESPELL.values())
    assert len(values) == len(set(values))
    assert not set(values) & set(NL_TTS_RESPELL.keys())
