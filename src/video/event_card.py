"""
Event Card Generator — Instagram carousel slides for weekly NL events.

Slide layout:
  Slide 1 (cover)  — full NL photo, "THIS WEEK IN THE NETHERLANDS · N EVENTS", branding
  Slide 2+ (list)  — 4-5 events per slide, date range header, @dutch_news_english footer

All slides include @dutch_news_english branding.
No AI disclaimer (removed per design decision).
"""

from __future__ import annotations

import math
import random
from io import BytesIO
from typing import List, Optional

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
CREAM         = (255, 230, 200)      # warm accent for subtitles on dark
GLASS_PANEL   = (5,   15,  35, 178)  # dark navy glass, ~70% opacity
ACCENT_BAR    = (255, 91,  20)
TEXT_TITLE    = (255, 255, 255)
TEXT_META     = (185, 210, 240)      # light blue-grey
TEXT_VENUE    = (120, 185, 255)      # lighter blue
PRICE_FREE    = (52,  211, 153)      # emerald
PRICE_PAID    = (251, 191,  36)      # amber
SEPARATOR     = (55,  85,  130)
HANDLE_COLOR  = (200, 220, 250)      # pale blue for @handle

# ── Geometry ───────────────────────────────────────────────────────────────────
CARD_SIZE      = 1080
PAD_H          = 58
HEADER_H       = 100      # orange header band on list slides
FOOTER_H       = 48       # @handle footer on list slides
PANEL_PAD      = 8
ACCENT_W       = 5
EVENTS_PER_SLIDE = 4      # events shown per list slide

_BG_QUERIES = [
    "amsterdam canal houses",
    "netherlands tulip fields",
    "delft windmill sunrise",
    "amsterdam bicycle street",
    "dutch sunset coastline",
    "rotterdam waterfront architecture",
]

ACCOUNT_HANDLE = "@dutch_news_english"


# ── Public API ──────────────────────────────────────────────────────────────────

