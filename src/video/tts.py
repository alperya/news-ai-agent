"""
Video package — Text-to-Speech & Subtitle Timing.

Primary:  ElevenLabs Multilingual v2 — natural, human-like speech ($5/mo Starter).
Fallback: edge-tts (free Microsoft Neural TTS) when no API key is set.
"""

import asyncio
import base64
import os
import re
import logging
from dataclasses import dataclass
from typing import List, Optional

import requests as http_requests

from .config import (
    EDGE_TTS_VOICE,
    EDGE_TTS_RATE,
    EDGE_TTS_PITCH,
    ELEVENLABS_API_KEY,
    ELEVENLABS_MODEL,
    ELEVENLABS_TTS_URL,
    ELEVENLABS_VOICE_ID,
)

logger = logging.getLogger(__name__)


@dataclass
class SubtitleSegment:
    """A subtitle line with timing."""
    text: str
    start: float   # seconds
    end: float     # seconds


# ── Public API ────────────────────────────────────────────────────────────────

def generate_tts(
    text: str, output_audio: str, output_subs: str,
) -> List[SubtitleSegment]:
    """Generate TTS audio + subtitle segments.

    Tries ElevenLabs first (natural voice), falls back to edge-tts (free).
    """
    api_key = ELEVENLABS_API_KEY or os.environ.get("ELEVENLABS_API_KEY", "")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", ELEVENLABS_VOICE_ID)

    if api_key:
        try:
            segments = _elevenlabs_tts(
                text, output_audio, output_subs, api_key, voice_id,
            )
            logger.info("🎙️  ElevenLabs TTS — natural voice ✅")
            return segments
        except Exception as e:
            logger.warning(f"⚠️  ElevenLabs failed, falling back to edge-tts: {e}")
    else:
        logger.info("ℹ️  No ELEVENLABS_API_KEY — using free edge-tts")

    return _edge_tts(text, output_audio, output_subs)


def clean_for_narration(text: str) -> str:
    """Clean post text for TTS narration.

    Removes hashtags, URLs, emojis, 'Kaynak:' lines.
    """
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or all(
            w.startswith("#") for w in stripped.split() if w
        ):
            continue
        if "Kaynak:" in line:
            continue
        if stripped == ".":
            continue
        cleaned.append(line)

    result = "\n".join(cleaned).strip()
    result = re.sub(r"https?://\S+", "", result)
    result = re.sub(r"^[\U00002600-\U0001FFFF\s]+", "", result).strip()
    result = re.sub(r"[\U00002600-\U0001FFFF]", "", result).strip()
    return result.strip()


def parse_srt(srt_text: str) -> List[SubtitleSegment]:
    """Parse SRT content into SubtitleSegment list."""
    segments: List[SubtitleSegment] = []
    pattern = re.compile(
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\s*-->\s*"
        r"(\d{2}):(\d{2}):(\d{2}),(\d{3})"
    )
    lines = srt_text.strip().split("\n")
    i = 0
    while i < len(lines):
        match = pattern.search(lines[i])
        if match:
            h1, m1, s1, ms1 = (int(match.group(j)) for j in range(1, 5))
            h2, m2, s2, ms2 = (int(match.group(j)) for j in range(5, 9))
            start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
            end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000
            i += 1
            text_parts = []
            while (
                i < len(lines)
                and lines[i].strip()
                and not pattern.search(lines[i])
                and not lines[i].strip().isdigit()
            ):
                text_parts.append(lines[i].strip())
                i += 1
            text = " ".join(text_parts)
            if text:
                segments.append(SubtitleSegment(text=text, start=start, end=end))
        else:
            i += 1
    return segments


def group_subtitle_segments(
    segments: List[SubtitleSegment], max_words: int = 8,
) -> List[SubtitleSegment]:
    """Group word-level segments into subtitle chunks.

    Each chunk contains at most *max_words* words (default 8 = 2 lines × 4).
    """
    if not segments:
        return segments

    grouped: List[SubtitleSegment] = []
    cur_words: List[str] = []
    cur_start: Optional[float] = None
    cur_end: float = 0.0

    for seg in segments:
        seg_words = seg.text.split()
        if cur_start is None:
            cur_words = seg_words
            cur_start = seg.start
            cur_end = seg.end
        elif len(cur_words) + len(seg_words) <= max_words:
            cur_words.extend(seg_words)
            cur_end = seg.end
        else:
            grouped.append(SubtitleSegment(
                text=" ".join(cur_words), start=cur_start, end=cur_end,
            ))
            cur_words = seg_words
            cur_start = seg.start
            cur_end = seg.end

    if cur_words and cur_start is not None:
        grouped.append(SubtitleSegment(
            text=" ".join(cur_words), start=cur_start, end=cur_end,
        ))
    return grouped


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

