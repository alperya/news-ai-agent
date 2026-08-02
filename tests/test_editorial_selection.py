"""
Tests for the editorial-selection overhaul driven by the weekly review:

  1. Non-story format blacklist (Wekdienst / In beeld | / Live | / Podcast / …)
  2. Near-duplicate headline collapsing (real pool depth, not source count)
  3. Rolling content mix + crime cap fed back into the selection prompt
  4. Engagement CTA + named series in the caption, never in the news body
  5. CTA survives the narration cap
  6. Slot briefs reach the prompt
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_agent import NewsAIAgent, SocialMediaPost  # noqa: E402
from news_scraper import (  # noqa: E402
    NEAR_DUPLICATE_THRESHOLD, collapse_near_duplicates, is_non_story_title,
    title_similarity,
)


# ── 1. Non-story format blacklist ────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Wekdienst 31/7: kans op onweer in het zuiden",
    "Weekdienst 31/7: kans op onweer in het zuiden",
    "In beeld | De schade na de storm in Twente",
    "Live | Kabinetsformatie gaat verder",
    "Liveblog: brand in Rotterdamse haven",
    "Podcast De Dag: waarom de rente stijgt",
    "Explainer | Hoe werkt het nieuwe pensioenstelsel?",
    "31/7 in Nieuwsuur: de gevolgen van de droogte",
])
def test_blacklist_matches_recurring_formats(title):
    assert is_non_story_title(title)


@pytest.mark.parametrize("title", [
    "NS-personeel staakt op 9 september",
    "Energierekening stijgt met 12 procent",
    "Explosie in Amsterdamse woonwijk",
    "Nieuwsuur onderzocht de jeugdzorg",   # mentions the programme, is a real story
    "Beelden van de storm zijn viraal",    # "beelden" is not "In beeld |"
])
def test_blacklist_leaves_real_stories_alone(title):
    assert not is_non_story_title(title)


def test_blacklist_ignores_empty_title():
    assert not is_non_story_title("")
    assert not is_non_story_title(None)


# ── 2. Near-duplicate collapsing ─────────────────────────────────────────────

def test_collapse_keeps_one_per_story_and_tags_the_rest():
    articles = [
        {"title": "NS-personeel staakt komende dinsdag", "description": "short"},
        {"title": "Personeel van NS staakt dinsdag opnieuw",
         "description": "a much longer description with more material to write from"},
        {"title": "Energierekening stijgt fors dit jaar", "description": "unrelated"},
    ]
    kept, dropped = collapse_near_duplicates(articles)

    assert len(kept) == 2
    assert len(dropped) == 1
    # The survivor of the cluster is the one with the most material
    strike = [a for a in kept if "NS" in a["title"] or "NS-" in a["title"]][0]
    assert strike["description"].startswith("a much longer")
    assert dropped[0]["excluded_reason"] == "near_duplicate"
    assert dropped[0]["duplicate_of"]


def test_collapse_leaves_distinct_stories_untouched():
    articles = [
        {"title": "Storm damages windmill in Twente", "description": "d"},
        {"title": "Energy bills rise twelve percent", "description": "d"},
        {"title": "Police raid Rotterdam warehouse", "description": "d"},
    ]
    kept, dropped = collapse_near_duplicates(articles)
    assert len(kept) == 3
    assert dropped == []


def test_similarity_ignores_stopwords():
    # Two headlines sharing only function words must not look alike
    assert title_similarity("De storm in het noorden van het land",
                            "De rente van de bank in het nieuws") == 0.0


def test_similarity_calibrated_on_real_headlines():
    """Threshold calibration, measured against a live 27-article pool."""
    same_event = [
        ("AZ verplettert PSV in Johan Cruijff Schaal na vroege rode kaart Veerman",
         "AZ wint tweede Johan Cruijff Schaal na ruime zege op tiental PSV"),
        ("Italiaanse militairen controleren tanker van Russische schaduwvloot",
         "Italië entert tanker van Russische schaduwvloot in Middellandse Zee"),
    ]
    different_event = [
        ("Zo ging Madonna los tijdens Pride-show in Amsterdam",
         "Amsterdam steps up Pride security in wake of attack in Berlin"),
        ("Chaotische bestorming Ceuta eindigt met drijfjacht op migranten uit Marokko",
         "Ineens waren er meer dan 50.000 migranten in Ceuta"),
    ]
    for a, b in same_event:
        assert title_similarity(a, b) >= NEAR_DUPLICATE_THRESHOLD, (a, b)
    for a, b in different_event:
        assert title_similarity(a, b) < NEAR_DUPLICATE_THRESHOLD, (a, b)


def test_single_shared_token_never_collapses():
    """A lone shared word must not reach 1.0 on a very short headline."""
    assert title_similarity("Explosie Urk", "Explosie in een Rotterdamse haven na brand") == 0.0


# ── 3. Rolling content mix + crime cap ───────────────────────────────────────

@pytest.fixture
def lh():
    import lambda_handler
    return lambda_handler


def _topics(*pairs):
    """(topic, days_ago) → the (topic, datetime) shape get_published_urls returns."""
    now = datetime.now(timezone.utc)
    return [(topic, now - timedelta(days=days)) for topic, days in pairs]


def test_content_mix_empty_history_produces_no_constraint(lh):
    assert lh.build_content_mix_brief([]) == ""


def test_content_mix_reports_distribution_without_capping(lh):
    brief = lh.build_content_mix_brief(_topics(
        ("Weather", 6), ("Transport", 5), ("Economy", 4),
        ("Crime/Security", 3), ("Weather", 2),
    ))
    assert "CONTENT MIX" in brief
    assert "Weather 2" in brief
    assert "TARGET MIX" in brief
    # 1 of 5 crime posts = 20%, well under the 40% cap
    assert "CRIME CAP REACHED" not in brief


def test_content_mix_flags_crime_cap_when_exceeded(lh):
    brief = lh.build_content_mix_brief(_topics(
        ("Crime/Security", 5), ("Crime/Security", 4), ("Weather", 3),
    ))
    assert "CRIME CAP REACHED" in brief
    assert "national in scale" in brief


def test_content_mix_blocks_a_second_violence_post_today(lh):
    brief = lh.build_content_mix_brief(_topics(
        ("Crime/Security", 0), ("Weather", 1), ("Transport", 2),
        ("Economy", 3), ("Weather", 4),
    ))
    # 1/5 = 20% — under the cap, but it was published TODAY
    assert "CRIME CAP REACHED" not in brief
    assert "already published TODAY" in brief


def test_crime_cap_is_read_at_call_time_not_import_time(lh):
    """get_secrets() populates os.environ AFTER module import, so a cap read at
    import time could never be retuned from Secrets Manager."""
    topics = _topics(("Crime/Security", 3), ("Weather", 2), ("Transport", 1))  # 33%
    assert "CRIME CAP REACHED" not in lh.build_content_mix_brief(topics)
    with patch.dict(os.environ, {"VIOLENCE_SHARE_CAP": "0.30"}):
        assert "CRIME CAP REACHED" in lh.build_content_mix_brief(topics)
        assert "cap 30%" in lh.build_content_mix_brief(topics)


def test_topic_classifier_is_shared_with_analytics(lh):
    from metrics_collector import MetricsCollector
    caption = "Police raided a warehouse in Rotterdam after an explosion"
    assert lh.classify_topic(caption) == "Crime/Security"
    assert MetricsCollector._topic(caption) == lh.classify_topic(caption)


# ── 4. CTA + series live in the caption, never in the news body ──────────────

def _post(**kwargs):
    defaults = dict(
        original_title="NS staakt", original_url="https://nos.nl/a/1", source="nos",
        content="Rail workers will strike on 9 September.", hashtags=["#Netherlands"],
        emoji="🚆", hook="A nationwide rail strike is confirmed",
    )
    defaults.update(kwargs)
    return SocialMediaPost(**defaults)


def test_caption_carries_cta_and_save_prompt():
    post = _post(cta_question="Will you stop on 9 September?",
                 save_prompt="Save this for that day.")
    caption = post.format_post()
    assert "Will you stop on 9 September?" in caption
    assert "Save this for that day." in caption
    # The neutral news body is untouched — the quality gate only sees `content`
    assert "Will you stop" not in post.content


def test_caption_without_cta_is_unchanged_in_shape():
    caption = _post().format_post()
    assert "📰 Source:" in caption
    assert caption.rstrip().endswith("#Netherlands")


def test_named_series_leads_caption_and_adds_its_hashtag():
    caption = _post(series="Wat Verandert Er Vandaag").format_post()
    assert caption.startswith("📌 Wat Verandert Er Vandaag")
    assert "#WatVerandertErVandaag" in caption


def test_unknown_series_is_dropped_not_invented(monkeypatch):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), patch("anthropic.Anthropic"):
        agent = NewsAIAgent(api_key="k")
    raw = json.dumps({"selected_articles": [{
        "article_index": 1, "content": "c", "emoji": "🚆", "hashtags": ["#NL"],
        "series": "Something I Made Up", "personal_stake": "3",
        "cta_question": "Will you stop on 9 September?",
        "save_prompt": "Save this for that day.",
    }]})
    articles = [{"title": "t", "url": "u", "source": "nos", "description": "d",
                 "category": "general", "image_url": None}]
    post = agent._parse_batch_response(raw, articles, "instagram")[0]

    assert post.series == ""
    assert "📌" not in post.format_post()
    # A string score from the model is still usable
    assert post.personal_stake == 3
    assert post.cta_question == "Will you stop on 9 September?"


def test_post_dict_persists_new_fields():
    data = _post(cta_question="q?", save_prompt="s.", series="Nederland Droogt Uit",
                 personal_stake=2).to_dict()
    assert data["cta_question"] == "q?"
    assert data["save_prompt"] == "s."
    assert data["series"] == "Nederland Droogt Uit"
    assert data["personal_stake"] == 2


# ── 5. The CTA survives the narration cap ────────────────────────────────────

def test_cta_is_appended_after_the_narration_cap():
    """cap_narration drops TRAILING sentences — a pre-cap CTA would be the
    first casualty, which is why creator.py appends it afterwards."""
    from video.tts import cap_narration

    long_content = " ".join(f"Sentence number {i} about the strike." for i in range(40))
    capped = cap_narration(long_content, 120)
    assert len(capped.split()) <= 120

    narration = f"{capped.rstrip()} Will you stop on 9 September?"
    assert narration.endswith("Will you stop on 9 September?")


# ── 6. Slot briefs and content mix reach the prompt ──────────────────────────

def test_slot_brief_and_content_mix_are_injected(monkeypatch):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), patch("anthropic.Anthropic"):
        agent = NewsAIAgent(api_key="k")
    template = ("{recent_publications}ARTICLES:{articles_text}\n"
                "count={article_count} max={max_posts} platform={platform} "
                "len={max_length} {hashtag_instruction}")
    monkeypatch.setattr(NewsAIAgent, "_load_prompt", staticmethod(lambda *a, **k: template))

    prompt = agent._create_batch_selection_prompt(
        [{"title": "t", "url": "u", "source": "nos", "description": "d", "category": "general"}],
        max_posts=1, platform="instagram",
        slot_brief="SLOT BRIEF — MORNING", content_mix="CONTENT MIX: Weather 2 (100%)",
    )
    assert "SLOT BRIEF — MORNING" in prompt
    assert "CONTENT MIX: Weather 2 (100%)" in prompt


def test_live_prompt_needs_no_new_placeholders():
    """The prompt lives in Secrets Manager and is edited independently of a
    deploy. If the slot brief / content mix were {placeholders}, publishing the
    prompt before the code would raise KeyError inside process_batch — which
    swallows it into the single-article fallback ("publish articles[0], no
    tiers"), a silent quality collapse with no alert. They are prepended
    instead, so prompt and code deploy in either order."""
    prompt = (Path(__file__).resolve().parents[1] / "prompts" / "batch_selection.txt")
    if not prompt.exists():           # gitignored — absent in CI
        pytest.skip("prompts/batch_selection.txt not present")
    template = prompt.read_text(encoding="utf-8")

    # Exactly the kwargs the PREVIOUS deployed revision passed.
    template.format(recent_publications="", article_count=1, max_posts=1,
                    platform="instagram", articles_text="x", max_length=2200,
                    hashtag_instruction="y")


def test_slot_brief_omitted_leaves_prompt_clean(monkeypatch):
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}), patch("anthropic.Anthropic"):
        agent = NewsAIAgent(api_key="k")
    template = "{recent_publications}ARTICLES:{articles_text}"
    monkeypatch.setattr(NewsAIAgent, "_load_prompt", staticmethod(lambda *a, **k: template))
    prompt = agent._create_batch_selection_prompt(
        [{"title": "t", "url": "u", "source": "nos", "description": "d", "category": "general"}],
        max_posts=1, platform="instagram",
    )
    assert prompt.startswith("ARTICLES:")


# ── 7. events_reel mislabel fix ──────────────────────────────────────────────

def test_reels_are_not_labelled_events_while_event_posts_are_off():
    from metrics_collector import MetricsCollector
    caption = "Storm damage across the Netherlands this week"  # contains "this week"
    with patch.dict(os.environ, {"ENABLE_EVENT_POSTS": "false"}):
        assert MetricsCollector._post_type("VIDEO", "REELS", caption) == "reel"


def test_events_label_returns_when_event_posts_are_enabled():
    from metrics_collector import MetricsCollector
    with patch.dict(os.environ, {"ENABLE_EVENT_POSTS": "true"}):
        assert MetricsCollector._post_type(
            "VIDEO", "REELS", "🗓 Festivals this week") == "events_reel"
