"""
Tests for geographic footage safety — never show the wrong place.

Covers the three defences added after viewers read generic stock footage as
"AI-generated fake images of our town":
  1. place_mode tiering + query sanitizing (no place we can't source)
  2. the Pexels URL-slug pre-filter (free rejection of clips naming a place)
  3. the vision gate with no fail-open floor (rejected clips stay rejected)
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import footage_geo as G


# ── 1. Tiering: which places may we name at all? ─────────────────────────────

def test_place_mode_derivation():
    """Small places → no_stock; big ones and country scope → stock_ok."""
    assert G.derive_place_mode("Deventer", "city") == "no_stock"
    assert G.derive_place_mode("Moerdijk", "city") == "no_stock"
    assert G.derive_place_mode("Zeeland", "region") == "no_stock"
    assert G.derive_place_mode("Amsterdam", "city") == "stock_ok"
    assert G.derive_place_mode("Rotterdam", "city") == "stock_ok"
    assert G.derive_place_mode("Netherlands", "country") == "stock_ok"
    assert G.derive_place_mode("Germany", "country") == "stock_ok"
    assert G.derive_place_mode("", "none") == "none"
    # A place_type of city with no place name is not a geographic anchor
    assert G.derive_place_mode("", "city") == "none"


# ── 2. Query sanitizing ──────────────────────────────────────────────────────

def test_sanitize_query_strips_place_tokens():
    banned = G.banned_places_for("Deventer", "no_stock")
    assert G.sanitize_query("deventer harbour", banned) == "harbour"
    assert G.sanitize_query("deventer", banned) == ""
    # Identifying framing goes too — an aerial always claims a location
    assert G.sanitize_query("aerial cityscape", banned) == ""


def test_sanitize_keeps_national_terms_but_not_other_cities():
    """For a Deventer story, "rotterdam port" is still the bug we're removing."""
    banned = G.banned_places_for("Deventer", "no_stock")
    assert G.sanitize_query("dutch canal barge", banned) == "dutch canal barge"
    assert G.sanitize_query("netherlands farmland", banned) == "netherlands farmland"
    assert "rotterdam" not in G.sanitize_query("rotterdam port crane", banned)


def test_sanitize_is_noop_for_stock_ok_stories():
    """A genuine Amsterdam story may absolutely ask for Amsterdam."""
    out = G.sanitize_queries(["amsterdam skyline", "amsterdam canal"], "Amsterdam", "stock_ok")
    assert out == ["amsterdam skyline", "amsterdam canal"]


# ── 2b. National anchor: "no place" must still mean the Netherlands ──────────

def test_anchor_added_when_sanitizing_removed_every_geographic_cue():
    """The Venray regression: 5 correct, careful, entirely placeless queries."""
    out = G.ensure_national_anchor(
        ["forest fire firefighters hose", "burning heathland close up",
         "smouldering ground smoke"],
        "no_stock", "venray",
    )
    assert out[:3] == ["forest fire firefighters hose", "burning heathland close up",
                       "smouldering ground smoke"]  # order kept, cover unchanged
    anchored = [q for q in out if "dutch" in q or "netherlands" in q]
    assert len(anchored) >= G.MIN_ANCHORED_QUERIES


def test_anchor_is_a_noop_when_queries_already_promise_the_country():
    queries = ["dutch canal summer heat", "netherlands farmland drought", "thermometer"]
    assert G.ensure_national_anchor(queries, "stock_ok", "netherlands") == queries


def test_stock_ok_story_is_anchored_by_its_own_place():
    """An Amsterdam story already reads as Dutch — don't pad it with canals."""
    queries = ["amsterdam canal boat", "amsterdam street crowd", "police tape"]
    assert G.ensure_national_anchor(queries, "stock_ok", "amsterdam") == queries


def test_anchor_leaves_an_empty_plan_empty():
    """No queries means generation failed; the caller's fallbacks decide, not us."""
    assert G.ensure_national_anchor([], "no_stock", "venray") == []


