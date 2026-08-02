"""
Video package — Visual Effects & Composition.

Ken Burns, gradient overlays, subtitle clips, stock-footage scene assembly.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from collections import namedtuple
from typing import List, Optional

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter
from moviepy import (
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
)

from .config import (
    FONT_MONTSERRAT_PATH as FONT_PATH,
    FPS,
    HOOK_FONT_SIZE,
    KB_ZOOM_RANGE,
    MIN_CLIP_DURATION,
    SUBTITLE_BG_COLOR,
    SUBTITLE_FONT_SIZE,
    SUBTITLE_SAFE_MARGIN,
    SUBTITLE_TEXT_COLOR,
    VIDEO_HEIGHT,
    VIDEO_WIDTH,
)
from .tts import SubtitleSegment

logger = logging.getLogger(__name__)

# A static overlay image to burn into the final video via ffmpeg:
#   png_path — full-width (1080px) RGBA image; x is always 0
#   y        — vertical offset in the frame
#   start/end — time window in seconds the overlay is visible
OverlaySpec = namedtuple("OverlaySpec", ["png_path", "y", "start", "end"])


# ── Perceptual hashing (near-duplicate cover detection) ───────────────────────

def compute_dhash(img_path: str, hash_size: int = 8) -> int:
    """Perceptual difference-hash (dHash) of an image → 64-bit int.

    Resizes to (hash_size+1)×hash_size grayscale and compares horizontally
    adjacent pixels. Visually similar images yield small Hamming distances
    even when served at different URLs / resolutions (e.g. NOS's recurring
    "weekdienst" template card). Pure-Pillow — no extra dependency.
    """
    with Image.open(img_path) as img:
        small = img.convert("L").resize(
            (hash_size + 1, hash_size), Image.Resampling.LANCZOS,
        )
        pixels = list(small.getdata())

    bits = 0
    for row in range(hash_size):
        for col in range(hash_size):
            left = pixels[row * (hash_size + 1) + col]
            right = pixels[row * (hash_size + 1) + col + 1]
            bits = (bits << 1) | (1 if left > right else 0)
    return bits


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two integer hashes."""
    return bin(a ^ b).count("1")


# ── Stock-footage scene assembly ──────────────────────────────────────────────

