"""
Video package — Audio Mixing.

Combines TTS narration with background music.
"""

import logging
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

from .config import BG_MUSIC_VOLUME, MUSIC_FILE

logger = logging.getLogger(__name__)


def mix_audio(
    narration_path: str,
    narration_duration: float,
    total_duration: float,
) -> AudioClip:
    """Mix TTS narration with background music.

    • Narration at full volume.
    • Background music at BG_MUSIC_VOLUME, trimmed/looped to *total_duration*.
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

    music_path = find_music_file()
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
            logger.info(
                f"🎵 Background music mixed at {BG_MUSIC_VOLUME * 100:.0f}% volume",
            )
            return mixed
        except Exception as e:
            logger.warning(f"⚠️  Could not load background music: {e}")

    return narration


def find_music_file() -> Optional[Path]:
    """Locate the background music file."""
    if MUSIC_FILE.exists():
        return MUSIC_FILE
    # Lambda flat-layout fallback
    alt = Path(__file__).parent.parent / "music" / "news_music.mp3"
    if alt.exists():
        return alt
    logger.warning("⚠️  No background music file found")
    return None