def test_build_footage_plan_anchors_place_specific_stories():
    plan = G.build_footage_plan(
        place="Venray", place_type="city",
        queries=["smouldering forest floor", "firefighter hose heathland"],
    )
    assert plan["place_mode"] == "no_stock"
    assert sum(1 for q in plan["queries"] if "dutch" in q) >= G.MIN_ANCHORED_QUERIES


# ── 2c. Terrain the Netherlands does not have ────────────────────────────────

def test_impossible_terrain_rejected():
    """Measured: a Venray drought Reel shipped a rocky mountain river.

    It names no place, so the identifiability test passes it — and it single
    handedly stops the Reel reading as Dutch news.
    """
    assert G.shows_impossible_terrain("peaceful-river-flowing-through-rocky-landscape-1")
    assert G.shows_impossible_terrain("serene-riverbank-with-stone-reflections-in-valley-2")
    assert G.shows_impossible_terrain("aerial-view-of-mountain-forest-3")
    assert G.shows_impossible_terrain("palm-trees-on-a-tropical-beach-4")


def test_dutch_landscape_is_not_impossible_terrain():
    """The country has dunes, beaches and heath — only the geology is banned."""
    assert not G.shows_impossible_terrain("charred-forest-floor-after-a-fire-1")
    assert not G.shows_impossible_terrain("sand-dunes-and-beach-grass-2")
    assert not G.shows_impossible_terrain("low-water-in-a-wide-river-3")
    assert not G.shows_impossible_terrain("")


def test_build_footage_plan_drops_empty_and_duplicate_queries():
    plan = G.build_footage_plan(
        place="Deventer", place_type="city",
        queries=["deventer harbour", "deventer", "deventer harbour", "low water river"],
        avoid=["recognisable skyline"],
    )
    assert plan["place_mode"] == "no_stock"
    # empty + dupe dropped; the story's own queries keep their order and lead
    assert plan["queries"][:2] == ["harbour", "low water river"]
    assert plan["avoid"] == ["recognisable skyline"]


def test_build_footage_plan_always_returns_valid_dict():
    plan = G.build_footage_plan()
    assert plan["place_mode"] == "none"
    assert plan["queries"] == [] and plan["avoid"] == []


# ── 3. Slug pre-filter ───────────────────────────────────────────────────────

def test_slug_prefilter_rejects_named_places():
    banned = G.slug_banned_places_for("Deventer", "no_stock")
    assert G.claims_a_place("/video/aerial-view-of-hamburg-harbor-12345/", banned)
    assert G.claims_a_place("/video/rotterdam-harbor-at-dusk-77/", banned)
    assert not G.claims_a_place("/video/close-up-of-crane-hook-999/", banned)


def test_harlingen_slug_rejected_for_deventer_story():
    """The real incident: a Deventer harbour Reel used a Harlingen clip and a
    viewer named the port from the video ("147Km from Deventer")."""
    banned = G.slug_banned_places_for("Deventer", "no_stock")
    assert G.claims_a_place("/video/harlingen-port-sunset-4711/", banned)


def test_own_place_slug_allowed_for_stock_ok():
    """An Amsterdam story may use a clip whose slug says Amsterdam."""
    banned = G.slug_banned_places_for("Amsterdam", "stock_ok")
    assert not G.claims_a_place("/video/amsterdam-canal-boats-12/", banned)


def test_world_slug_rejected_even_for_stock_ok():
    """The real incident: a Schiphol story used Kansai (Osaka) airport footage.

    stock_ok lifts the query restriction, not the wrong-place restriction.
    """
    banned = G.slug_banned_places_for("Schiphol", "stock_ok")
    assert G.claims_a_place("/video/kansai-airport-terminal-88/", banned)
    assert G.claims_a_place("/video/osaka-airport-interior-3/", banned)
    assert not G.claims_a_place("/video/schiphol-departures-hall-9/", banned)