def _stitch_clips_to_file(
    clip_paths: List[str],
    total_duration: float,
    lead_duration: Optional[float] = None,
) -> Optional[str]:
    """Stitch stock clips into a single portrait merged.mp4 (pure ffmpeg).

    Processes clips **sequentially** via ffmpeg to stay within Lambda's
    3 GB RAM limit:
      1. Each clip → trim + resize to 1080×1920 → temp file (then close)
      2. All temp files → ffmpeg concat demuxer → single merged.mp4

    Landscape clips get a blurred-dark background so the full frame is
    visible instead of being aggressively cropped. Returns the merged.mp4
    path, or None if there is nothing to stitch / ffmpeg fails (callers
    decide the fallback). The merged file may be shorter than
    *total_duration*; consumers loop/cut it themselves.

    *lead_duration* pins the first clip's length and splits the remainder
    evenly across the rest. Without it every clip gets the same slice, so a
    cover rendered at 4 s is silently trimmed to ``total/len(clips)`` — which
    made the cover-duration constant a no-op. Default None keeps that original
    even-split behaviour for callers that don't have a distinguished lead.
    """
    if not clip_paths:
        return None

    from imageio_ffmpeg import get_ffmpeg_exe
    ffmpeg_bin = get_ffmpeg_exe()

    if lead_duration and len(clip_paths) > 1:
        segment_dur = max(
            (total_duration - lead_duration) / (len(clip_paths) - 1), MIN_CLIP_DURATION,
        )
    else:
        lead_duration = None
        segment_dur = max(total_duration / len(clip_paths), MIN_CLIP_DURATION)
    proc_dir = os.path.join(tempfile.gettempdir(), "_stock_proc")
    if os.path.exists(proc_dir):
        shutil.rmtree(proc_dir, ignore_errors=True)
    os.makedirs(proc_dir, exist_ok=True)

    trimmed: list[str] = []

    for i, path in enumerate(clip_paths):
        try:
            clip = VideoFileClip(path)
            target = lead_duration if (lead_duration and i == 0) else segment_dur
            dur = min(target, clip.duration)
            is_portrait = (clip.h / clip.w) >= (VIDEO_HEIGHT / VIDEO_WIDTH) * 0.85
            clip.close()

            out = os.path.join(proc_dir, f"seg_{i}.mp4")

            if is_portrait:
                vf = (
                    f"scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT}"
                )
                cmd = [
                    ffmpeg_bin, "-y", "-i", path, "-t", str(dur),
                    "-vf", vf,
                    "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-r", str(FPS), out,
                ]
            else:
                # Landscape: blurred dark background + centered foreground
                fc = (
                    f"split[bg][fg];"
                    f"[bg]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
                    f"force_original_aspect_ratio=increase,"
                    f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},"
                    f"gblur=sigma=25,eq=brightness=-0.75[bgo];"
                    f"[fg]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:"
                    f"force_original_aspect_ratio=decrease[fgo];"
                    f"[bgo][fgo]overlay=(W-w)/2:(H-h)/2"
                )
                cmd = [
                    ffmpeg_bin, "-y", "-i", path, "-t", str(dur),
                    "-filter_complex", fc,
                    "-an", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-r", str(FPS), out,
                ]

            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode == 0 and os.path.exists(out):
                trimmed.append(out)
                logger.info(f"   ✂️  scene {i+1}/{len(clip_paths)} ready")
            else:
                stderr = result.stderr.decode(errors="replace")[-300:]
                logger.warning(f"⚠️  ffmpeg clip {i}: {stderr}")
        except Exception as e:
            logger.warning(f"⚠️  Could not process clip {path}: {e}")

    if not trimmed:
        shutil.rmtree(proc_dir, ignore_errors=True)
        return None

    # Concatenate all segments — re-encode to guarantee compatibility
    # (segments from different Pexels sources may have incompatible
    # codec profiles / timestamp bases that break -c copy concat)
    concat_file = os.path.join(proc_dir, "list.txt")
    with open(concat_file, "w") as f:
        for p in trimmed:
            f.write(f"file '{p}'\n")

    merged_path = os.path.join(proc_dir, "merged.mp4")
    concat_result = subprocess.run(
        [ffmpeg_bin, "-y", "-f", "concat", "-safe", "0",
         "-i", concat_file,
         "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-an", merged_path],
        capture_output=True, timeout=180,
    )
    if concat_result.returncode != 0:
        stderr = concat_result.stderr.decode(errors="replace")[-500:]
        logger.error(f"❌ ffmpeg concat failed: {stderr}")
        shutil.rmtree(proc_dir, ignore_errors=True)
        return None

    logger.info(f"   🎞️  Merged {len(trimmed)} scenes → merged.mp4")
    return merged_path


def compose_stock_scenes(
    clip_paths: List[str],
    total_duration: float,
) -> CompositeVideoClip:
    """Stock clips → a single moviepy background clip with gradient overlay.

    Wraps `_stitch_clips_to_file` (ffmpeg) then opens the merged file once
    (1 reader, not N), looping/cutting to *total_duration* and adding the
    readability gradient. Used by the daily-fact Story path; the news Reel
    path consumes `_stitch_clips_to_file` directly and applies the gradient
    in its single ffmpeg assembly pass.
    """
    merged_path = _stitch_clips_to_file(clip_paths, total_duration)
    if not merged_path:
        return make_fallback_background(total_duration)

    # Open single merged file (1 ffmpeg reader instead of 9)
    combined = VideoFileClip(merged_path)
    if combined.duration < total_duration:
        loops = int(total_duration / combined.duration) + 1
        combined = concatenate_videoclips(
            [combined] * loops, method="compose",
        )
    combined = combined.subclipped(0, total_duration)

    gradient = make_gradient_overlay(total_duration)
    return CompositeVideoClip(
        [combined, gradient], size=(VIDEO_WIDTH, VIDEO_HEIGHT),
    )


# ── Ken Burns (still image) ──────────────────────────────────────────────────

