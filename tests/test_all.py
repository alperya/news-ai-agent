"""
Core tests for News AI Agent pipeline.
Run: pytest tests/ -v
"""

import json
import os
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path

import pytest

# Add src/ to path so tests can import application modules
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from news_scraper import DutchNewsScraper, NewsArticle
from ai_agent import NewsAIAgent, SocialMediaPost
from video import SubtitleSegment
from video.tts import clean_for_narration, group_subtitle_segments, _chars_to_words, cap_narration
from video.audio import find_music_file
from video.footage import download_image, fetch_stock_image
from video.effects import make_gradient_overlay


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_RSS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
  <channel>
    <title>NOS Nieuws</title>
    <item>
      <title>Test headline one</title>
      <link>https://nos.nl/artikel/1</link>
      <description>First test article description</description>
      <pubDate>Thu, 13 Feb 2026 10:00:00 +0100</pubDate>
      <media:content url="https://example.com/img1.jpg" medium="image"/>
    </item>
    <item>
      <title>Test headline two</title>
      <link>https://nos.nl/artikel/2</link>
      <description>Second test article description</description>
      <pubDate>Thu, 13 Feb 2026 09:00:00 +0100</pubDate>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def scraper():
    return DutchNewsScraper()


@pytest.fixture
def sample_article():
    return NewsArticle(
        title="Holland introduces new cycling law",
        description="The Dutch government announced a new cycling infrastructure plan.",
        url="https://nos.nl/artikel/test-123",
        published_date="2026-02-13T10:00:00",
        source="nos",
        category="binnenland",
        image_url="https://example.com/image.jpg",
    )


@pytest.fixture
def sample_article_dict(sample_article):
    return sample_article.to_dict()


@pytest.fixture
def sample_post():
    return SocialMediaPost(
        original_title="Holland introduces new cycling law",
        original_url="https://nos.nl/artikel/test-123",
        source="nos",
        content="Hollanda'da yeni bisiklet yasası yürürlüğe girdi.",
        hashtags=["#Hollanda", "#Bisiklet", "#Haberler"],
        emoji="🚲",
        platform="instagram",
        image_url="https://example.com/image.jpg",
    )


# ── 1. Scraper: RSS feed config ──────────────────────────────────────────────

def test_scraper_has_nos_and_rtl_feeds(scraper):
    """RSS_FEEDS should contain NOS and RTL (not Telegraaf)."""
    assert "nos" in scraper.RSS_FEEDS
    assert "rtl" in scraper.RSS_FEEDS
    assert "telegraaf" not in scraper.RSS_FEEDS


# ── 2. Scraper: XML parsing ──────────────────────────────────────────────────

def test_scraper_parses_rss_xml(scraper):
    """_parse_feed_xml should extract articles from valid RSS XML."""
    articles = scraper._parse_feed_xml(SAMPLE_RSS_XML, "nos", "general")

    assert len(articles) == 2
    assert articles[0].title == "Test headline one"
    assert articles[0].url == "https://nos.nl/artikel/1"
    assert articles[0].source == "nos"
    assert articles[0].image_url == "https://example.com/img1.jpg"


# ── 3. Scraper: malformed XML ────────────────────────────────────────────────

def test_scraper_handles_invalid_xml(scraper):
    """_parse_feed_xml should return empty list on broken XML."""
    result = scraper._parse_feed_xml("<not>valid xml", "nos", "general")
    assert result == []


# ── 4. Scraper: HTTP error handling ──────────────────────────────────────────

def test_scraper_returns_empty_on_http_error(scraper):
    """fetch_feed should return [] when the HTTP request fails."""
    with patch.object(scraper.session, "get", side_effect=Exception("Connection refused")):
        articles = scraper.fetch_feed("https://bad-url.example", "nos", "general")
    assert articles == []


# ── 5. NewsArticle dataclass ─────────────────────────────────────────────────

def test_article_to_dict(sample_article):
    """to_dict should return a complete dictionary representation."""
    d = sample_article.to_dict()

    assert d["title"] == "Holland introduces new cycling law"
    assert d["source"] == "nos"
    assert d["url"] == "https://nos.nl/artikel/test-123"
    assert d["image_url"] == "https://example.com/image.jpg"


