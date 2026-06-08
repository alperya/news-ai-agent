"""
Event Card Generator — Instagram carousel slides for weekly NL events.

Slide layout:
  Slide 1 (cover)  — full NL photo, centered text block with subtle pills, branding
  Slide 2+ (list)  — full NL photo visible, per-event transparent text pills, @handle footer

Design principle: photo is the hero. Text sits on minimal transparent pills.
"""

from __future__ import annotations

import logging
import random
from io import BytesIO
from typing import List, Optional

logger = logging.getLogger(__name__)

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .config import (
    FONT_PATH,           # Poppins-Bold
    FONT_SEMIBOLD_PATH,  # Poppins-SemiBold
    FONT_REGULAR_PATH,   # Poppins-Regular
    PEXELS_API_KEY,
    EVENTS_MUSIC_FILE,
)

# ── Palette ────────────────────────────────────────────────────────────────────
BRAND_ORANGE  = (255, 91,  20)
WHITE         = (255, 255, 255)
CREAM         = (255, 235, 210)
TEXT_TITLE    = (255, 255, 255)
TEXT_META     = (230, 240, 255)
TEXT_VENUE    = (200, 230, 255)
HANDLE_COLOR  = (255, 255, 255)

# Semi-transparent pills (RGBA) — drawn on RGBA canvas then composited
PILL_TEXT  = (0, 0, 0, 120)   # behind each text line on cover
PILL_EVENT = (0, 0, 0, 150)   # behind each event row on list slides
PILL_DATE  = (0, 0, 0, 140)   # date range badge top-right
FOOTER_BAR = (0, 0, 0, 130)   # @handle footer bar

# ── Geometry ───────────────────────────────────────────────────────────────────
CARD_SIZE        = 1080
PAD_H            = 52
FOOTER_H         = 52
EVENTS_PER_SLIDE = 4

_BG_QUERIES = [
    "dutch windmill landscape blue sky",
    "amsterdam canal bicycles sunny",
    "rotterdam modern architecture skyline",
    "dutch countryside green meadows cows",
    "netherlands coastal dunes beach",
    "amsterdam waterfront people",
    "delft historic old town netherlands",
]

ACCOUNT_HANDLE = "@dutch_news_english"


# ── Public API ──────────────────────────────────────────────────────────────────