def prepare_image_for_portrait(img_path: str, tmp_dir: str) -> str:
    """Create portrait composite: full image centred + blurred dark background.

    Result is ~35 % larger than VIDEO dimensions to give Ken Burns room.
    Landscape images are shown in full (no aggressive crop) with a
    blurred, darkened version of the image filling the background.
    """
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        target_w = int(VIDEO_WIDTH * 1.35)
        target_h = int(VIDEO_HEIGHT * 1.35)

        # 1. Blurred background — fill the canvas
        bg_scale = max(target_w / img.width, target_h / img.height)
        bg = img.resize(
            (int(img.width * bg_scale), int(img.height * bg_scale)),
            Image.Resampling.LANCZOS,
        )
        left = (bg.width - target_w) // 2
        top = (bg.height - target_h) // 2
        bg = bg.crop((left, top, left + target_w, top + target_h))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
        bg = ImageEnhance.Brightness(bg).enhance(0.25)

        # 2. Foreground — fit original within canvas (no crop)
        fg_scale = min(target_w / img.width, target_h / img.height)
        fg = img.resize(
            (int(img.width * fg_scale), int(img.height * fg_scale)),
            Image.Resampling.LANCZOS,
        )
        x = (target_w - fg.width) // 2
        y = (target_h - fg.height) // 2
        bg.paste(fg, (x, y))

        out = os.path.join(tmp_dir, "portrait_bg.jpg")
        bg.save(out, "JPEG", quality=92)
        return out


