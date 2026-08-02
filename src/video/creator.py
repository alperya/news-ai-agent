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
from typing import List, Optional, Set

from moviepy import AudioFileClip, CompositeVideoClip

from .config import FPS, VIDEO_WIDTH, VIDEO_HEIGHT, BG_MUSIC_VOLUME, reading_seconds
from .tts import generate_tts, clean_for_narration, cap_narration
from .footage import fetch_stock_clips, fetch_stock_image, download_image
from .effects import (
    OverlaySpec,
    compose_stock_scenes,
    compute_dhash,
    hamming,
    build_hook_overlays,
    build_source_label_overlay,
    build_subtitle_overlays,
    make_fact_overlay,
    make_ken_burns_clip,
    make_fallback_background,
    prepare_image_for_portrait,
    render_fallback_bg_mp4,
    render_gradient_png,
    render_ken_burns_mp4,
    _stitch_clips_to_file,
)
from .ffcompose import assemble_reel
from .audio import mix_audio

logger = logging.getLogger(__name__)

# Max Hamming distance (out of 64 dHash bits) for two cover images to count as
# near-duplicates — catches NOS's recurring "weekdienst" template card even at
# a different URL. Lower = stricter; ~8-10 distinguishes near-dups from distinct.
COVER_HASH_THRESHOLD = 10

# Length of the real-photo cover scene in the hybrid composition (seconds).
COVER_SCENE_DURATION = 4.0

# Longer cover for stories tied to one specific place. Stock footage of a small
# Dutch town does not exist, so every stock second is generic by design — the
# article's own photo is the only frame that actually shows the place. It also
# reprises mid-Reel, but that slot plays at the regular body-segment length
# (the stitcher pins only the lead clip), so the reprise is rendered shorter.
COVER_SCENE_DURATION_PLACE = 7.0
COVER_REPRISE_DURATION = 3.5

