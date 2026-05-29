"""
Video package — Main Orchestrator.

Generates Instagram Reels from news content:
  1. TTS narration   (edge-tts, $0)
  2. Stock footage    (Pexels API, $0) ── or Ken Burns on article image
  3. Subtitle overlay
  4. Background music
"""

import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import List, Optional

from moviepy import AudioFileClip, CompositeVideoClip

from .config import FPS, VIDEO_WIDTH, VIDEO_HEIGHT
from .tts import generate_tts, clean_for_narration
from .footage import fetch_stock_clips, fetch_stock_image, download_image
from .effects import (
    compose_stock_scenes,
    make_ken_burns_clip,
    make_fallback_background,
    make_subtitle_clip,
    prepare_image_for_portrait,
)
from .audio import mix_audio

logger = logging.getLogger(__name__)

# Keywords/emojis that signal positive/happy news
_POSITIVE_EMOJIS = frozenset(
    "🎉🏆✅🥇🎊🎯💪🌟⭐🏅🥈🥉👏🤝💚🎓📈🚀"
)
_POSITIVE_KEYWORDS = {
    "success", "successful", "won", "winning", "victory", "champion",
    "record", "growth", "increase", "award", "celebration", "happy",
    "encouraging", "positive", "agreement", "peace", "supports",
    "developed", "innovation", "breakthrough", "progress", "improvement",
    "overwinning", "succes", "winnaar", "kampioen",
    "groei", "stijging", "deal", "doorbraak", "innovatie",
}


def _detect_mood(title: str, content: str, emoji: str) -> str:
    """Classify news sentiment as 'positive' or 'neutral'."""
    # Check emoji
    if any(ch in _POSITIVE_EMOJIS for ch in emoji):
        return "positive"

    # Check keywords in title + content
    text = f"{title} {content}".lower()
    if sum(1 for kw in _POSITIVE_KEYWORDS if kw in text) >= 2:
        return "positive"

    return "neutral"


def create_news_video(
    title: str,
    content: str,
    source: str,
    hashtags: List[str],
    output_path: str,
    emoji: str = "📰",
    image_url: Optional[str] = None,
    footage_queries: Optional[List[str]] = None,
) -> str:
    """Create a Reels-format news video.

    Args:
        title:           News headline.
        content:         Full post text (used for TTS narration).
        source:          News source name.
        hashtags:        List of hashtag strings.
        output_path:     Where to save the final .mp4.
        emoji:           Emoji for branding (unused in video).
        image_url:       URL of the news article image (Ken Burns fallback).
        footage_queries: AI-generated Pexels queries (specific → generic).
                         Falls back to keyword extraction when empty.

    Returns:
        Absolute path to the generated video file.
    """
    logger.info(f"🎬 Creating news video: {title[:50]}...")

    # Clean up leftover /tmp files from previous (timed-out) Lambda runs
    _cleanup_tmp()

    tmp_dir = tempfile.mkdtemp()
    audio_path = os.path.join(tmp_dir, "narration.mp3")
    subs_path = os.path.join(tmp_dir, "subs.srt")

    try:
        # ── 1. TTS narration ──────────────────────────────────────────
        logger.info("🔊 Generating TTS narration...")
        narration_text = clean_for_narration(content)
        subtitle_segments = generate_tts(narration_text, audio_path, subs_path)
        logger.info(f"   {len(subtitle_segments)} subtitle segments")

        narration_clip = AudioFileClip(audio_path)
        narration_duration = narration_clip.duration
        narration_clip.close()
        total_duration = narration_duration + 1.0

        # ── 2. Background visuals ────────────────────────────────────
        logger.info("🖼️  Building background visuals...")
        background = _build_background(
            title, content, image_url, tmp_dir, total_duration,
            footage_queries=footage_queries,
        )

        # ── 3. Compose layers ────────────────────────────────────────
        logger.info("🎨 Composing video layers...")
        layers = [background]
        for seg in subtitle_segments:
            layers.extend(make_subtitle_clip(seg))

        video = CompositeVideoClip(layers, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
        video = video.with_duration(total_duration)

        # ── 4. Mix audio ─────────────────────────────────────────────
        logger.info("🎵 Mixing audio...")
        mood = _detect_mood(title, content, emoji)
        logger.info(f"   🎭 Detected mood: {mood}")
        mixed = mix_audio(audio_path, narration_duration, total_duration, mood=mood)
        video = video.with_audio(mixed)

        # ── 5. Render ────────────────────────────────────────────────
        logger.info(f"💾 Rendering to {output_path}...")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        temp_audio = os.path.join(
            tempfile.gettempdir(),
            Path(output_path).stem + "_TEMP_AUDIO.mp4",
        )
        video.write_videofile(
            output_path,
            fps=FPS,
            codec="libx264",
            audio_codec="aac",
            preset="ultrafast",
            bitrate="4000k",
            threads=4,
            logger=None,
            temp_audiofile=temp_audio,
            ffmpeg_params=[
                "-profile:v", "high",
                "-pix_fmt", "yuv420p",
            ],
        )
        video.close()

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"✅ Video created: {output_path} ({size_mb:.1f} MB)")
    return os.path.abspath(output_path)


# ── Internal ──────────────────────────────────────────────────────────────────

def _cleanup_tmp():
    """Remove leftover temp files from previous Lambda invocations.

    When a Lambda times out during video rendering, the `finally` cleanup
    never runs, leaving large video files in /tmp.  On retry the same
    execution environment is reused, causing 'No space left on device'.
    """
    tmp_root = tempfile.gettempdir()
    for entry in os.listdir(tmp_root):
        path = os.path.join(tmp_root, entry)
        try:
            if os.path.isdir(path) and entry.startswith("tmp"):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.isfile(path) and (
                entry.endswith(".mp4")
                or entry.endswith(".mp3")
                or entry.endswith(".srt")
            ):
                os.remove(path)
        except OSError:
            pass


def _build_background(
    title: str,
    content: str,
    image_url: Optional[str],
    tmp_dir: str,
    duration: float,
    footage_queries: Optional[List[str]] = None,
) -> CompositeVideoClip:
    """Build visuals with 4-tier fallback:
    1. Pexels stock video clips → multi-scene composition
    2. Article image_url       → Ken Burns
    3. Pexels stock photo      → Ken Burns
    4. Animated gradient       → Ken Burns motion
    """
    # Priority 1 — Stock footage from Pexels
    clip_paths = fetch_stock_clips(title, content, tmp_dir, footage_queries=footage_queries)
    if clip_paths:
        logger.info(f"   🎬 Using {len(clip_paths)} stock video clips")
        return compose_stock_scenes(clip_paths, duration)

    # Priority 2 — Ken Burns on article image
    img_path = download_image(image_url, tmp_dir) if image_url else None
    if img_path:
        prepared = prepare_image_for_portrait(img_path, tmp_dir)
        logger.info("   🖼️  Ken Burns effect on news image")
        return make_ken_burns_clip(prepared, duration)

    # Priority 3 — Ken Burns on Pexels stock photo
    stock_img = fetch_stock_image(title, content, tmp_dir)
    if stock_img:
        prepared = prepare_image_for_portrait(stock_img, tmp_dir)
        logger.info("   🖼️  Ken Burns effect on Pexels stock photo")
        return make_ken_burns_clip(prepared, duration)

    # Priority 4 — Animated gradient
    logger.info("   🎨 Animated gradient fallback")
    return make_fallback_background(duration)