def make_ken_burns_clip(
    img_path: str, duration: float,
) -> CompositeVideoClip:
    """Slow zoom + pan on a still image with gradient overlay."""
    prepared = np.array(Image.open(img_path))
    img_h, img_w = prepared.shape[:2]

    def _frame(t):
        progress = t / duration if duration > 0 else 0
        zoom = KB_ZOOM_RANGE[0] + (KB_ZOOM_RANGE[1] - KB_ZOOM_RANGE[0]) * progress
        crop_w = int(VIDEO_WIDTH / zoom)
        crop_h = int(VIDEO_HEIGHT / zoom)
        cx = max(0, min(int((img_w - crop_w) * (0.3 + 0.4 * progress)), img_w - crop_w))
        cy = max(0, min(int((img_h - crop_h) * (0.4 + 0.2 * progress)), img_h - crop_h))
        crop = prepared[cy: cy + crop_h, cx: cx + crop_w]
        pil = Image.fromarray(crop).resize(
            (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS,
        )
        return np.array(pil)

    kb = VideoClip(_frame, duration=duration).with_fps(FPS)
    gradient = make_gradient_overlay(duration)
    return CompositeVideoClip(
        [kb, gradient], size=(VIDEO_WIDTH, VIDEO_HEIGHT),
    )


def render_ken_burns_mp4(img_path: str, duration: float, out_path: str) -> Optional[str]:
    """Render a Ken Burns (slow zoom) clip of a still image to an MP4 via ffmpeg.

    Used for the hybrid Reel cover: the moviepy `make_ken_burns_clip` generates
    frames in pure Python (PIL resize per frame), which is far too slow inside
    the final composite render on Lambda. Pre-rendering to a file with ffmpeg's
    native `zoompan` (~0.4 s) lets the cover join the stock clips in
    `compose_stock_scenes` as a fast file-backed scene. Returns the path, or
    None if ffmpeg fails (caller falls back to a stock-only cover).
    """
    from imageio_ffmpeg import get_ffmpeg_exe
    ffmpeg_bin = get_ffmpeg_exe()
    frames = max(1, int(duration * FPS))
    # Scale up 2× before zoompan to avoid quality loss / jitter on the zoom.
    vf = (
        f"scale={VIDEO_WIDTH * 2}:{VIDEO_HEIGHT * 2}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH * 2}:{VIDEO_HEIGHT * 2},"
        f"zoompan=z='min(zoom+0.0010,1.15)':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={VIDEO_WIDTH}x{VIDEO_HEIGHT}:fps={FPS}"
    )
    cmd = [
        ffmpeg_bin, "-y", "-loop", "1", "-i", img_path, "-t", str(duration),
        "-vf", vf, "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-an", "-r", str(FPS), out_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=60)
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
        logger.warning(
            f"⚠️  ffmpeg cover render failed: {result.stderr.decode(errors='replace')[-300:]}"
        )
    except Exception as e:
        logger.warning(f"⚠️  Could not render Ken Burns cover: {e}")
    return None


# ── Overlays ──────────────────────────────────────────────────────────────────

def make_gradient_overlay(duration: float) -> ColorClip:
    """Semi-transparent gradient: darker at top/bottom for readability.

    Uses an explicit moviepy *mask* so the overlay is truly transparent.
    (The previous implementation converted RGBA → RGB, which produced an
    opaque black frame that covered the entire video behind it.)
    """
    mask_data = np.zeros((VIDEO_HEIGHT, VIDEO_WIDTH), dtype=np.float64)
    for y in range(VIDEO_HEIGHT):
        if y < VIDEO_HEIGHT * 0.15:
            mask_data[y, :] = 0.70 * (1 - y / (VIDEO_HEIGHT * 0.15))
        elif y > VIDEO_HEIGHT * 0.65:
            progress = (y - VIDEO_HEIGHT * 0.65) / (VIDEO_HEIGHT * 0.35)
            mask_data[y, :] = 0.78 * progress
        else:
            mask_data[y, :] = 0.12

    clip = (
        ColorClip(size=(VIDEO_WIDTH, VIDEO_HEIGHT), color=(0, 0, 0))
        .with_duration(duration)
        .with_position((0, 0))
    )
    mask = ImageClip(mask_data, is_mask=True).with_duration(duration)
    return clip.with_mask(mask)


def render_gradient_png(out_path: str) -> str:
    """Write the readability gradient as a full-frame black RGBA PNG.

    Same darkening profile as `make_gradient_overlay` (time-invariant), so it can
    be burned once via ffmpeg overlay instead of composited every frame: black
    pixels whose alpha follows the top/bottom darkening curve.
    """
    alpha = np.zeros((VIDEO_HEIGHT, VIDEO_WIDTH), dtype=np.uint8)
    for y in range(VIDEO_HEIGHT):
        if y < VIDEO_HEIGHT * 0.15:
            a = 0.70 * (1 - y / (VIDEO_HEIGHT * 0.15))
        elif y > VIDEO_HEIGHT * 0.65:
            a = 0.78 * ((y - VIDEO_HEIGHT * 0.65) / (VIDEO_HEIGHT * 0.35))
        else:
            a = 0.12
        alpha[y, :] = int(round(a * 255))

    rgba = np.zeros((VIDEO_HEIGHT, VIDEO_WIDTH, 4), dtype=np.uint8)
    rgba[:, :, 3] = alpha  # RGB stays black
    Image.fromarray(rgba).save(out_path)
    return out_path


def build_subtitle_overlays(
    segments: List[SubtitleSegment],
    hook_duration: float,
    tmp_dir: str,
) -> List[OverlaySpec]:
    """Render word-timed subtitles as static RGBA PNGs + time windows.

    Each line gets its own rounded-rectangle orange background sized to the
    line. When word-wrapping yields >2 lines, the segment's duration is split
    proportionally by word count across 2-line chunks so no words are dropped.
    Segments starting within the hook window are skipped (the hook covers them).

    Returns a list of OverlaySpec to be burned by the single ffmpeg pass.
    """
    from PIL import ImageFont

    try:
        font = ImageFont.truetype(FONT_PATH, SUBTITLE_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default(size=SUBTITLE_FONT_SIZE)

    pad_x = 28
    max_text_width = VIDEO_WIDTH - 2 * SUBTITLE_SAFE_MARGIN - 2 * pad_x
    y_pos = int(VIDEO_HEIGHT * 0.80)

    specs: List[OverlaySpec] = []
    idx = 0
    for segment in segments:
        if segment.start < hook_duration:
            continue

        words = segment.text.split()
        lines: list[str] = []
        current_line: list[str] = []
        for word in words:
            test_line = " ".join(current_line + [word])
            left, _, right, _ = font.getbbox(test_line)
            if (right - left) > max_text_width and current_line:
                lines.append(" ".join(current_line))
                current_line = [word]
            else:
                current_line.append(word)
        if current_line:
            lines.append(" ".join(current_line))

        dur = segment.end - segment.start
        total_words = len(words) or 1
        line_chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)]
        chunk_word_counts = [sum(len(l.split()) for l in chunk) for chunk in line_chunks]

        current_start = segment.start
        for chunk, wc in zip(line_chunks, chunk_word_counts):
            chunk_end = current_start + dur * (wc / total_words)
            canvas = _render_subtitle_chunk_png(chunk, font)
            png = os.path.join(tmp_dir, f"sub_{idx}.png")
            canvas.save(png)
            specs.append(OverlaySpec(png, y_pos, current_start, max(chunk_end, current_start + 0.05)))
            current_start = chunk_end
            idx += 1

    return specs


