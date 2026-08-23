"""
Video package — Stock Footage Fetcher (Pexels API).

Searches Pexels for royalty-free video clips matching the news topic.
Falls back to downloading the article's static image for Ken Burns.

Cost: $0/month  (Pexels API is free — https://www.pexels.com/api/)
"""

import logging
import os
import re
from typing import List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests as http_requests
from PIL import Image

from footage_geo import (
    NATIONAL_ANCHOR_QUERIES,
    banned_places_for,
    claims_a_place,
    claims_identifiable_framing,
    sanitize_query,
    shows_impossible_terrain,
    slug_banned_places_for,
)

from .config import (
    PEXELS_API_KEY,
    PEXELS_IMAGE_SEARCH_URL,
    PEXELS_PER_PAGE,
    PEXELS_VIDEO_SEARCH_URL,
    STOCK_CLIP_COUNT,
)

logger = logging.getLogger(__name__)

# How many queries feed the pooled candidate search before escalating.
POOL_QUERIES = 3
# Thumbnails sent to the vision gate in one call.
#
# Kept at 15 (not lowered to ~10) on purpose: the gate legitimately rejects a
# large share of candidates, and a thinner batch pushes the search into the
# escalation ladder more often — which costs an EXTRA vision call, wiping out
# the saving. The cost lever is thumbnail size below, not batch size.
#
# Must stay <= ai_agent.VISION_BATCH_LIMIT; tests/test_footage_geo.py asserts it.
VALIDATION_BATCH = 15

# Width, in pixels, of the thumbnails handed to the vision gate.
#
# Pexels' `image` field is a full-size preview JPEG. Sent at native size these
# are the single largest input in the whole pipeline: 15 images per call, up to
# 2 calls per run, 2 runs a day. On the high-resolution tier an image can cost
# ~4,784 tokens, so the thumbnails alone can dominate the daily bill — far more
# than the choice of model does.
#
# The gate answers four yes/no questions: is this the story's subject, does it
# name a place, could it be the Netherlands, does it hit the avoid list. None of
# those need a 1920px image; 640px is comfortably enough to read a skyline, a
# mountain or a fireplace. This is a cost change, not a quality trade.
VISION_THUMBNAIL_WIDTH = 640
# Per-candidate decisions persisted into posts_*.json. Separate budgets: a run
# whose rejections fill one shared cap used to record NO used clips at all, so
# the audit trail went silent in exactly the runs worth auditing.
AUDIT_USED_LIMIT = 12
AUDIT_REJECT_LIMIT = 25

# Town-free by construction — the final tier when every real query has been
# filtered out. Not neutral though: "abstract light bokeh" is the worst a news
# Reel can look, so the rescue tier promises the *country* instead. A national
# term can never become a wrong-town claim.
_LAST_RESORT_QUERIES = NATIONAL_ANCHOR_QUERIES

