"""
Tests for cover/footage de-duplication:
  - perceptual hash (dHash) near-duplicate detection
  - Pexels fresh-first ordering + used-id capture (with graceful all-stale fallback)
  - get_published_urls footage fingerprint scan (reuse window + backward compat)
"""

import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

# Add src/ to path so tests can import application modules
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


# ── 1. Perceptual hash ───────────────────────────────────────────────────────

def _half_black_white(path, size=200, swap=False):
    img = Image.new("RGB", (size, size), "black")
    lo, hi = (0, size // 2) if swap else (size // 2, size)
    for x in range(lo, hi):
        for y in range(size):
            img.putpixel((x, y), (255, 255, 255))
    img.save(path)


def test_dhash_near_duplicate_within_threshold(tmp_path):
    """Same image at a different size/format → small Hamming distance."""
    from video.effects import compute_dhash, hamming
    a = tmp_path / "a.jpg"
    _half_black_white(str(a))
    b = tmp_path / "b.png"
    Image.open(a).resize((90, 90)).save(b)  # same content, different size/format
    assert hamming(compute_dhash(str(a)), compute_dhash(str(b))) <= 10


def test_dhash_distinct_images_exceed_threshold(tmp_path):
    """Visually different images → large Hamming distance."""
    from video.effects import compute_dhash, hamming
    a = tmp_path / "a.jpg"
    _half_black_white(str(a))
    c = tmp_path / "c.jpg"
    grad = Image.new("RGB", (200, 200))
    for y in range(200):
        for x in range(200):
            grad.putpixel((x, y), (y, y // 2, 255 - y))
    grad.save(c)
    assert hamming(compute_dhash(str(a)), compute_dhash(str(c))) > 10


# ── 2. Pexels fresh-first ordering ───────────────────────────────────────────

def test_fetch_clips_fresh_first_and_records_ids():
    """Recently-used clip ids move to the back; cover (used_ids[0]) is fresh."""
    import video.footage as F
    with patch.object(F, "_pexels_video_search",
                      side_effect=lambda *a, orientation="", **k:
                          [{"id": i, "image": None} for i in (10, 11, 12, 13)]
                          if orientation == "portrait" else []), \
         patch.object(F, "_download_clip",
                      side_effect=lambda v, idx, d: f"/tmp/stock_{idx}.mp4"):
        used = []
        paths = F._fetch_clips_for_query(
            "k", "weather", "/tmp", count=3, headline="h", ai_agent=None,
            exclude_ids={10, 11}, used_ids_out=used,
        )
    assert len(paths) == 3
    assert used[0] == 12          # cover is fresh
    assert set(used[:2]) == {12, 13}


def test_fetch_clips_all_stale_still_returns_clips():
    """When every candidate is stale, still return clips (never empty → no gradient)."""
    import video.footage as F
    with patch.object(F, "_pexels_video_search",
                      side_effect=lambda *a, orientation="", **k:
                          [{"id": i, "image": None} for i in (10, 11)]
                          if orientation == "portrait" else []), \
         patch.object(F, "_download_clip",
                      side_effect=lambda v, idx, d: f"/tmp/stock_{idx}.mp4"):
        used = []
        paths = F._fetch_clips_for_query(
            "k", "weather", "/tmp", count=2, headline="h", ai_agent=None,
            exclude_ids={10, 11}, used_ids_out=used,
        )
    assert len(paths) == 2
    assert set(used) == {10, 11}


# ── 2b. Cover render is a fast file-backed MP4 (not slow per-frame moviepy) ───

def test_render_ken_burns_mp4_produces_file(tmp_path):
    """render_ken_burns_mp4 should emit a non-empty mp4 via ffmpeg."""
    from video.effects import render_ken_burns_mp4
    img = tmp_path / "cover.jpg"
    Image.new("RGB", (1458, 2592), (40, 80, 160)).save(img)
    out = tmp_path / "cover.mp4"
    result = render_ken_burns_mp4(str(img), 3.0, str(out))
    assert result == str(out)
    assert out.exists() and out.stat().st_size > 0


# ── 3. Signatures wire the dedup params through ───────────────────────────────

def test_create_news_video_has_dedup_params():
    from video import create_news_video
    params = inspect.signature(create_news_video).parameters
    for p in ("exclude_media_ids", "recent_image_urls",
              "recent_cover_hashes", "used_media_ids", "cover_meta_out"):
        assert p in params


# ── 4. get_published_urls footage scan ───────────────────────────────────────

def _s3_stub(objects):
    """Build a boto3-client stub whose list/get return *objects* {key: posts_list}."""
    client = MagicMock()
    client.list_objects_v2.return_value = {
        "Contents": [{"Key": k} for k in objects]
    }

    def _get_object(Bucket, Key):
        body = MagicMock()
        body.read.return_value = json.dumps(objects[Key]).encode("utf-8")
        return {"Body": body}

    client.get_object.side_effect = _get_object
    return client


def _key_for(days_ago):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return f"posts_{dt.strftime('%Y%m%d_%H%M%S')}.json"


def test_get_published_urls_collects_recent_footage():
    """Footage fingerprints are collected only within the reuse window (default 30d)."""
    import lambda_handler
    recent = _key_for(2)
    old = _key_for(40)
    objects = {
        recent: [{
            "original_url": "https://nos.nl/a/1",
            "original_title": "Heat",
            "pexels_media_ids": [101, 102],
            "cover_image_url": "https://img/recent.jpg",
            "cover_image_hash": 123456,
        }],
        old: [{
            "original_url": "https://nos.nl/a/2",
            "original_title": "Old",
            "pexels_media_ids": [999],
            "cover_image_url": "https://img/old.jpg",
            "cover_image_hash": 777,
        }],
    }
    with patch.object(lambda_handler.boto3, "client", return_value=_s3_stub(objects)):
        urls, titles, footage = lambda_handler.get_published_urls("bucket")

    # all-time URLs include both; footage only the recent one
    assert urls == {"https://nos.nl/a/1", "https://nos.nl/a/2"}
    assert footage["pexels_ids"] == {101, 102}
    assert footage["image_urls"] == {"https://img/recent.jpg"}
    assert footage["cover_hashes"] == [123456]


def test_get_published_urls_backward_compatible_with_old_posts():
    """Posts lacking the new fields yield empty footage sets, no error."""
    import lambda_handler
    objects = {
        _key_for(1): [{
            "original_url": "https://nos.nl/a/3",
            "image_url": "https://img/legacy.jpg",  # legacy photo-post field
        }],
    }
    with patch.object(lambda_handler.boto3, "client", return_value=_s3_stub(objects)):
        urls, titles, footage = lambda_handler.get_published_urls("bucket")

    assert urls == {"https://nos.nl/a/3"}
    assert footage["pexels_ids"] == set()
    # legacy image_url is still tracked as a recent cover URL
    assert footage["image_urls"] == {"https://img/legacy.jpg"}
    assert footage["cover_hashes"] == []