def test_country_slug_rejected():
    """Measured: a Venray wildfire Reel took its COVER from a clip slugged
    "controlled farm fire in south africa". The list was cities and landmarks
    only, so a whole country walked straight through it."""
    banned = G.slug_banned_places_for("Venray", "no_stock")
    assert G.claims_a_place("/video/controlled-farm-fire-in-south-africa-33661463/", banned)
    assert G.claims_a_place("/video/wildfire-in-california-hills-5/", banned)


def test_own_country_still_allowed_for_a_foreign_story():
    """A story about Turkey may show Turkey — stock_ok drops it from the ban."""
    banned = G.slug_banned_places_for("Turkey", "stock_ok")
    assert "turkey" not in banned
    assert G.claims_a_place("/video/turkish-flag-over-ankara-1/", banned) is False
    # Erring wide is still accepted: a named city stays banned even so.
    assert G.claims_a_place("/video/istanbul-street-market-2/", banned)


def test_named_dutch_river_rejected_for_a_different_dutch_town():
    """Measured: a Deventer (IJssel) story pulled seven "nederrijn river" clips.

    A named river is exactly as specific as a named town.
    """
    banned = G.slug_banned_places_for("Deventer", "no_stock")
    assert G.claims_a_place("/video/aerial-view-of-nederrijn-river-and-ferry-1/", banned)
    assert G.claims_a_place("/video/wheat-field-in-drenthe-2/", banned)
    assert not G.claims_a_place("/video/scenic-dutch-countryside-river-3/", banned)


def test_dutch_region_allowed_for_a_national_story():
    """A national story IS the whole country — Drenthe is the Netherlands."""
    banned = G.slug_banned_places_for("Netherlands", "stock_ok")
    assert not G.claims_a_place("/video/wheat-field-in-drenthe-2/", banned)


def test_unnamed_city_framing_rejected_only_for_place_stories():
    """An unnamed city aerial under a Deventer headline reads as Deventer;
    under an Amsterdam headline a skyline is simply correct."""
    assert G.claims_identifiable_framing(
        "/video/an-aerial-view-of-a-city-with-a-river-and-buildings-2/", "no_stock")
    assert G.claims_identifiable_framing("/video/harbour-skyline-at-dusk-3/", "no_stock")
    assert not G.claims_identifiable_framing("/video/aerial-view-of-farmland-4/", "no_stock")
    assert not G.claims_identifiable_framing("/video/harbour-skyline-at-dusk-3/", "stock_ok")


def test_slug_prefilter_ignores_ambiguous_words():
    """"Goes" is a Dutch town AND an English verb — never treat it as a place."""
    banned = G.banned_places_for("Deventer", "no_stock")
    assert not G.claims_a_place("/video/man-goes-fishing-1/", banned)


def test_no_places_banned_without_a_geographic_anchor():
    """The filter is gated at the call site: nothing to protect, nothing banned.

    `claims_a_place` itself still flags world landmarks regardless of the banned
    set — `fetch_stock_clips` only invokes it when place_mode is "no_stock".
    """
    assert G.banned_places_for("", "none") == set()
    assert G.banned_places_for("Amsterdam", "stock_ok") == set()


# ── 4. fetch_stock_clips behaviour ───────────────────────────────────────────

def _vid(i, query="q", slug=None, thumb=True):
    return {
        "id": i,
        "image": f"https://img/{i}.jpg" if thumb else None,
        "url": slug or f"https://www.pexels.com/video/generic-clip-{i}/",
    }


def _patch_pexels(monkeypatch, videos, calls=None):
    """Stub the Pexels search + download so no network or ffmpeg is touched."""
    import video.footage as F

    def _search(api_key, query, per_page=15, orientation=""):
        if calls is not None:
            calls.append(query)
        return [dict(v, _query=query) for v in videos] if orientation == "portrait" else []

    monkeypatch.setattr(F, "_pexels_video_search", _search)
    monkeypatch.setattr(F, "_download_clip", lambda v, idx, d: f"/tmp/stock_{v['id']}.mp4")
    monkeypatch.setattr(F, "PEXELS_API_KEY", "test-key")
    return F


