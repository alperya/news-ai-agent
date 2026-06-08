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
from PIL import Image, ImageDraw, ImageFont

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
CARD_W           = 1080   # Instagram Reels width
CARD_H           = 1920   # Instagram Reels height (9:16)
CARD_SIZE        = CARD_W  # legacy alias used in helper functions
PAD_H            = 52
FOOTER_H         = 70
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


def _portrait_bg() -> "Image.Image":
    """Fetch and return a colorful NL portrait photo at CARD_W × CARD_H (1080×1920).

    Falls back to a gradient if Pexels is unavailable.
    """
    photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), orientation="portrait")
    if photo:
        scale = max(CARD_W / photo.width, CARD_H / photo.height)
        nw = int(photo.width * scale)
        nh = int(photo.height * scale)
        photo = photo.resize((nw, nh), Image.Resampling.LANCZOS)
        left = (nw - CARD_W) // 2
        top  = (nh - CARD_H) // 2
        photo = photo.crop((left, top, left + CARD_W, top + CARD_H))
        # Very light dim so overlay text remains readable
        dim = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 40))
        return Image.alpha_composite(photo.convert("RGBA"), dim)
    return _gradient_fallback(CARD_W, CARD_H).convert("RGBA")


# ── Public API ──────────────────────────────────────────────────────────────────

