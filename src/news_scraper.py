"""
News Scraper for Dutch News Sites
Supports: NOS.nl, RTL Nieuws
"""

import requests
from typing import List, Dict, Optional
from datetime import datetime
import logging
from dataclasses import dataclass, asdict
import json
import xml.etree.ElementTree as ET
import html
import re

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
        'rtl': {
            'general': 'https://www.rtl.nl/rss.xml'
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
            response = self.session.get(url, timeout=15)
            response.raise_for_status()

            articles = self._parse_feed_xml(response.text, source, category)

            if not articles:
                logger.warning(f"Feed {source}/{category} has no entries")
                return []

            logger.info(f"Successfully fetched {len(articles)} articles from {source}/{category}")
            return articles
            
        except Exception as e:
            logger.error(f"Error fetching feed {url}: {str(e)}")
            return []
    
    def _parse_feed_xml(self, xml_text: str, source: str, category: str) -> List[NewsArticle]:
        """Parse RSS/Atom XML into NewsArticle list"""
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.error(f"XML parse error: {str(e)}")
            return []

        ns = {
            'content': 'http://purl.org/rss/1.0/modules/content/',
            'media': 'http://search.yahoo.com/mrss/',
            'atom': 'http://www.w3.org/2005/Atom'
        }

        items = []

        # RSS items
        channel = root.find('channel')
        if channel is not None:
            items = channel.findall('item')
        else:
            # Atom entries
            items = root.findall('atom:entry', ns)

        articles: List[NewsArticle] = []
        for item in items[:5]:
            entry = self._extract_entry(item, ns)
            article = self._parse_entry(entry, source, category)
            if article:
                articles.append(article)

        return articles

    def _extract_entry(self, item: ET.Element, ns: Dict) -> Dict:
        """Extract fields from RSS/Atom item"""
        def text(elem: Optional[ET.Element]) -> str:
            return html.unescape(elem.text.strip()) if elem is not None and elem.text else ''

        title = text(item.find('title')) or text(item.find('atom:title', ns))

        link = ''
        link_elem = item.find('link')
        if link_elem is not None and link_elem.text:
            link = link_elem.text.strip()
        else:
            atom_link = item.find('atom:link[@rel="alternate"]', ns) or item.find('atom:link', ns)
            if atom_link is not None:
                link = atom_link.attrib.get('href', '')

        description = text(item.find('description')) or text(item.find('summary'))
        if not description:
            description = text(item.find('atom:summary', ns))

        published = text(item.find('pubDate')) or text(item.find('updated'))
        if not published:
            published = text(item.find('atom:updated', ns)) or text(item.find('atom:published', ns))

        image_url = None
        media_content = item.find('media:content', ns)
        if media_content is not None:
            image_url = media_content.attrib.get('url')
        enclosure = item.find('enclosure')
        if enclosure is not None and enclosure.attrib.get('type', '').startswith('image/'):
            image_url = enclosure.attrib.get('url') or enclosure.attrib.get('href')

        return {
            'title': title,
            'link': link,
            'description': description,
            'published': published,
            'image_url': image_url
        }

    def _parse_entry(self, entry: Dict, source: str, category: str) -> Optional[NewsArticle]:
        """Parse entry dict into NewsArticle"""
        try:
            published = entry.get('published', '')
            if published:
                try:
                    published = datetime.fromisoformat(published).isoformat()
                except Exception:
                    pass

            description = entry.get('description', '')
            if description:
                description = re.sub('<[^<]+?>', '', description).strip()
                description = description[:280] + '...' if len(description) > 280 else description

            return NewsArticle(
                title=entry.get('title', 'No title'),
                description=description,
                url=entry.get('link', ''),
                published_date=published,
                source=source,
                category=category,
                image_url=entry.get('image_url')
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
