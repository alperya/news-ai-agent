"""
Event Card Generator — PIL-based 1080×1080 infographic for Instagram feed posts.

Layout (top → bottom):
  Header bar  : "📅 THIS WEEK IN NL" (orange) + date range (grey)
  Event rows  : emoji + title (white bold) / location · date · price (grey)
  Separator   : thin grey line between rows
  Footer      : AI disclaimer (small, muted)
"""

from __future__ import annotations

from typing import List, Optional

from PIL import Image, ImageDraw, ImageFont

from .config import FONT_PATH

# ── Design tokens ──────────────────────────────────────────────────────────────

CARD_SIZE    = 1080          # square
BG_COLOR     = "#111827"     # near-black
ACCENT       = "#FF5B14"     # brand orange
TEXT_PRIMARY = "#F9E8D9"     # cream
TEXT_SECONDARY = "#9CA3AF"   # grey
SEPARATOR    = "#374151"     # dark separator line

PAD_H = 60   # horizontal margin
PAD_V = 52   # top/bottom margin

HEADER_TITLE_SIZE = 52
HEADER_SUB_SIZE   = 32
EVENT_TITLE_SIZE  = 38
EVENT_META_SIZE   = 30
FOOTER_SIZE       = 22

MAX_EVENTS = 7    # never show more than this


def create_event_card(
    events: List[dict],
    date_range: str,
    output_path: str,
) -> str:
    """
    Render a 1080×1080 JPEG infographic listing NL events for the week.

    Args:
        events:      List of dicts with keys: title, location, date_label,
                     price (optional), emoji (optional).
        date_range:  Human-readable range, e.g. "12–18 June 2025".
        output_path: Where to save the JPEG (e.g. "/tmp/event_card.jpg").

    Returns:
        output_path (for chaining).
    """
    events = events[:MAX_EVENTS]

    img = Image.new("RGB", (CARD_SIZE, CARD_SIZE), BG_COLOR)
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

    # ── Header ─────────────────────────────────────────────────────────────────
    y = PAD_V

    header_text = "📅  THIS WEEK IN NL"
    draw.text((PAD_H, y), header_text, font=f_header_title, fill=ACCENT)
    y += _line_height(draw, header_text, f_header_title) + 10

    draw.text((PAD_H, y), date_range, font=f_header_sub, fill=TEXT_SECONDARY)
    y += _line_height(draw, date_range, f_header_sub) + 24

    # Header bottom separator
    draw.line([(PAD_H, y), (CARD_SIZE - PAD_H, y)], fill=SEPARATOR, width=2)
    y += 24

    # ── Event rows ─────────────────────────────────────────────────────────────
    max_title_width = CARD_SIZE - 2 * PAD_H - 50  # leave room for emoji column

    for idx, ev in enumerate(events):
        emoji   = ev.get("emoji", "📍")
        title   = ev.get("title", "")
        location = ev.get("location", "")
        date_lbl = ev.get("date_label", "")
        price   = ev.get("price") or ""

        # Truncate / wrap title to 2 lines maximum
        title_lines = _wrap_text(title, f_event_title, max_title_width, max_lines=2)
        meta_parts = [p for p in [location, date_lbl, price] if p]
        meta_line  = "  ·  ".join(meta_parts)

        # Emoji column (fixed x)
        emoji_x = PAD_H
        emoji_y = y
        draw.text((emoji_x, emoji_y), emoji, font=f_event_title, fill=TEXT_PRIMARY)

        # Title text (indented past emoji)
        title_x = PAD_H + 52
        for line in title_lines:
            draw.text((title_x, y), line, font=f_event_title, fill=TEXT_PRIMARY)
            y += _line_height(draw, line, f_event_title) + 2

        # Meta line (location · date · price)
        y += 4
        draw.text((title_x, y), meta_line, font=f_event_meta, fill=TEXT_SECONDARY)
        y += _line_height(draw, meta_line, f_event_meta) + 16

        # Row separator (skip after last event)
        if idx < len(events) - 1:
            draw.line([(PAD_H, y), (CARD_SIZE - PAD_H, y)], fill=SEPARATOR, width=1)
            y += 18

    # ── Footer ─────────────────────────────────────────────────────────────────
    disclaimer = (
        "⚠️  AI-assisted curation — verify details before attending. Events may change."
    )
    footer_lines = _wrap_text(disclaimer, f_footer, CARD_SIZE - 2 * PAD_H, max_lines=2)
    footer_y = CARD_SIZE - PAD_V - len(footer_lines) * (_line_height(draw, "A", f_footer) + 4)

    # Subtle top border for footer zone
    draw.line([(PAD_H, footer_y - 14), (CARD_SIZE - PAD_H, footer_y - 14)],
              fill=SEPARATOR, width=1)

    for line in footer_lines:
        draw.text((PAD_H, footer_y), line, font=f_footer, fill=TEXT_SECONDARY)
        footer_y += _line_height(draw, line, f_footer) + 4

    img.save(output_path, "JPEG", quality=92, optimize=True)
    return output_path


# ── Helpers ────────────────────────────────────────────────────────────────────

def _line_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    try:
        _, _, _, h = font.getbbox(text or "A")
        return int(h)
    except Exception:
        return font.size  # type: ignore[attr-defined]


def _wrap_text(
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int = 2,
) -> List[str]:
    """Word-wrap text to fit within max_width pixels, capped at max_lines lines."""
    words = text.split()
    lines: List[str] = []
    current: List[str] = []

    for word in words:
        test = " ".join(current + [word])
        try:
            left, _, right, _ = font.getbbox(test)
            w = right - left
        except Exception:
            w = len(test) * (font.size // 2)  # type: ignore[attr-defined]

        if w > max_width and current:
            lines.append(" ".join(current))
            current = [word]
            if len(lines) >= max_lines:
                break
        else:
            current.append(word)

    if current and len(lines) < max_lines:
        line = " ".join(current)
        # Ellipsize if still too wide on last line
        try:
            left, _, right, _ = font.getbbox(line)
            if (right - left) > max_width:
                line = line[:40].rstrip() + "…"
        except Exception:
            pass
        lines.append(line)

    return lines or [text[:50]]
