"""
AI Agent using Claude API for content processing
Transforms raw news into engaging social media content
"""

import anthropic
import os
from typing import List, Dict, Optional
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
    image_url: Optional[str] = None
    
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
        if self.platform == "instagram":
            # Instagram format: emoji + content + hashtags (no link in caption)
            return f"{self.emoji} {self.content}\n\n{hashtags_str}"
        else:
            # Twitter format: emoji + content + hashtags + link
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
                platform=target_platform,
                image_url=article.get('image_url')
            )
            
        except Exception as e:
            logger.error(f"Error processing article: {str(e)}")
            raise
    
    def _create_prompt(self, article: Dict, platform: str) -> str:
        """Create prompt for Claude"""
        if platform == "twitter":
            max_length = 280
        elif platform == "instagram":
            max_length = 2200  # Instagram caption limit
        else:
            max_length = 500
        
        prompt = f"""Sen Türkçe sosyal medya içerik uzmanısın. Felemenkçe haberleri Türkçe'ye çevirip sosyal medya post'una dönüştürüyorsun.

HABER DETAYLARI:
Başlık: {article['title']}
Açıklama: {article['description']}
Kaynak: {article['source'].upper()}
Kategori: {article.get('category', 'genel')}

HEDEİ KİTLE: Hollanda'da yaşayan Türkler

GÖREV:
{platform} için Hollanda'daki Türklerin ilgisini çekecek, gündemlerini yakından ilgilendiren bir Türkçe post oluştur ki:
1. Haberin özünü özetlesin
2. Hollanda'daki Türk toplumu için önemli ve ilgi çekici olsun
3. Maksimum {max_length} karakter olsun (link ve hashtag'ler hariç)
4. Uygun bir emoji kullansın
5. {'5-10 ilgili hashtag içersin (sadece Türkçe)' if platform == 'instagram' else '3-5 ilgili hashtag içersin (sadece Türkçe)'}

ÖNCELİK KONULAR:
- Hollanda'daki Türk toplumunu doğrudan etkileyen yasalar, kararlar
- Ekonomi, enflasyon, maaş, vergi haberleri
- Eğitim, sağlık, ulaşım
- Türkiye-Hollanda ilişkileri
- Yerel önemli olaylar

YANIT FORMATI (JSON):
{{
    "content": "Emoji, hashtag veya link olmadan post metni",
    "emoji": "Tek bir uygun emoji",
    "hashtags": ["#Hollanda", "#Türkler", "#Haberler"]  # Sadece Türkçe hashtag kullan
}}

Önemli: 
- İçeriği tarafsız ve gerçekçi tut. Sansasyonel dil kullanma. Haberi Türkçe'ye doğal bir şekilde çevir.
- Hashtag'ler SADECE Türkçe olmalı. Felemenkçe veya İngilizce hashtag kullanma."""

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
                'content': 'Son dakika haberi',
                'emoji': '📰',
                'hashtags': ['#Hollanda', '#Haberler', '#Gündem']
            }
    
    def process_batch(self, articles: List[Dict], max_posts: int = 10, platform: str = "twitter") -> List[SocialMediaPost]:
        """Process multiple articles"""
        posts = []
        
        for i, article in enumerate(articles[:max_posts]):
            try:
                post = self.process_article(article, target_platform=platform)
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
