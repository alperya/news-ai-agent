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


# ── Editorial pool filters ────────────────────────────────────────────────────
# The RSS feeds carry recurring NOS/RTL *formats* alongside real stories:
# the weekly agenda card, photo galleries, rolling liveblogs, podcast episodes.
# They read like news to the selection prompt but make terrible standalone
# posts — three of one review week's engagement-floor posts were exactly these.
# The blacklist is enforced in code because a soft prompt rule can be ignored.
_NON_STORY_PATTERNS = [
    r'^we{1,2}kdienst\b',       # NOS spells it "Weekdienst"; the review wrote "Wekdienst"
    r'^in beeld\s*[|:]',
    r'^live\s*[|:]',
    r'^liveblog\b',
    r'^podcast\b',
    r'\bpodcast de dag\b',
    r'^explainer\s*[|:]',
    r'\bin nieuwsuur\b',
]

_NON_STORY_RE = re.compile('|'.join(_NON_STORY_PATTERNS), re.IGNORECASE)

# Titles below this similarity are treated as separate stories. Calibrated on a
# live 27-article pool: same-event pairs scored 0.44–1.00 ("AZ verplettert PSV
# in Johan Cruijff Schaal" / "AZ wint tweede Johan Cruijff Schaal"), while the
# closest genuinely-different pair scored 0.29 ("Madonna at Pride" / "Pride
# security stepped up"). 0.40 sits in that gap.
NEAR_DUPLICATE_THRESHOLD = 0.40

# A single shared token can reach similarity 1.0 on a very short headline;
# require at least this many shared content words before collapsing.
_MIN_SHARED_TOKENS = 2

# Dutch + English function words carry no topical signal; leaving them in
# inflates the overlap between any two headlines.
_STOPWORDS = {
    'de', 'het', 'een', 'en', 'van', 'in', 'op', 'te', 'voor', 'met', 'aan',
    'is', 'zijn', 'bij', 'door', 'naar', 'dat', 'die', 'niet', 'om', 'ook',
    'na', 'uit', 'over', 'als', 'maar', 'meer', 'nog', 'weer', 'wordt', 'werd',
    'the', 'a', 'an', 'and', 'of', 'to', 'for', 'with', 'on', 'at',
    'are', 'was', 'were', 'be', 'by', 'from', 'as', 'that', 'this',
}


def is_non_story_title(title: str) -> bool:
    """True when the title is a recurring non-story format, not a news event."""
    if not title:
        return False
    return bool(_NON_STORY_RE.search(title.strip()))


def _title_tokens(title: str) -> set:
    """Lowercased content words of a title, for similarity comparison."""
    words = re.findall(r'\w+', (title or '').lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def title_similarity(a: str, b: str) -> float:
    """Overlap coefficient of two titles' content words (0.0–1.0).

    Deliberately NOT Jaccard: headlines for the same event differ wildly in
    length ("AZ verplettert PSV in Johan Cruijff Schaal na vroege rode kaart
    Veerman" vs "AZ wint tweede Johan Cruijff Schaal na ruime zege op tiental
    PSV"), and Jaccard punishes that difference through the union term — the
    pair scores 0.29 there but 0.44 here. The overlap coefficient asks the
    question that matters: how much of the SHORTER headline is contained in
    the longer one.
    """
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    shared = ta & tb
    if len(shared) < _MIN_SHARED_TOKENS:
        return 0.0
    return len(shared) / min(len(ta), len(tb))


def collapse_near_duplicates(articles: List[Dict],
                             threshold: float = NEAR_DUPLICATE_THRESHOLD):
    """Cluster articles reporting the same story and keep one per cluster.

    Five sources covering one event turn a 27-article pool into ~14 real
    options — but the selection prompt sees 27 and reads the pool as deeper
    than it is. Keeps the cluster member with the longest description (the
    most material for the AI to write from) and tags the rest.

    Returns:
        tuple[list[dict], list[dict]]: (kept, dropped). Each dropped article
        gets ``excluded_reason='near_duplicate'`` and ``duplicate_of``.
    """
    kept: List[Dict] = []
    dropped: List[Dict] = []

    for article in articles:
        title = article.get('title', '')
        match = next(
            (k for k in kept if title_similarity(title, k.get('title', '')) >= threshold),
            None,
        )
        if match is None:
            kept.append(article)
            continue

        # Same story — keep whichever has more to write from.
        if len(article.get('description') or '') > len(match.get('description') or ''):
            kept[kept.index(match)] = article
            loser, winner = match, article
        else:
            loser, winner = article, match

        loser['excluded_reason'] = 'near_duplicate'
        loser['duplicate_of'] = winner.get('title', '')
        dropped.append(loser)

    return kept, dropped


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
        },
        'nu': {
            'algemeen': 'https://www.nu.nl/rss/Algemeen',
            'binnenland': 'https://www.nu.nl/rss/Binnenland',
        },
        'dutchnews': {
            'expat': 'https://www.dutchnews.nl/feed/',
        },
        'volkskrant': {
            'kwaliteit': 'https://www.volkskrant.nl/rss.xml',
        },
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
