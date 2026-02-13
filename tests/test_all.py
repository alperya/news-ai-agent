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

from news_scraper import DutchNewsScraper, NewsArticle, save_articles_json
from ai_agent import NewsAIAgent, SocialMediaPost, save_posts_json


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

def test_scraper_has_nos_and_nu_feeds(scraper):
    """RSS_FEEDS should contain NOS and NU.nl (not Telegraaf)."""
    assert "nos" in scraper.RSS_FEEDS
    assert "nu" in scraper.RSS_FEEDS
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
    assert "📰 Kaynak: https://nos.nl/artikel/test-123" in formatted
    assert "#Hollanda" in formatted


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
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic") as mock_cls:
            agent = NewsAIAgent(api_key="test-key")
            mock_response = MagicMock()
            mock_response.content = [MagicMock(
                text='{"pass": true, "corrected_content": "Hollanda algoritma skandalı", "issues": ["algoritme → algoritma"]}'
            )]
            mock_cls.return_value.messages.create.return_value = mock_response
            agent.client = mock_cls.return_value

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
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic") as mock_cls:
            agent = NewsAIAgent(api_key="test-key")
            mock_response = MagicMock()
            mock_response.content = [MagicMock(
                text='{"pass": false, "reason": "Metin tamamen anlamsız"}'
            )]
            mock_cls.return_value.messages.create.return_value = mock_response
            agent.client = mock_cls.return_value

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
    with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
        with patch("anthropic.Anthropic") as mock_cls:
            agent = NewsAIAgent(api_key="test-key")
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text='{"pass": true}')]
            mock_cls.return_value.messages.create.return_value = mock_response
            agent.client = mock_cls.return_value

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
