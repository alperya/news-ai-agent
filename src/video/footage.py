"""
Video package — Stock Footage Fetcher (Pexels API).

Searches Pexels for royalty-free video clips matching the news topic.
Falls back to downloading the article's static image for Ken Burns.

Cost: $0/month  (Pexels API is free — https://www.pexels.com/api/)
"""

import logging
import os
import re
from typing import List, Optional

import requests as http_requests
from PIL import Image

from .config import (
    PEXELS_API_KEY,
    PEXELS_IMAGE_SEARCH_URL,
    PEXELS_PER_PAGE,
    PEXELS_VIDEO_SEARCH_URL,
    STOCK_CLIP_COUNT,
)

logger = logging.getLogger(__name__)

# ── Dutch → English keyword map for better Pexels results ────────────────────

_NL_EN = {
    "nederland": "netherlands", "turkije": "turkey", "duitsland": "germany",
    "frankrijk": "france", "engeland": "england", "europa": "europe",
    "fiets": "cycling bicycle", "aardbeving": "earthquake",
    "overstroming": "flood", "oorlog": "war", "vrede": "peace",
    "economie": "economy finance", "gezondheid": "health hospital",
    "onderwijs": "education school", "technologie": "technology",
    "regering": "government parliament", "verkiezing": "election voting",
    "klimaat": "climate weather", "energie": "energy solar wind",
    "politie": "police", "rechtbank": "court justice",
    "voetbal": "football soccer", "sport": "sports",
    "vervoer": "transport traffic", "infrastructuur": "infrastructure road",
    "migratie": "migration immigration", "milieu": "environment nature",
    "landbouw": "agriculture farm", "woning": "housing building",
    "luchtvaart": "aviation airplane", "zee": "sea ocean",
    "bos": "forest fire", "nucleair": "nuclear energy",
}

_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "are", "was", "were", "be", "been",
    "has", "have", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "not", "no", "nor", "so",
    "if", "then", "than", "that", "this", "these", "those", "it", "its",
    "as", "up", "out", "about", "into", "over", "after", "before", "new",
    "also", "more", "very", "most", "all", "some", "any", "each", "every",
}


# ── Public API ────────────────────────────────────────────────────────────────

def extract_search_query(title: str, content: str = "") -> str:
    """Extract English search keywords from title/content for Pexels."""
    text = f"{title} {content}".lower()
    words = re.findall(r"[a-z]+", text)

    english: List[str] = []
    for w in words:
        if w in _STOP_WORDS or len(w) < 3:
            continue
        if w in _NL_EN:
            english.append(_NL_EN[w])
        elif len(w) > 4:
            english.append(w)

    if not english:
        english = [w for w in words if w not in _STOP_WORDS and len(w) > 4][:3]
    if not english:
        english = ["news", "breaking"]

    return " ".join(english[:4])


def fetch_stock_clips(
    title: str,
    content: str,
    tmp_dir: str,
    count: int = STOCK_CLIP_COUNT,
    footage_queries: Optional[List[str]] = None,
    avoid_terms: Optional[List[str]] = None,
    headline: str = "",
    ai_agent=None,
) -> List[str]:
    """Fetch stock video clips from Pexels matching the news topic.

    When *footage_queries* are provided (AI-generated, most-specific first)
    each query is tried in order until enough clips are found. Falls back to
    keyword-extracted query if all AI queries return too few results.

    *avoid_terms* and *ai_agent* enable thumbnail validation: before
    downloading, Haiku vision checks each thumbnail against the headline and
    skips misleading clips (wrong weather, wrong landmark, etc.).

    Returns list of local file paths (up to *count*).
    """
    api_key = PEXELS_API_KEY or os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        logger.info("ℹ️  PEXELS_API_KEY not set — skipping stock footage")
        return []

    # Build query list: AI-generated first, keyword fallback last
    fallback_query = extract_search_query(title, content)
    queries = list(footage_queries) + [fallback_query] if footage_queries else [fallback_query]

    for query in queries:
        paths = _fetch_clips_for_query(
            api_key, query, tmp_dir, count,
            headline=headline or title,
            avoid_terms=avoid_terms,
            ai_agent=ai_agent,
        )
        if paths:
            return paths

    logger.warning("⚠️  No Pexels video results for any query")
    return []