def _elevenlabs_tts(
    text: str,
    output_audio: str,
    output_subs: str,
    api_key: str,
    voice_id: str,
) -> List[SubtitleSegment]:
    """Generate speech via ElevenLabs with word-level timestamps.

    Falls back to the regular TTS endpoint (no timestamps) if the
    ``with-timestamps`` endpoint returns 401/403.
    """
    # ── Try with-timestamps first (gives word-level alignment) ────────
    url_ts = f"{ELEVENLABS_TTS_URL}/{voice_id}/with-timestamps"
    payload = {
        "text": text,
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability": 0.55,
            "similarity_boost": 0.80,
            "style": 0.35,
        },
    }
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
    }

    resp = http_requests.post(url_ts, headers=headers, json=payload, timeout=90)

    if resp.status_code == 200:
        data = resp.json()
        audio_bytes = base64.b64decode(data["audio_base64"])
        with open(output_audio, "wb") as f:
            f.write(audio_bytes)

        alignment = data.get("alignment", {})
        characters = alignment.get("characters", [])
        starts = alignment.get("character_start_times_seconds", [])
        ends = alignment.get("character_end_times_seconds", [])
        word_segments = _chars_to_words(characters, starts, ends)
        _write_srt(word_segments, output_subs)
        return group_subtitle_segments(word_segments, max_words=8)

    # ── Fallback: regular TTS endpoint (no timestamps) ────────────────
    if resp.status_code in (401, 403):
        logger.warning(
            "⚠️  with-timestamps endpoint returned %s — trying regular TTS",
            resp.status_code,
        )
        url_reg = f"{ELEVENLABS_TTS_URL}/{voice_id}"
        resp2 = http_requests.post(url_reg, headers=headers, json=payload, timeout=90)
        resp2.raise_for_status()

        with open(output_audio, "wb") as f:
            f.write(resp2.content)

        # Estimate word timings from audio duration
        from moviepy import AudioFileClip
        clip = AudioFileClip(output_audio)
        duration = clip.duration
        clip.close()
        word_segments = _estimate_word_timings(text, duration)
        _write_srt(word_segments, output_subs)
        return group_subtitle_segments(word_segments, max_words=8)

    # Other errors — raise so caller can fall back to edge-tts
    resp.raise_for_status()


def _chars_to_words(
    chars: list, starts: list, ends: list,
) -> List[SubtitleSegment]:
    """Convert character-level timestamps to word-level segments."""
    segments: List[SubtitleSegment] = []
    word = ""
    word_start: Optional[float] = None
    word_end: Optional[float] = None

    for i, ch in enumerate(chars):
        if ch in (" ", "\n"):
            if word.strip() and word_start is not None:
                segments.append(SubtitleSegment(
                    text=word.strip(), start=word_start, end=word_end,
                ))
            word = ""
            word_start = None
            word_end = None
        else:
            if word_start is None:
                word_start = starts[i] if i < len(starts) else 0.0
            word += ch
            word_end = ends[i] if i < len(ends) else word_start + 0.1

    if word.strip() and word_start is not None:
        segments.append(SubtitleSegment(
            text=word.strip(), start=word_start, end=word_end,
        ))
    return segments


def _write_srt(segments: List[SubtitleSegment], path: str) -> None:
    """Write SubtitleSegments as SRT for debugging."""
    lines = []
    for idx, seg in enumerate(segments, 1):
        s = _srt_ts(seg.start)
        e = _srt_ts(seg.end)
        lines.append(f"{idx}\n{s} --> {e}\n{seg.text}\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _srt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds % 1) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _estimate_word_timings(
    text: str, total_duration: float,
) -> List[SubtitleSegment]:
    """Estimate evenly-spaced word timings when no alignment data."""
    words = text.split()
    if not words:
        return []
    dur_per_word = total_duration / len(words)
    segments: List[SubtitleSegment] = []
    for i, w in enumerate(words):
        segments.append(SubtitleSegment(
            text=w,
            start=i * dur_per_word,
            end=(i + 1) * dur_per_word,
        ))
    return segments


# ── edge-tts fallback ────────────────────────────────────────────────────────

def _edge_tts(
    text: str, output_audio: str, output_subs: str,
) -> List[SubtitleSegment]:
    """Free fallback TTS via edge-tts."""
    import edge_tts

    async def _run():
        comm = edge_tts.Communicate(
            text, EDGE_TTS_VOICE, rate=EDGE_TTS_RATE, pitch=EDGE_TTS_PITCH,
        )
        await comm.save(output_audio)

        comm2 = edge_tts.Communicate(
            text, EDGE_TTS_VOICE, rate=EDGE_TTS_RATE, pitch=EDGE_TTS_PITCH,
        )
        submaker = edge_tts.SubMaker()
        async for chunk in comm2.stream():
            if chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                submaker.feed(chunk)

        srt_content = submaker.get_srt()
        with open(output_subs, "w", encoding="utf-8") as f:
            f.write(srt_content)
        return srt_content

    srt = asyncio.run(_run())
    segments = parse_srt(srt)
    return group_subtitle_segments(segments, max_words=8)