def _render_subtitle_chunk_png(lines: list, font):
    """Render 1–2 wrapped text lines as one orange-background RGBA image."""
    from PIL import ImageDraw

    pad_x, pad_y = 28, 12
    line_gap = 8
    corner_radius = 12

    line_metrics: list[tuple[int, int, int]] = []
    for line in lines:
        left, top, right, bottom = font.getbbox(line)
        line_metrics.append((int(right - left), int(bottom - top), int(top)))

    max_line_h = max(h for _, h, _ in line_metrics) if line_metrics else SUBTITLE_FONT_SIZE
    row_h = max_line_h + 2 * pad_y
    total_h = len(lines) * row_h + max(0, len(lines) - 1) * line_gap

    canvas = Image.new("RGBA", (VIDEO_WIDTH, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    bg_rgba = _hex_to_rgb(SUBTITLE_BG_COLOR) + (255,)
    text_rgba = _hex_to_rgb(SUBTITLE_TEXT_COLOR) + (255,)

    y = 0
    for line, (lw, lh, top_off) in zip(lines, line_metrics):
        box_w = lw + 2 * pad_x
        box_x = (VIDEO_WIDTH - box_w) // 2
        draw.rounded_rectangle(
            [(box_x, y), (box_x + box_w, y + row_h)],
            radius=corner_radius,
            fill=bg_rgba,
        )
        text_x = box_x + pad_x
        text_y = y + (row_h - lh) // 2 - top_off
        draw.text((text_x, text_y), line, font=font, fill=text_rgba)
        y += row_h + line_gap

    return canvas


def build_hook_overlays(hook_text: str, tmp_dir: str, duration: float = 3.0) -> List[OverlaySpec]:
    """Large centered hook overlay (white text on a dark scrim) for the first N s.

    Positioned in the upper half so it never collides with the orange subtitle
    bar at 80%. Scrim + text are flattened into one RGBA PNG (white text over a
    55%-opacity black bar) and returned as a single OverlaySpec.
    """
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(FONT_PATH, HOOK_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default(size=HOOK_FONT_SIZE)

    pad_x, pad_y = 40, 20
    max_text_width = VIDEO_WIDTH - 2 * pad_x

    # Word-wrap hook text
    words = hook_text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        left, _, right, _ = font.getbbox(test)
        if (right - left) > max_text_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))

    # Measure total text block height
    line_gap = 10
    line_heights = []
    for line in lines:
        left, top, right, bottom = font.getbbox(line)
        line_heights.append((int(right - left), int(bottom - top), int(top)))

    max_lh = max(h for _, h, _ in line_heights) if line_heights else HOOK_FONT_SIZE
    row_h = max_lh + 2 * pad_y
    total_text_h = len(lines) * row_h + max(0, len(lines) - 1) * line_gap

    # Position scrim in upper-center zone (30–50% of frame height)
    scrim_top = int(VIDEO_HEIGHT * 0.30)
    scrim_h = total_text_h + 2 * pad_y

    # One canvas: 55%-opacity black scrim, then white text on top.
    canvas = Image.new("RGBA", (VIDEO_WIDTH, scrim_h), (0, 0, 0, int(255 * 0.55)))
    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)

    y = pad_y
    for line, (lw, lh, top_off) in zip(lines, line_heights):
        text_x = (VIDEO_WIDTH - lw) // 2
        text_y = y + (row_h - lh) // 2 - top_off
        draw.text((text_x, text_y), line, font=font, fill=white)
        y += row_h + line_gap

    png = os.path.join(tmp_dir, "hook.png")
    canvas.save(png)
    return [OverlaySpec(png, scrim_top, 0.0, duration)]


