"""Shared pytest fixtures used across multiple test files."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("AI_PROMPT_QUALITY_CHECK", "Check: {content}")

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from news_scraper import NewsArticle
from ai_agent import NewsAIAgent, SocialMediaPost
from event_scraper import EventItem


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
def sample_post():
    return SocialMediaPost(
        original_title="Holland introduces new cycling law",
        original_url="https://nos.nl/artikel/test-123",
        source="nos",
        content="🚲 Holland's new cycling law brings major changes.\n\nThe Dutch government announced a sweeping new infrastructure plan.\n\n#Netherlands #Cycling #Dutch",
        hashtags=["#Netherlands", "#Cycling", "#Dutch"],
    )


@pytest.fixture
def ai_agent():
    # Patch _ls_wrap_anthropic to a passthrough so agent.client stays as the MagicMock.
    # Without this, LangSmith wraps the mock and agent.client becomes a real wrapper object,
    # making it impossible to configure return values via agent.client.messages.create.
    with patch("ai_agent.anthropic.Anthropic") as mock_anthropic, \
         patch("ai_agent._ls_wrap_anthropic", new=lambda c: c):
        mock_client = MagicMock()
        mock_anthropic.return_value = mock_client
        agent = NewsAIAgent(api_key="test-key")
    return agent


@pytest.fixture
def sample_event():
    return EventItem(
        title="Amsterdam Jazz Festival 2026",
        description="Annual jazz festival in Amsterdam city centre.",
        url="https://example.com/events/jazz-2026",
        start_date=(datetime.now(timezone.utc) + timedelta(days=3)).isoformat(),
        location="Amsterdam",
        source="test",
        category="festival",
        price="€15",
    )


@pytest.fixture
def sample_events_dicts():
    base = datetime.now(timezone.utc) + timedelta(days=2)
    return [
        {"title": "Amsterdam Jazz Festival", "description": "Jazz in Amsterdam",
         "location": "Amsterdam", "start_date": base.isoformat(), "price": "€15",
         "category": "concert", "source": "test", "url": "https://ex.com/1"},
        {"title": "Rotterdam Art Show", "description": "Modern art exhibition",
         "location": "Rotterdam", "start_date": (base + timedelta(days=1)).isoformat(),
         "price": "Free", "category": "museum", "source": "test", "url": "https://ex.com/2"},
        {"title": "Utrecht Family Day", "description": "Family activities in Utrecht",
         "location": "Utrecht", "start_date": (base + timedelta(days=2)).isoformat(),
         "price": None, "category": "family", "source": "test", "url": "https://ex.com/3"},
    ]