def test_keyword_fallback_is_sanitized(monkeypatch):
    """The regression: extract_search_query keeps raw Dutch place names.

    Even with no AI queries at all, "deventer" must never reach Pexels.
    """
    calls = []
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2)], calls)
    F.fetch_stock_clips(
        title="Deventer sluit haven vanwege lage waterstanden",
        content="De gemeente Deventer sluit de haven.",
        tmp_dir="/tmp", count=2, footage_queries=[],
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert calls, "expected at least one Pexels search"
    assert not any("deventer" in q.lower() for q in calls), calls


def test_no_fail_open_to_rejected_clips(monkeypatch):
    """A clip the vision gate FAILED must never be downloaded anyway.

    The old code fell back to `videos[:2]` when fewer than 2 passed — i.e. it
    used exactly the clips it had just rejected as misleading.
    """
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2), _vid(3)])
    agent = MagicMock()
    agent.validate_footage_thumbnails.return_value = [False, False, False]

    used, audit = [], []
    paths = F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=3,
        footage_queries=["low water river"], headline="h", ai_agent=agent,
        used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert agent.validate_footage_thumbnails.called
    assert used == [], f"rejected clips were downloaded anyway: {used}"
    assert paths == []  # caller degrades to photo/gradient — no place beats wrong place
    assert [a["reason"] for a in audit] == ["vision"] * 3


def test_unjudged_clips_are_dropped_for_place_stories(monkeypatch):
    """Only 15 thumbnails fit one vision call; the rest are unverified.

    For a place-specific story an unchecked clip may be exactly the skyline we
    are keeping off screen, so it must not slip through on the tail.
    """
    videos = [_vid(i) for i in range(1, 21)]  # 20 candidates, batch limit is 15
    F = _patch_pexels(monkeypatch, videos)
    agent = MagicMock()
    agent.validate_footage_thumbnails.return_value = [True] * 15

    used, audit = [], []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=20,
        footage_queries=["low water river"], headline="h", ai_agent=agent,
        used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert set(used) <= set(range(1, 16)), f"unjudged clips used: {used}"
    assert any(a["reason"] == "unjudged" for a in audit)


def test_vision_gate_receives_place_mode(monkeypatch):
    """The gate must know it is judging identifiability, not plausibility."""
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2)])
    agent = MagicMock()
    agent.validate_footage_thumbnails.return_value = [True, True]
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=2,
        footage_queries=["low water river"], headline="h", ai_agent=agent,
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert agent.validate_footage_thumbnails.call_args.kwargs["place_mode"] == "no_stock"


def test_slug_filter_applied_before_download(monkeypatch):
    """Clips whose slug names a foreign city are dropped without a vision call."""
    videos = [
        _vid(1, slug="https://www.pexels.com/video/hamburg-harbor-aerial-1/"),
        _vid(2, slug="https://www.pexels.com/video/close-up-crane-hook-2/"),
        _vid(3, slug="https://www.pexels.com/video/singapore-port-3/"),
    ]
    F = _patch_pexels(monkeypatch, videos)
    used, audit = [], []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=3,
        footage_queries=["low water river"], used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert 1 not in used and 3 not in used, used
    assert 2 in used
    assert any(a["reason"] == "slug" and a["ok"] is False for a in audit)


def test_slug_filter_allows_own_place_for_stock_ok_story(monkeypatch):
    """An Amsterdam story may use a clip whose slug says Amsterdam,
    but a world landmark is still rejected even in stock_ok mode."""
    videos = [
        _vid(1, slug="https://www.pexels.com/video/amsterdam-canal-1/"),
        _vid(2, slug="https://www.pexels.com/video/kansai-airport-terminal-2/"),
    ]
    F = _patch_pexels(monkeypatch, videos)
    used, audit = [], []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=2,
        footage_queries=["amsterdam canal"], used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Amsterdam", "city"),
    )
    assert 1 in used
    assert 2 not in used, "Kansai clip must not survive a stock_ok story"
    assert any(a["pexels_id"] == 2 and a["reason"] == "slug" for a in audit)


