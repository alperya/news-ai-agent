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

from .config import FPS, VIDEO_WIDTH, VIDEO_HEIGHT, BG_MUSIC_VOLUME, reading_seconds
from .tts import generate_tts, clean_for_narration
from .footage import fetch_stock_clips, fetch_stock_image, download_image
from .effects import (
    compose_stock_scenes,
    make_hook_clip,
    make_fact_overlay,
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

# Keywords that signal death or serious injury — triggers sad_music
_SAD_KEYWORDS = {
    # Dutch — death
    "dood", "doden", "dode", "dodelijk", "dodelijke", "omgekomen", "omgekome",
    "overlijden", "overleden", "gestorven", "sterfgeval", "sterfte",
    "slachtoffer", "slachtoffers",
    # Dutch — injury
    "gewond", "gewonden", "gewonde", "zwaargewond", "verwond", "verwonden", "letsel",
    # Dutch — violent events
    "aanslag", "schietpartij", "steekpartij", "moord", "doodslag",
    "explosie", "bombardement", "ramp", "tragedie", "catastrofe",
    # English (NOS/RTL sometimes use English terms)
    "dead", "death", "killed", "fatal", "fatality", "fatalities",
    "casualty", "casualties", "victim", "victims",
    "injury", "injured", "wounded", "shooting", "stabbing", "murder",
    "tragedy", "disaster",
}


def _detect_mood(title: str, content: str, emoji: str) -> str:
    """Classify news sentiment as 'positive', 'sad', or 'neutral'."""
    text = f"{title} {content}".lower()

    # Sad takes priority — death/injury overrides any positive framing
    if any(kw in text for kw in _SAD_KEYWORDS):
        return "sad"

    # Check emoji
    if any(ch in _POSITIVE_EMOJIS for ch in emoji):
        return "positive"

    # Check keywords in title + content
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
    hook: str = "",
    avoid_terms: Optional[List[str]] = None,
    ai_agent=None,
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
        raw_text = (hook + ". " + content) if hook else content
        narration_text = clean_for_narration(raw_text)
        # Hard cap: truncate to 100 words to stay within 30-45 s
        words = narration_text.split()
        if len(words) > 100:
            logger.warning(f"⚠️  Narration too long ({len(words)} words), truncating to 100")
            narration_text = " ".join(words[:100])
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
            avoid_terms=avoid_terms,
            ai_agent=ai_agent,
        )

        # ── 3. Compose layers ────────────────────────────────────────
        logger.info("🎨 Composing video layers...")
        hook_duration = 3.0 if hook else 0.0
        layers = [background]
        for seg in subtitle_segments:
            if seg.start < hook_duration:
                continue
            layers.extend(make_subtitle_clip(seg))
        if hook:
            layers.extend(make_hook_clip(hook, duration=hook_duration))

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


def create_fact_video(
    fact_text: str,
    footage_queries: Optional[List[str]],
    music_path: str,
    output_path: str,
    duration: Optional[float] = None,
) -> str:
    """Create a short vertical "Did you know?" Dutch-fact video for Stories.

    No TTS narration — the fact is read on screen — so the duration is sized
    to the reading time of *fact_text* (short → higher story completion).
    Background = Pexels Dutch B-roll (footage_queries), with Ken Burns /
    gradient fallbacks; audio = background music only.

    Args:
        fact_text:        The fact shown on screen (English).
        footage_queries:  Pexels search queries (specific → generic).
        music_path:       Background music file (e.g. FACT_STORY_MUSIC).
        output_path:      Where to save the final .mp4.
        duration:         Override length in seconds; defaults to reading time.

    Returns:
        Absolute path to the generated video file.
    """
    from moviepy import AudioFileClip, concatenate_audioclips
    from moviepy.audio.fx import AudioFadeOut

    if duration is None:
        duration = reading_seconds(fact_text)
    logger.info(f"🎬 Creating fact video ({duration:.1f}s): {fact_text[:60]}...")

    _cleanup_tmp()
    tmp_dir = tempfile.mkdtemp()

    try:
        # ── 1. Background visuals (Pexels footage → Ken Burns → gradient) ──
        logger.info("🖼️  Building background visuals...")
        # Short story → only need a few clips (each scene ≥ MIN_CLIP_DURATION)
        clip_count = max(2, int(duration / 3) + 1)
        clip_paths = fetch_stock_clips(
            fact_text, "", tmp_dir,
            count=clip_count,
            footage_queries=footage_queries,
            headline=fact_text,
        )
        if clip_paths:
            logger.info(f"   🎬 Using {len(clip_paths)} stock video clips")
            background = compose_stock_scenes(clip_paths, duration)
        else:
            stock_img = fetch_stock_image(fact_text, "", tmp_dir)
            if stock_img:
                prepared = prepare_image_for_portrait(stock_img, tmp_dir)
                logger.info("   🖼️  Ken Burns on Pexels stock photo")
                background = make_ken_burns_clip(prepared, duration)
            else:
                logger.info("   🎨 Animated gradient fallback")
                background = make_fallback_background(duration)

        # ── 2. Compose text overlay ──
        logger.info("🎨 Composing fact overlay...")
        layers = [background] + make_fact_overlay(fact_text, duration)
        video = CompositeVideoClip(layers, size=(VIDEO_WIDTH, VIDEO_HEIGHT))
        video = video.with_duration(duration)

        # ── 3. Background music only (trim/loop to duration, fade out) ──
        logger.info("🎵 Adding background music...")
        try:
            music = AudioFileClip(str(music_path))
            if music.duration >= duration:
                music = music.subclipped(0, duration)
            else:
                loops = int(duration / music.duration) + 1
                music = concatenate_audioclips([music] * loops).subclipped(0, duration)
            music = music.with_volume_scaled(BG_MUSIC_VOLUME)
            music = music.with_effects([AudioFadeOut(1.0)])
            video = video.with_audio(music)
        except Exception as e:
            logger.warning(f"⚠️  Could not load background music: {e}")

        # ── 4. Render ──
        logger.info(f"💾 Rendering to {output_path}...")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        temp_audio = os.path.join(
            tempfile.gettempdir(), Path(output_path).stem + "_TEMP_AUDIO.mp4",
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
            ffmpeg_params=["-profile:v", "high", "-pix_fmt", "yuv420p"],
        )
        video.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    logger.info(f"✅ Fact video created: {output_path} ({size_mb:.1f} MB)")
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
    avoid_terms: Optional[List[str]] = None,
    ai_agent=None,
) -> CompositeVideoClip:
    """Build visuals with 4-tier fallback:
    1. Pexels stock video clips → multi-scene composition
    2. Article image_url       → Ken Burns
    3. Pexels stock photo      → Ken Burns
    4. Animated gradient       → Ken Burns motion
    """
    # Priority 1 — Stock footage from Pexels
    clip_paths = fetch_stock_clips(
        title, content, tmp_dir,
        footage_queries=footage_queries,
        avoid_terms=avoid_terms,
        headline=title,
        ai_agent=ai_agent,
    )
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
