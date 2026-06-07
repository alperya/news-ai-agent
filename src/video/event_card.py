"""
Event Card Generator — PIL-based 1080×1080 infographic for Instagram feed posts.

Layout:
  Header band : full-width orange bar — "📅 THIS WEEK IN NL" (white) + date range
  Event rows  : orange left accent | emoji title (white) / venue · city · date · price (grey)
  Footer      : AI disclaimer (very small, muted)
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .config import FONT_PATH

# ── Palette ────────────────────────────────────────────────────────────────────
BG_TOP       = (13,  27, 42)    # deep navy — top of gradient
BG_BOTTOM    = (24,  44, 68)    # slightly lighter navy — bottom
HEADER_BG    = (255, 91, 20)    # brand orange (#FF5B14)
HEADER_TEXT  = (255, 255, 255)
ACCENT_BAR   = (255, 91, 20)    # orange left stripe per event
TEXT_TITLE   = (255, 255, 255)  # white
TEXT_META    = (148, 163, 184)  # slate-400 — readable on dark
TEXT_DATE    = (255, 200, 150)  # warm light-orange for date
TEXT_PRICE_FREE  = (52, 211, 153)  # emerald — "Free"
TEXT_PRICE_PAID  = (251, 191, 36)  # amber — paid events
SEPARATOR    = (30,  58,  95)   # dark blue-grey, subtle
FOOTER_TEXT  = (71, 100, 130)   # muted blue

# ── Geometry ───────────────────────────────────────────────────────────────────
CARD_SIZE     = 1080
PAD_H         = 64       # left/right margin
HEADER_H      = 110      # height of orange header band
FOOTER_H      = 44       # reserved at bottom
ACCENT_W      = 5        # width of left orange bar per event
ACCENT_GAP    = 14       # gap between accent bar and text

# ── Font sizes ─────────────────────────────────────────────────────────────────
HEADER_TITLE_SIZE = 52
HEADER_SUB_SIZE   = 29
EVENT_TITLE_SIZE  = 36
EVENT_META_SIZE   = 27
FOOTER_SIZE       = 20

MAX_EVENTS = 7


def create_event_card(
    events: List[dict],
    date_range: str,
    output_path: str,
) -> str:
    """Render a 1080×1080 JPEG infographic listing NL events for the week.

    Each event dict should have: title, location, date_label,
    venue (optional), price (optional), emoji (optional).
    """
    events = events[:MAX_EVENTS]

    # ── Gradient background ────────────────────────────────────────────────────
    t = np.linspace(0, 1, CARD_SIZE)
    bg = np.zeros((CARD_SIZE, CARD_SIZE, 3), dtype=np.uint8)
    for c, (top, bot) in enumerate(zip(BG_TOP, BG_BOTTOM)):
        channel = (top + (bot - top) * t).astype(np.uint8)
        bg[:, :, c] = channel[:, np.newaxis]
    img = Image.fromarray(bg)
    draw = ImageDraw.Draw(img)

    # ── Fonts ──────────────────────────────────────────────────────────────────
    def font(size: int) -> ImageFont.FreeTypeFont:
        try:
            return ImageFont.truetype(FONT_PATH, size)
        except Exception:
            return ImageFont.load_default(size=size)

    f_header_title = font(HEADER_TITLE_SIZE)
    f_header_sub   = font(HEADER_SUB_SIZE)
    f_event_title  = font(EVENT_TITLE_SIZE)
    f_event_meta   = font(EVENT_META_SIZE)
    f_footer       = font(FOOTER_SIZE)

    # ── Header band ────────────────────────────────────────────────────────────
    draw.rectangle([(0, 0), (CARD_SIZE, HEADER_H)], fill=HEADER_BG)

    header_text = "📅  THIS WEEK IN NL"
    ht_h = _line_height(f_header_title, header_text)
    hs_h = _line_height(f_header_sub, date_range)
    total_h_text = ht_h + 8 + hs_h
    hy = (HEADER_H - total_h_text) // 2

    draw.text((PAD_H, hy), header_text, font=f_header_title, fill=HEADER_TEXT)
    draw.text((PAD_H, hy + ht_h + 8), date_range, font=f_header_sub, fill=(255, 210, 175))

    # ── Event area geometry ────────────────────────────────────────────────────
    usable_h = CARD_SIZE - HEADER_H - FOOTER_H - 8
    n = len(events)
    # Distribute vertical space evenly across events
    slot_h = usable_h // max(n, 1)

    text_x = PAD_H + ACCENT_W + ACCENT_GAP

    for idx, ev in enumerate(events):
        emoji    = ev.get("emoji", "📍")
        title    = ev.get("title", "")
        venue    = ev.get("venue") or ""
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")
        price    = ev.get("price") or ""

        slot_top = HEADER_H + 8 + idx * slot_h
        # Center text block vertically within slot
        title_lines = _wrap_text(title, f_event_title, CARD_SIZE - text_x - PAD_H, max_lines=2)
        t_h = sum(_line_height(f_event_title, l) + 2 for l in title_lines)
        m_h = _line_height(f_event_meta, "A")
        block_h = t_h + 6 + m_h
        y = slot_top + (slot_h - block_h) // 2

        # Orange accent bar
        bar_top = slot_top + 10
        bar_bot = slot_top + slot_h - 10
        draw.rectangle([(PAD_H, bar_top), (PAD_H + ACCENT_W - 1, bar_bot)], fill=ACCENT_BAR)

        # Emoji
        draw.text((text_x, y), emoji, font=f_event_title, fill=TEXT_TITLE)

        # Title (after emoji with small gap)
        em_w = _text_width(f_event_title, emoji)
        title_text_x = text_x + em_w + 10
        for line in title_lines:
            draw.text((title_text_x, y), line, font=f_event_title, fill=TEXT_TITLE)
            y += _line_height(f_event_title, line) + 2

        # Meta line: venue · location · date · price
        y += 6
        meta_x = title_text_x
        meta_parts = []
        if venue:
            meta_parts.append(venue)
        if location:
            meta_parts.append(location)
        if date_lbl:
            meta_parts.append(date_lbl)

        # Draw price with color (if present), then regular meta
        if meta_parts:
            meta_str = "  ·  ".join(meta_parts)
            draw.text((meta_x, y), meta_str, font=f_event_meta, fill=TEXT_META)
            if price:
                meta_w = _text_width(f_event_meta, meta_str)
                price_color = TEXT_PRICE_FREE if price.lower() in ("free", "gratis") else TEXT_PRICE_PAID
                draw.text((meta_x + meta_w + 12, y), price, font=f_event_meta, fill=price_color)
        elif price:
            price_color = TEXT_PRICE_FREE if price.lower() in ("free", "gratis") else TEXT_PRICE_PAID
            draw.text((meta_x, y), price, font=f_event_meta, fill=price_color)

        # Row separator (skip after last)
        if idx < n - 1:
            sep_y = slot_top + slot_h - 1
            draw.line([(PAD_H + ACCENT_W + 4, sep_y), (CARD_SIZE - PAD_H, sep_y)],
                      fill=SEPARATOR, width=1)

    # ── Footer ─────────────────────────────────────────────────────────────────
    disclaimer = "⚠️  AI-assisted curation — always verify details before attending"
    footer_lines = _wrap_text(disclaimer, f_footer, CARD_SIZE - 2 * PAD_H, max_lines=2)
    footer_y = CARD_SIZE - FOOTER_H + 4

    draw.line([(PAD_H, footer_y - 6), (CARD_SIZE - PAD_H, footer_y - 6)],
              fill=SEPARATOR, width=1)
    for line in footer_lines:
        draw.text((PAD_H, footer_y), line, font=f_footer, fill=FOOTER_TEXT)
        footer_y += _line_height(f_footer, line) + 3

    img.save(output_path, "JPEG", quality=93, optimize=True)
    return output_path


# ── Helpers ────────────────────────────────────────────────────────────────────

def _line_height(font: ImageFont.FreeTypeFont, text: str) -> int:
    try:
        _, _, _, h = font.getbbox(text or "A")
        return int(h)
    except Exception:
        return font.size  # type: ignore[attr-defined]


def _text_width(font: ImageFont.FreeTypeFont, text: str) -> int:
    try:
        left, _, right, _ = font.getbbox(text or "")
        return int(right - left)
    except Exception:
        return len(text) * (font.size // 2)  # type: ignore[attr-defined]


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 2,
) -> List[str]:
    words = text.split()
    lines: List[str] = []
    current: List[str] = []

    for word in words:
        test = " ".join(current + [word])
        if _text_width(font, test) > max_width and current:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) >= max_lines:
                break
        else:
            current.append(word)

    if current and len(lines) < max_lines:
        line = " ".join(current)
        if _text_width(font, line) > max_width:
            line = line[:42].rstrip() + "…"
        lines.append(line)

    return lines or [text[:52]]
