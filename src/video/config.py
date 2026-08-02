"""
Video package — Configuration & Constants.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # pick up .env when running locally (no-op in Lambda)

# ── Video dimensions (Instagram Reels: 9:16 portrait) ────────────────────────

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30

# ── TTS — ElevenLabs (primary) ───────────────────────────────────────────────
#  Free: 10 k credits/month (~10 min).  Starter $5/month: 30 k (~30 min).
#  30-35 posts × ~40 s narration ≈ 20-25 min → Starter covers it comfortably.

ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.environ.get(
    "ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9",  # Daniel — calm, professional
)
ELEVENLABS_MODEL = "eleven_multilingual_v2"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_SPEED = 1.0  # 0.7–1.2; 1.0 = normal, lower = slower/calmer narration

# ── TTS — edge-tts fallback (free, $0) ───────────────────────────────────────

# English voice — the narration is English. This was "tr-TR-AhmetNeural" (a
# leftover from the project's early Turkish-content phase), which meant any
# ElevenLabs outage published English news read by a Turkish voice.
EDGE_TTS_VOICE = "en-US-ChristopherNeural"
EDGE_TTS_RATE = "-15%"
EDGE_TTS_PITCH = "+2Hz"

# ── Audio ─────────────────────────────────────────────────────────────────────

BG_MUSIC_VOLUME = 0.24  # background music loudness (0.0–1.0)

# ── Colours ───────────────────────────────────────────────────────────────────

SUBTITLE_BG_COLOR = "#FF5B14"    # orange subtitle background
SUBTITLE_TEXT_COLOR = "#F9E8D9"  # cream / beige subtitle text
SUBTITLE_SAFE_MARGIN = 40        # px margin from video edge for subtitle boxes

# ── Font ──────────────────────────────────────────────────────────────────────

SUBTITLE_FONT_SIZE = 62
HOOK_FONT_SIZE = 68

# Bundled fonts (OFL license)
_FONTS_DIR = Path(__file__).parent.parent / "fonts"
_LAMBDA_DIR = Path(__file__).parent.parent  # flat Lambda layout

_SYSTEM_FALLBACKS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _find_font_file(name: str) -> str:
    candidates = [
        _FONTS_DIR / name,
        _LAMBDA_DIR / name,
    ] + [Path(p) for p in _SYSTEM_FALLBACKS]
    for f in candidates:
        if f.exists():
            return str(f)
    return "Helvetica"


# Poppins (primary — used for event cards & influencer-style posts)
FONT_PATH          = _find_font_file("Poppins-Bold.ttf")
FONT_SEMIBOLD_PATH = _find_font_file("Poppins-SemiBold.ttf")
FONT_REGULAR_PATH  = _find_font_file("Poppins-Regular.ttf")

# Montserrat (used for Reels subtitles / hook overlays)
FONT_MONTSERRAT_PATH = _find_font_file("Montserrat-Bold.ttf")

# NotoEmoji — emoji fallback for PIL rendering on Lambda (no system emoji font)
FONT_EMOJI_PATH = _find_font_file("NotoEmoji-Regular.ttf")

# ── Paths ─────────────────────────────────────────────────────────────────────

_VIDEO_PKG_DIR = Path(__file__).parent     # src/video/
_SRC_DIR = _VIDEO_PKG_DIR.parent           # src/
MUSIC_FILE = _SRC_DIR / "music" / "news_music.mp3"
CALM_MUSIC_FILE = _SRC_DIR / "music" / "calm_music.mp3"
ALTERNATIVE_MUSIC_FILE = _SRC_DIR / "music" / "alternative_music.mp3"
EVENTS_MUSIC_FILE = _SRC_DIR / "music" / "events_music.mp3"
SAD_MUSIC_FILE = _SRC_DIR / "music" / "sad_music.mp3"
# Daily "Did you know?" Dutch-fact story — upbeat lifestyle background track
FACT_STORY_MUSIC = _SRC_DIR / "music" / "story_dutch_lifestyle_30sec.mp3"

# Brand logo — used on the weekly fact-carousel closing slide.
_LOGO_DIR = _SRC_DIR / "logo"


def brand_logo_path() -> str:
    """Resolve the brand logo: ``BRAND_LOGO_PATH`` env wins, else the
    highest-resolution PNG bundled under ``src/logo/`` (so dropping in a
    higher-res version is picked up automatically). Returns ``""`` if none
    (renderer draws a text wordmark instead). Resolved at call time so env set at
    startup is honoured.
    """
    env = os.environ.get("BRAND_LOGO_PATH")
    if env and Path(env).exists():
        return env
    if not _LOGO_DIR.exists():
        return ""
    pngs = sorted(_LOGO_DIR.glob("*.[pP][nN][gG]"))
    if not pngs:
        return ""

    # Prefer a purpose-made badge (transparent corners: squircle/round) over the
    # raw square source when present.
    badges = [p for p in pngs if any(k in p.stem.lower() for k in ("squircle", "round"))]
    candidates = badges or pngs

    def _width(p: Path) -> int:
        try:
            from PIL import Image
            with Image.open(p) as im:
                return im.width
        except Exception:
            return 0

    return str(max(candidates, key=_width))


def reading_seconds(
    text: str,
    wps: float = 2.5,
    min_s: float = 5.0,
    max_s: float = 15.0,
    buffer: float = 1.0,
) -> float:
    """Estimate how long a viewer needs to read *text* on screen.

    Used to size the daily fact story so it's exactly long enough to read
    (short → higher story completion rate). ``wps`` = words read per second
    (2.5 ≈ deliberate on-screen reading, slower than silent prose).
    """
    words = len(text.split())
    seconds = words / wps + buffer
    return max(min_s, min(max_s, seconds))

# ── Stock footage (Pexels — free API) ─────────────────────────────────────────

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_IMAGE_SEARCH_URL = "https://api.pexels.com/v1/search"
STOCK_CLIP_COUNT = 9
PEXELS_PER_PAGE = 20       # request extra, pick best N
MIN_CLIP_DURATION = 3.0    # seconds — minimum per scene

# ── Ken Burns ─────────────────────────────────────────────────────────────────

KB_ZOOM_RANGE = (1.0, 1.25)