def thumbnail_for_vision(url: str, width: int = VISION_THUMBNAIL_WIDTH) -> str:
    """Ask Pexels for a smaller preview before sending it to the vision gate.

    Pexels serves resized variants of its own images through query parameters,
    so this costs one URL rewrite and no download, no re-encode and no extra
    request from our side — the model fetches the smaller image directly.

    Non-Pexels hosts are returned untouched: an unknown CDN may not honour these
    parameters, and a URL that 404s would fail the clip rather than shrink it.
    Only `images.pexels.com` is rewritten, and existing parameters are merged
    rather than replaced so any signing or format hints survive.
    """
    if not url:
        return url
    try:
        parsed = urlparse(url)
        if not parsed.netloc.endswith("pexels.com"):
            return url
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        params.update({"auto": "compress", "cs": "tinysrgb", "w": str(width), "dpr": "1"})
        # A stale `h` from the original URL would fight the new width and can
        # produce an odd crop; width alone preserves aspect ratio.
        params.pop("h", None)
        return urlunparse(parsed._replace(query=urlencode(params)))
    except Exception:  # a malformed URL must not lose the clip
        return url


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
    exclude_ids: Optional[Set[int]] = None,
    used_ids_out: Optional[List[int]] = None,
    footage_plan: Optional[dict] = None,
    audit_out: Optional[List[dict]] = None,
) -> List[str]:
    """Fetch stock video clips from Pexels matching the news topic.

    Candidates from the first ``POOL_QUERIES`` queries are **pooled** (search is
    free and fast) so one narrow query can't win the whole Reel on a single weak
    hit. Only downloads — the expensive part — are limited to the survivors.

    Filtering runs in three widening stages, cheapest first:
      1. slug pre-filter — drop clips whose Pexels URL names a place the story
         cannot claim, or describes terrain the Netherlands does not have
         (free, no API call);
      2. fresh-first reorder of *exclude_ids* (recently used clips to the back);
      3. Haiku vision gate via *ai_agent* + *avoid_terms*.

    When too few clips survive, it escalates to the remaining (more generic)
    queries and finally to `_LAST_RESORT_QUERIES` — never to clips the gate
    rejected. Returning ``[]`` is a valid outcome: the caller then degrades to a
    stock photo or a gradient, which beats showing the wrong place.

    *footage_plan* carries the story's place and ``place_mode`` (see
    :mod:`footage_geo`). *audit_out* collects one dict per considered candidate
    so the decision is reviewable after publish.

    Returns list of local file paths (up to *count*).
    """
    api_key = PEXELS_API_KEY or os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        logger.info("ℹ️  PEXELS_API_KEY not set — skipping stock footage")
        return []

    plan = footage_plan or {}
    place_mode = plan.get("place_mode", "none")
    place = plan.get("place", "")
    banned = banned_places_for(place, place_mode)
    # The slug set is wider than the query set and also active for stock_ok
    # stories: a Schiphol story may ask for "schiphol airport", but a clip
    # slugged "kansai-airport" is still the wrong airport.
    slug_banned = slug_banned_places_for(place, place_mode)

    # Keyword fallback is derived from the *Dutch* headline: it keeps place
    # names ("deventer") — so it is sanitized like any AI query — and it keeps
    # untranslated Dutch words, which Pexels simply does not understand. A live
    # run really searched 'uitzonderlijke situatie droogte brand' and scored the
    # results as if they were about drought. So it is a *replacement* for the AI
    # queries, never an addition to them.
    queries = [q for q in (footage_queries or []) if q]
    if not queries:
        fallback_query = extract_search_query(title, content)
        if place_mode == "no_stock":
            fallback_query = sanitize_query(fallback_query, banned)
        queries = [fallback_query] if fallback_query else []
    if not queries:
        # Straight to the neutral tier — and don't list it twice below.
        queries = list(_LAST_RESORT_QUERIES)
        last_resort_tier: List[str] = []
    else:
        last_resort_tier = list(_LAST_RESORT_QUERIES)

    tiers = [queries[:POOL_QUERIES], queries[POOL_QUERIES:], last_resort_tier]
    seen_ids: Set[int] = set()
    survivors: List[dict] = []
    minimum = min(2, count)

    for tier_idx, tier in enumerate(tiers):
        is_last_resort = tier_idx == len(tiers) - 1
        # The neutral tier is a rescue, not a top-up: only reach for it when the
        # real queries left us with nothing to build a Reel from.
        if is_last_resort and len(survivors) >= minimum:
            break
        if not tier:
            continue
        candidates = _search_pool(api_key, tier, seen_ids)

        if candidates:
            kept = []
            for v in candidates:
                slug = v.get("url", "")
                # Terrain runs in every place mode and for a different reason
                # than the place ban: a mountain gorge names nowhere, so the
                # vision gate passes it, while making the Reel stop looking Dutch.
                if shows_impossible_terrain(slug):
                    _audit(audit_out, v, "terrain", ok=False)
                elif slug_banned and claims_a_place(slug, slug_banned):
                    _audit(audit_out, v, "slug", ok=False)
                elif claims_identifiable_framing(slug, place_mode):
                    _audit(audit_out, v, "framing", ok=False)
                else:
                    kept.append(v)
            if len(candidates) != len(kept):
                logger.info(
                    f"   🗺️  Dropped {len(candidates) - len(kept)} clip(s) "
                    f"naming a place or showing non-Dutch terrain"
                )
            candidates = kept

        # The last-resort tier is neutral by construction — a third vision call
        # buys nothing and risks emptying an already-thin pool.
        if candidates and ai_agent and headline and not is_last_resort:
            candidates = _validate(ai_agent, candidates, headline, avoid_terms,
                                   place_mode, audit_out, place=place)

        # Survivors accumulate across tiers: a single good clip from a specific
        # query is worth keeping, it just isn't enough to stop searching. Keep
        # going until there is enough variety to fill the Reel.
        survivors.extend(candidates)
        if len(survivors) >= count:
            break

    if not survivors:
        logger.warning("⚠️  No usable Pexels clips — caller will degrade to photo/gradient")
        return []

    return _download_pool(
        _fresh_first(survivors, exclude_ids), count, tmp_dir, used_ids_out, audit_out,
    )


