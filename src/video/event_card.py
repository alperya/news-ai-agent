"""
Event Card Generator — Instagram-ready 1080×1080 image for weekly event posts.

Design language:
  - Beautiful Netherlands/Amsterdam photo from Pexels as background
  - Frosted white semi-transparent overlay (editorial magazine feel)
  - Solid orange header band at top (brand anchor)
  - Poppins Bold for titles, Poppins SemiBold for meta
  - 📍 venue · city · date  |  price in green/orange
  - Slim orange left accent bar per event row

Also exports:
  create_event_slide()  — individual 1080×1080 card for a single event (carousel slides)
"""

from __future__ import annotations

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
)

# ── Palette ────────────────────────────────────────────────────────────────────
HEADER_BG     = (255, 91, 20)      # brand orange
HEADER_TEXT   = (255, 255, 255)    # white
HEADER_DATE   = (255, 215, 180)    # warm tint for date range
ACCENT_BAR    = (255, 91, 20)      # orange left stripe
TEXT_TITLE    = (15,  23, 42)      # near-black (dark navy) — on white overlay
TEXT_META     = (71,  85, 105)     # slate-600
TEXT_VENUE    = (30,  64, 175)     # blue — venue stands out from grey city text
PRICE_FREE    = (5,  150,  105)    # emerald
PRICE_PAID    = (234, 88, 12)      # orange-600
SEPARATOR     = (226, 232, 240)    # light grey
FOOTER_TEXT   = (148, 163, 184)    # slate-400
SLIDE_OVERLAY = (0, 0, 0, 170)    # dark overlay for individual event slides

# ── Geometry ───────────────────────────────────────────────────────────────────
CARD_SIZE  = 1080
PAD_H      = 56
HEADER_H   = 108
FOOTER_H   = 42
ACCENT_W   = 5
ACCENT_GAP = 14
MAX_EVENTS = 7

# ── Pexels search queries — beautiful Netherlands scenes ───────────────────────
_BG_QUERIES = [
    "amsterdam canal houses",
    "netherlands tulip fields",
    "delft blue windmill",
    "amsterdam bicycle street",
    "dutch coastline sunset",
    "rotterdam modern architecture",
]


# ── Public API ──────────────────────────────────────────────────────────────────

