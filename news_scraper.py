"""
News Scraper for Dutch News Sites
Supports: NOS.nl, NU.nl, Telegraaf.nl
"""

import feedparser
import requests
from typing import List, Dict, Optional
from datetime import datetime
import logging
from dataclasses import dataclass, asdict
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class NewsArticle:
    """Data class for news articles"""
    title: str
    description: str
    url: str
    published_date: str
    source: str
    category: Optional[str] = None
    image_url: Optional[str] = None
    
    def to_dict(self) -> Dict:
        return asdict(self)


class DutchNewsScraper:
    """Scraper for Dutch news sources using RSS feeds"""
    
    RSS_FEEDS = {
        'nos': {
            'general': 'https://feeds.nos.nl/nosnieuwsalgemeen',
            'binnenland': 'https://feeds.nos.nl/nosnieuwsbinnenland',
            'buitenland': 'https://feeds.nos.nl/nosnieuwsbuitenland',
            'sport': 'https://feeds.nos.nl/nossportalgemeen'
        },
        'nu': {
            'general': 'https://www.nu.nl/rss/Algemeen',
            'binnenland': 'https://www.nu.nl/rss/Binnenland',
            'economie': 'https://www.nu.nl/rss/Economie',
            'tech': 'https://www.nu.nl/rss/Tech'
        },
        'telegraaf': {
            'algemeen': 'https://www.telegraaf.nl/rss',
            'binnenland': 'https://www.telegraaf.nl/rss/binnenland',
            'buitenland': 'https://www.telegraaf.nl/rss/buitenland',
            'financieel': 'https://www.telegraaf.nl/rss/financieel'
        }
    }
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (compatible; NewsAIAgent/1.0)'
        })
    
    def fetch_feed(self, url: str, source: str, category: str) -> List[NewsArticle]:
        """Fetch and parse RSS feed"""
        try:
            logger.info(f"Fetching feed: {source}/{category} from {url}")
            feed = feedparser.parse(url)
            
            # Check feed status and log warnings
            if hasattr(feed, 'status') and feed.status != 200:
                logger.warning(f"Feed {source}/{category} returned status {feed.status}")
            
            if not feed.entries:
                logger.warning(f"Feed {source}/{category} has no entries. Feed bozo: {getattr(feed, 'bozo', False)}, bozo_exception: {getattr(feed, 'bozo_exception', None)}")
                return []
            
            articles = []
            for entry in feed.entries[:5]:
                article = self._parse_entry(entry, source, category)
                if article:
                    articles.append(article)
            
            if len(articles) == 0 and len(feed.entries) > 0:
                logger.warning(f"Feed {source}/{category} has {len(feed.entries)} entries but none could be parsed")
            
            logger.info(f"Successfully fetched {len(articles)} articles from {source}/{category}")
            return articles
            
        except Exception as e:
            logger.error(f"Error fetching feed {url}: {str(e)}")
            return []
    
    def _parse_entry(self, entry, source: str, category: str) -> Optional[NewsArticle]:
        """Parse RSS entry into NewsArticle"""
        try:
            published = entry.get('published', '')
            if not published and hasattr(entry, 'published_parsed'):
                published = datetime(*entry.published_parsed[:6]).isoformat()
            
            image_url = None
            if hasattr(entry, 'media_content'):
                image_url = entry.media_content[0].get('url')
            elif hasattr(entry, 'enclosures') and entry.enclosures:
                image_url = entry.enclosures[0].get('href')
            
            description = entry.get('summary', entry.get('description', ''))
            if description:
                import re
                description = re.sub('<[^<]+?>', '', description).strip()
                description = description[:280] + '...' if len(description) > 280 else description
            
            return NewsArticle(
                title=entry.get('title', 'No title'),
                description=description,
                url=entry.get('link', ''),
                published_date=published,
                source=source,
                category=category,
                image_url=image_url
            )
        except Exception as e:
            logger.error(f"Error parsing entry: {str(e)}")
            return None
    
    def scrape_all_sources(self, max_articles_per_source: int = 3) -> List[NewsArticle]:
        """Scrape articles from all configured sources"""
        all_articles = []
        
        for source, categories in self.RSS_FEEDS.items():
            for category, feed_url in categories.items():
                articles = self.fetch_feed(feed_url, source, category)
                all_articles.extend(articles[:max_articles_per_source])
        
        logger.info(f"Total articles scraped: {len(all_articles)}")
        return all_articles
    
    def scrape_source(self, source: str, category: str = 'general') -> List[NewsArticle]:
        """Scrape specific source and category"""
        if source not in self.RSS_FEEDS:
            logger.error(f"Unknown source: {source}")
            return []
        
        categories = self.RSS_FEEDS[source]
        if category not in categories:
            logger.warning(f"Unknown category {category}, using first available")
            category = list(categories.keys())[0]
        
        return self.fetch_feed(categories[category], source, category)


def save_articles_json(articles: List[NewsArticle], filename: str = 'articles.json'):
    """Save articles to JSON file"""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump([article.to_dict() for article in articles], f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(articles)} articles to {filename}")


if __name__ == "__main__":
    scraper = DutchNewsScraper()
    articles = scraper.scrape_all_sources(max_articles_per_source=2)
    
    for article in articles[:5]:
        print(f"\n📰 {article.source.upper()} - {article.category}")
        print(f"📌 {article.title}")
        print(f"📝 {article.description[:100]}...")
        print(f"🔗 {article.url}")
    
    save_articles_json(articles)