def test_terrain_filter_applied_before_download(monkeypatch):
    """Measured: a Venray drought Reel shipped a rocky mountain river.

    It names no place, so neither the place ban nor the vision gate's
    identifiability test stops it — only the terrain check does, and it runs in
    every place mode because a mountain is wrong for an Amsterdam story too.
    """
    videos = [
        _vid(1, slug="https://www.pexels.com/video/river-through-rocky-landscape-1/"),
        _vid(2, slug="https://www.pexels.com/video/low-water-in-a-wide-river-2/"),
        _vid(3, slug="https://www.pexels.com/video/palm-trees-tropical-beach-3/"),
    ]
    F = _patch_pexels(monkeypatch, videos)
    used, audit = [], []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=3,
        footage_queries=["low water river"], used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Amsterdam", "city"),
    )
    assert used == [2], used
    assert {a["pexels_id"] for a in audit if a["reason"] == "terrain"} == {1, 3}


def test_pool_interleaves_queries_so_one_cannot_fill_the_reel(monkeypatch):
    """Measured: a heat-record Reel ran nine Amsterdam canal clips in a row.

    Pexels returns 20 hits per query and a Reel uses 9, so concatenating the
    pool meant the first query supplied every scene. It still leads (it supplies
    the cover) — it just no longer wins the whole Reel.
    """
    import video.footage as F

    def _search(api_key, query, per_page=15, orientation=""):
        if orientation != "portrait":
            return []
        base = {"a": 100, "b": 200, "c": 300}[query]
        return [_vid(base + i, query=query) for i in range(20)]

    monkeypatch.setattr(F, "_pexels_video_search", _search)
    monkeypatch.setattr(F, "_download_clip", lambda v, idx, d: f"/tmp/{v['id']}.mp4")
    monkeypatch.setattr(F, "PEXELS_API_KEY", "test-key")

    used = []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=9,
        footage_queries=["a", "b", "c"], used_ids_out=used,
    )
    assert used[0] == 100, "the first query must still supply the cover"
    assert len({i // 100 for i in used}) == 3, f"all three queries must feature: {used}"


def test_dutch_keyword_fallback_never_joins_ai_queries(monkeypatch):
    """A live run searched Pexels for 'uitzonderlijke situatie droogte brand'.

    extract_search_query keeps untranslated Dutch, which Pexels does not
    understand — its results were then scored as if they were about drought. The
    fallback is a replacement for the AI queries, never an addition to them.
    """
    calls = []
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2)], calls)
    F.fetch_stock_clips(
        title="'Uitzonderlijke situatie' door droogte, brand bij Venray",
        content="De brandweer blijft bezig met blussen.",
        tmp_dir="/tmp", count=2,
        footage_queries=["smouldering forest floor", "firefighter hose heathland"],
        footage_plan=G.build_footage_plan("Venray", "city"),
    )
    assert not any("droogte" in q or "uitzonderlijke" in q for q in calls), calls


def test_audit_keeps_used_clips_when_rejections_are_plentiful(monkeypatch):
    """The trail used to go silent in exactly the runs worth auditing.

    One shared cap meant a heavily-filtered run spent it all on rejections and
    recorded none of the nine clips that actually shipped (`used=0`).
    """
    videos = [
        _vid(i, slug=f"https://www.pexels.com/video/hamburg-harbor-{i}/")
        for i in range(1, 30)
    ] + [_vid(90), _vid(91)]
    F = _patch_pexels(monkeypatch, videos)
    used, audit = [], []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=2,
        footage_queries=["low water river"], used_ids_out=used, audit_out=audit,
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert used == [90, 91], used
    assert [a["pexels_id"] for a in audit if a["ok"]] == [90, 91]
    assert sum(1 for a in audit if not a["ok"]) == F.AUDIT_REJECT_LIMIT