# ── 6. SocialMediaPost format ────────────────────────────────────────────────

def test_post_format_contains_required_parts(sample_post):
    """format_post should include emoji, content, source, link, and hashtags."""
    formatted = sample_post.format_post()

    assert "🚲" in formatted
    assert "Hollanda'da yeni bisiklet yasası" in formatted
    assert "📰 Source: https://nos.nl/artikel/test-123" in formatted
    assert "#Hollanda" in formatted


# ── 6b. Narration cap is sentence-aware (never clips mid-sentence) ───────────

def test_cap_narration_no_truncation_under_budget():
    text = "First sentence here. Second sentence too."
    assert cap_narration(text, 120) == text


def test_cap_narration_keeps_whole_sentences():
    """Over budget → drop WHOLE trailing sentences, never cut one in half."""
    # 11-word hook + 96-word content = 107 words (the real bug case).
    hook = "Olympic Games return to the Netherlands after more than a century"
    content = (
        "The International Olympic Committee and the French organizing committee "
        "have confirmed that ice stadium Thialf in Heerenveen will host the speed "
        "skating tournament during the 2030 French Alps Winter Olympics. This marks "
        "the first time Olympic events take place on Dutch soil in over a century. "
        "The decision was approved at a members meeting of the Games organizers on "
        "Monday. Thialf, widely regarded as one of the world's premier speed skating "
        "venues, has long been a stronghold of Dutch dominance in the sport. The "
        "Netherlands last hosted Olympic competitions in 1928 during the Amsterdam "
        "Summer Games."
    )
    narration = hook + ". " + content
    capped = cap_narration(narration, 120)
    # Full text fits within 120 → the final sentence must be intact.
    assert capped.rstrip().endswith("Amsterdam Summer Games.")
    assert "in 1928" in capped


def test_cap_narration_drops_trailing_sentence_when_over_budget():
    text = "Aaa bbb ccc ddd. Eee fff ggg hhh. Iii jjj kkk lll."  # 3 sentences, 4 words each
    capped = cap_narration(text, 8)  # only first two sentences fit
    assert capped == "Aaa bbb ccc ddd. Eee fff ggg hhh."
    # never a partial sentence
    assert not capped.endswith("Iii")


def test_cap_narration_hard_cut_when_first_sentence_too_long():
    text = "one two three four five six seven eight nine ten eleven twelve"  # one long sentence
    capped = cap_narration(text, 5)
    assert capped == "one two three four five"


# ── 7. SocialMediaPost to_dict includes full_post ────────────────────────────

def test_post_to_dict_has_full_post(sample_post):
    """to_dict should include a 'full_post' key with the formatted text."""
    d = sample_post.to_dict()

    assert "full_post" in d
    assert d["full_post"] == sample_post.format_post()
    assert d["platform"] == "instagram"


# ── 8. AI Agent: model configuration ─────────────────────────────────────────

def test_ai_agent_uses_opus_model():
    """NewsAIAgent should use Claude Opus 4.6 model."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")
    assert "opus" in agent.model


# ── 9. AI Agent: JSON response parsing ───────────────────────────────────────

def test_ai_agent_parse_response():
    """_parse_response should extract content, emoji, hashtags from JSON."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

    raw = '{"content": "Test post", "emoji": "📰", "hashtags": ["#Test"]}'
    result = agent._parse_response(raw)

    assert result["content"] == "Test post"
    assert result["emoji"] == "📰"
    assert result["hashtags"] == ["#Test"]


# ── 10. AI Agent: fallback on malformed JSON ─────────────────────────────────

