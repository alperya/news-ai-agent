"""
Tests for editorial-transparency selection logging (top-7 runner-ups +
selection_reason) and the weekly SelectionReviewer digest.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_agent import NewsAIAgent, SocialMediaPost  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def agent():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            return NewsAIAgent(api_key="test-key")


_ARTICLES = [
    {"title": "Storm destroys monumental windmill", "url": "u1", "source": "nos",
     "description": "d1", "category": "general", "image_url": None},
    {"title": "Swimmer breaks world record", "url": "u2", "source": "nos",
     "description": "d2", "category": "sport", "image_url": None},
]


# ── 1. Parse extracts selection_reason + runner_ups (top-7 transparency) ──────

def test_parse_extracts_reason_and_runner_ups(agent):
    raw = json.dumps({
        "selected_articles": [{
            "article_index": 1,
            "hook": "A storm flattens an icon",
            "content": "text",
            "emoji": "🌪️",
            "hashtags": ["#Netherlands"],
            "selection_reason": "Tier 1a — storm damage, strong visuals.",
        }],
        "runner_ups": [
            {"title": "Swimmer breaks world record", "tier": "Tier 1c",
             "reason_not_selected": "Strong but niche audience."},
        ],
    })
    posts = agent._parse_batch_response(raw, _ARTICLES, "instagram")
    assert len(posts) == 1
    p = posts[0]
    assert p.selection_reason.startswith("Tier 1a")
    assert len(p.runner_ups) == 1
    assert p.runner_ups[0]["title"] == "Swimmer breaks world record"
    # persisted to the post record
    d = p.to_dict()
    assert d["selection_reason"].startswith("Tier 1a")
    assert d["runner_ups"][0]["tier"] == "Tier 1c"


# ── 2. Back-compat: old response without the new fields must not crash ────────

def test_parse_back_compat_without_new_fields(agent):
    raw = json.dumps({
        "selected_articles": [{
            "article_index": 2,
            "hook": "Record falls",
            "content": "text",
            "emoji": "🏊",
            "hashtags": ["#Swimming"],
        }],
    })
    posts = agent._parse_batch_response(raw, _ARTICLES, "instagram")
    assert len(posts) == 1
    assert posts[0].selection_reason == ""
    assert posts[0].runner_ups == []
    assert posts[0].to_dict()["runner_ups"] == []


# ── 3. Malformed runner_ups type is tolerated ────────────────────────────────

def test_parse_tolerates_bad_runner_ups_type(agent):
    raw = json.dumps({
        "selected_articles": [{"article_index": 1, "content": "t", "emoji": "x",
                               "hashtags": []}],
        "runner_ups": "not-a-list",
    })
    posts = agent._parse_batch_response(raw, _ARTICLES, "instagram")
    assert posts[0].runner_ups == []


# ── 4. Dedup instruction distinguishes specific event vs broad theme ──────────

def test_dedup_prompt_allows_escalating_followups(agent, monkeypatch):
    # The dedup wording is built in CODE (recent_publications), not the prompt
    # template — so inject a minimal template to stay independent of the
    # gitignored prompts/batch_selection.txt (absent in CI).
    template = (
        "{recent_publications}ARTICLES:{articles_text}\n"
        "count={article_count} max={max_posts} platform={platform} "
        "len={max_length} {hashtag_instruction}"
    )
    monkeypatch.setattr(NewsAIAgent, "_load_prompt",
                        staticmethod(lambda *a, **k: template))
    prompt = agent._create_batch_selection_prompt(
        _ARTICLES, max_posts=1, platform="instagram",
        recently_published=["Code red heat in eight provinces"],
    )
    # Topic-arc dedup: same event + same angle is blocked, escalation is allowed
    assert "SAME EVENT + SAME ANGLE ONLY" in prompt
    assert "EXPLICITLY ALLOWED" in prompt
    assert "NEW HARD NUMBER" in prompt
    # A continuation must be framed as an escalation, not a re-report
    assert "escalation frame" in prompt


# ── 5. SelectionReviewer: caption-prefix engagement join ──────────────────────

@pytest.fixture
def reviewer():
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("boto3.client"), patch("boto3.resource"), patch("anthropic.Anthropic"):
            from selection_reviewer import SelectionReviewer
            return SelectionReviewer()


def test_attach_engagement_matches_by_caption(reviewer):
    runs = [{
        "when": __import__("datetime").datetime(2026, 6, 27, 20, 41,
                                                tzinfo=__import__("datetime").timezone.utc),
        "content": "Marrit Steenbergen has broken the world record in the 100-meter freestyle",
        "full_post": "🏊 Marrit Steenbergen has broken the world record in the 100-meter freestyle ...",
        "engagement": None,
    }]
    metrics = [{
        "caption_preview": "🏊 Marrit Steenbergen has broken the world record in the 100-meter freestyle. The reigning",
        "normalized_engagement_rate": "1.23",
        "reach": "5000", "saves": "40", "comments": "8", "post_type": "reel",
        "published_at": "2026-06-27T20:50:00+00:00",
    }]
    reviewer._attach_engagement(runs, metrics)
    assert runs[0]["engagement"] is not None
    assert runs[0]["engagement"]["normalized_engagement_rate"] == "1.23"


# ── 6. SelectionReviewer: email renders chosen + runner-ups ───────────────────

def test_format_email_lists_chosen_and_runner_ups(reviewer):
    import datetime as dt
    runs = [{
        "when": dt.datetime(2026, 6, 28, 9, 0, tzinfo=dt.timezone.utc),
        "pool_size": 27,
        "chosen_title": "Shorttracker Roes missing",
        "selection_reason": "Tier 2 — emotional human interest.",
        "runner_ups": [
            {"title": "Storm destroys windmill", "tier": "Tier 1a",
             "reason_not_selected": "Skipped as weather already covered."},
        ],
        "engagement": {"normalized_engagement_rate": "0.40"},
    }]
    body = reviewer._format_email(runs, "AI review text here.", days=7)
    assert "Shorttracker Roes missing" in body
    assert "Storm destroys windmill" in body
    assert "AI review text here." in body
    assert "0.40% NER" in body