def test_stock_ok_gate_receives_place(monkeypatch):
    """The plausibility gate gets the concrete place so 'wrong location'
    is checkable ('schiphol' vs a generic 'wrong country or landmark')."""
    F = _patch_pexels(monkeypatch, [_vid(1)])
    agent = MagicMock()
    agent.validate_footage_thumbnails.return_value = [True]
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=1,
        footage_queries=["schiphol airport"], headline="h", ai_agent=agent,
        footage_plan=G.build_footage_plan("Schiphol", "city"),
    )
    kwargs = agent.validate_footage_thumbnails.call_args.kwargs
    assert kwargs["place_mode"] == "stock_ok"
    assert kwargs["place"] == "schiphol"


def test_empty_queries_search_last_resort_once(monkeypatch):
    """With no usable queries at all, the neutral tier runs exactly once.

    Each query is legitimately searched twice per tier (portrait + any
    orientation top-up); the bug being guarded against is the same queries
    appearing in TWO tiers, which doubles that to four.
    """
    from collections import Counter
    calls = []
    F = _patch_pexels(monkeypatch, [], calls)
    F.fetch_stock_clips(
        title="Deventer", content="", tmp_dir="/tmp", count=3,
        footage_queries=[],
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    counts = Counter(calls)
    assert set(counts) == set(F._LAST_RESORT_QUERIES)
    assert all(n <= 2 for n in counts.values()), f"query searched across two tiers: {counts}"


def test_no_duplicate_ids_in_used_ids_out(monkeypatch):
    """Pooling across queries must not download the same clip twice."""
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2)])
    used = []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=9,
        footage_queries=["a", "b", "c"], used_ids_out=used,
    )
    assert len(used) == len(set(used)), used


def test_audit_records_used_clips(monkeypatch):
    F = _patch_pexels(monkeypatch, [_vid(1), _vid(2)])
    audit = []
    F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=2,
        footage_queries=["low water river"], audit_out=audit,
    )
    used_entries = [a for a in audit if a["reason"] == "used"]
    assert len(used_entries) == 2
    assert all(set(a) == {"pexels_id", "query", "ok", "reason"} for a in audit)


def test_returns_empty_rather_than_wrong_place(monkeypatch):
    """No candidates anywhere → [] so the caller degrades to photo/gradient."""
    F = _patch_pexels(monkeypatch, [])
    paths = F.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=3,
        footage_queries=["low water river"],
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert paths == []


# ── 5. fetch_stock_image (Priority 3) is sanitized too ───────────────────────

def test_stock_image_query_is_sanitized(monkeypatch):
    """The photo fallback derives its own query from the Dutch title."""
    import video.footage as F
    captured = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"photos": []}

    def _get(url, headers=None, params=None, timeout=None):
        captured["query"] = params["query"]
        return _Resp()

    monkeypatch.setattr(F.http_requests, "get", _get)
    monkeypatch.setattr(F, "PEXELS_API_KEY", "test-key")
    F.fetch_stock_image(
        "Deventer sluit haven", "De haven van Deventer", "/tmp",
        footage_plan=G.build_footage_plan("Deventer", "city"),
    )
    assert "deventer" not in captured["query"].lower(), captured


# ── 6. generate_footage_queries: the entry point ─────────────────────────────

def _agent_returning(text):
    from unittest.mock import patch as _patch
    from ai_agent import NewsAIAgent
    with _patch("ai_agent.anthropic.Anthropic") as mock_anthropic, \
         _patch("ai_agent._ls_wrap_anthropic", new=lambda c: c):
        client = MagicMock()
        mock_anthropic.return_value = client
        agent = NewsAIAgent(api_key="test-key")
    block = MagicMock()
    block.text = text
    agent.client.messages.create.return_value = MagicMock(
        content=[block], usage=MagicMock(input_tokens=1, output_tokens=1),
    )
    return agent