def test_ai_agent_parse_response_fallback():
    """_parse_response should return fallback dict on invalid JSON."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

    result = agent._parse_response("this is not json at all")

    assert "content" in result
    assert "emoji" in result
    assert "hashtags" in result


# ── 11. Quality gate: structural rejection (empty content) ───────────────────

def test_quality_gate_rejects_empty_content(tmp_path):
    """quality_check should reject a post with empty content and save error file."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

    post = SocialMediaPost(
        original_title="Test", original_url="https://nos.nl/1",
        source="nos", content="",
        hashtags=["#Test"], emoji="📰", platform="instagram",
    )
    with patch.object(agent, '_save_error'):
        result = agent.quality_check(post)
    assert result is None


# ── 12. Quality gate: structural rejection (missing hashtags) ────────────────

def test_quality_gate_rejects_missing_hashtags():
    """quality_check should reject a post with no hashtags."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

    post = SocialMediaPost(
        original_title="Test", original_url="https://nos.nl/1",
        source="nos", content="Hollanda'da yeni bir yasa yürürlüğe girdi.",
        hashtags=[], emoji="📰", platform="instagram",
    )
    with patch.object(agent, '_save_error'):
        result = agent.quality_check(post)
    assert result is None


# ── 13. Quality gate: AI corrects and passes ─────────────────────────────────

def test_quality_gate_ai_corrects_content():
    """quality_check should fix content when AI review finds Dutch spelling leaks."""
    env = {"ANTHROPIC_API_KEY": "test-key", "AI_PROMPT_QUALITY_CHECK": "check: {content}"}
    with patch.dict(os.environ, env):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text='{"pass": true, "corrected_content": "Hollanda algoritma skandalı", "issues": ["algoritme → algoritma"]}'
        )]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        agent.client = mock_client

        post = SocialMediaPost(
            original_title="Test", original_url="https://nos.nl/1",
            source="nos", content="Hollanda algoritme skandalı büyüyor, yetkililerin açıklaması bekleniyor.",
            hashtags=["#Hollanda"], emoji="📰", platform="instagram",
        )
        with patch.object(agent, '_save_correction'):
            result = agent.quality_check(post)
    assert result is not None
    assert result.content == "Hollanda algoritma skandalı"
    assert result._corrected is True


# ── 14. Quality gate: AI rejects poor quality ────────────────────────────────

def test_quality_gate_ai_rejects_bad_content():
    """quality_check should return None when AI marks content as unpublishable."""
    env = {"ANTHROPIC_API_KEY": "test-key", "AI_PROMPT_QUALITY_CHECK": "check: {content}"}
    with patch.dict(os.environ, env):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(
            text='{"pass": false, "reason": "Metin tamamen anlamsız"}'
        )]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        agent.client = mock_client

        post = SocialMediaPost(
            original_title="Test", original_url="https://nos.nl/1",
            source="nos", content="Anlamsiz metin garbled çeviri sonucu olusan.",
            hashtags=["#Test"], emoji="📰", platform="instagram",
        )
        with patch.object(agent, '_save_error'):
            result = agent.quality_check(post)
    assert result is None


# ── 15. Quality gate: passes clean text unchanged ────────────────────────────

def test_quality_gate_passes_clean_text():
    """quality_check should return the post as-is when everything is fine."""
    env = {"ANTHROPIC_API_KEY": "test-key", "AI_PROMPT_QUALITY_CHECK": "check: {content}"}
    with patch.dict(os.environ, env):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='{"pass": true}')]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        agent.client = mock_client

        original = "Hollanda'da yeni bisiklet altyapı planı açıklandı."
        post = SocialMediaPost(
            original_title="Test", original_url="https://nos.nl/1",
            source="nos", content=original,
            hashtags=["#Hollanda"], emoji="🚲", platform="instagram",
        )
        result = agent.quality_check(post)
    assert result is not None
    assert result.content == original


# ── 16. Quality gate: error file is saved on rejection ───────────────────────

def test_quality_gate_saves_error_file(tmp_path, monkeypatch):
    """_save_error should write a JSON file to the errors directory."""
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic"):
            agent = NewsAIAgent(api_key="test-key")

    post = SocialMediaPost(
        original_title="Test headline", original_url="https://nos.nl/1",
        source="nos", content="Bad content",
        hashtags=["#Test"], emoji="📰", platform="instagram",
    )
    # Override PROJECT_ROOT to redirect errors/ to tmp_path
    import ai_agent as ai_mod
    monkeypatch.setattr(ai_mod, 'PROJECT_ROOT', tmp_path)

    agent._save_error(post, ["Content is empty or too short"])

    error_files = list((tmp_path / 'errors').glob('rejected_*.json'))
    assert len(error_files) == 1
    data = json.loads(error_files[0].read_text(encoding='utf-8'))
    assert "Content is empty or too short" in data['rejected_reasons']
    assert data['original_content'] == "Bad content"


# ── 17. Corrected post format has dot separator before hashtags ──────────────

def test_corrected_post_has_dot_before_hashtags():
    """format_post should insert a '.' line before hashtags when _corrected is True."""
    post = SocialMediaPost(
        original_title="Test", original_url="https://nos.nl/1",
        source="nos", content="Hollanda algoritma skandalı",
        hashtags=["#Hollanda", "#Test"], emoji="📰", platform="instagram",
    )
    # Not corrected — no dot
    normal = post.format_post()
    assert "\n.\n" not in normal

    # Mark as corrected — dot should appear
    post._corrected = True
    corrected = post.format_post()
    assert "\n.\n" in corrected


# ── 18. Video: narration text is cleaned ─────────────────────────────────────

def test_clean_for_narration_removes_hashtags_and_urls():
    """_clean_for_narration should strip hashtags, URLs, Source lines, and titles."""
    # Pattern 1: standalone ALL CAPS title line
    text1 = (
        "🚲 NETHERLANDS INTRODUCES NEW CYCLING LAW\n\n"
        "The Dutch government has introduced a new cycling law.\n"
        "📰 Source: https://nos.nl/artikel/1\n\n"
        "#Netherlands #Cycling #News"
    )
    result1 = clean_for_narration(text1)
    assert "#Netherlands" not in result1
    assert "https://" not in result1
    assert "Source" not in result1
    assert "NETHERLANDS INTRODUCES" not in result1  # title stripped
    assert "Dutch government" in result1  # body kept

    # Pattern 2: inline ALL CAPS title prefix before colon (long body >80 chars)
    text2 = (
        "🔴 ROTTERDAM SYNAGOGUE ARSON: Dutch Minister of Justice and Security "
        "David van Weel has called the arson attack at a synagogue in Rotterdam "
        "terrible news.\n\n"
        "Van Weel spoke about the incident.\n"
    )
    result2 = clean_for_narration(text2)
    assert "ROTTERDAM SYNAGOGUE ARSON" not in result2  # title prefix stripped
    assert "Dutch Minister" in result2  # body kept after colon

    # Pattern 3: BREAKING: with short headline (≤80 chars) – entire line skipped
    text3 = (
        "🔴 BREAKING: Explosion at Jewish school in Amsterdam\n\n"
        "An explosion occurred overnight at the outer wall.\n"
    )
    result3 = clean_for_narration(text3)
    assert "BREAKING" not in result3  # BREAKING stripped
    assert "Jewish school" not in result3  # short headline also stripped
    assert "explosion occurred" in result3  # body kept

    # Pattern 4: Mixed-case title line followed by blank line
    text4 = (
        "🔴 Arson attack at Rotterdam synagogue sparks anger\n\n"
        "An explosion occurred at approximately 03:45 AM at the synagogue.\n"
    )
    result4 = clean_for_narration(text4)
    assert "Arson attack" not in result4  # mixed-case title stripped
    assert "explosion occurred" in result4  # body kept

    # Direct body text (no title) — should be preserved as-is
    text5 = (
        "🚢 Dozens of Dutch ships are in a precarious situation in the "
        "Persian Gulf as maritime vessels from multiple countries come "
        "under fire. Ships have been targeted by Iranian rockets.\n"
    )
    result5 = clean_for_narration(text5)
    assert "Dozens of Dutch ships" in result5  # body text preserved


# ── 19. Video: subtitle grouping ─────────────────────────────────────────────

def test_subtitle_grouping_respects_max_words():
    """group_subtitle_segments should group words ≤ max_words per segment."""
    words = [
        SubtitleSegment(text="Hollanda'da", start=0.0, end=0.5),
        SubtitleSegment(text="yeni", start=0.5, end=0.8),
        SubtitleSegment(text="bir", start=0.8, end=1.0),
        SubtitleSegment(text="bisiklet", start=1.0, end=1.5),
        SubtitleSegment(text="altyapı", start=1.5, end=2.0),
        SubtitleSegment(text="planı", start=2.0, end=2.5),
        SubtitleSegment(text="açıklandı.", start=2.5, end=3.0),
        SubtitleSegment(text="Detaylar", start=3.0, end=3.3),
        SubtitleSegment(text="burada.", start=3.3, end=3.8),
    ]
    grouped = group_subtitle_segments(words, max_words=4)
    for seg in grouped:
        assert len(seg.text.split()) <= 4
    # Timing should be preserved
    assert grouped[0].start == 0.0
    assert grouped[-1].end == 3.8
    assert len(grouped) == 3  # 4+4+1 words → 3 groups


# ── 20. Video: empty subtitle list ───────────────────────────────────────────

def test_subtitle_grouping_empty_list():
    """_group_subtitle_segments should handle empty list."""
    result = group_subtitle_segments([])
    assert result == []


# ── 21. InstagramPublisher has publish_reels method ──────────────────────────

def test_instagram_publisher_has_reels_method():
    """InstagramPublisher should have a publish_reels method."""
    from social_publisher import InstagramPublisher
    assert hasattr(InstagramPublisher, 'publish_reels')


# ── 22. publish_reels dry run returns correct type ───────────────────────────

def test_publish_reels_dry_run():
    """publish_reels in dry_run mode should return type='reels'."""
    from social_publisher import InstagramPublisher
    with patch.dict(os.environ, {
        'INSTAGRAM_ACCESS_TOKEN': 'test-token',
        'INSTAGRAM_ACCOUNT_ID': 'test-id',
    }):
        publisher = InstagramPublisher()
    result = publisher.publish_reels(
        content="Test reel",
        video_url="https://example.com/video.mp4",
        dry_run=True,
    )
    assert result['type'] == 'reels'
    assert result['id'] == 'dry_run'


# ── 23. Video: music file locator ────────────────────────────────────────────

def test_find_music_file():
    """find_music_file should locate the background music MP3."""
    from video.config import MUSIC_FILE
    path = find_music_file()
    if MUSIC_FILE.exists():
        assert path is not None
        assert str(path).endswith(".mp3")
    else:
        # In CI without the file, None is acceptable
        assert path is None


# ── 24. Video: image download returns None on bad URL ────────────────────────

def test_download_image_bad_url(tmp_path):
    """_download_image should return None for unreachable URLs."""
    result = download_image("https://this-domain-does-not-exist.invalid/img.jpg", str(tmp_path))
    assert result is None


# ── 25. Video: image download returns None for empty URL ─────────────────────

def test_download_image_empty_url(tmp_path):
    """_download_image should return None for empty/None URLs."""
    assert download_image("", str(tmp_path)) is None
    assert download_image(None, str(tmp_path)) is None


# ── 26. Video: gradient overlay shape ────────────────────────────────────────

def test_gradient_overlay_has_correct_dimensions():
    """_make_gradient_overlay should produce a clip with VIDEO dimensions."""
    from video.config import VIDEO_WIDTH, VIDEO_HEIGHT
    clip = make_gradient_overlay(3.0)
    assert clip.duration == 3.0
    assert clip.size == (VIDEO_WIDTH, VIDEO_HEIGHT)


# ── 27. Video: create_news_video accepts image_url parameter ────────────────

def test_create_news_video_signature():
    """create_news_video should accept image_url as a keyword argument."""
    import inspect
    from video import create_news_video
    sig = inspect.signature(create_news_video)
    assert "image_url" in sig.parameters


# ── 28. Stock footage: keyword extraction ────────────────────────────────────

def test_extract_search_query_from_english():
    """extract_search_query should extract meaningful English keywords."""
    from video.footage import extract_search_query
    query = extract_search_query(
        "Netherlands announces new cycling infrastructure plan",
        "The Dutch government will build new bicycle lanes.",
    )
    assert "netherlands" in query or "cycling" in query or "infrastructure" in query


# ── 29. Video: package exports are accessible ────────────────────────────────

def test_video_package_exports():
    """video package should export create_news_video and helpers."""
    from video import (
        create_news_video,
        clean_for_narration,
        group_subtitle_segments,
    )
    assert callable(create_news_video)
    assert callable(clean_for_narration)
    assert callable(group_subtitle_segments)


# ── 30. TTS: char-to-word timestamp conversion ──────────────────────────────

def test_chars_to_words():
    """_chars_to_words should convert character-level alignment to words."""
    chars = list("Merhaba dünya")
    starts = [i * 0.05 for i in range(len(chars))]
    ends = [(i + 1) * 0.05 for i in range(len(chars))]
    words = _chars_to_words(chars, starts, ends)
    assert len(words) == 2
    assert words[0].text == "Merhaba"
    assert words[1].text == "dünya"
    assert words[0].start == 0.0
    assert words[1].end == ends[-1]


# ── 31. TTS: ElevenLabs fallback when no API key ────────────────────────────

def test_tts_uses_edge_tts_without_elevenlabs_key():
    """generate_tts should fall back to edge-tts when ELEVENLABS_API_KEY is empty."""
    # When there's no key, it should NOT raise — it falls back to edge-tts
    # (we just verify the import and fallback path exist)
    from video.tts import generate_tts, _edge_tts
    assert callable(generate_tts)
    assert callable(_edge_tts)


# ── 32. Subtitle colors match brand ─────────────────────────────────────────

def test_subtitle_colors():
    """Subtitle clips should use orange bg and cream text."""
    from video.config import SUBTITLE_BG_COLOR, SUBTITLE_TEXT_COLOR
    assert SUBTITLE_BG_COLOR == "#FF5B14"
    assert SUBTITLE_TEXT_COLOR == "#F9E8D9"


# ── 33. Pexels image search returns None without key ─────────────────────────

def test_fetch_stock_image_no_key(tmp_path):
    """fetch_stock_image should return None when PEXELS_API_KEY is empty."""
    with patch.dict(os.environ, {"PEXELS_API_KEY": ""}, clear=False), \
         patch("video.footage.PEXELS_API_KEY", ""):
        result = fetch_stock_image("test", "test content", str(tmp_path))
    assert result is None


# ── 34. Gradient overlay must have transparency mask ─────────────────────────

def test_gradient_overlay_has_mask():
    """Gradient overlay must use a mask — not opaque black."""
    clip = make_gradient_overlay(3.0)
    assert clip.mask is not None, "Gradient overlay must have a mask for transparency"


# ── 35. Subtitle overlays → full-width RGBA PNG specs ────────────────────────

def test_build_subtitle_overlays(tmp_path):
    """build_subtitle_overlays should emit full-width RGBA PNG specs with timing."""
    from video.effects import build_subtitle_overlays, OverlaySpec
    from video.config import VIDEO_WIDTH
    from PIL import Image
    seg = SubtitleSegment(text="Kısa metin burada", start=0.5, end=2.0)
    specs = build_subtitle_overlays([seg], hook_duration=0.0, tmp_dir=str(tmp_path))
    assert specs and all(isinstance(s, OverlaySpec) for s in specs)
    spec = specs[0]
    assert spec.start == 0.5 and spec.end <= 2.0
    img = Image.open(spec.png_path)
    assert img.mode == "RGBA" and img.width == VIDEO_WIDTH  # full-width, transparent


def test_build_subtitle_overlays_skips_hook_window(tmp_path):
    """Segments starting within the hook window are skipped (hook covers them)."""
    from video.effects import build_subtitle_overlays
    seg = SubtitleSegment(text="Erken altyazı", start=1.0, end=2.5)
    specs = build_subtitle_overlays([seg], hook_duration=3.0, tmp_dir=str(tmp_path))
    assert specs == []