def _search_pool(api_key: str, queries: List[str], seen_ids: Set[int]) -> List[dict]:
    """Search several queries and pool the results, de-duplicated by clip id.

    Results are **interleaved** round-robin, not concatenated. Pexels returns 20
    hits per query and a Reel uses 9, so concatenating meant the first query
    supplied every single scene: a heat-record Reel ran nine Amsterdam canal
    clips in a row because "dutch canal summer heat" was asked first. The first
    query still leads — it supplies the cover — it just no longer wins the whole
    Reel, which is where visual variety comes from within a single run.
    """
    per_query: List[List[dict]] = []
    for query in queries:
        logger.info(f"🔍 Searching Pexels videos for: '{query}'")
        videos = _pexels_video_search(
            api_key, query, per_page=PEXELS_PER_PAGE, orientation="portrait",
        )
        if len(videos) < PEXELS_PER_PAGE // 2:
            videos = videos + _pexels_video_search(api_key, query, per_page=PEXELS_PER_PAGE)
        bucket: List[dict] = []
        for v in videos:
            vid = v.get("id")
            if vid is not None and vid in seen_ids:
                continue
            if vid is not None:
                seen_ids.add(vid)
            v.setdefault("_query", query)
            bucket.append(v)
        per_query.append(bucket)

    pooled: List[dict] = []
    for rank in range(max((len(b) for b in per_query), default=0)):
        for bucket in per_query:
            if rank < len(bucket):
                pooled.append(bucket[rank])
    return pooled


def _fresh_first(videos: List[dict], exclude_ids: Optional[Set[int]]) -> List[dict]:
    """Move clips used within the reuse window to the back (soft, never a filter).

    Keeps the cover fresh while letting stale clips still fill the body, so a
    small pool never degrades all the way to a gradient.
    """
    if not exclude_ids:
        return videos
    fresh = [v for v in videos if v.get("id") not in exclude_ids]
    stale = [v for v in videos if v.get("id") in exclude_ids]
    if fresh and stale:
        logger.info(f"   ♻️  Moved {len(stale)} recently-used clip(s) to the back")
        return fresh + stale
    return videos


def _validate(
    ai_agent, videos: List[dict], headline: str, avoid_terms: Optional[List[str]],
    place_mode: str, audit_out: Optional[List[dict]], place: str = "",
) -> List[dict]:
    """Run the Haiku vision gate; return only the clips that passed.

    Clips without a thumbnail can't be judged and are appended at the back
    rather than dropped. There is deliberately **no** floor that falls back to
    rejected clips: a rejected clip is one that claims the wrong place, which is
    the failure we are removing.
    """
    with_thumbs = [v for v in videos if v.get("image")]
    without_thumbs = [v for v in videos if not v.get("image")]
    if not with_thumbs:
        return videos

    batch = with_thumbs[:VALIDATION_BATCH]
    details: List[dict] = []
    results = ai_agent.validate_footage_thumbnails(
        headline, avoid_terms or [], [thumbnail_for_vision(v["image"]) for v in batch],
        place_mode=place_mode, place=place, details_out=details,
    )
    # strict: `results` is one verdict per clip in `batch`. A length mismatch
    # would silently drop unjudged clips into the discard pile — the exact class
    # of invisible footage failure the audit trail exists to catch.
    passed = [v for v, ok in zip(batch, results, strict=True) if ok]
    for v, ok in zip(batch, results, strict=True):
        if not ok:
            _audit(audit_out, v, "vision", ok=False)

    # Candidates beyond the batch limit were never judged. For a place-specific
    # story "unjudged" is not good enough — an unchecked clip may well be the
    # skyline we're trying to keep off screen — so drop them and let the caller
    # escalate to the next query instead. Other modes keep them as a tail.
    unjudged = [] if place_mode == "no_stock" else with_thumbs[VALIDATION_BATCH:]
    if place_mode == "no_stock":
        for v in with_thumbs[VALIDATION_BATCH:]:
            _audit(audit_out, v, "unjudged", ok=False)
        without_thumbs = []
    return passed + unjudged + without_thumbs