def create_cover_slide(
    event_count: int,
    date_range: str,
    output_path: str,
) -> str:
    """Slide 1 — native 1080×1920 portrait NL photo, centered text block, branding."""
    img = _portrait_bg()

    f_label   = _font(FONT_SEMIBOLD_PATH, 46)
    f_country = _font(FONT_PATH, 90)
    f_count   = _font(FONT_PATH, 112)
    f_date    = _font(FONT_SEMIBOLD_PATH, 42)
    f_handle  = _font(FONT_REGULAR_PATH, 32)

    label_text   = "THIS WEEK IN"
    country_text = "THE NETHERLANDS"
    count_text   = f"{event_count} EVENTS"

    lines = [
        (label_text,   f_label,   CREAM),
        (country_text, f_country, WHITE),
        (count_text,   f_count,   BRAND_ORANGE),
        (date_range,   f_date,    CREAM),
    ]

    gap        = 22
    pill_pad_x = 28
    pill_pad_y = 12

    line_heights = [_lh(f, t) for (t, f, _) in lines]
    total_h = sum(line_heights) + gap * (len(lines) - 1)
    start_y = (CARD_H - total_h) // 2  # true vertical centre of portrait frame

    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    y = start_y
    for text, font, color in lines:
        tw = _tw(font, text)
        th = _lh(font, text)
        cx = (CARD_W - tw) // 2
        od.rounded_rectangle(
            [(cx - pill_pad_x, y - pill_pad_y),
             (cx + tw + pill_pad_x, y + th + pill_pad_y)],
            radius=12,
            fill=PILL_TEXT,
        )
        od.text((cx, y), text, font=font, fill=color)
        y += th + gap

    footer_top = CARD_H - FOOTER_H
    od.rectangle([(0, footer_top), (CARD_W, CARD_H)], fill=FOOTER_BAR)
    fw = _tw(f_handle, ACCOUNT_HANDLE)
    fh = _lh(f_handle, ACCOUNT_HANDLE)
    od.text(((CARD_W - fw) // 2, footer_top + (FOOTER_H - fh) // 2),
            ACCOUNT_HANDLE, font=f_handle, fill=HANDLE_COLOR)

    od.rectangle([(0, 0), (CARD_W, 8)], fill=BRAND_ORANGE)

    img = Image.alpha_composite(img, overlay).convert("RGB")
    img.save(output_path, "JPEG", quality=94, optimize=True)
    return output_path


def create_event_list_slide(
    events: List[dict],
    date_range: str,
    output_path: str,
    slide_number: int = 2,
) -> str:
    """Slide 2+ — native 1080×1920 portrait NL photo, event pills, handle footer."""
    events = events[:EVENTS_PER_SLIDE]

    img = _portrait_bg()  # RGBA 1080×1920

    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    f_date_badge = _font(FONT_SEMIBOLD_PATH, 30)
    f_title      = _font(FONT_PATH, 50)
    f_meta       = _font(FONT_SEMIBOLD_PATH, 34)
    f_handle     = _font(FONT_REGULAR_PATH, 30)

    # Date range badge — top right corner
    bw = _tw(f_date_badge, date_range)
    bh = _lh(f_date_badge, date_range)
    badge_pad   = 14
    badge_right = CARD_W - PAD_H
    badge_top   = 36
    od.rounded_rectangle(
        [(badge_right - bw - badge_pad * 2, badge_top),
         (badge_right, badge_top + bh + badge_pad)],
        radius=10,
        fill=PILL_DATE,
    )
    od.text((badge_right - bw - badge_pad, badge_top + badge_pad // 2),
            date_range, font=f_date_badge, fill=CREAM)

    pill_margin_x = PAD_H - 12
    pill_v_pad    = 16
    pill_gap      = 28   # space between consecutive pills

    dot_x  = pill_margin_x + 20
    tx     = dot_x + 18
    max_tw = CARD_W - tx - pill_margin_x

    # ── Pass 1: measure each pill's height ──────────────────────────────────
    pill_data = []  # [(title_lines, block_h, title_w, meta_w, venue, location, date_lbl)]
    for ev in events:
        title    = ev.get("title", "")
        venue    = ev.get("venue") or ""
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")

        title_lines = _wrap(title, f_title, max_tw)
        t_block = sum(_lh(f_title, l) + 3 for l in title_lines)
        meta_h  = _lh(f_meta, "A") + 4
        block_h = t_block + meta_h + 10

        title_w = max(_tw(f_title, l) for l in title_lines) if title_lines else 0
        if venue:
            meta_w = 14 + _tw(f_meta, venue)
            if location or date_lbl:
                ld = f"{location}  ·  {date_lbl}" if location and date_lbl else location or date_lbl
                meta_w += _tw(f_meta, "  ·  ") + _tw(f_meta, ld)
        elif location or date_lbl:
            ld = f"{location}  ·  {date_lbl}" if location and date_lbl else location or date_lbl
            meta_w = _tw(f_meta, ld)
        else:
            meta_w = 0

        pill_data.append((title_lines, block_h, title_w, meta_w, venue, location, date_lbl))

    pill_heights = [block_h + 2 * pill_v_pad for (_, block_h, *_) in pill_data]
    total_group_h = sum(pill_heights) + pill_gap * (len(pill_data) - 1)

    # Centre the whole group vertically, below the date badge
    badge_bottom = badge_top + bh + badge_pad + 20
    group_start  = badge_bottom + (CARD_H - FOOTER_H - badge_bottom - total_group_h) // 2

    # ── Pass 2: draw ─────────────────────────────────────────────────────────
    y = group_start
    for (title_lines, block_h, title_w, meta_w, venue, location, date_lbl), pill_h in zip(pill_data, pill_heights):
        pill_right = min(tx + max(title_w, meta_w) + 24, CARD_W - pill_margin_x)
        pill_top   = y
        pill_bot   = y + pill_h
        text_y     = y + pill_v_pad

        od.rounded_rectangle(
            [(pill_margin_x, pill_top), (pill_right, pill_bot)],
            radius=14,
            fill=PILL_EVENT,
        )

        dot_y = text_y + _lh(f_title, "A") // 2 - 5
        od.ellipse([(dot_x, dot_y), (dot_x + 10, dot_y + 10)], fill=BRAND_ORANGE)

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

        y += pill_h + pill_gap

    # Footer bar + handle
    od.rectangle([(0, CARD_H - FOOTER_H), (CARD_W, CARD_H)], fill=FOOTER_BAR)
    fw = _tw(f_handle, ACCOUNT_HANDLE)
    fh = _lh(f_handle, ACCOUNT_HANDLE)
    od.text(((CARD_W - fw) // 2, CARD_H - FOOTER_H + (FOOTER_H - fh) // 2),
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

    if not slide_paths:
        raise ValueError("No slide paths provided to generate_reels_video")

    # Slides are already native 1080×1920 — use them directly, no composition step
    # Build ffmpeg concat list — loop slides to fill target_duration
    # Use absolute paths so ffmpeg resolves them correctly regardless of cwd
    abs_frames = [os.path.abspath(p) for p in slide_paths]
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

    for p in [concat_file, silent_path]:
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
