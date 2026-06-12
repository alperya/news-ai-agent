"""
Video package — Audio Mixing.

Combines TTS narration with background music.
"""

import logging
import random
from pathlib import Path
from typing import Optional

import numpy as np
from moviepy import (
    AudioClip,
    AudioFileClip,
    CompositeAudioClip,
    concatenate_audioclips,
)
from moviepy.audio.fx import AudioFadeOut

from .config import BG_MUSIC_VOLUME, ALTERNATIVE_MUSIC_FILE, CALM_MUSIC_FILE, MUSIC_FILE, SAD_MUSIC_FILE

logger = logging.getLogger(__name__)


def mix_audio(
    narration_path: str,
    narration_duration: float,
    total_duration: float,
    mood: str = "neutral",
) -> AudioClip:
    """Mix TTS narration with background music.

    • Narration at full volume.
    • Background music at BG_MUSIC_VOLUME, trimmed/looped to *total_duration*.
    • *mood*: 'positive' → news_music, anything else → calm_music.
    """
    narration = AudioFileClip(narration_path)

    # Pad narration with silence to match total_duration
    if narration.duration < total_duration:
        silence = AudioClip(
            lambda t: np.zeros((1 if np.isscalar(t) else len(t), 2)),
            duration=total_duration - narration.duration,
            fps=44100,
        )
        narration = concatenate_audioclips([narration, silence])

    music_path = find_music_file(mood)
    if music_path:
        try:
            music = AudioFileClip(str(music_path))
            if music.duration >= total_duration:
                music = music.subclipped(0, total_duration)
            else:
                loops = int(total_duration / music.duration) + 1
                music = concatenate_audioclips([music] * loops).subclipped(
                    0, total_duration,
                )
            music = music.with_volume_scaled(BG_MUSIC_VOLUME)
            music = music.with_effects([AudioFadeOut(1.0)])  # fade out last 1s
            mixed = CompositeAudioClip([narration, music]).with_duration(
                total_duration,
            )
            music_name = music_path.name
            logger.info(
                f"🎵 Background music: {music_name} at {BG_MUSIC_VOLUME * 100:.0f}% volume (mood={mood})",
            )
            return mixed
        except Exception as e:
            logger.warning(f"⚠️  Could not load background music: {e}")

    return narration


def find_music_file(mood: str = "neutral") -> Optional[Path]:
    """Locate the background music file based on news mood.

    *mood* = 'positive' → news_music.mp3
    *mood* = 'sad'      → sad_music.mp3 (death/injury news)
    *mood* = anything else → calm_music.mp3 (60%) or alternative_music.mp3 (40%)
    """
    if mood == "positive":
        primary, fallback_name = MUSIC_FILE, "news_music.mp3"
    elif mood == "sad":
        primary, fallback_name = SAD_MUSIC_FILE, "sad_music.mp3"
    else:
        if random.random() < 0.6:
            primary, fallback_name = CALM_MUSIC_FILE, "calm_music.mp3"
        else:
            primary, fallback_name = ALTERNATIVE_MUSIC_FILE, "alternative_music.mp3"

    if primary.exists():
        return primary
    # Lambda flat-layout fallback
    alt = Path(__file__).parent.parent / "music" / fallback_name
    if alt.exists():
        return alt
    # Ultimate fallback: try the other neutral tracks, then news_music
    for f in (CALM_MUSIC_FILE, ALTERNATIVE_MUSIC_FILE, MUSIC_FILE):
        if f.exists():
            return f
    logger.warning("⚠️  No background music file found")
    return None