def test_generate_footage_queries_returns_plan_and_scrubs_place():
    """Even if the model disobeys and names the town, the code strips it."""
    agent = _agent_returning(
        '{"place": "Deventer", "place_type": "city", '
        '"queries": ["deventer harbour", "low water river", "barge deck closeup"], '
        '"avoid": ["recognisable skyline"]}'
    )
    queries, avoid, plan = agent.generate_footage_queries("Deventer sluit haven", "…")
    assert plan["place_mode"] == "no_stock"
    assert plan["place"] == "deventer"
    assert not any("deventer" in q for q in queries), queries
    assert avoid == ["recognisable skyline"]


def test_generate_footage_queries_uses_shipped_prompt_without_secret(monkeypatch):
    """No prompt file and no Secrets Manager entry must still produce queries.

    Raising here would drop the pipeline back to keyword extraction off the
    Dutch headline — reintroducing the exact bug this change removes.
    """
    monkeypatch.delenv("AI_PROMPT_FOOTAGE_QUERIES", raising=False)
    import ai_agent as A
    monkeypatch.setattr(A, "PROMPTS_DIR", Path("/nonexistent"))
    agent = _agent_returning('{"place": "", "place_type": "none", "queries": ["dutch canal"], "avoid": []}')
    queries, _, plan = agent.generate_footage_queries("t", "d")
    assert queries[0] == "dutch canal"
    assert plan["place_mode"] == "none"


def test_generate_footage_queries_failure_returns_valid_plan():
    agent = _agent_returning("not json at all")
    queries, avoid, plan = agent.generate_footage_queries("t", "d")
    assert queries == [] and avoid == []
    assert plan["place_mode"] == "none"  # callers never need to guard the dict


# ── 7. Vision-gate prompt content ────────────────────────────────────────────

def _gate_prompt_text(agent):
    """The text block sent to Haiku in the last validate call."""
    content = agent.client.messages.create.call_args.kwargs["messages"][0]["content"]
    return content[0]["text"]


def test_no_stock_prompt_mentions_season_contradiction():
    """The Twente incident: a frost story ran blooming-summer-fields footage.

    Identifiability alone doesn't catch that — the contradiction check must be
    a first-class criterion, not something the avoid list may or may not carry.
    """
    agent = _agent_returning("1: PASS")
    agent.validate_footage_thumbnails(
        "Frost in Twente", [], ["https://img/1.jpg"], place_mode="no_stock",
    )
    text = _gate_prompt_text(agent)
    assert "season that contradicts" in text
    assert "could name the place" in text


def test_stock_ok_prompt_names_the_place():
    """The Kansai incident: 'wrong country or landmark' was too vague —
    the gate now gets the story's concrete location."""
    agent = _agent_returning("1: PASS")
    agent.validate_footage_thumbnails(
        "Long queues at Schiphol", [], ["https://img/1.jpg"],
        place_mode="stock_ok", place="schiphol",
    )
    text = _gate_prompt_text(agent)
    assert "footage of schiphol is fine" in text
    assert "identifiable as a DIFFERENT place" in text


@pytest.mark.parametrize("mode,place", [
    ("no_stock", "venray"), ("stock_ok", "amsterdam"), ("none", ""),
])
def test_every_mode_gets_the_same_four_tests(mode, place):
    """One criteria block, not two.

    The split that used to live here is the whole reason a heatwave Reel
    shipped cosy-fireplace clips: the relevance question existed only in the
    stock_ok branch and the identifiability question only in the no_stock one,
    so a decorative indoor fire passed whichever branch it landed in.
    """
    agent = _agent_returning("1: PASS")
    agent.validate_footage_thumbnails(
        "Heath fire near Venray", ["mountains or rocky terrain"],
        ["https://img/1.jpg"], place_mode=mode, place=place,
    )
    text = _gate_prompt_text(agent)
    assert "1. SUBJECT" in text and "2. PLACE" in text
    assert "3. TERRAIN" in text and "4. AVOID" in text
    # The measured failures, named so a future edit can't quietly drop them
    assert "fireplace" in text          # decorative stand-in for a wildfire
    assert "rocky river gorges" in text  # terrain the Netherlands does not have
    assert "mountains or rocky terrain" in text  # the story's own avoid terms


