"""
Weekly Dutch-fact carousel cards (static PIL images, no video/audio).

Renders the facts shown in Stories over the week into a 4:5 (1080×1350) feed
carousel — Instagram's tallest feed ratio for maximum reach. Carousels are a
discovery + save surface (non-followers see them on the feed/Explore, and
educational "collection" posts are highly saveable).

Each fact card sits on a relevant **Pexels photo background** (fetched from the
fact's ``footage_queries`` — the same visual source the daily Story video uses)
darkened with a scrim so the text stays legible; if no image is available it
falls back to a solid brand background. Design mirrors the daily-fact Story
palette (orange #FF5B14 accent, cream #F9E8D9 text). Kept emoji-free on purpose
— PIL can't render colour/flag emoji, so those live only in the caption.

Slides:
  • Slide 1 — brand heading ("THIS WEEK IN DUTCH FACTS") ABOVE the first
    "DID YOU KNOW?" fact (the most-seen slide brands the set *and* delivers
    content, not just a title card).
  • Slides 2..N — one fact each ("DID YOU KNOW?" eyebrow + fact body).
  • Final slide — a special image + the brand logo (or a wordmark fallback) and
    a "Follow" call-to-action. No @handle on any card — Instagram already shows
    the account at the top of the post.
"""

import logging
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import (
    FONT_PATH,
    FONT_SEMIBOLD_PATH,
    FONT_REGULAR_PATH,
    SUBTITLE_BG_COLOR,
    SUBTITLE_TEXT_COLOR,
)

logger = logging.getLogger(__name__)

# 4:5 feed ratio (tallest allowed → most screen real estate / reach)
CARD_WIDTH = 1080
CARD_HEIGHT = 1350

_BG_COLOR = (23, 19, 16)            # warm near-black fallback when no photo
_MUTED_COLOR = (210, 200, 192)      # page number
_SCRIM_ALPHA = 160                  # 0–255 black overlay over photos for legibility
_CTA_SCRIM_ALPHA = 175
_BRAND_HEADING = "THIS WEEK IN DUTCH FACTS"
_EYEBROW = "DID YOU KNOW?"
_WORDMARK = "DUTCH DAILY"
_PAD_X = 90


def _hex_to_rgb(value: str) -> tuple:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


_ORANGE = _hex_to_rgb(SUBTITLE_BG_COLOR)
_CREAM = _hex_to_rgb(SUBTITLE_TEXT_COLOR)


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default(size=size)


def _background(bg_path: str, scrim_alpha: int) -> Image.Image:
    """Cover-fit the photo to 4:5 and darken it; solid fallback if unavailable."""
    base = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _BG_COLOR)
    if bg_path:
        try:
            with Image.open(bg_path) as im:
                photo = ImageOps.fit(im.convert("RGB"), (CARD_WIDTH, CARD_HEIGHT), centering=(0.5, 0.5))
            base = photo
        except Exception as e:
            logger.warning(f"⚠️  Could not load carousel background {bg_path}: {e}")
    base = base.convert("RGBA")
    scrim = Image.new("RGBA", (CARD_WIDTH, CARD_HEIGHT), (0, 0, 0, scrim_alpha))
    return Image.alpha_composite(base, scrim)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> list:
    """Greedy word-wrap to a pixel width (same approach as effects.make_fact_overlay)."""
    words = text.split()
    lines: list = []
    current: list = []
    for word in words:
        test = " ".join(current + [word])
        left, _, right, _ = font.getbbox(test)
        if (right - left) > max_w and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines


def _draw_centered(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
                   y: int, fill: tuple, shadow: bool = True) -> None:
    """Draw horizontally-centered text with a soft shadow for legibility on photos."""
    left, top, right, bottom = font.getbbox(text)
    x = (CARD_WIDTH - (right - left)) // 2
    if shadow:
        draw.text((x + 3, y - top + 3), text, font=font, fill=(0, 0, 0, 160))
    draw.text((x, y - top), text, font=font, fill=fill)


def _draw_block(draw: ImageDraw.ImageDraw, lines: list, font: ImageFont.FreeTypeFont,
                y: int, fill: tuple, line_gap: int) -> int:
    _, top, _, bottom = font.getbbox("Ay")
    line_h = (bottom - top) + line_gap
    for line in lines:
        _draw_centered(draw, line, font, y, fill)
        y += line_h
    return len(lines) * line_h


def _page_number(draw: ImageDraw.ImageDraw, page: str) -> None:
    if not page:
        return
    small = _font(FONT_REGULAR_PATH, 34)
    left, top, right, _ = small.getbbox(page)
    x = CARD_WIDTH - _PAD_X - (right - left)
    y = CARD_HEIGHT - 70
    draw.text((x + 2, y - top + 2), page, font=small, fill=(0, 0, 0, 160))
    draw.text((x, y - top), page, font=small, fill=_MUTED_COLOR)