def create_cover_slide(
    event_count: int,
    date_range: str,
    output_path: str,
) -> str:
    """Slide 1 — full NL photo, centered text block with transparent pills, branding."""
    photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), "square")
    if photo:
        bg = _crop_square(photo, CARD_SIZE)
    else:
        bg = _gradient_fallback(CARD_SIZE, CARD_SIZE)

    img = bg.convert("RGBA")

    # Very subtle vignette — only edges darkened, max alpha 45
    vignette = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    vd = ImageDraw.Draw(vignette)
    for i in range(40):
        alpha = int(45 * ((40 - i) / 40) ** 2)
        off = i * 6
        vd.rectangle([(off, off), (CARD_SIZE - off, CARD_SIZE - off)],
                     outline=(0, 0, 0, alpha), width=6)
    img = Image.alpha_composite(img, vignette)

    # Build text lines
    f_label   = _font(FONT_SEMIBOLD_PATH, 38)
    f_country = _font(FONT_PATH, 76)
    f_count   = _font(FONT_PATH, 96)
    f_date    = _font(FONT_SEMIBOLD_PATH, 34)
    f_handle  = _font(FONT_REGULAR_PATH, 27)

    label_text   = "THIS WEEK IN"
    country_text = "THE NETHERLANDS"
    count_text   = f"{event_count} EVENTS"

    lines = [
        (label_text,   f_label,   CREAM),
        (country_text, f_country, WHITE),
        (count_text,   f_count,   BRAND_ORANGE),
        (date_range,   f_date,    CREAM),
    ]

    gap = 18
    pill_pad_x = 28
    pill_pad_y = 10

    # Measure total block height (text heights + gaps)
    line_heights = [_lh(f, t) for (t, f, _) in lines]
    total_h = sum(line_heights) + gap * (len(lines) - 1)

    # True vertical center
    start_y = (CARD_SIZE - total_h) // 2

    # Draw pills + text on RGBA overlay
    overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    y = start_y
    for text, font, color in lines:
        tw = _tw(font, text)
        th = _lh(font, text)
        cx = (CARD_SIZE - tw) // 2
        od.rounded_rectangle(
            [(cx - pill_pad_x, y - pill_pad_y),
             (cx + tw + pill_pad_x, y + th + pill_pad_y)],
            radius=12,
            fill=PILL_TEXT,
        )
        od.text((cx, y), text, font=font, fill=color)
        y += th + gap

    # Footer bar + handle
    footer_top = CARD_SIZE - FOOTER_H
    od.rectangle([(0, footer_top), (CARD_SIZE, CARD_SIZE)], fill=FOOTER_BAR)
    fw = _tw(f_handle, ACCOUNT_HANDLE)
    fh = _lh(f_handle, ACCOUNT_HANDLE)
    od.text(((CARD_SIZE - fw) // 2, footer_top + (FOOTER_H - fh) // 2),
            ACCOUNT_HANDLE, font=f_handle, fill=HANDLE_COLOR)

    # Orange top accent strip (thin)
    od.rectangle([(0, 0), (CARD_SIZE, 8)], fill=BRAND_ORANGE)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(output_path, "JPEG", quality=94, optimize=True)
    return output_path


def create_event_list_slide(
    events: List[dict],
    date_range: str,
    output_path: str,
    slide_number: int = 2,
) -> str:
    """Slide 2+ — full NL photo visible, per-event transparent pills, handle footer."""
    events = events[:EVENTS_PER_SLIDE]

    photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), "square")
    if photo:
        bg = _crop_square(photo, CARD_SIZE)
    else:
        bg = _gradient_fallback(CARD_SIZE, CARD_SIZE)

    img = bg.convert("RGBA")

    # Subtle uniform darkening so text is readable on any bright photo
    dim = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 55))
    img = Image.alpha_composite(img, dim)

    overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    f_date_badge = _font(FONT_SEMIBOLD_PATH, 26)
    f_title      = _font(FONT_PATH, 34)
    f_meta       = _font(FONT_SEMIBOLD_PATH, 24)
    f_handle     = _font(FONT_REGULAR_PATH, 24)

    # Date range badge — top right corner
    badge_text = date_range
    bw = _tw(f_date_badge, badge_text)
    bh = _lh(f_date_badge, badge_text)
    badge_pad = 12
    badge_right = CARD_SIZE - PAD_H
    badge_top = 28
    od.rounded_rectangle(
        [(badge_right - bw - badge_pad * 2, badge_top),
         (badge_right, badge_top + bh + badge_pad)],
        radius=10,
        fill=PILL_DATE,
    )
    od.text((badge_right - bw - badge_pad, badge_top + badge_pad // 2),
            badge_text, font=f_date_badge, fill=CREAM)

    # Divide usable vertical space into equal slots
    content_top = 90
    content_bot = CARD_SIZE - FOOTER_H - 10
    usable_h = content_bot - content_top
    slot_h = usable_h // max(len(events), 1)

    pill_margin_x = PAD_H - 12
    pill_margin_y = 10

    for idx, ev in enumerate(events):
        title    = ev.get("title", "")
        venue    = ev.get("venue") or ""
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")
        price    = ev.get("price") or ""

        slot_top = content_top + idx * slot_h
        slot_bot = slot_top + slot_h

        max_tw = CARD_SIZE - pill_margin_x * 2 - 36
        title_lines = _wrap(title, f_title, max_tw)
        t_block = sum(_lh(f_title, l) + 3 for l in title_lines)
        meta_h = _lh(f_meta, "A") + 4
        block_h = t_block + meta_h + 10
        text_y = slot_top + (slot_h - block_h) // 2

        # Pill behind entire event block
        od.rounded_rectangle(
            [(pill_margin_x, slot_top + pill_margin_y),
             (CARD_SIZE - pill_margin_x, slot_bot - pill_margin_y)],
            radius=14,
            fill=PILL_EVENT,
        )

        # Orange accent dot
        dot_x = pill_margin_x + 20
        dot_y = text_y + _lh(f_title, "A") // 2 - 5
        od.ellipse([(dot_x, dot_y), (dot_x + 10, dot_y + 10)], fill=BRAND_ORANGE)

        # Title lines
        tx = dot_x + 18
        for line in title_lines:
            od.text((tx, text_y), line, font=f_title, fill=TEXT_TITLE)
            text_y += _lh(f_title, line) + 3

        text_y += 6
        cur_x = tx

        if venue:
            od.ellipse([(cur_x, text_y + _lh(f_meta, "A") // 2 - 4),
                        (cur_x + 7, text_y + _lh(f_meta, "A") // 2 + 3)],
                       fill=TEXT_VENUE)
            cur_x += 14
            od.text((cur_x, text_y), venue, font=f_meta, fill=TEXT_VENUE)
            cur_x += _tw(f_meta, venue)
            if location or date_lbl:
                od.text((cur_x, text_y), "  ·  ", font=f_meta, fill=(160, 185, 215, 200))
                cur_x += _tw(f_meta, "  ·  ")

        if location or date_lbl:
            ld = f"{location}  ·  {date_lbl}" if location and date_lbl else location or date_lbl
            od.text((cur_x, text_y), ld, font=f_meta, fill=TEXT_META)

    # Footer bar + handle
    od.rectangle([(0, CARD_SIZE - FOOTER_H), (CARD_SIZE, CARD_SIZE)], fill=FOOTER_BAR)
    fw = _tw(f_handle, ACCOUNT_HANDLE)
    fh = _lh(f_handle, ACCOUNT_HANDLE)
    od.text(((CARD_SIZE - fw) // 2, CARD_SIZE - FOOTER_H + (FOOTER_H - fh) // 2),
            ACCOUNT_HANDLE, font=f_handle, fill=HANDLE_COLOR)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


def generate_carousel_slides(
    events: List[dict],
    date_range: str,
    tmp_prefix: str,
) -> List[str]:
    """Generate all carousel slides: 1 cover + N list slides (4 events each).

    Returns list of local file paths ordered cover → list slides.
    """
    paths: List[str] = []

    cover_path = f"{tmp_prefix}_cover.jpg"
    create_cover_slide(
        event_count=len(events),
        date_range=date_range,
        output_path=cover_path,
    )
    paths.append(cover_path)

    chunks = [events[i:i + EVENTS_PER_SLIDE] for i in range(0, len(events), EVENTS_PER_SLIDE)]
    for slide_idx, chunk in enumerate(chunks, start=2):
        list_path = f"{tmp_prefix}_list{slide_idx}.jpg"
        create_event_list_slide(
            events=chunk,
            date_range=date_range,
            output_path=list_path,
            slide_number=slide_idx,
        )
        paths.append(list_path)

    return paths


def generate_reels_video(
    slide_paths: List[str],
    output_path: str,
    music_path: Optional[str] = None,
    target_duration: float = 60.0,
    slide_duration: float = 5.0,
) -> str:
    """Create a 1080×1920 Reels video (~60 s) from carousel slides with music.

    Slides loop to fill target_duration (5 s each — sweet spot for Instagram
    Reels: long enough to read, short enough to hold attention).
    Built entirely with ffmpeg concat demuxer for maximum player compatibility.
    """
    import math
    import os
    import subprocess
    from pathlib import Path
    import imageio_ffmpeg

    # Use moviepy's bundled ffmpeg binary — works in Lambda without system ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

    REEL_W, REEL_H = 1080, 1920

    if not slide_paths:
        raise ValueError("No slide paths provided to generate_reels_video")

    # Build portrait frames (blur bg + centred square slide)
    frame_paths: List[str] = []
    for sp in slide_paths:
        slide = Image.open(sp).convert("RGB")
        bg = _crop_to_size(slide, REEL_W, REEL_H)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=22))
        dark = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 0, 80))
        bg = Image.alpha_composite(bg.convert("RGBA"), dark).convert("RGB")
        top = (REEL_H - REEL_W) // 2
        bg.paste(slide, (0, top))
        frame_path = sp + "_reel_frame.jpg"
        bg.save(frame_path, "JPEG", quality=92)
        frame_paths.append(frame_path)

    # Build ffmpeg concat list — loop slides to fill target_duration
    # Use absolute paths so ffmpeg resolves them correctly regardless of cwd
    abs_frames = [os.path.abspath(p) for p in frame_paths]
    total_clips = math.ceil(target_duration / slide_duration)
    concat_lines: List[str] = []
    for i in range(total_clips):
        p = abs_frames[i % len(abs_frames)]
        concat_lines.append(f"file '{p}'")
        concat_lines.append(f"duration {slide_duration}")
    # ffmpeg concat needs the last file listed once more without duration
    concat_lines.append(f"file '{abs_frames[(total_clips - 1) % len(abs_frames)]}'")

    concat_file = output_path + "_concat.txt"
    with open(concat_file, "w") as f:
        f.write("\n".join(concat_lines) + "\n")

    music = music_path or str(EVENTS_MUSIC_FILE)

    # Step 1: build silent video from concat
    silent_path = output_path + ".silent.mp4"
    cmd_video = [
        FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c:v", "libx264", "-preset", "ultrafast", "-b:v", "4000k",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        silent_path,
    ]
    r = subprocess.run(cmd_video, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg video pass failed:\n{r.stderr[-1000:]}")

    # Step 2: computed duration (avoids ffprobe dependency)
    actual_dur = total_clips * slide_duration
    fade_start = max(0.0, actual_dur - 3.0)

    # Step 3: mux audio trimmed to exactly actual_dur
    if Path(music).exists():
        cmd_mux = [
            FFMPEG, "-y",
            "-i", silent_path,
            "-i", music,
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-af", f"afade=t=out:st={fade_start:.3f}:d=3",
            "-t", str(actual_dur),
            "-movflags", "+faststart",
            output_path,
        ]
        r2 = subprocess.run(cmd_mux, capture_output=True, text=True)
        if r2.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed:\n{r2.stderr[-1000:]}")
    else:
        import shutil
        shutil.move(silent_path, output_path)
        return output_path

    for p in [concat_file, silent_path, *frame_paths]:
        try:
            os.remove(p)
        except OSError:
            pass

    return output_path





# ── Internals ──────────────────────────────────────────────────────────────────

def _is_colorful(img: Image.Image, min_avg_saturation: float = 12.0) -> bool:
    """Return True only if image has sufficient colour saturation (rejects B&W photos)."""
    import colorsys
    small = img.convert("RGB").resize((40, 40))
    pixels = list(small.getdata())
    total_sat = sum(
        colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[1]
        for r, g, b in pixels
    )
    return (total_sat / len(pixels)) * 100 >= min_avg_saturation


def _fetch_pexels_photo(query: str, orientation: str = "square") -> Optional[Image.Image]:
    import os
    # Read at call time so Lambda secrets loaded after import are picked up
    api_key = os.environ.get("PEXELS_API_KEY") or PEXELS_API_KEY
    if not api_key:
        logger.warning("PEXELS_API_KEY not set — using gradient fallback")
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 15, "orientation": orientation},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"Pexels API returned {r.status_code} for query '{query}'")
            return None
        photos = r.json().get("photos", [])
        if not photos:
            logger.warning(f"Pexels returned no photos for query '{query}'")
            return None
        candidates = photos[:10]
        random.shuffle(candidates)
        for photo in candidates:
            src = photo.get("src", {})
            url = src.get("large2x") or src.get("large") or src.get("medium")
            if not url:
                continue
            try:
                ir = requests.get(url, timeout=15)
                ir.raise_for_status()
                img = Image.open(BytesIO(ir.content)).convert("RGB")
                if _is_colorful(img):
                    logger.info(f"Pexels photo fetched OK: {url[:60]}")
                    return img
            except Exception:
                continue
        logger.warning(f"Pexels: all candidates rejected (B&W or download failed) for '{query}'")
        return None
    except Exception as e:
        logger.warning(f"Pexels fetch error: {e}")
        return None