def test_gate_explicitly_passes_generically_dutch_wide_shots():
    """The regression this fixes: 'PASS only if close and generic' left the
    account showing nowhere, which reads as fake just like the wrong town did."""
    agent = _agent_returning("1: PASS")
    agent.validate_footage_thumbnails(
        "Drought near Venray", [], ["https://img/1.jpg"], place_mode="no_stock",
    )
    text = _gate_prompt_text(agent)
    assert "polders" in text and "flat farmland" in text
    assert "do not fail them for being wide" in text


def test_vision_batch_limit_matches_caller_batch():
    """The two batch caps must agree, or clips are silently dropped unjudged.

    `footage.fetch_stock_clips` slices its candidates to VALIDATION_BATCH and
    then zips that batch against the verdicts the gate returns. The gate applies
    its own VISION_BATCH_LIMIT cap. If the caller's batch were the larger of the
    two, the surplus clips would never be judged and would vanish from `passed`
    with no audit entry — the invisible-footage-failure class this module exists
    to prevent. The zip is strict=True so a mismatch raises, and this test makes
    it fail in CI rather than mid-render.
    """
    import ai_agent
    import video.footage as F

    assert F.VALIDATION_BATCH <= ai_agent.VISION_BATCH_LIMIT, (
        f"VALIDATION_BATCH ({F.VALIDATION_BATCH}) exceeds VISION_BATCH_LIMIT "
        f"({ai_agent.VISION_BATCH_LIMIT}); the surplus clips would be dropped unjudged"
    )


# ── Vision cost controls ────────────────────────────────────────────────────

def test_thumbnail_downsized_for_pexels():
    """Pexels preview JPEGs are the largest single input in the pipeline —
    15 per call, up to 2 calls per run. The gate answers four yes/no questions
    that do not need full resolution."""
    import video.footage as F

    out = F.thumbnail_for_vision("https://images.pexels.com/videos/1/x.jpg")
    assert f"w={F.VISION_THUMBNAIL_WIDTH}" in out
    assert "auto=compress" in out


def test_thumbnail_replaces_existing_size_params():
    """A stale w/h from the original URL must not fight the new width."""
    import video.footage as F

    out = F.thumbnail_for_vision(
        "https://images.pexels.com/videos/1/x.jpg?w=1200&h=630&cs=srgb"
    )
    assert "w=1200" not in out
    assert "h=630" not in out
    assert f"w={F.VISION_THUMBNAIL_WIDTH}" in out


def test_thumbnail_leaves_foreign_hosts_alone():
    """An unknown CDN may not honour these params; a 404 would fail the clip
    rather than shrink it, which is strictly worse than a large image."""
    import video.footage as F

    url = "https://cdn.example.com/thumb.jpg"
    assert F.thumbnail_for_vision(url) == url


def test_thumbnail_survives_malformed_url():
    import video.footage as F

    assert F.thumbnail_for_vision("") == ""
    assert F.thumbnail_for_vision("not a url") == "not a url"


def test_vision_gate_receives_downsized_thumbnails(monkeypatch):
    """Guards the wiring: the resize must actually reach the API call."""
    import video.footage as F

    F2 = _patch_pexels(monkeypatch, [_vid(1), _vid(2)])
    agent = MagicMock()
    agent.validate_footage_thumbnails.return_value = [True, True]
    F2.fetch_stock_clips(
        title="t", content="c", tmp_dir="/tmp", count=2,
        footage_queries=["dutch canal"], headline="h", ai_agent=agent,
    )
    urls = agent.validate_footage_thumbnails.call_args[0][2]
    assert urls, "no thumbnails passed to the gate"
    for url in urls:
        if "pexels.com" in url:
            assert f"w={F.VISION_THUMBNAIL_WIDTH}" in url