def _render_fact_card(fact_text: str, page: str, bg_path: str, brand_heading: bool) -> Image.Image:
    img = _background(bg_path, _SCRIM_ALPHA)
    draw = ImageDraw.Draw(img)

    body_font = _font(FONT_PATH, 66)
    eyebrow_font = _font(FONT_SEMIBOLD_PATH, 42)
    heading_font = _font(FONT_PATH, 58)

    max_w = CARD_WIDTH - 2 * _PAD_X
    body_lines = _wrap(draw, fact_text, body_font, max_w)

    _, btop, _, bbottom = body_font.getbbox("Ay")
    body_line_h = (bbottom - btop) + 20
    body_h = len(body_lines) * body_line_h

    _, etop, _, ebottom = eyebrow_font.getbbox(_EYEBROW)
    eyebrow_h = (ebottom - etop) + 40

    heading_h = 0
    heading_lines = []
    if brand_heading:
        heading_lines = _wrap(draw, _BRAND_HEADING, heading_font, max_w)
        _, htop, _, hbottom = heading_font.getbbox("Ay")
        heading_line_h = (hbottom - htop) + 12
        heading_h = len(heading_lines) * heading_line_h + 60

    block_h = heading_h + eyebrow_h + body_h
    y = (CARD_HEIGHT - block_h) // 2

    if brand_heading:
        y += _draw_block(draw, heading_lines, heading_font, y, _CREAM, 12)
        y += 60

    _draw_centered(draw, _EYEBROW, eyebrow_font, y, _ORANGE)
    y += eyebrow_h

    _draw_block(draw, body_lines, body_font, y, _CREAM, 20)

    _page_number(draw, page)
    return img


# Target on-card logo width. The source is never upscaled past its native width
# (small logos stay crisp but smaller; a hi-res logo renders at the full target).
_LOGO_TARGET_W = 400


def _load_logo(logo_path: str):
    """Return an RGBA logo sized to <= _LOGO_TARGET_W (no upscaling), or None."""
    if not (logo_path and Path(logo_path).exists()):
        return None
    try:
        with Image.open(logo_path) as raw:
            logo = raw.convert("RGBA")
    except Exception as e:
        logger.warning(f"⚠️  Could not load logo {logo_path}: {e}")
        return None
    if logo.width > _LOGO_TARGET_W:
        h = int(logo.height * _LOGO_TARGET_W / logo.width)
        logo = logo.resize((_LOGO_TARGET_W, h), Image.LANCZOS)
    return logo


def _render_cta_card(bg_path: str, logo_path: str) -> Image.Image:
    """Closing slide — special image, the brand logo (or wordmark), and a follow CTA."""
    img = _background(bg_path, _CTA_SCRIM_ALPHA)
    draw = ImageDraw.Draw(img)

    big = _font(FONT_PATH, 72)
    max_w = CARD_WIDTH - 2 * _PAD_X
    lines = _wrap(draw, "Follow for your daily dose of Dutch facts", big, max_w)
    _, top, _, bottom = big.getbbox("Ay")
    line_h = (bottom - top) + 18
    text_block_h = len(lines) * line_h

    brand_gap = 70
    logo = _load_logo(logo_path)
    wordmark_font = None
    if logo is not None:
        brand_h = logo.height
    else:
        wordmark_font = _font(FONT_PATH, 92)
        _, wtop, _, wbottom = wordmark_font.getbbox("Ay")
        brand_h = wbottom - wtop

    # Centre the [brand + gap + text] block as a whole.
    total_h = brand_h + brand_gap + text_block_h
    top_y = (CARD_HEIGHT - total_h) // 2

    if logo is not None:
        img.alpha_composite(logo, ((CARD_WIDTH - logo.width) // 2, top_y))
    else:
        _draw_centered(draw, _WORDMARK, wordmark_font, top_y, _CREAM)

    _draw_block(draw, lines, big, top_y + brand_h + brand_gap, _CREAM, 18)
    return img


def render_fact_carousel(facts: list, out_dir, bg_paths: list = None,
                         cta_bg_path: str = "", logo_path: str = "") -> list:
    """Render weekly facts into 4:5 carousel PNGs. Returns the written paths in order.

    ``facts``      — ``[{"text": ...}, ...]`` oldest→newest (from
                     ``dutch_facts.get_weekly_facts``).
    ``bg_paths``   — optional per-fact background image paths (parallel to
                     ``facts``); a missing/None entry falls back to a solid card.
    ``cta_bg_path``— optional special background for the closing follow slide.
    ``logo_path``  — optional brand logo for the closing slide (else a wordmark).

    Produces fact slides (slide 1 branded) + a follow CTA. Total slides =
    len(facts) + 1, capped at Instagram's 10-image carousel limit.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    max_fact_slides = 9  # + 1 CTA = 10 (Instagram carousel limit)
    facts = facts[:max_fact_slides]
    bg_paths = (bg_paths or [])[:max_fact_slides]
    total = len(facts)

    paths: list = []
    for i, fact in enumerate(facts):
        bg = bg_paths[i] if i < len(bg_paths) else None
        card = _render_fact_card(
            fact_text=fact["text"],
            page=f"{i + 1}/{total}",
            bg_path=bg,
            brand_heading=(i == 0),
        )
        p = out / f"card_{i + 1:02d}.png"
        card.convert("RGB").save(p, "PNG")
        paths.append(p)

    cta = _render_cta_card(cta_bg_path, logo_path)
    cta_path = out / f"card_{total + 1:02d}.png"
    cta.convert("RGB").save(cta_path, "PNG")
    paths.append(cta_path)

    logger.info(f"🖼️  Rendered {len(paths)} carousel cards → {out}")
    return paths