def _fetch_clips_for_query(
    api_key: str,
    query: str,
    tmp_dir: str,
    count: int,
    headline: str = "",
    avoid_terms: Optional[List[str]] = None,
    ai_agent=None,
) -> List[str]:
    """Try a single query and return downloaded clip paths (empty list if insufficient)."""
    logger.info(f"🔍 Searching Pexels videos for: '{query}'")

    # Prefer portrait clips for 9:16 Reels
    videos = _pexels_video_search(api_key, query, per_page=PEXELS_PER_PAGE, orientation="portrait")

    # Any orientation if portrait yields too few
    if len(videos) < count:
        extra = _pexels_video_search(api_key, query, per_page=PEXELS_PER_PAGE)
        seen_ids = {v.get("id") for v in videos}
        videos.extend(v for v in extra if v.get("id") not in seen_ids)

    if not videos:
        return []

    # Thumbnail validation: filter out misleading clips before downloading
    if ai_agent and headline and videos:
        thumbnail_urls = [v["image"] for v in videos if v.get("image")]
        if thumbnail_urls:
            validation = ai_agent.validate_footage_thumbnails(headline, avoid_terms or [], thumbnail_urls)
            # zip against original videos list (thumbnails may be fewer if some had no image)
            videos_with_thumbs = [v for v in videos if v.get("image")]
            videos_without_thumbs = [v for v in videos if not v.get("image")]
            passed = [v for v, ok in zip(videos_with_thumbs, validation) if ok]
            # safety net: if everything got rejected, fall back to top 2 unvalidated
            if len(passed) < 2:
                logger.warning("⚠️  Thumbnail validation rejected all clips — using top 2 unvalidated")
                passed = videos[:2]
            videos = passed + videos_without_thumbs

    paths: List[str] = []
    for idx, video in enumerate(videos):
        if len(paths) >= count:
            break
        clip_path = _download_clip(video, idx, tmp_dir)
        if clip_path:
            paths.append(clip_path)

    logger.info(f"📥 Downloaded {len(paths)}/{count} clips for '{query}'")
    return paths


def _pexels_video_search(
    api_key: str, query: str, per_page: int = 15, orientation: str = "",
) -> list:
    """Search Pexels videos API. Returns list of video dicts."""
    try:
        params: dict = {"query": query, "per_page": per_page}
        if orientation:
            params["orientation"] = orientation
        resp = http_requests.get(
            PEXELS_VIDEO_SEARCH_URL,
            headers={"Authorization": api_key},
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("videos", [])
    except Exception as e:
        logger.warning(f"⚠️  Pexels video search error: {e}")
        return []


def download_image(url: Optional[str], tmp_dir: str) -> Optional[str]:
    """Download news article image.  Returns local path or None."""
    if not url:
        return None
    try:
        resp = http_requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
                "Referer": url.split("/")[0] + "//" + url.split("/")[2] + "/",
            },
            allow_redirects=True,
        )
        resp.raise_for_status()
        img_path = os.path.join(tmp_dir, "news_image.jpg")
        with open(img_path, "wb") as f:
            f.write(resp.content)
        with Image.open(img_path) as img:
            img.verify()
        return img_path
    except Exception as e:
        logger.warning(f"⚠️  Could not download image: {e}")
        return None


def fetch_stock_image(
    title: str, content: str, tmp_dir: str,
) -> Optional[str]:
    """Search Pexels for a single stock PHOTO matching the news topic.

    Returns local path to downloaded image, or None.
    Useful as a Ken Burns fallback when video clips aren't available.
    """
    api_key = PEXELS_API_KEY or os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None

    query = extract_search_query(title, content)
    logger.info(f"🖼️  Searching Pexels images for: '{query}'")

    try:
        resp = http_requests.get(
            PEXELS_IMAGE_SEARCH_URL,
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 1, "orientation": "portrait"},
            timeout=10,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            logger.warning(f"⚠️  No Pexels images for '{query}'")
            return None

        img_url = photos[0]["src"].get("large2x") or photos[0]["src"]["large"]
        return download_image(img_url, tmp_dir)
    except Exception as e:
        logger.warning(f"⚠️  Pexels image search error: {e}")
        return None


# ── Internal ──────────────────────────────────────────────────────────────────

def _download_clip(
    video_data: dict, idx: int, tmp_dir: str,
) -> Optional[str]:
    """Download a single Pexels video clip.

    Strictly caps at 1080×1920 — 4K is never downloaded because
    Instagram Reels downscales to 1080×1920 anyway, and 4K
    clips consume too much Lambda RAM.
    """
    video_files = video_data.get("video_files", [])
    if not video_files:
        return None

    mp4s = [vf for vf in video_files if vf.get("file_type") == "video/mp4"]
    if not mp4s:
        mp4s = video_files

    # Hard cap: discard anything above 1920 in either dimension
    mp4s = [
        vf for vf in mp4s
        if vf.get("height", 0) <= 1920 and vf.get("width", 0) <= 1920
    ]
    if not mp4s:
        logger.info(f"   ⏭️  clip {idx}: skipped (only 4K available)")
        return None

    # Sort by resolution descending — pick the best within our cap
    mp4s.sort(
        key=lambda v: v.get("height", 0) * v.get("width", 0), reverse=True,
    )
    chosen = mp4s[0]  # highest within ≤1920 cap

    url = chosen.get("link")
    if not url:
        return None

    try:
        resp = http_requests.get(url, timeout=60, stream=True)
        resp.raise_for_status()
        path = os.path.join(tmp_dir, f"stock_{idx}.mp4")
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
        logger.info(
            f"   📥 clip {idx}: {chosen.get('width')}×{chosen.get('height')}"
        )
        return path
    except Exception as e:
        logger.warning(f"⚠️  Failed to download clip {idx}: {e}")
        return None