def _crop_square(img: Image.Image, size: int) -> Image.Image:
    w, h = img.size
    s = min(w, h)
    img = img.crop(((w - s) // 2, (h - s) // 2, (w + s) // 2, (h + s) // 2))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _crop_to_size(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    nw, nh = int(img.width * scale), int(img.height * scale)
    img = img.resize((nw, nh), Image.Resampling.LANCZOS)
    left = (nw - w) // 2
    top  = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _gradient_fallback(w: int, h: int) -> Image.Image:
    t = np.linspace(0, 1, h)
    bg = np.zeros((h, w, 3), dtype=np.uint8)
    for c, (tv, bv) in enumerate(zip((80, 120, 160), (40, 70, 110))):
        col = (tv + (bv - tv) * t).astype(np.uint8)
        bg[:, :, c] = col[:, np.newaxis]
    return Image.fromarray(bg)


def _font(path: str, size: int) -> "ImageFont.FreeTypeFont | ImageFont.ImageFont":
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _lh(font: "ImageFont.FreeTypeFont | ImageFont.ImageFont", text: str) -> int:
    try:
        _, _, _, h = font.getbbox(text or "A")
        return int(h)
    except Exception:
        return font.size  # type: ignore[attr-defined]


def _tw(font: "ImageFont.FreeTypeFont | ImageFont.ImageFont", text: str) -> int:
    try:
        left, _, right, _ = font.getbbox(text or "")
        return int(right - left)
    except Exception:
        return len(text) * (font.size // 2)  # type: ignore[attr-defined]


def _wrap(text: str, font: "ImageFont.FreeTypeFont | ImageFont.ImageFont", max_width: int, max_lines: int = 2) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current: List[str] = []
    for word in words:
        test = " ".join(current + [word])
        if _tw(font, test) > max_width and current:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) >= max_lines:
                break
        else:
            current.append(word)
    if current and len(lines) < max_lines:
        line = " ".join(current)
        if _tw(font, line) > max_width:
            line = line[:44].rstrip() + "…"
        lines.append(line)
    return lines or [text[:54]]