def create_event_card(
    events: List[dict],
    date_range: str,
    output_path: str,
) -> str:
    """Render the weekly event overview card (slide 1 of the carousel).

    events dicts: title, location, venue (opt), date_label, price (opt), emoji (opt)
    """
    events = events[:MAX_EVENTS]
    img = _make_background()
    draw = ImageDraw.Draw(img)

    f_bold   = _font(FONT_PATH, 50)
    f_semi   = _font(FONT_SEMIBOLD_PATH, 28)
    f_title  = _font(FONT_PATH, 34)
    f_meta   = _font(FONT_SEMIBOLD_PATH, 25)
    f_footer = _font(FONT_REGULAR_PATH, 18)

    # ── Orange header band ────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (CARD_SIZE, HEADER_H)], fill=HEADER_BG)
    ht_h = _lh(f_bold, "A")
    hs_h = _lh(f_semi, "A")
    hy = (HEADER_H - ht_h - 6 - hs_h) // 2
    draw.text((PAD_H, hy), "THIS WEEK IN NL", font=f_bold, fill=HEADER_TEXT)
    draw.text((PAD_H, hy + ht_h + 6), date_range, font=f_semi, fill=HEADER_DATE)

    # ── Event rows (evenly distributed) ───────────────────────────────────────
    usable_h = CARD_SIZE - HEADER_H - FOOTER_H - 4
    slot_h = usable_h // max(len(events), 1)
    text_x = PAD_H + ACCENT_W + ACCENT_GAP

    for idx, ev in enumerate(events):
        emoji    = ev.get("emoji", "•")
        title    = ev.get("title", "")
        venue    = ev.get("venue") or ""
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")
        price    = ev.get("price") or ""

        slot_top = HEADER_H + 4 + idx * slot_h
        title_lines = _wrap(title, f_title, CARD_SIZE - text_x - PAD_H - 46)
        t_block = sum(_lh(f_title, l) + 2 for l in title_lines)
        m_block = _lh(f_meta, "A")
        block_h = t_block + 6 + m_block
        y = slot_top + (slot_h - block_h) // 2

        # Accent bar
        draw.rectangle(
            [(PAD_H, slot_top + 8), (PAD_H + ACCENT_W - 1, slot_top + slot_h - 8)],
            fill=ACCENT_BAR,
        )

        # Small colored dot as category indicator + title
        dot_x = text_x
        _draw_pin(draw, (dot_x, y + _lh(f_title, "A") // 2 - 4), ACCENT_BAR, size=10)
        title_x = dot_x + 18
        for line in title_lines:
            draw.text((title_x, y), line, font=f_title, fill=TEXT_TITLE)
            y += _lh(f_title, line) + 2

        y += 6
        # Meta: pin + Venue, City · Date  |  price (colored)
        meta_x = title_x
        cur_x = meta_x

        if venue:
            # Small blue pin circle + venue
            cur_x += _draw_pin(draw, (cur_x, y + _lh(f_meta, "A") // 2 - 4), TEXT_VENUE, size=8)
            draw.text((cur_x, y), venue, font=f_meta, fill=TEXT_VENUE)
            cur_x += _tw(f_meta, venue)
            if location or date_lbl:
                draw.text((cur_x, y), "  ·  ", font=f_meta, fill=SEPARATOR)
                cur_x += _tw(f_meta, "  ·  ")

        if location or date_lbl:
            loc_date = f"{location}  ·  {date_lbl}" if location and date_lbl else (location or date_lbl)
            draw.text((cur_x, y), loc_date, font=f_meta, fill=TEXT_META)
            cur_x += _tw(f_meta, loc_date)

        if price:
            price_color = PRICE_FREE if price.lower() in ("free", "gratis") else PRICE_PAID
            draw.text((cur_x, y), "   |   ", font=f_meta, fill=SEPARATOR)
            cur_x += _tw(f_meta, "   |   ")
            draw.text((cur_x, y), price, font=f_meta, fill=price_color)

        # Separator
        if idx < len(events) - 1:
            sy = slot_top + slot_h - 1
            draw.line(
                [(PAD_H + ACCENT_W + 4, sy), (CARD_SIZE - PAD_H, sy)],
                fill=SEPARATOR, width=1,
            )

    # ── Footer ─────────────────────────────────────────────────────────────────
    footer_y = CARD_SIZE - FOOTER_H + 6
    draw.line([(PAD_H, footer_y - 6), (CARD_SIZE - PAD_H, footer_y - 6)], fill=SEPARATOR, width=1)
    disclaimer = "AI-assisted curation — verify details before attending"
    draw.text((PAD_H, footer_y), disclaimer, font=f_footer, fill=FOOTER_TEXT)

    img.save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


def create_event_slide(
    event: dict,
    output_path: str,
    event_image_url: Optional[str] = None,
) -> Optional[str]:
    """Render a single event slide (carousel slides 2+).

    Uses the event's own image as background if available,
    falls back to a Pexels photo matching the event category.
    Returns output_path on success, None if background unavailable.
    """
    bg: Optional[Image.Image] = None

    # Try event's own image first
    if event_image_url:
        try:
            resp = requests.get(event_image_url, timeout=10)
            resp.raise_for_status()
            bg = Image.open(BytesIO(resp.content)).convert("RGB")
        except Exception:
            bg = None

    # Fallback: Pexels based on category
    if bg is None:
        category = event.get("category", "other")
        query = _category_to_pexels_query(category)
        bg = _fetch_pexels_photo(query, orientation="square")

    if bg is None:
        return None

    bg = _crop_square(bg, CARD_SIZE)
    # Dark overlay for legibility on individual slides
    overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), SLIDE_OVERLAY)
    img = Image.alpha_composite(bg.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    f_title = _font(FONT_PATH, 54)
    f_meta  = _font(FONT_SEMIBOLD_PATH, 32)
    f_price = _font(FONT_PATH, 40)

    title    = event.get("title", "")
    venue    = event.get("venue") or ""
    location = event.get("location", "")
    date_lbl = event.get("date_label", "")
    price    = event.get("price") or ""

    title_lines = _wrap(title, f_title, CARD_SIZE - 2 * PAD_H, max_lines=3)

    # Calculate total block height to center it vertically
    meta_h = _lh(f_meta, "A") + 8 if (venue or location or date_lbl) else 0
    price_h = _lh(f_price, "A") + 6 if price else 0
    dot_indicator_h = 28  # large orange dot at top of block
    title_h = sum(_lh(f_title, l) + 4 for l in title_lines)
    total_h = dot_indicator_h + 16 + title_h + meta_h + price_h
    y = (CARD_SIZE - total_h) // 2

    # Large centered orange dot as event indicator (instead of emoji)
    dot_r = 12
    cx = CARD_SIZE // 2
    draw.ellipse([(cx - dot_r, y), (cx + dot_r, y + dot_r * 2)], fill=HEADER_BG)
    y += dot_indicator_h + 16

    # Title (centered)
    for line in title_lines:
        lw = _tw(f_title, line)
        draw.text(((CARD_SIZE - lw) // 2, y), line, font=f_title, fill=(255, 255, 255))
        y += _lh(f_title, line) + 4

    # Venue · location · date (centered)
    meta_parts = []
    if venue:
        meta_parts.append(venue)
    if location and location != venue:
        meta_parts.append(location)
    if date_lbl:
        meta_parts.append(date_lbl)
    if meta_parts:
        meta_str = "  ·  ".join(meta_parts)
        mw = _tw(f_meta, meta_str)
        y += 8
        draw.text(((CARD_SIZE - mw) // 2, y), meta_str, font=f_meta, fill=(210, 220, 240))
        y += _lh(f_meta, "A") + 8

    # Price (centered, colored)
    if price:
        price_color = (52, 211, 153) if price.lower() in ("free", "gratis") else (251, 146, 60)
        pw = _tw(f_price, price)
        draw.text(((CARD_SIZE - pw) // 2, y), price, font=f_price, fill=price_color)

    # Bottom brand strip
    draw.rectangle([(0, CARD_SIZE - 50), (CARD_SIZE, CARD_SIZE)], fill=HEADER_BG)
    f_brand = _font(FONT_PATH, 24)
    brand = "THIS WEEK IN NL"
    bw = _tw(f_brand, brand)
    draw.text(((CARD_SIZE - bw) // 2, CARD_SIZE - 38), brand, font=f_brand, fill=(255, 255, 255))

    img.save(output_path, "JPEG", quality=92, optimize=True)
    return output_path


# ── Internals ──────────────────────────────────────────────────────────────────

def _make_background() -> Image.Image:
    """Fetch NL photo from Pexels and apply frosted white overlay.

    Falls back to a navy gradient if Pexels is unavailable.
    """
    query = random.choice(_BG_QUERIES)
    photo = _fetch_pexels_photo(query, orientation="square")
    if photo:
        img = _crop_square(photo, CARD_SIZE)
        # Very slight blur to push the photo further into the background
        img = img.filter(ImageFilter.GaussianBlur(radius=2))
        # Frosted white overlay — enough to make dark text readable
        overlay = Image.new("RGBA", (CARD_SIZE, CARD_SIZE), (255, 255, 255, 195))
        img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        return img

    # Fallback: soft navy gradient
    t = np.linspace(0, 1, CARD_SIZE)
    bg = np.zeros((CARD_SIZE, CARD_SIZE, 3), dtype=np.uint8)
    for c, (top, bot) in enumerate(zip((220, 228, 240), (241, 245, 249))):
        channel = (top + (bot - top) * t).astype(np.uint8)
        bg[:, :, c] = channel[:, np.newaxis]
    return Image.fromarray(bg)


def _fetch_pexels_photo(query: str, orientation: str = "square") -> Optional[Image.Image]:
    key = PEXELS_API_KEY
    if not key:
        return None
    try:
        resp = requests.get(
            "https://api.pexels.com/v1/search",
            headers={"Authorization": key},
            params={"query": query, "per_page": 10, "orientation": orientation},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        photos = resp.json().get("photos", [])
        if not photos:
            return None
        photo = random.choice(photos[:5])
        src = photo.get("src", {})
        url = src.get("large2x") or src.get("large") or src.get("medium")
        if not url:
            return None
        img_resp = requests.get(url, timeout=15)
        img_resp.raise_for_status()
        return Image.open(BytesIO(img_resp.content)).convert("RGB")
    except Exception:
        return None


def _crop_square(img: Image.Image, size: int) -> Image.Image:
    w, h = img.size
    short = min(w, h)
    left = (w - short) // 2
    top  = (h - short) // 2
    img  = img.crop((left, top, left + short, top + short))
    return img.resize((size, size), Image.Resampling.LANCZOS)


def _category_to_pexels_query(category: str) -> str:
    return {
        "festival":  "outdoor festival crowd",
        "concert":   "concert live music stage",
        "music":     "concert live music stage",
        "museum":    "art museum gallery",
        "art":       "art gallery exhibition",
        "sport":     "sports event stadium",
        "theatre":   "theatre stage performance",
        "film":      "cinema movie screen",
        "family":    "family fun outdoors",
        "outdoor":   "outdoor park amsterdam",
    }.get(category.lower(), "amsterdam canal")


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _draw_pin(draw: ImageDraw.ImageDraw, xy: tuple, color: tuple, size: int = 8) -> int:
    """Draw a small filled circle (pin indicator) at xy. Returns width consumed."""
    x, y = xy
    r = size // 2
    cx, cy = x + r, y + r + 2
    draw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=color)
    return size + 6


def _strip_emoji(text: str) -> str:
    """Remove emoji codepoints that Poppins cannot render."""
    result = ""
    for ch in text:
        cp = ord(ch)
        if not (
            0x2600 <= cp <= 0x27BF
            or 0x1F300 <= cp <= 0x1FAFF
            or 0x2300 <= cp <= 0x23FF
        ):
            result += ch
    return result


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
