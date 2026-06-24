"""
Video package — Visual Effects & Composition.

Ken Burns, gradient overlays, subtitle clips, stock-footage scene assembly.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from typing import List

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


# ── Stock-footage scene assembly ──────────────────────────────────────────────

def compose_stock_scenes(
    clip_paths: List[str],
    total_duration: float,
) -> CompositeVideoClip:
    """Compose multiple stock video clips into a single portrait video.

    Processes clips **sequentially** via ffmpeg to stay within Lambda's
    3 GB RAM limit:
      1. Each clip → trim + resize to 1080×1920 → temp file (then close)
      2. All temp files → ffmpeg concat demuxer → single merged.mp4
      3. Open merged.mp4 as one VideoFileClip (1 reader, not 9)

    Landscape clips get a blurred-dark background so the full frame is
    visible instead of being aggressively cropped.
    """
    if not clip_paths:
        return make_fallback_background(total_duration)

    from imageio_ffmpeg import get_ffmpeg_exe
    ffmpeg_bin = get_ffmpeg_exe()

    segment_dur = max(total_duration / len(clip_paths), MIN_CLIP_DURATION)
    proc_dir = os.path.join(tempfile.gettempdir(), "_stock_proc")
    if os.path.exists(proc_dir):
        shutil.rmtree(proc_dir, ignore_errors=True)
    os.makedirs(proc_dir, exist_ok=True)

    trimmed: list[str] = []

    for i, path in enumerate(clip_paths):
        try:
            clip = VideoFileClip(path)
            dur = min(segment_dur, clip.duration)
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
        return make_fallback_background(total_duration)

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
        return make_fallback_background(total_duration)

    logger.info(f"   🎞️  Merged {len(trimmed)} scenes → merged.mp4")

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


def make_subtitle_clip(segment: SubtitleSegment) -> list:
    """Subtitle overlay with per-line fitted orange background.

    Each line of text gets its own rounded-rectangle background
    sized exactly to that line's width — no wasted orange area.

    When word-wrapping produces more than 2 lines (e.g. a long Dutch
    compound word fills line 1 entirely, pushing later words into line 3),
    the segment's duration is split proportionally by word count across
    2-line chunks so that no words are silently dropped.
    """
    from PIL import ImageDraw, ImageFont

    try:
        font = ImageFont.truetype(FONT_PATH, SUBTITLE_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default(size=SUBTITLE_FONT_SIZE)

    pad_x, pad_y = 28, 12
    max_text_width = VIDEO_WIDTH - 2 * SUBTITLE_SAFE_MARGIN - 2 * pad_x

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

    y_pos = int(VIDEO_HEIGHT * 0.80)
    dur = segment.end - segment.start
    total_words = len(words) or 1

    line_chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)]
    chunk_word_counts = [sum(len(l.split()) for l in chunk) for chunk in line_chunks]

    clips: list = []
    current_start = segment.start
    for chunk, wc in zip(line_chunks, chunk_word_counts):
        fraction = wc / total_words
        chunk_end = current_start + dur * fraction
        clips.extend(_render_subtitle_chunk(chunk, font, current_start, chunk_end, y_pos))
        current_start = chunk_end

    return clips


def _render_subtitle_chunk(
    lines: list,
    font,
    start: float,
    end: float,
    y_pos: int,
) -> list:
    """Render 1–2 wrapped text lines as one orange-background subtitle clip."""
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

    r, g, b, a = canvas.split()
    rgb_img = np.array(Image.merge("RGB", (r, g, b)))
    alpha_img = np.array(a).astype(np.float64) / 255.0

    dur = max(end - start, 0.05)
    clip = ImageClip(rgb_img).with_duration(dur)
    mask = ImageClip(alpha_img, is_mask=True).with_duration(dur)
    clip = clip.with_mask(mask)
    clip = clip.with_position((0, y_pos)).with_start(start)
    return [clip]


def make_hook_clip(hook_text: str, duration: float = 3.0) -> list:
    """Large centered hook overlay shown for the first N seconds.

    White bold text on a full-width semi-transparent dark scrim,
    positioned in the upper half of the frame to avoid colliding
    with the orange subtitle bar at 80%.
    Returns ``[scrim_clip, text_clip]``.
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

    # ── Scrim (full-width semi-transparent dark bar) ──
    scrim_arr = np.zeros((scrim_h, VIDEO_WIDTH, 4), dtype=np.uint8)
    scrim_arr[:, :, 3] = int(255 * 0.55)  # 55% opacity black

    r_s, g_s, b_s, a_s = (
        Image.fromarray(scrim_arr[:, :, 0]),
        Image.fromarray(scrim_arr[:, :, 1]),
        Image.fromarray(scrim_arr[:, :, 2]),
        Image.fromarray(scrim_arr[:, :, 3]),
    )
    scrim_rgb = np.array(Image.merge("RGB", (r_s, g_s, b_s)))
    scrim_alpha = np.array(a_s).astype(np.float64) / 255.0

    scrim_clip = ImageClip(scrim_rgb).with_duration(duration)
    scrim_mask = ImageClip(scrim_alpha, is_mask=True).with_duration(duration)
    scrim_clip = scrim_clip.with_mask(scrim_mask).with_position((0, scrim_top))

    # ── Text ──
    canvas = Image.new("RGBA", (VIDEO_WIDTH, scrim_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    white = (255, 255, 255, 255)

    y = pad_y
    for line, (lw, lh, top_off) in zip(lines, line_heights):
        text_x = (VIDEO_WIDTH - lw) // 2
        text_y = y + (row_h - lh) // 2 - top_off
        draw.text((text_x, text_y), line, font=font, fill=white)
        y += row_h + line_gap

    r_t, g_t, b_t, a_t = canvas.split()
    text_rgb = np.array(Image.merge("RGB", (r_t, g_t, b_t)))
    text_alpha = np.array(a_t).astype(np.float64) / 255.0

    text_clip = ImageClip(text_rgb).with_duration(duration)
    text_mask = ImageClip(text_alpha, is_mask=True).with_duration(duration)
    text_clip = text_clip.with_mask(text_mask).with_position((0, scrim_top))

    return [scrim_clip, text_clip]


def _styled_logo(logo_path, diameter: int = 190, opacity: float = 0.88) -> "Image.Image":
    """Turn a square logo into a circular brand medallion with a soft shadow.

    The source watermark has a solid cream background, which reads as a pasted
    square over footage. Masking it to a circle, adding a subtle drop shadow,
    and easing the opacity makes it sit on the video like an intentional
    watermark rather than a sticker.
    """
    from PIL import ImageDraw

    logo = Image.open(logo_path).convert("RGBA").resize(
        (diameter, diameter), Image.Resampling.LANCZOS,
    )
    # Circular mask → drop the square corners (scaled by opacity for a watermark feel)
    mask = Image.new("L", (diameter, diameter), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter - 1, diameter - 1), fill=int(255 * opacity))
    logo.putalpha(mask)

    pad = int(diameter * 0.22)
    size = diameter + 2 * pad
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # Soft drop shadow, nudged slightly down
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    drop = int(pad * 0.35)
    ImageDraw.Draw(shadow).ellipse(
        (pad, pad + drop, pad + diameter, pad + diameter + drop), fill=(0, 0, 0, 110),
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(pad * 0.45))

    out = Image.alpha_composite(out, shadow)
    out.alpha_composite(logo, (pad, pad))
    return out


def make_fact_overlay(
    fact_text: str,
    duration: float,
    header: str = "DID YOU KNOW?",
    logo_path=None,
    handle: str = "@dutch_news_english",
) -> list:
    """Full-duration overlay for the daily Dutch-fact story.

    Layout (centered vertical block over a soft dark scrim):
      • small orange header  ("DID YOU KNOW?")
      • large white fact body, word-wrapped, the hero
      • brand logo watermark near the bottom (falls back to the @handle text)

    Returns ``[scrim_clip, text_clip]`` covering the whole frame.
    """
    from PIL import ImageDraw, ImageFont

    header_size = 52
    body_size = 72
    handle_size = 44

    def _font(size: int):
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            return ImageFont.load_default(size=size)

    header_font = _font(header_size)
    body_font = _font(body_size)
    handle_font = _font(handle_size)

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

    # Brand sign-off near the bottom: logo watermark if available, else @handle
    logo_pasted = False
    if logo_path:
        try:
            coin = _styled_logo(logo_path, diameter=190)
            # Bottom-right corner. The coin image carries its own transparent
            # pad, so small frame margins still leave a comfortable safe zone
            # (clears Instagram's Story reply bar at the bottom-center).
            lx = VIDEO_WIDTH - coin.width - 24
            ly = VIDEO_HEIGHT - coin.height - 100
            canvas.alpha_composite(coin, (lx, ly))
            logo_pasted = True
        except Exception as e:
            logger.warning(f"⚠️  Could not load logo watermark: {e}")
    if not logo_pasted:
        _draw_centered(handle, handle_font, int(VIDEO_HEIGHT * 0.90), white)

    r, g, b, a = canvas.split()
    text_rgb = np.array(Image.merge("RGB", (r, g, b)))
    text_alpha = np.array(a).astype(np.float64) / 255.0
    text_clip = ImageClip(text_rgb).with_duration(duration)
    text_mask = ImageClip(text_alpha, is_mask=True).with_duration(duration)
    text_clip = text_clip.with_mask(text_mask).with_position((0, 0))

    return [scrim_clip, text_clip]


def make_fallback_background(duration: float) -> CompositeVideoClip:
    """Animated gradient background (dark blue→teal) when no visuals are
    available.  Uses Ken Burns motion so the screen is never static."""
    # Build an oversized gradient image (Ken Burns needs room)
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

    # Add subtle grain for texture
    grain = np.random.randint(0, 6, img.shape, dtype=np.uint8)
    img = np.clip(img.astype(np.int16) + grain, 0, 255).astype(np.uint8)

    prepared = img  # already oversized numpy array

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
