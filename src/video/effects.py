"""
Video package — Visual Effects & Composition.

Ken Burns, gradient overlays, subtitle clips, stock-footage scene assembly.
"""

import logging
import os
from typing import List

import numpy as np
from PIL import Image, ImageFilter
from moviepy import (
    ColorClip,
    CompositeVideoClip,
    ImageClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
)

from .config import (
    FONT_PATH,
    FPS,
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

    Each clip is resized to fill 1080×1920 (centre-cropped) and trimmed
    to an equal share of *total_duration*.
    """
    if not clip_paths:
        return make_fallback_background(total_duration)

    segment_dur = max(total_duration / len(clip_paths), MIN_CLIP_DURATION)
    trimmed: list = []

    for path in clip_paths:
        try:
            clip = VideoFileClip(path)
            dur = min(segment_dur, clip.duration)
            clip = clip.subclipped(0, dur)
            clip = _fit_to_portrait(clip)
            trimmed.append(clip)
        except Exception as e:
            logger.warning(f"⚠️  Could not process clip {path}: {e}")
            continue

    if not trimmed:
        return make_fallback_background(total_duration)

    combined = concatenate_videoclips(trimmed, method="compose")

    # Loop if shorter than needed
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
    """Resize / crop image with ~35 % overshoot for Ken Burns room."""
    with Image.open(img_path) as img:
        img = img.convert("RGB")
        target_w = int(VIDEO_WIDTH * 1.35)
        target_h = int(VIDEO_HEIGHT * 1.35)
        img_ratio = img.width / img.height
        target_ratio = target_w / target_h

        if img_ratio > target_ratio:
            new_h = target_h
            new_w = int(img.width * (target_h / img.height))
        else:
            new_w = target_w
            new_h = int(img.height * (target_w / img.width))

        img = img.resize((new_w, new_h), Image.LANCZOS)
        left = (new_w - target_w) // 2
        top = (new_h - target_h) // 2
        img = img.crop((left, top, left + target_w, top + target_h))
        img = img.filter(ImageFilter.GaussianBlur(radius=2))

        out = os.path.join(tmp_dir, "portrait_bg.jpg")
        img.save(out, "JPEG", quality=92)
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
            (VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS,
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
    Returns a single-element list ``[masked_clip]``.
    """
    from PIL import ImageDraw, ImageFont

    # Load font first — needed for pixel-width measurement
    try:
        font = ImageFont.truetype(FONT_PATH, SUBTITLE_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default(size=SUBTITLE_FONT_SIZE)

    # Wrap lines by measured pixel width (not fixed word count)
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
    lines = lines[:2]  # max 2 visible lines

    pad_x, pad_y = 28, 12
    line_gap = 8
    corner_radius = 12

    # Measure each line
    line_metrics: list[tuple[int, int, int]] = []  # (width, height, top_offset)
    for line in lines:
        left, top, right, bottom = font.getbbox(line)
        line_metrics.append((right - left, bottom - top, top))

    max_line_h = max(h for _, h, _ in line_metrics) if line_metrics else SUBTITLE_FONT_SIZE
    row_h = max_line_h + 2 * pad_y
    total_h = len(lines) * row_h + max(0, len(lines) - 1) * line_gap

    # Create transparent RGBA canvas (full video width for centring)
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

    # Split RGBA → RGB + alpha mask for moviepy
    r, g, b, a = canvas.split()
    rgb_img = np.array(Image.merge("RGB", (r, g, b)))
    alpha_img = np.array(a).astype(np.float64) / 255.0

    dur = segment.end - segment.start
    y_pos = int(VIDEO_HEIGHT * 0.72)

    clip = ImageClip(rgb_img).with_duration(dur)
    mask = ImageClip(alpha_img, is_mask=True).with_duration(dur)
    clip = clip.with_mask(mask)
    clip = clip.with_position((0, y_pos)).with_start(segment.start)

    return [clip]


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
            (VIDEO_WIDTH, VIDEO_HEIGHT), Image.LANCZOS,
        )
        return np.array(pil)

    bg = VideoClip(_frame, duration=duration).with_fps(FPS)
    gradient = make_gradient_overlay(duration)
    return CompositeVideoClip(
        [bg, gradient], size=(VIDEO_WIDTH, VIDEO_HEIGHT),
    )


# ── Internal ──────────────────────────────────────────────────────────────────

def _fit_to_portrait(clip) -> VideoClip:
    """Resize and centre-crop any clip to fill 1080×1920."""
    scale = max(VIDEO_WIDTH / clip.w, VIDEO_HEIGHT / clip.h)
    new_w = int(clip.w * scale)
    new_h = int(clip.h * scale)
    clip = clip.resized((new_w, new_h))
    x1 = (new_w - VIDEO_WIDTH) // 2
    y1 = (new_h - VIDEO_HEIGHT) // 2
    return clip.cropped(
        x1=x1, y1=y1, x2=x1 + VIDEO_WIDTH, y2=y1 + VIDEO_HEIGHT,
    )


def _hex_to_rgb(hex_color: str) -> tuple:
    """Convert ``'#FF5B14'`` → ``(255, 91, 20)``."""
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
