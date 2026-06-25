"""Tests for reading_seconds() — sizes the fact story to its reading time."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from video.config import reading_seconds


def test_short_text_clamps_to_min():
    assert reading_seconds("two words") == 5.0


def test_very_long_text_clamps_to_max():
    assert reading_seconds("word " * 200) == 15.0


def test_scales_with_word_count():
    secs = reading_seconds(" ".join(["word"] * 25))  # 25 / 2.5 + 1 = 11s
    assert 10.0 < secs < 12.0