def build_source_label_overlay(
    tmp_dir: str,
    start: float,
    duration: float = 3.5,
    text: str = "STOCK FOOTAGE · ILLUSTRATION",
) -> List[OverlaySpec]:
    """Small pill marking the visuals as library footage, not the actual scene.

    Viewers were reading generic stock clips as "the AI generated a fake image
    of this place". Labelling the footage is the standard journalistic answer
    (and cheap): it can't make a clip more accurate, but it removes the false
    claim that the clip depicts the location.

    Sits at ~12% of frame height — above the hook scrim (30–50%) and well clear
    of the subtitle bar (80%). Keep *start* under ~10 s: ffcompose loops a short
    background with ``-stream_loop -1``, so late overlays can drift out of sync.
    """
    from PIL import ImageDraw, ImageFont

    font_size = max(24, HOOK_FONT_SIZE // 3)
    try:
        font = ImageFont.truetype(FONT_PATH, font_size)
    except Exception:
        font = ImageFont.load_default(size=font_size)

    pad_x, pad_y = 22, 12
    left, top, right, bottom = font.getbbox(text)
    tw, th = int(right - left), int(bottom - top)
    pill_w, pill_h = tw + 2 * pad_x, th + 2 * pad_y

    # Full-width canvas: the ffmpeg overlay chain always places PNGs at x=0.
    canvas = Image.new("RGBA", (VIDEO_WIDTH, pill_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    pill_x = (VIDEO_WIDTH - pill_w) // 2
    draw.rounded_rectangle(
        [pill_x, 0, pill_x + pill_w, pill_h], radius=pill_h // 2,
        fill=(0, 0, 0, int(255 * 0.55)),
    )
    draw.text((pill_x + pad_x, pad_y - top), text, font=font, fill=(255, 255, 255, 230))

    png = os.path.join(tmp_dir, "source_label.png")
    canvas.save(png)
    return [OverlaySpec(png, int(VIDEO_HEIGHT * 0.12), start, start + duration)]


def make_fact_overlay(
    fact_text: str,
    duration: float,
    header: str = "DID YOU KNOW?",
) -> list:
    """Full-duration overlay for the daily Dutch-fact story.

    Layout (centered vertical block over a soft dark scrim):
      • small orange header  ("DID YOU KNOW?")
      • large white fact body, word-wrapped, the hero

    No branding is baked in — Instagram already shows the account avatar and
    handle in the Story's top-left corner.

    Returns ``[scrim_clip, text_clip]`` covering the whole frame.
    """
    from PIL import ImageDraw, ImageFont

    header_size = 52
    body_size = 72

    def _font(size: int):
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            return ImageFont.load_default(size=size)

    header_font = _font(header_size)
    body_font = _font(body_size)

    pad_x = 80
    max_text_width = VIDEO_WIDTH - 2 * pad_x

    # Word-wrap the fact body
    words = fact_text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        test = " ".join(current + [word])
        left, _, right, _ = body_font.getbbox(test)
        if (right - left) > max_text_width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))

    line_gap = 18
    body_line_h = body_size + line_gap
    body_block_h = len(lines) * body_line_h
    header_gap = 48
    header_h = header_size + header_gap

    block_h = header_h + body_block_h
    block_top = (VIDEO_HEIGHT - block_h) // 2

    # ── Scrim: soft full-width dark band behind the text block ──
    scrim_pad = 90
    scrim_top = max(0, block_top - scrim_pad)
    scrim_bottom = min(VIDEO_HEIGHT, block_top + block_h + scrim_pad)
    scrim_h = scrim_bottom - scrim_top
    scrim_arr = np.zeros((scrim_h, VIDEO_WIDTH, 4), dtype=np.uint8)
    scrim_arr[:, :, 3] = int(255 * 0.45)
    scrim_rgb = np.array(
        Image.merge("RGB", [Image.fromarray(scrim_arr[:, :, i]) for i in range(3)])
    )
    scrim_alpha = np.array(Image.fromarray(scrim_arr[:, :, 3])).astype(np.float64) / 255.0
    scrim_clip = ImageClip(scrim_rgb).with_duration(duration)
    scrim_mask = ImageClip(scrim_alpha, is_mask=True).with_duration(duration)
    scrim_clip = scrim_clip.with_mask(scrim_mask).with_position((0, scrim_top))

    # ── Text canvas (full frame) ──
    canvas = Image.new("RGBA", (VIDEO_WIDTH, VIDEO_HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    orange = _hex_to_rgb(SUBTITLE_BG_COLOR) + (255,)
    white = (255, 255, 255, 255)

    def _draw_centered(text, font, y, fill):
        left, top, right, bottom = font.getbbox(text)
        w = right - left
        x = (VIDEO_WIDTH - w) // 2
        draw.text((x, y - top), text, font=font, fill=fill)

    y = block_top
    _draw_centered(header, header_font, y, orange)
    y += header_h
    for line in lines:
        _draw_centered(line, body_font, y, white)
        y += body_line_h

    r, g, b, a = canvas.split()
    text_rgb = np.array(Image.merge("RGB", (r, g, b)))
    text_alpha = np.array(a).astype(np.float64) / 255.0
    text_clip = ImageClip(text_rgb).with_duration(duration)
    text_mask = ImageClip(text_alpha, is_mask=True).with_duration(duration)
    text_clip = text_clip.with_mask(text_mask).with_position((0, 0))

    return [scrim_clip, text_clip]


def _gradient_base_image() -> np.ndarray:
    """Oversized dark blue→teal gradient with grain (Ken Burns needs room)."""
    h = int(VIDEO_HEIGHT * 1.35)
    w = int(VIDEO_WIDTH * 1.35)
    img = np.zeros((h, w, 3), dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            fy = y / h
            fx = x / w
            r = int(12 + 28 * fy + 8 * fx)
            g = int(18 + 22 * fy + 14 * fx)
            b = int(55 + 35 * (1 - fy) + 12 * fx)
            img[y, x] = [r, g, b]

    grain = np.random.randint(0, 6, img.shape, dtype=np.uint8)
    return np.clip(img.astype(np.int16) + grain, 0, 255).astype(np.uint8)


def render_fallback_bg_mp4(out_path: str, duration: float) -> Optional[str]:
    """File-backed animated-gradient background for the ffmpeg news path.

    Rare fallback (no article photo, no Pexels video, no Pexels photo). Writes
    the gradient base to a PNG then reuses ffmpeg `render_ken_burns_mp4` so the
    final assembly always has a real background file to overlay onto.
    """
    base_dir = os.path.dirname(out_path) or tempfile.gettempdir()
    base_png = os.path.join(base_dir, "fallback_base.png")
    Image.fromarray(_gradient_base_image()).save(base_png)
    return render_ken_burns_mp4(base_png, duration, out_path)


def make_fallback_background(duration: float) -> CompositeVideoClip:
    """Animated gradient background (dark blue→teal) when no visuals are
    available.  Uses Ken Burns motion so the screen is never static."""
    prepared = _gradient_base_image()  # oversized numpy array

    def _frame(t):
        progress = t / duration if duration > 0 else 0
        zoom = KB_ZOOM_RANGE[0] + (KB_ZOOM_RANGE[1] - KB_ZOOM_RANGE[0]) * progress
        crop_w = int(VIDEO_WIDTH / zoom)
        crop_h = int(VIDEO_HEIGHT / zoom)
        img_h, img_w = prepared.shape[:2]
        cx = max(0, min(int((img_w - crop_w) * (0.3 + 0.4 * progress)), img_w - crop_w))
        cy = max(0, min(int((img_h - crop_h) * (0.4 + 0.2 * progress)), img_h - crop_h))
        crop = prepared[cy: cy + crop_h, cx: cx + crop_w]
        pil = Image.fromarray(crop).resize(
            (VIDEO_WIDTH, VIDEO_HEIGHT), Image.Resampling.LANCZOS,
        )
        return np.array(pil)

    bg = VideoClip(_frame, duration=duration).with_fps(FPS)
    gradient = make_gradient_overlay(duration)
    return CompositeVideoClip(
        [bg, gradient], size=(VIDEO_WIDTH, VIDEO_HEIGHT),
    )


# ── Internal ──────────────────────────────────────────────────────────────────


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert ``'#FF5B14'`` → ``(255, 91, 20)``."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
