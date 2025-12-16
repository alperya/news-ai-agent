"""Tests"""
import pytest
from news_scraper import DutchNewsScraper

@pytest.fixture
def scraper():
    return DutchNewsScraper()

def test_init(scraper):
    assert scraper is not None
    assert 'nos' in scraper.RSS_FEEDS

def test_feeds(scraper):
    for source, cats in scraper.RSS_FEEDS.items():
        assert len(cats) > 0
