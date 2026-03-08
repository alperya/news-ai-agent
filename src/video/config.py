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

# ── TTS — edge-tts fallback (free, $0) ───────────────────────────────────────

EDGE_TTS_VOICE = "tr-TR-AhmetNeural"
EDGE_TTS_RATE = "-15%"
EDGE_TTS_PITCH = "+2Hz"

# ── Audio ─────────────────────────────────────────────────────────────────────

BG_MUSIC_VOLUME = 0.24  # background music loudness (0.0–1.0)

# ── Colours ───────────────────────────────────────────────────────────────────

SUBTITLE_BG_COLOR = "#FF5B14"    # orange subtitle background
SUBTITLE_TEXT_COLOR = "#F9E8D9"  # cream / beige subtitle text
SUBTITLE_SAFE_MARGIN = 40        # px margin from video edge for subtitle boxes

# ── Font ──────────────────────────────────────────────────────────────────────

SUBTITLE_FONT_SIZE = 48

# Bundled Montserrat Bold (OFL license) — fallback to system fonts
_BUNDLED_FONT = Path(__file__).parent.parent / "fonts" / "Montserrat-Bold.ttf"
_LAMBDA_FONT = Path(__file__).parent.parent / "Montserrat-Bold.ttf"  # flat Lambda layout
_SYSTEM_FONTS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
]


def _find_font() -> str:
    for f in [_BUNDLED_FONT, _LAMBDA_FONT] + [Path(p) for p in _SYSTEM_FONTS]:
        if f.exists():
            return str(f)
    return "Helvetica"


FONT_PATH = _find_font()

# ── Paths ─────────────────────────────────────────────────────────────────────

_VIDEO_PKG_DIR = Path(__file__).parent     # src/video/
_SRC_DIR = _VIDEO_PKG_DIR.parent           # src/
MUSIC_FILE = _SRC_DIR / "music" / "news_music.mp3"

# ── Stock footage (Pexels — free API) ─────────────────────────────────────────

PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
PEXELS_VIDEO_SEARCH_URL = "https://api.pexels.com/videos/search"
PEXELS_IMAGE_SEARCH_URL = "https://api.pexels.com/v1/search"
STOCK_CLIP_COUNT = 9
PEXELS_PER_PAGE = 20       # request extra, pick best N
MIN_CLIP_DURATION = 3.0    # seconds — minimum per scene

# ── Ken Burns ─────────────────────────────────────────────────────────────────

KB_ZOOM_RANGE = (1.0, 1.25)
