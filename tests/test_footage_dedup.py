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
                      side_effect=lambda v, idx, d: f"/tmp/stock_{idx}.mp4"), \
         patch.object(F, "PEXELS_API_KEY", "test-key"):
        used = []
        paths = F.fetch_stock_clips(
            "k", "weather", "/tmp", count=3, footage_queries=["weather"],
            headline="h", ai_agent=None, exclude_ids={10, 11}, used_ids_out=used,
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
                      side_effect=lambda v, idx, d: f"/tmp/stock_{idx}.mp4"), \
         patch.object(F, "PEXELS_API_KEY", "test-key"):
        used = []
        paths = F.fetch_stock_clips(
            "k", "weather", "/tmp", count=2, footage_queries=["weather"],
            headline="h", ai_agent=None, exclude_ids={10, 11}, used_ids_out=used,
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


# ── 2c. Hybrid _build_background: cover-first ordering + cover_meta correctness ─

def _patch_build_background(monkeypatch, *, cover_render, body_clips):
    """Stub creator._build_background's heavy deps; capture the stitched clip list."""
    import video.creator as C
    captured = {}
    monkeypatch.setattr(C, "download_image", lambda url, d: "/tmp/news.jpg")
    monkeypatch.setattr(C, "compute_dhash", lambda p: 42)
    monkeypatch.setattr(C, "prepare_image_for_portrait", lambda p, d: "/tmp/prep.jpg")
    # Mirror the real function: it returns the path it rendered to (or None on
    # ffmpeg failure), so distinct output paths stay distinct.
    monkeypatch.setattr(C, "render_ken_burns_mp4",
                        lambda img, dur, out: out if cover_render else None)

    def _fake_fetch(*a, used_ids_out=None, **k):
        if used_ids_out is not None:
            used_ids_out.extend([1001, 1002])
        return list(body_clips)

    monkeypatch.setattr(C, "fetch_stock_clips", _fake_fetch)
    monkeypatch.setattr(
        C, "_stitch_clips_to_file",
        lambda paths, dur, lead_duration=None:
            captured.update(paths=paths, lead_duration=lead_duration) or "MERGED",
    )
    return C, captured


def test_hybrid_cover_is_first_and_meta_recorded(monkeypatch):
    """Fresh photo → cover mp4 prepended before stock clips; cover_meta recorded."""
    C, captured = _patch_build_background(
        monkeypatch, cover_render="/tmp/cover_kb.mp4",
        body_clips=["/tmp/b0.mp4", "/tmp/b1.mp4"],
    )
    meta = {}
    result = C._build_background(
        "t", "c", "https://img/fresh.jpg", "/tmp", 30.0,
        recent_image_urls=set(), recent_cover_hashes=[], cover_meta_out=meta,
    )
    assert result == "MERGED"
    assert captured["paths"] == ["/tmp/cover_kb.mp4", "/tmp/b0.mp4", "/tmp/b1.mp4"]
    assert meta == {"cover_image_url": "https://img/fresh.jpg", "cover_image_hash": 42}


def test_cover_render_failure_falls_back_to_stock_without_meta(monkeypatch):
    """If the cover mp4 render fails, use stock alone and do NOT record the photo."""
    C, captured = _patch_build_background(
        monkeypatch, cover_render=None,  # ffmpeg failed
        body_clips=["/tmp/b0.mp4", "/tmp/b1.mp4"],
    )
    meta = {}
    result = C._build_background(
        "t", "c", "https://img/fresh.jpg", "/tmp", 30.0,
        recent_image_urls=set(), recent_cover_hashes=[], cover_meta_out=meta,
    )
    assert result == "MERGED"
    assert captured["paths"] == ["/tmp/b0.mp4", "/tmp/b1.mp4"]  # no cover prepended
    assert meta == {}  # photo wasn't the cover → not recorded


def test_recently_used_photo_skipped_as_cover(monkeypatch):
    """A photo whose URL was used recently is skipped → stock cover, no meta."""
    C, captured = _patch_build_background(
        monkeypatch, cover_render="/tmp/cover_kb.mp4",
        body_clips=["/tmp/b0.mp4"],
    )
    meta = {}
    result = C._build_background(
        "t", "c", "https://img/seen.jpg", "/tmp", 30.0,
        recent_image_urls={"https://img/seen.jpg"}, recent_cover_hashes=[],
        cover_meta_out=meta,
    )
    assert result == "MERGED"
    assert captured["paths"] == ["/tmp/b0.mp4"]  # stock only, no cover render
    assert meta == {}


# ── 2e. Place-specific stories get more of the only authentic footage ────────

def test_cover_duration_honoured_for_place_stories(monkeypatch):
    """The article photo is the only frame that really shows the place.

    Without lead_duration the stitcher splits time evenly and the cover constant
    is a no-op, so assert the value actually reaches _stitch_clips_to_file.
    """
    import video.creator as C
    _, captured = _patch_build_background(
        monkeypatch, cover_render="/tmp/cover_kb.mp4",
        body_clips=[f"/tmp/b{i}.mp4" for i in range(6)],
    )
    C._build_background(
        "t", "c", "https://img/fresh.jpg", "/tmp", 35.0,
        recent_image_urls=set(), recent_cover_hashes=[],
        footage_plan={"place": "deventer", "place_mode": "no_stock"},
    )
    assert captured["lead_duration"] == C.COVER_SCENE_DURATION_PLACE
    # …and the photo returns mid-Reel rather than holding a long static opening
    assert captured["paths"].count("/tmp/cover_kb.mp4") == 1
    assert "/tmp/cover_kb2.mp4" in captured["paths"]


def test_cover_duration_default_for_non_place_stories(monkeypatch):
    """A story with no geographic anchor keeps the original 4 s cover."""
    import video.creator as C
    _, captured = _patch_build_background(
        monkeypatch, cover_render="/tmp/cover_kb.mp4",
        body_clips=[f"/tmp/b{i}.mp4" for i in range(6)],
    )
    C._build_background(
        "t", "c", "https://img/fresh.jpg", "/tmp", 35.0,
        recent_image_urls=set(), recent_cover_hashes=[],
        footage_plan={"place": "", "place_mode": "none"},
    )
    assert captured["lead_duration"] == C.COVER_SCENE_DURATION
    assert "/tmp/cover_kb2.mp4" not in captured["paths"]


def test_build_background_reports_bg_meta(monkeypatch):
    """bg_meta_out drives the stock-footage disclosure label's timing."""
    import video.creator as C
    _, _ = _patch_build_background(
        monkeypatch, cover_render="/tmp/cover_kb.mp4", body_clips=["/tmp/b0.mp4"],
    )
    bg_meta = {}
    C._build_background(
        "t", "c", "https://img/fresh.jpg", "/tmp", 35.0,
        recent_image_urls=set(), recent_cover_hashes=[], bg_meta_out=bg_meta,
    )
    assert bg_meta["source"] == "hybrid"
    assert bg_meta["has_real_cover"] is True
    assert bg_meta["stock_starts_at"] == C.COVER_SCENE_DURATION


# ── 2d. Single-pass ffmpeg assembly (render speedup) ─────────────────────────

def test_assemble_reel_produces_video(tmp_path):
    """assemble_reel burns overlays + audio onto bg in one ffmpeg pass."""
    import subprocess
    from imageio_ffmpeg import get_ffmpeg_exe
    from video.effects import render_gradient_png, build_hook_overlays, OverlaySpec
    from video.ffcompose import assemble_reel

    ff = get_ffmpeg_exe()
    bg = tmp_path / "bg.mp4"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i",
                    "color=c=navy:s=1080x1920:d=3:r=30",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    str(bg)], capture_output=True, check=True)
    audio = tmp_path / "a.m4a"
    subprocess.run([ff, "-y", "-f", "lavfi", "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=44100",
                    "-t", "5", "-c:a", "aac", str(audio)], capture_output=True, check=True)

    overlays = [OverlaySpec(render_gradient_png(str(tmp_path / "grad.png")), 0, 0.0, 5.0)]
    overlays += build_hook_overlays("Test hook here", str(tmp_path), duration=3.0)

    out = tmp_path / "out.mp4"
    result = assemble_reel(str(bg), str(audio), overlays, 5.0, str(out))
    assert result == str(out)
    assert out.exists() and out.stat().st_size > 0


