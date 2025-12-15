"""
AI Agent using Claude API for content processing
Transforms raw news into engaging social media content
"""

import anthropic
import os
from typing import List, Dict
import logging
from dataclasses import dataclass
import json

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class SocialMediaPost:
    """Processed social media post"""
    original_title: str
    original_url: str
    source: str
    content: str
    hashtags: List[str]
    emoji: str
    platform: str = "twitter"
    
    def to_dict(self) -> Dict:
        return {
            'original_title': self.original_title,
            'original_url': self.original_url,
            'source': self.source,
            'content': self.content,
            'hashtags': self.hashtags,
            'emoji': self.emoji,
            'platform': self.platform,
            'full_post': self.format_post()
        }
    
    def format_post(self) -> str:
        """Format complete social media post"""
        hashtags_str = ' '.join(self.hashtags)
        return f"{self.emoji} {self.content}\n\n{hashtags_str}\n\n🔗 {self.original_url}"


class NewsAIAgent:
    """AI Agent for processing news into social media content"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.model = "claude-sonnet-4-20250514"
    
    def process_article(self, article: Dict, target_platform: str = "twitter") -> SocialMediaPost:
        """Process single article into social media post"""
        prompt = self._create_prompt(article, target_platform)
        
        try:
            logger.info(f"Processing article: {article['title'][:50]}...")
            
            response = self.client.messages.create(
                model=self.model,
                max_tokens=500,
                temperature=0.7,
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )
            
            result = self._parse_response(response.content[0].text)
            
            return SocialMediaPost(
                original_title=article['title'],
                original_url=article['url'],
                source=article['source'],
                content=result['content'],
                hashtags=result['hashtags'],
                emoji=result['emoji'],
                platform=target_platform
            )
            
        except Exception as e:
            logger.error(f"Error processing article: {str(e)}")
            raise
    
    def _create_prompt(self, article: Dict, platform: str) -> str:
        """Create prompt for Claude"""
        max_length = 280 if platform == "twitter" else 500
        
        prompt = f"""Je bent een social media expert gespecialiseerd in Nederlandse nieuwscontent.

ARTIKEL DETAILS:
Titel: {article['title']}
Beschrijving: {article['description']}
Bron: {article['source'].upper()}
Categorie: {article.get('category', 'algemeen')}

TAAK:
Creëer een pakkende {platform} post in het Nederlands die:
1. De kern van het nieuws samenvat
2. Engaging en informatief is
3. Maximaal {max_length} karakters is (ZONDER link en hashtags)
4. Een passende emoji gebruikt
5. 3-5 relevante hashtags bevat

RESPONSE FORMAT (JSON):
{{
    "content": "De post tekst zonder emoji, hashtags of link",
    "emoji": "Een enkele relevante emoji",
    "hashtags": ["#hashtag1", "#hashtag2", "#hashtag3"]
}}

Belangrijk: Houd de content feitelijk en neutraal. Gebruik geen sensationele taal."""

        return prompt
    
    def _parse_response(self, response_text: str) -> Dict:
        """Parse Claude's JSON response"""
        try:
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = json.loads(response_text)
            
            required = ['content', 'emoji', 'hashtags']
            if not all(key in result for key in required):
                raise ValueError("Missing required fields in response")
            
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {response_text}")
            return {
                'content': 'Breaking news update',
                'emoji': '📰',
                'hashtags': ['#nieuws', '#netherlands']
            }
    
    def process_batch(self, articles: List[Dict], max_posts: int = 10) -> List[SocialMediaPost]:
        """Process multiple articles"""
        posts = []
        
        for i, article in enumerate(articles[:max_posts]):
            try:
                post = self.process_article(article)
                posts.append(post)
                logger.info(f"Processed {i+1}/{min(len(articles), max_posts)}")
            except Exception as e:
                logger.error(f"Failed to process article {i+1}: {str(e)}")
                continue
        
        logger.info(f"Successfully processed {len(posts)} posts")
        return posts


def save_posts_json(posts: List[SocialMediaPost], filename: str = 'social_posts.json'):
    """Save processed posts to JSON"""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump([post.to_dict() for post in posts], f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(posts)} posts to {filename}")


if __name__ == "__main__":
    with open('articles.json', 'r', encoding='utf-8') as f:
        articles = json.load(f)
    
    agent = NewsAIAgent()
    posts = agent.process_batch(articles, max_posts=5)
    
    for post in posts:
        print("\n" + "="*60)
        print(post.format_post())
    
    save_posts_json(posts)
