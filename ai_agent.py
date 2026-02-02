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
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

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
            'image_url': self.image_url,
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
                temperature=0.2,
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
        
        # Get prompt template from environment variable (required)
        prompt_template = os.getenv('AI_PROMPT_SINGLE_ARTICLE')
        if not prompt_template:
            raise ValueError(
                "AI_PROMPT_SINGLE_ARTICLE environment variable is required. "
                "Please set it in your .env file. See .env.example for the format."
            )
        
        # Convert \n to actual newlines (for .env file format)
        prompt_template = prompt_template.replace('\\n', '\n')
        
        hashtag_instruction = '5-10 ilgili hashtag içersin (sadece Türkçe)' if platform == 'instagram' else '3-5 ilgili hashtag içersin (sadece Türkçe)'
        
        prompt = prompt_template.format(
            title=article['title'],
            description=article['description'],
            source=article['source'].upper(),
            category=article.get('category', 'genel'),
            platform=platform,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )
        
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
        """Process multiple articles - selects which articles to post in a single API call"""
        if not articles:
            logger.warning("No articles to process")
            return []
        
        try:
            logger.info(f"Selecting and processing articles from {len(articles)} total articles (max {max_posts} posts)")
            
            # Single API call to select articles and create posts
            posts = self._select_and_process_articles(articles, max_posts, platform)
            
            logger.info(f"Successfully selected and processed {len(posts)} posts")
            return posts
            
        except Exception as e:
            logger.error(f"Error in batch processing: {str(e)}")
            # Fallback: process first article only
            if articles:
                try:
                    logger.info("Falling back to processing first article only")
                    post = self.process_article(articles[0], target_platform=platform)
                    return [post]
                except:
                    pass
            return []
    
    def _select_and_process_articles(self, articles: List[Dict], max_posts: int, platform: str) -> List[SocialMediaPost]:
        """Select which articles to post and create posts for them in a single API call"""
        prompt = self._create_batch_selection_prompt(articles, max_posts, platform)
        
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4000,
                temperature=0.2,
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )
            
            result = self._parse_batch_response(response.content[0].text, articles, platform)
            return result
            
        except Exception as e:
            logger.error(f"Error in article selection: {str(e)}")
            raise
    
    def _create_batch_selection_prompt(self, articles: List[Dict], max_posts: int, platform: str) -> str:
        """Create prompt for selecting articles and creating posts"""
        if platform == "twitter":
            max_length = 280
        elif platform == "instagram":
            max_length = 2200
        else:
            max_length = 500
        
        # Format all articles for the prompt
        articles_text = ""
        for i, article in enumerate(articles, 1):
            articles_text += f"""
HABER {i}:
- Başlık: {article['title']}
- Açıklama: {article['description']}
- Kaynak: {article['source'].upper()}
- Kategori: {article.get('category', 'genel')}
- URL: {article['url']}
"""
        
        # Get prompt template from environment variable (required)
        prompt_template = os.getenv('AI_PROMPT_BATCH_SELECTION')
        if not prompt_template:
            raise ValueError(
                "AI_PROMPT_BATCH_SELECTION environment variable is required. "
                "Please set it in your .env file. See .env.example for the format."
            )
        
        # Convert \n to actual newlines (for .env file format)
        prompt_template = prompt_template.replace('\\n', '\n')
        
        hashtag_instruction = '5-10 ilgili hashtag içersin (sadece Türkçe)' if platform == 'instagram' else '3-5 ilgili hashtag içersin (sadece Türkçe)'
        
        prompt = prompt_template.format(
            article_count=len(articles),
            max_posts=max_posts,
            platform=platform,
            articles_text=articles_text,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )
        
        return prompt
    
    def _parse_batch_response(self, response_text: str, articles: List[Dict], platform: str) -> List[SocialMediaPost]:
        """Parse batch response and create SocialMediaPost objects"""
        try:
            import re
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = json.loads(response_text)
            
            if 'selected_articles' not in result:
                raise ValueError("Missing 'selected_articles' field in response")
            
            posts = []
            for selected in result['selected_articles']:
                article_index = selected.get('article_index')
                if article_index is None:
                    logger.warning("Missing article_index in selected article, skipping")
                    continue
                
                # Convert to 0-based index
                idx = article_index - 1
                if idx < 0 or idx >= len(articles):
                    logger.warning(f"Invalid article_index {article_index}, skipping")
                    continue
                
                article = articles[idx]
                
                post = SocialMediaPost(
                    original_title=article['title'],
                    original_url=article['url'],
                    source=article['source'],
                    content=selected.get('content', ''),
                    hashtags=selected.get('hashtags', []),
                    emoji=selected.get('emoji', '📰'),
                    platform=platform,
                    image_url=article.get('image_url')
                )
                posts.append(post)
            
            if not posts:
                logger.warning("No valid posts created from batch response")
            
            return posts
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {response_text[:500]}")
            # Fallback: return empty list
            return []
        except Exception as e:
            logger.error(f"Error parsing batch response: {str(e)}")
            return []


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