def _download_pool(
    videos: List[dict], count: int, tmp_dir: str,
    used_ids_out: Optional[List[int]], audit_out: Optional[List[dict]],
) -> List[str]:
    """Download up to *count* clips, skipping ids already taken."""
    paths: List[str] = []
    taken: Set[int] = set()
    for idx, video in enumerate(videos):
        if len(paths) >= count:
            break
        vid = video.get("id")
        if vid is not None and vid in taken:
            continue
        clip_path = _download_clip(video, idx, tmp_dir)
        if not clip_path:
            continue
        paths.append(clip_path)
        _audit(audit_out, video, "used", ok=True)
        if vid is not None:
            taken.add(vid)
            if used_ids_out is not None:
                used_ids_out.append(vid)
    logger.info(f"📥 Downloaded {len(paths)}/{count} clips")
    return paths


def _audit(audit_out: Optional[List[dict]], video: dict, reason: str, ok: bool) -> None:
    """Record one candidate decision, capped so posts_*.json stays small.

    Accepted and rejected clips get **separate** budgets. With one shared cap a
    heavily-filtered run spent it all on rejections and recorded none of the
    clips that actually shipped, so the audit read ``used=0`` for a Reel with
    nine clips in it — silent precisely when the trail was needed.
    """
    if audit_out is None:
        return
    limit = AUDIT_USED_LIMIT if ok else AUDIT_REJECT_LIMIT
    if sum(1 for a in audit_out if a.get("ok") is ok) >= limit:
        return
    audit_out.append({
        "pexels_id": video.get("id"),
        "query": video.get("_query", ""),
        "ok": ok,
        "reason": reason,
    })


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


def fetch_pexels_photo(
    query: str, dest_path: str, orientation: str = "portrait",
) -> Optional[str]:
    """Fetch a single Pexels PHOTO for an explicit *query* → save to *dest_path*.

    Unlike :func:`fetch_stock_image` (which derives its own query from a
    headline), this takes the query verbatim — used by the weekly fact carousel,
    which already has curated ``footage_queries`` per fact. Returns the saved
    path, or None on any miss/error (caller falls back to a solid background).
    """
    api_key = PEXELS_API_KEY or os.environ.get("PEXELS_API_KEY", "")
    if not api_key or not query:
        return None
    try:
        resp = http_requests.get(
            PEXELS_IMAGE_SEARCH_URL,
            headers={"Authorization": api_key},
            params={"query": query, "per_page": 1, "orientation": orientation},
            timeout=10,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
        if not photos:
            return None
        img_url = photos[0]["src"].get("large2x") or photos[0]["src"].get("large")
        img_resp = http_requests.get(img_url, timeout=15)
        img_resp.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(img_resp.content)
        with Image.open(dest_path) as img:
            img.verify()
        return dest_path
    except Exception as e:
        logger.warning(f"⚠️  Pexels photo fetch failed for '{query}': {e}")
        return None


def fetch_stock_image(
    title: str, content: str, tmp_dir: str,
    footage_plan: Optional[dict] = None,
) -> Optional[str]:
    """Search Pexels for a single stock PHOTO matching the news topic.

    Returns local path to downloaded image, or None.
    Useful as a Ken Burns fallback when video clips aren't available.

    The query comes from the *Dutch* headline via :func:`extract_search_query`,
    which keeps raw place names — so it is sanitized here too, or this fallback
    would reintroduce the wrong-city problem the video path just designed out.
    """
    api_key = PEXELS_API_KEY or os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        return None

    plan = footage_plan or {}
    query = extract_search_query(title, content)
    if plan.get("place_mode") == "no_stock":
        query = sanitize_query(query, banned_places_for(plan.get("place", ""), "no_stock"))
    if not query:
        return None
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