def test_render_gradient_png_is_full_frame_rgba(tmp_path):
    """render_gradient_png writes a full-frame RGBA image (black, graded alpha)."""
    from video.effects import render_gradient_png
    from video.config import VIDEO_WIDTH, VIDEO_HEIGHT
    from PIL import Image
    p = render_gradient_png(str(tmp_path / "g.png"))
    img = Image.open(p)
    assert img.mode == "RGBA" and img.size == (VIDEO_WIDTH, VIDEO_HEIGHT)


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
    contents = [{"Key": k} for k in objects]
    client.list_objects_v2.return_value = {"Contents": contents}

    # get_published_urls paginates: an unpaginated call caps at 1000 keys and
    # would silently return only the oldest posts once the prefix grows past
    # that. Split the stub across two pages so the test would fail if the
    # production code ever went back to reading a single response.
    mid = max(1, len(contents) // 2)
    paginator = MagicMock()
    paginator.paginate.return_value = iter(
        [{"Contents": contents[:mid]}, {"Contents": contents[mid:]}]
    )
    client.get_paginator.return_value = paginator

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
        urls, titles, footage, topics = lambda_handler.get_published_urls("bucket")

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
        urls, titles, footage, topics = lambda_handler.get_published_urls("bucket")

    assert urls == {"https://nos.nl/a/3"}
    assert footage["pexels_ids"] == set()
    # legacy image_url is still tracked as a recent cover URL
    assert footage["image_urls"] == {"https://img/legacy.jpg"}
    assert footage["cover_hashes"] == []


def test_get_published_urls_paginates_past_the_1000_key_cap():
    """The silent cliff this prevents.

    `list_objects_v2` returns at most 1000 keys. At 2 posts/day the `posts_`
    prefix crosses that around day 500, and because keys sort as
    `posts_YYYYMMDD_HHMMSS`, an unpaginated read would return the OLDEST 1000
    and drop every recent post from the window — breaking URL dedup, the 3-day
    title window, the 7-day content mix, the violence cap and footage dedup at
    once, with no error raised.

    Simulates that: 1200 old keys on page 1, the recent ones on page 2. A
    single-page reader would never see the recent post.
    """
    recent_key = _key_for(0)
    objects = {recent_key: [{"original_url": "https://recent.example/story"}]}
    old_keys = [f"posts_2024{m:02d}{d:02d}_120000.json"
                for m in range(1, 13) for d in range(1, 101)][:1200]
    for k in old_keys:
        objects[k] = [{"original_url": f"https://old.example/{k}"}]

    client = MagicMock()
    page1 = [{"Key": k} for k in old_keys]
    page2 = [{"Key": recent_key}]

    def _get_object(Bucket, Key):
        body = MagicMock()
        body.read.return_value = json.dumps(objects[Key]).encode("utf-8")
        return {"Body": body}

    client.get_object.side_effect = _get_object
    paginator = MagicMock()
    paginator.paginate.return_value = iter([{"Contents": page1}, {"Contents": page2}])
    client.get_paginator.return_value = paginator
    # Deliberately sabotaged: a single-page read must not be what the code uses.
    client.list_objects_v2.return_value = {"Contents": page1}

    import lambda_handler
    with patch.object(lambda_handler.boto3, "client", return_value=client):
        urls, _titles, _footage, _topics = lambda_handler.get_published_urls("bucket")

    assert "https://recent.example/story" in urls, (
        "recent post lost — get_published_urls is not paginating"
    )
    assert len(urls) == 1201
