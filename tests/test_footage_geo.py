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


def test_build_footage_plan_drops_empty_and_duplicate_queries():
    plan = G.build_footage_plan(
        place="Deventer", place_type="city",
        queries=["deventer harbour", "deventer", "deventer harbour", "low water river"],
        avoid=["recognisable skyline"],
    )
    assert plan["place_mode"] == "no_stock"
    assert plan["queries"] == ["harbour", "low water river"]  # empty + dupe dropped
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
    assert queries == ["dutch canal"]
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
    assert "could a viewer name the place" in text


def test_stock_ok_prompt_names_the_place():
    """The Kansai incident: 'wrong country or landmark' was too vague —
    the gate now gets the story's concrete location."""
    agent = _agent_returning("1: PASS")
    agent.validate_footage_thumbnails(
        "Long queues at Schiphol", [], ["https://img/1.jpg"],
        place_mode="stock_ok", place="schiphol",
    )
    text = _gate_prompt_text(agent)
    assert "located in schiphol" in text
    assert "different recognisable location" in text