# Narration word budget = hook (~8-12 words) + content (prompt-capped 75-95
# words) + slack. ~120 words ≈ 48 s of speech. Capping happens on sentence
# boundaries (cap_narration) so the final sentence is never clipped — the old
# raw 100-word cut dropped the tail of the last sentence (hook + 96-word content
# = 107 words → lost "...in 1928 during the Amsterdam Summer Games.").
MAX_NARRATION_WORDS = 120

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
    exclude_media_ids: Optional[Set[int]] = None,
    recent_image_urls: Optional[Set[str]] = None,
    recent_cover_hashes: Optional[List[int]] = None,
    used_media_ids: Optional[List[int]] = None,
    cover_meta_out: Optional[dict] = None,
    footage_plan: Optional[dict] = None,
    footage_audit_out: Optional[List[dict]] = None,
) -> str:
    """Create a Reels-format news video.

    Args:
        title:           News headline.
        content:         Full post text (used for TTS narration).
        source:          News source name.
        hashtags:        List of hashtag strings.
        output_path:     Where to save the final .mp4.
        emoji:           Emoji for branding (unused in video).
        image_url:       URL of the news article image (preferred as cover).
        footage_queries: AI-generated Pexels queries (specific → generic).
                         Falls back to keyword extraction when empty.
        exclude_media_ids:   Pexels video ids used within the reuse window
                             (de-prioritised so the cover is fresh).
        recent_image_urls:   Article image URLs used recently (skip as cover).
        recent_cover_hashes: dHashes of recent covers (near-dup detection).
        used_media_ids:      Out-param — appended with Pexels ids actually used.
        cover_meta_out:      Out-param dict — populated with cover_image_url /
                             cover_image_hash when a real photo becomes the cover.
        footage_plan:        Geographic footage plan (see :mod:`footage_geo`) —
                             decides how strictly footage is filtered and how
                             much screen time the real photo earns.
        footage_audit_out:   Out-param list — per-candidate accept/reject trail.

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
        # Cap narration length, but only on SENTENCE boundaries so the final
        # sentence is never clipped mid-thought. Budget fits a ~12-word hook +
        # a 95-word content (prompt-enforced max) with slack (~48 s narration).
        original_words = len(narration_text.split())
        narration_text = cap_narration(narration_text, MAX_NARRATION_WORDS)
        if len(narration_text.split()) < original_words:
            logger.warning(
                f"⚠️  Narration too long ({original_words} words), capped to "
                f"{len(narration_text.split())} on a sentence boundary"
            )
        subtitle_segments = generate_tts(narration_text, audio_path, subs_path)
        logger.info(f"   {len(subtitle_segments)} subtitle segments")

        narration_clip = AudioFileClip(audio_path)
        narration_duration = narration_clip.duration
        narration_clip.close()
        total_duration = narration_duration + 1.0

        # ── 2. Background visuals (returns a background mp4 path) ─────
        logger.info("🖼️  Building background visuals...")
        bg_meta: dict = {}
        bg_path = _build_background(
            title, content, image_url, tmp_dir, total_duration,
            footage_queries=footage_queries,
            avoid_terms=avoid_terms,
            ai_agent=ai_agent,
            exclude_ids=exclude_media_ids,
            recent_image_urls=recent_image_urls,
            recent_cover_hashes=recent_cover_hashes,
            used_ids_out=used_media_ids,
            cover_meta_out=cover_meta_out,
            footage_plan=footage_plan,
            audit_out=footage_audit_out,
            bg_meta_out=bg_meta,
            headline=hook or content[:180],
        )

        # ── 3. Build static overlays (gradient + subtitles + hook) ───
        logger.info("🎨 Building overlays...")
        hook_duration = 3.0 if hook else 0.0
        overlays = [OverlaySpec(render_gradient_png(os.path.join(tmp_dir, "grad.png")),
                                0, 0.0, total_duration)]
        overlays += build_subtitle_overlays(subtitle_segments, hook_duration, tmp_dir)
        if hook:
            overlays += build_hook_overlays(hook, tmp_dir, duration=hook_duration)
        # Label the stock segments so viewers stop reading generic footage as an
        # AI-generated picture of the location.
        if bg_meta.get("source") in ("hybrid", "stock") and total_duration > 8:
            overlays += build_source_label_overlay(
                tmp_dir, start=bg_meta.get("stock_starts_at", 0.0) + 0.3,
            )

        # ── 4. Mix audio → file (cheap; not per-frame) ───────────────
        logger.info("🎵 Mixing audio...")
        mood = _detect_mood(title, content, emoji)
        logger.info(f"   🎭 Detected mood: {mood}")
        mixed = mix_audio(audio_path, narration_duration, total_duration, mood=mood)
        mixed_audio_path = os.path.join(tmp_dir, "mixed.m4a")
        mixed.write_audiofile(mixed_audio_path, codec="aac", fps=44100, logger=None)

        # ── 5. Final assembly — single ffmpeg pass ───────────────────
        logger.info(f"💾 Rendering to {output_path}...")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        assemble_reel(bg_path, mixed_audio_path, overlays, total_duration, output_path)

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
    exclude_ids: Optional[Set[int]] = None,
    recent_image_urls: Optional[Set[str]] = None,
    recent_cover_hashes: Optional[List[int]] = None,
    used_ids_out: Optional[List[int]] = None,
    cover_meta_out: Optional[dict] = None,
    footage_plan: Optional[dict] = None,
    audit_out: Optional[List[dict]] = None,
    bg_meta_out: Optional[dict] = None,
    headline: str = "",
) -> str:
    """Build the background and return a single **mp4 file path** (no overlays).

    Freshness-aware fallback chain:
    1. Real news photo (if fresh) → cover mp4 + Pexels stock body (hybrid)
    2. Pexels stock video clips    → stitched mp4 (id-deduped)
    3. Pexels stock photo          → Ken Burns mp4
    4. Animated gradient           → Ken Burns mp4

    A real news photo is preferred as the cover (authentic + naturally distinct
    per article) but only when it is *fresh*: its URL was not used recently and
    it is not a perceptual near-duplicate of a recent cover (catches NOS's
    recurring "weekdienst" template card). Otherwise the cover falls back to a
    fresh Pexels stock clip. The readability gradient is NOT baked here — it is
    burned as an overlay in the single ffmpeg assembly pass.

    For place-specific stories (``footage_plan["place_mode"] == "no_stock"``)
    the article photo is the only footage that genuinely depicts the place, so
    it gets a longer cover and a second appearance mid-Reel. *bg_meta_out*
    reports what was actually built, which the caller uses to time the
    stock-footage disclosure label.
    """
    recent_image_urls = recent_image_urls or set()
    recent_cover_hashes = recent_cover_hashes or []
    place_specific = (footage_plan or {}).get("place_mode") == "no_stock"

    def _record_bg_meta(source: str, cover_seconds: float = 0.0, has_real_cover: bool = False):
        """Report the background's shape so overlays can be timed against it."""
        if bg_meta_out is not None:
            bg_meta_out.update({
                "source": source,
                "cover_seconds": cover_seconds,
                "stock_starts_at": cover_seconds,
                "has_real_cover": has_real_cover,
            })

    # Priority 1 — Real news photo as cover (only when fresh)
    cover_img_path = None
    cover_dhash = None
    if image_url and image_url not in recent_image_urls:
        candidate = download_image(image_url, tmp_dir)
        if candidate:
            try:
                cover_dhash = compute_dhash(candidate)
                is_near_dup = any(
                    hamming(cover_dhash, h) <= COVER_HASH_THRESHOLD
                    for h in recent_cover_hashes
                )
            except Exception as e:
                logger.warning(f"⚠️  Could not hash article image: {e}")
                cover_dhash, is_near_dup = None, False
            if is_near_dup:
                logger.info("   ♻️  Article image is a recent near-duplicate — using stock cover")
            else:
                cover_img_path = candidate
    elif image_url:
        logger.info("   ♻️  Article image URL used recently — using stock cover")

    def _record_cover_meta():
        """Persist the real photo as the cover only when it actually is one."""
        if cover_meta_out is not None:
            cover_meta_out["cover_image_url"] = image_url
            if cover_dhash is not None:
                cover_meta_out["cover_image_hash"] = cover_dhash

    # The vision gate and the avoid terms are written in English; the Dutch
    # headline would be judged against them. Prefer the English hook/content.
    footage_headline = headline or title

    if cover_img_path:
        # A place-specific story gets a longer cover: the article's own photo is
        # the only footage that actually shows the place, so it earns the time.
        base_cover = COVER_SCENE_DURATION_PLACE if place_specific else COVER_SCENE_DURATION
        cover_dur = min(base_cover, duration * 0.4)
        prepared = prepare_image_for_portrait(cover_img_path, tmp_dir)
        # Pre-render the cover to an MP4 with ffmpeg (fast) so it joins the stock
        # clips as a file-backed scene. Doing the Ken Burns in moviepy's final
        # composite (pure-Python per-frame) is too slow and blows the Lambda
        # 15-min timeout — that regression is why this path exists.
        cover_mp4 = render_ken_burns_mp4(
            prepared, cover_dur, os.path.join(tmp_dir, "cover_kb.mp4"),
        )
        body_paths = fetch_stock_clips(
            title, content, tmp_dir,
            footage_queries=footage_queries,
            avoid_terms=avoid_terms,
            headline=footage_headline,
            ai_agent=ai_agent,
            exclude_ids=exclude_ids,
            used_ids_out=used_ids_out,
            footage_plan=footage_plan,
            audit_out=audit_out,
        )
        if cover_mp4 and body_paths:
            scenes = [cover_mp4] + body_paths
            # Return to the real photo mid-Reel rather than holding it longer up
            # front — same authentic seconds, without a static opening.
            if place_specific and len(body_paths) >= 4:
                reprise = render_ken_burns_mp4(
                    prepared, COVER_REPRISE_DURATION, os.path.join(tmp_dir, "cover_kb2.mp4"),
                )
                if reprise:
                    mid = len(body_paths) // 2
                    scenes = [cover_mp4] + body_paths[:mid] + [reprise] + body_paths[mid:]
            merged = _stitch_clips_to_file(scenes, duration, lead_duration=cover_dur)
            if merged:
                logger.info(f"   🎬 Hybrid: real-photo cover + {len(body_paths)} stock scenes")
                _record_cover_meta()
                _record_bg_meta("hybrid", cover_seconds=cover_dur, has_real_cover=True)
                return merged
        if body_paths:
            # Cover render failed (or stitch failed) — use stock clips alone.
            # Cover is now a stock clip, so do NOT record the photo as the cover.
            merged = _stitch_clips_to_file(body_paths, duration)
            if merged:
                logger.info(f"   🎬 Cover render failed; using {len(body_paths)} stock clips")
                _record_bg_meta("stock")
                return merged
        # No usable stock body — fall back to a full Ken Burns cover (mp4).
        cover_only = render_ken_burns_mp4(prepared, duration, os.path.join(tmp_dir, "cover_full.mp4"))
        if cover_only:
            logger.info("   🖼️  Real-photo cover only (no stock body available)")
            _record_cover_meta()
            _record_bg_meta("photo", cover_seconds=duration, has_real_cover=True)
            return cover_only

    # Priority 2 — Stock footage from Pexels (cover from stock, id-deduped)
    clip_paths = fetch_stock_clips(
        title, content, tmp_dir,
        footage_queries=footage_queries,
        avoid_terms=avoid_terms,
        headline=footage_headline,
        ai_agent=ai_agent,
        exclude_ids=exclude_ids,
        used_ids_out=used_ids_out,
        footage_plan=footage_plan,
        audit_out=audit_out,
    )
    if clip_paths:
        merged = _stitch_clips_to_file(clip_paths, duration)
        if merged:
            logger.info(f"   🎬 Using {len(clip_paths)} stock video clips")
            _record_bg_meta("stock")
            return merged

    # Priority 3 — Ken Burns on Pexels stock photo
    stock_img = fetch_stock_image(title, content, tmp_dir, footage_plan=footage_plan)
    if stock_img:
        prepared = prepare_image_for_portrait(stock_img, tmp_dir)
        kb = render_ken_burns_mp4(prepared, duration, os.path.join(tmp_dir, "stockphoto.mp4"))
        if kb:
            logger.info("   🖼️  Ken Burns effect on Pexels stock photo")
            _record_bg_meta("stock_photo")
            return kb

    # Priority 4 — Animated gradient (file-backed)
    logger.info("   🎨 Animated gradient fallback")
    _record_bg_meta("gradient")
    return render_fallback_bg_mp4(os.path.join(tmp_dir, "fallback_bg.mp4"), duration)