def create_cover_slide(
    event_count: int,
    date_range: str,
    output_path: str,
) -> str:
    """Slide 1 — full NL photo, event count headline, branding."""
    photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), "square")
    if photo:
        bg = _crop_square(photo, CARD_SIZE)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=1))
    else:
        bg = _gradient_fallback(CARD_SIZE, CARD_SIZE)

    # Gradient overlay — darker at bottom for text, lighter at top
    overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    steps = 120
    for i in range(steps):
        frac = i / steps
        # Top 30%: slight dark vignette; bottom 50%: strong dark for text
        if frac < 0.35:
            alpha = int(80 * (1 - frac / 0.35))
        elif frac > 0.55:
            alpha = int(190 * ((frac - 0.55) / 0.45))
        else:
            alpha = 10
        y0 = int(frac * CARD_SIZE)
        y1 = int((frac + 1 / steps) * CARD_SIZE) + 1
        od.rectangle([(0, y0), (CARD_SIZE, y1)], fill=(0, 0, 20, alpha))

    img = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # Orange top accent strip (thin)
    draw.rectangle([(0, 0), (CARD_SIZE, 8)], fill=BRAND_ORANGE)

    f_label   = _font(FONT_SEMIBOLD_PATH, 34)
    f_country = _font(FONT_PATH, 72)
    f_count   = _font(FONT_PATH, 90)
    f_date    = _font(FONT_SEMIBOLD_PATH, 32)
    f_handle  = _font(FONT_REGULAR_PATH, 26)

    # Vertically center the text block in the lower 55% of the image
    label_text   = "THIS WEEK IN"
    country_text = "THE NETHERLANDS"
    count_text   = f"{event_count} EVENTS"

    lh_label   = _lh(f_label,   "A")
    lh_country = _lh(f_country, "A")
    lh_count   = _lh(f_count,   "A")
    lh_date    = _lh(f_date,    "A")
    gap = 14
    total_h = lh_label + gap + lh_country + gap + lh_count + gap + lh_date

    center_zone_top = int(CARD_SIZE * 0.40)
    center_zone_h   = CARD_SIZE - center_zone_top - 80
    y = center_zone_top + (center_zone_h - total_h) // 2

    def _draw_centered(text: str, font: ImageFont.FreeTypeFont, color: tuple, y_pos: int) -> None:
        w = _tw(font, text)
        draw.text(((CARD_SIZE - w) // 2, y_pos), text, font=font, fill=color)

    _draw_centered(label_text,   f_label,   CREAM,        y)
    y += lh_label + gap
    _draw_centered(country_text, f_country, WHITE,        y)
    y += lh_country + gap
    _draw_centered(count_text,   f_count,   BRAND_ORANGE, y)
    y += lh_count + gap
    _draw_centered(date_range,   f_date,    CREAM,        y)

    # Handle at bottom
    _draw_centered(ACCOUNT_HANDLE, f_handle, HANDLE_COLOR, CARD_SIZE - 52)

    img.save(output_path, "JPEG", quality=94, optimize=True)
    return output_path


def create_event_list_slide(
    events: List[dict],
    date_range: str,
    output_path: str,
    slide_number: int = 2,
) -> str:
    """Slide 2+ — 4 events on a frosted-glass panel over NL photo."""
    events = events[:EVENTS_PER_SLIDE]

    photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), "square")
    if photo:
        bg = _crop_square(photo, CARD_SIZE).filter(ImageFilter.GaussianBlur(radius=1))
    else:
        bg = _gradient_fallback(CARD_SIZE, CARD_SIZE)
    img = bg.convert("RGBA")

    # Glass panel over entire event area (below header, above footer)
    panel = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 0, 0))
    pd = ImageDraw.Draw(panel)
    pd.rounded_rectangle(
        [(PANEL_PAD, HEADER_H + PANEL_PAD),
         (CARD_SIZE - PANEL_PAD, CARD_SIZE - FOOTER_H - PANEL_PAD)],
        radius=14,
        fill=GLASS_PANEL,
    )
    img = Image.alpha_composite(img, panel).convert("RGB")
    draw = ImageDraw.Draw(img)

    f_header  = _font(FONT_PATH, 30)
    f_sub     = _font(FONT_SEMIBOLD_PATH, 22)
    f_title   = _font(FONT_PATH, 33)
    f_meta    = _font(FONT_SEMIBOLD_PATH, 23)
    f_handle  = _font(FONT_REGULAR_PATH, 23)

    # Orange header band
    draw.rectangle([(0, 0), (CARD_SIZE, HEADER_H)], fill=BRAND_ORANGE)
    header_text = "THIS WEEK IN NL"
    hw = _tw(f_header, header_text)
    hh = _lh(f_header, "A")
    sh = _lh(f_sub, "A")
    hy = (HEADER_H - hh - 6 - sh) // 2
    draw.text(((CARD_SIZE - hw) // 2, hy), header_text, font=f_header, fill=WHITE)
    dw = _tw(f_sub, date_range)
    draw.text(((CARD_SIZE - dw) // 2, hy + hh + 6), date_range, font=f_sub, fill=CREAM)

    # Event list
    panel_top = HEADER_H + PANEL_PAD + 4
    panel_bot = CARD_SIZE - FOOTER_H - PANEL_PAD - 4
    usable_h = panel_bot - panel_top
    slot_h = usable_h // max(len(events), 1)
    text_x = PAD_H + ACCENT_W + 12 + PANEL_PAD

    for idx, ev in enumerate(events):
        title    = ev.get("title", "")
        venue    = ev.get("venue") or ""
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")
        price    = ev.get("price") or ""

        slot_top_y = panel_top + idx * slot_h
        max_title_w = CARD_SIZE - text_x - PAD_H - 16
        title_lines = _wrap(title, f_title, max_title_w)
        t_block = sum(_lh(f_title, l) + 2 for l in title_lines)
        block_h = t_block + 5 + _lh(f_meta, "A")
        y = slot_top_y + (slot_h - block_h) // 2

        # Accent bar
        draw.rectangle(
            [(PAD_H + PANEL_PAD, slot_top_y + 6),
             (PAD_H + PANEL_PAD + ACCENT_W - 1, slot_top_y + slot_h - 6)],
            fill=ACCENT_BAR,
        )

        # Title dot + text
        _draw_pin(draw, (text_x, y + _lh(f_title, "A") // 2 - 5), ACCENT_BAR, size=9)
        tx = text_x + 16
        for line in title_lines:
            draw.text((tx, y), line, font=f_title, fill=TEXT_TITLE)
            y += _lh(f_title, line) + 2

        y += 5
        cur_x = tx
        if venue:
            cur_x += _draw_pin(draw, (cur_x, y + _lh(f_meta, "A") // 2 - 4), TEXT_VENUE, size=7)
            draw.text((cur_x, y), venue, font=f_meta, fill=TEXT_VENUE)
            cur_x += _tw(f_meta, venue)
            if location or date_lbl:
                draw.text((cur_x, y), "  ·  ", font=f_meta, fill=SEPARATOR)
                cur_x += _tw(f_meta, "  ·  ")
        if location or date_lbl:
            ld = f"{location}  ·  {date_lbl}" if location and date_lbl else location or date_lbl
            draw.text((cur_x, y), ld, font=f_meta, fill=TEXT_META)
            cur_x += _tw(f_meta, ld)
        if price:
            pc = PRICE_FREE if price.lower() in ("free", "gratis") else PRICE_PAID
            draw.text((cur_x, y), "   |   ", font=f_meta, fill=SEPARATOR)
            cur_x += _tw(f_meta, "   |   ")
            draw.text((cur_x, y), price, font=f_meta, fill=pc)

        if idx < len(events) - 1:
            sy = slot_top_y + slot_h - 1
            draw.line(
                [(PAD_H + PANEL_PAD + ACCENT_W + 4, sy), (CARD_SIZE - PAD_H - PANEL_PAD, sy)],
                fill=SEPARATOR, width=1,
            )

    # @handle footer
    hw2 = _tw(f_handle, ACCOUNT_HANDLE)
    draw.text(((CARD_SIZE - hw2) // 2, CARD_SIZE - FOOTER_H + 12),
              ACCOUNT_HANDLE, font=f_handle, fill=HANDLE_COLOR)

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

    # Slide 1: cover
    cover_path = f"{tmp_prefix}_cover.jpg"
    create_cover_slide(
        event_count=len(events),
        date_range=date_range,
        output_path=cover_path,
    )
    paths.append(cover_path)

    # Slides 2+: event lists
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


# ── Keep old entry-points as thin wrappers for backward-compat ─────────────────

def create_event_card(events: List[dict], date_range: str, output_path: str) -> str:
    """Backward-compat shim — creates first list slide."""
    return create_event_list_slide(events[:EVENTS_PER_SLIDE], date_range, output_path)


def create_event_slide(
    event: dict,
    output_path: str,
    event_image_url: Optional[str] = None,
) -> Optional[str]:
    """Single-event slide (kept for optional individual-event use)."""
    bg: Optional[Image.Image] = None
    if event_image_url:
        try:
            r = requests.get(event_image_url, timeout=10)
            r.raise_for_status()
            bg = Image.open(BytesIO(r.content)).convert("RGB")
        except Exception:
            bg = None
    if bg is None:
        bg = _fetch_pexels_photo(_category_to_query(event.get("category", "")), "square")
    if bg is None:
        return None

    bg = _crop_square(bg, CARD_SIZE)
    overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (0, 0, 20, 165))
    img = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    f_title = _font(FONT_PATH, 54)
    f_meta  = _font(FONT_SEMIBOLD_PATH, 32)
    f_price = _font(FONT_PATH, 40)
    f_handle = _font(FONT_REGULAR_PATH, 24)

    title    = event.get("title", "")
    venue    = event.get("venue") or ""
    location = event.get("location", "")
    date_lbl = event.get("date_label", "")
    price    = event.get("price") or ""

    title_lines = _wrap(title, f_title, CARD_SIZE - 2 * PAD_H, max_lines=3)
    meta_parts = [p for p in [venue, location if location != venue else None, date_lbl] if p]
    meta_str = "  ·  ".join(meta_parts) if meta_parts else ""
    meta_h = (_lh(f_meta, "A") + 10) if meta_str else 0
    price_h = (_lh(f_price, "A") + 8) if price else 0
    title_h = sum(_lh(f_title, l) + 4 for l in title_lines)
    dot_h = 28
    total_h = dot_h + 16 + title_h + meta_h + price_h
    y = (CARD_SIZE - total_h) // 2

    dot_r = 12
    draw.ellipse([(CARD_SIZE // 2 - dot_r, y), (CARD_SIZE // 2 + dot_r, y + dot_r * 2)],
                 fill=BRAND_ORANGE)
    y += dot_h + 16

    for line in title_lines:
        lw = _tw(f_title, line)
        draw.text(((CARD_SIZE - lw) // 2, y), line, font=f_title, fill=WHITE)
        y += _lh(f_title, line) + 4

    if meta_str:
        mw = _tw(f_meta, meta_str)
        y += 8
        draw.text(((CARD_SIZE - mw) // 2, y), meta_str, font=f_meta, fill=(200, 220, 245))
        y += _lh(f_meta, "A") + 8

    if price:
        pc = (52, 211, 153) if price.lower() in ("free", "gratis") else (251, 146, 60)
        pw = _tw(f_price, price)
        draw.text(((CARD_SIZE - pw) // 2, y), price, font=f_price, fill=pc)

    draw.rectangle([(0, CARD_SIZE - 50), (CARD_SIZE, CARD_SIZE)], fill=BRAND_ORANGE)
    f_brand = _font(FONT_PATH, 22)
    bw = _tw(f_brand, ACCOUNT_HANDLE)
    draw.text(((CARD_SIZE - bw) // 2, CARD_SIZE - 36), ACCOUNT_HANDLE, font=f_brand, fill=WHITE)

    img.save(output_path, "JPEG", quality=92, optimize=True)
    return output_path


def create_event_reel(
    card_path: str,
    output_path: str,
    slide_paths: Optional[List[str]] = None,
    music_path: Optional[str] = None,
) -> str:
    """Create 1080×1920 Reels video (reserved for future use)."""
    from moviepy import AudioFileClip, ImageClip, concatenate_videoclips
    from pathlib import Path

    REEL_W, REEL_H = 1080, 1920
    TOTAL_DURATION = 15
    slide_paths = slide_paths or []
    music_path = music_path or str(EVENTS_MUSIC_FILE)

    def _portrait_bg() -> Image.Image:
        photo = _fetch_pexels_photo(random.choice(_BG_QUERIES), "portrait")
        if photo:
            img = _crop_to_size(photo, REEL_W, REEL_H)
            overlay = Image.new("RGBA", (REEL_W, REEL_H), (0, 0, 20, 140))
            return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        return _gradient_fallback(REEL_W, REEL_H)

    clips = []
    for sp in slide_paths:
        try:
            slide_img = Image.open(sp).convert("RGB")
            bg = _portrait_bg()
            top = (REEL_H - CARD_SIZE) // 2
            bg.paste(slide_img, (0, top))
            fp = sp + "_reel.jpg"
            bg.save(fp, "JPEG", quality=92)
            clips.append(ImageClip(fp).with_duration(3.0).with_fps(30))
        except Exception:
            pass

    card_duration = max(TOTAL_DURATION - len(clips) * 3, 6)
    bg = _portrait_bg()
    card_img = Image.open(card_path).convert("RGB")
    top = (REEL_H - CARD_SIZE) // 2
    bg.paste(card_img, (0, top))
    mf = card_path + "_reel.jpg"
    bg.save(mf, "JPEG", quality=93)
    clips.append(ImageClip(mf).with_duration(card_duration).with_fps(30))

    video = concatenate_videoclips(clips, method="compose") if len(clips) > 1 else clips[0]
    actual_dur = sum(c.duration for c in clips)

    if Path(music_path).exists():
        try:
            audio = AudioFileClip(music_path)
            audio = audio.subclipped(0, min(actual_dur, audio.duration))
            audio = audio.audio_fadeout(min(2.0, actual_dur * 0.15))
            video = video.with_audio(audio)
        except Exception:
            pass

    video.write_videofile(output_path, fps=30, codec="libx264", audio_codec="aac",
                          bitrate="4000k", preset="ultrafast", logger=None)
    return output_path


# ── Internals ──────────────────────────────────────────────────────────────────

def _fetch_pexels_photo(query: str, orientation: str = "square") -> Optional[Image.Image]:
    if not PEXELS_API_KEY:
        return None
    try:
        r = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": PEXELS_API_KEY},
            params={"query": query, "per_page": 10, "orientation": orientation},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        photos = r.json().get("photos", [])
        if not photos:
            return None
        photo = random.choice(photos[:5])
        src = photo.get("src", {})
        url = src.get("large2x") or src.get("large") or src.get("medium")
        if not url:
            return None
        ir = requests.get(url, timeout=15)
        ir.raise_for_status()
        return Image.open(BytesIO(ir.content)).convert("RGB")
    except Exception:
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
    for c, (tv, bv) in enumerate(zip((15, 28, 50), (25, 42, 72))):
        col = (tv + (bv - tv) * t).astype(np.uint8)
        bg[:, :, c] = col[:, np.newaxis]
    return Image.fromarray(bg)


def _category_to_query(category: str) -> str:
    return {
        "festival": "outdoor festival crowd",
        "concert":  "concert live music stage",
        "music":    "concert live music stage",
        "museum":   "art museum gallery",
        "art":      "art gallery exhibition",
        "sport":    "sports event stadium",
        "theatre":  "theatre stage performance",
        "film":     "cinema movie screen",
        "family":   "family fun outdoors",
        "outdoor":  "outdoor park amsterdam",
    }.get(category.lower(), "amsterdam canal")


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _draw_pin(draw: ImageDraw.ImageDraw, xy: tuple, color: tuple, size: int = 8) -> int:
    x, y = xy
    draw.ellipse([(x, y), (x + size, y + size)], fill=color)
    return size + 6


def _lh(font: ImageFont.FreeTypeFont, text: str) -> int:
    try:
        _, _, _, h = font.getbbox(text or "A")
        return int(h)
    except Exception:
        return font.size  # type: ignore[attr-defined]


def _tw(font: ImageFont.FreeTypeFont, text: str) -> int:
    try:
        left, _, right, _ = font.getbbox(text or "")
        return int(right - left)
    except Exception:
        return len(text) * (font.size // 2)  # type: ignore[attr-defined]


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int, max_lines: int = 2) -> List[str]:
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
