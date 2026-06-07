"""
AI Agent using Claude API for content processing
Transforms raw news into engaging social media content
"""

import anthropic
import os
import re
import json
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from langsmith.wrappers import wrap_anthropic as _ls_wrap_anthropic
except ImportError:
    _ls_wrap_anthropic = None  # type: ignore[assignment]

try:
    from langfuse import observe as _lf_observe, get_client as _lf_get_client  # type: ignore[assignment]
    _LANGFUSE_AVAILABLE = True

    class _LangfuseCtx:
        """Thin adapter: maps our internal call to Langfuse v4 get_client() API."""
        def update_current_observation(
            self, *, model: Optional[str] = None, input=None, output=None,
            usage: Optional[dict] = None, metadata: Optional[dict] = None, **_: object,
        ) -> None:
            try:
                _lf_get_client().update_current_generation(
                    model=model,
                    input=input,
                    output=output,
                    usage_details=usage,
                    metadata=metadata,
                )
            except Exception:
                pass

    _lf_ctx = _LangfuseCtx()  # type: ignore[assignment]

except ImportError:
    _LANGFUSE_AVAILABLE = False

    def _lf_observe(fn=None, **kwargs):  # type: ignore[misc]
        if fn is not None:
            return fn
        return lambda f: f

    class _NoopCtx:  # type: ignore[no-redef]
        def update_current_observation(self, **kwargs: object) -> None:
            pass
    _lf_ctx = _NoopCtx()  # type: ignore[assignment]

try:
    import boto3
except ImportError:
    boto3 = None

# Load environment variables

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Project root: parent of src/ locally, same dir in Lambda flat ZIP
_THIS_DIR = Path(__file__).parent
PROJECT_ROOT = _THIS_DIR.parent if (_THIS_DIR / '__init__.py').exists() or _THIS_DIR.name == 'src' else _THIS_DIR
PROMPTS_DIR = PROJECT_ROOT / 'prompts'


@dataclass
class SocialMediaPost:
    """Processed social media post"""
    original_title: str
    original_url: str
    source: str
    content: str
    hashtags: List[str]
    emoji: str
    hook: str = ""
    platform: str = "twitter"
    image_url: Optional[str] = None
    _corrected: bool = field(default=False, init=False, repr=False)
    
    def to_dict(self) -> Dict:
        return {
            'original_title': self.original_title,
            'original_url': self.original_url,
            'source': self.source,
            'content': self.content,
            'hashtags': self.hashtags,
            'emoji': self.emoji,
            'hook': self.hook,
            'platform': self.platform,
            'image_url': self.image_url,
            'full_post': self.format_post()
        }
    
    def format_post(self) -> str:
        """Format complete social media post with source"""
        hashtags_str = ' '.join(self.hashtags)
        source_line = f"\n📰 Source: {self.original_url}"
        # Only prepend emoji if content doesn't already start with one
        content = self.content
        if not content or not self._starts_with_emoji(content):
            content = f"{self.emoji} {content}"
        # Add dot separator before hashtags if content was corrected by quality gate
        separator = "\n.\n" if self._corrected else "\n\n"
        return f"{content}{source_line}{separator}{hashtags_str}"

    @staticmethod
    def _starts_with_emoji(text: str) -> bool:
        """Check if text starts with an emoji character."""
        if not text:
            return False
        cp = ord(text[0])
        # Common emoji ranges: Misc Symbols, Dingbats, Emoticons, Transport, Supplemental
        return cp > 0x2600


class NewsAIAgent:
    """AI Agent for processing news into social media content"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        base_client = anthropic.Anthropic(api_key=self.api_key)
        self.client = _ls_wrap_anthropic(base_client) if _ls_wrap_anthropic else base_client
        self.model = "claude-opus-4-6"
        self.review_model = os.getenv('REVIEW_MODEL', 'claude-haiku-4-5-20251001')
        self.footage_model = os.getenv('FOOTAGE_QUERY_MODEL', 'claude-opus-4-7')

    @staticmethod
    def _load_prompt(filename: str, env_var: str) -> str:
        """Load prompt from file, fall back to env var (for Lambda)."""
        prompt_file = PROMPTS_DIR / filename
        if prompt_file.exists():
            return prompt_file.read_text(encoding='utf-8')
        value = os.getenv(env_var)
        if value:
            return value.replace('\\n', '\n')
        raise ValueError(f"Prompt not found: {prompt_file} or env var {env_var}")
    
    @_lf_observe(name="process_article")
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
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"platform": target_platform, "article_title": article.get('title', '')},
            )

            content_block = response.content[0]
            result = self._parse_response(getattr(content_block, 'text', ''))  # type: ignore[union-attr]
            
            return SocialMediaPost(
                original_title=article['title'],
                original_url=article['url'],
                source=article['source'],
                content=result['content'],
                hashtags=result['hashtags'],
                emoji=result['emoji'],
                hook=result.get('hook', ''),
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
            max_length = 2200
        else:
            max_length = 500

        prompt_template = self._load_prompt('single_article.txt', 'AI_PROMPT_SINGLE_ARTICLE')
        hashtag_instruction = 'Include 5-10 relevant hashtags (in English)' if platform == 'instagram' else 'Include 3-5 relevant hashtags (in English)'

        return prompt_template.format(
            title=article['title'],
            description=article['description'],
            source=article['source'].upper(),
            category=article.get('category', 'general'),
            platform=platform,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )
    
    def _parse_response(self, response_text: str) -> Dict:
        """Parse Claude's JSON response"""
        try:
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
                'hashtags': ['#Netherlands', '#News', '#Europe']
            }

    @_lf_observe(name="quality_check")
    def quality_check(self, post: SocialMediaPost) -> Optional[SocialMediaPost]:
        """Quality gate: structural checks + lightweight AI language review.
        Returns the (possibly corrected) post if it passes, None if rejected.
        """
        errors = []

        # ── Structural checks (no API call needed) ──
        if not post.content or len(post.content.strip()) < 20:
            errors.append("Content is empty or too short (min 20 chars)")
        if not post.emoji:
            errors.append("Emoji is missing")
        if not post.hashtags or len(post.hashtags) == 0:
            errors.append("Hashtags are missing")
        if not post.original_url:
            errors.append("Source URL is missing")
        if not post.source:
            errors.append("Source name is missing")

        if errors:
            logger.error(f"❌ Quality gate REJECTED (structural): {errors}")
            self._save_error(post, errors)
            return None

        # ── AI language review (fast/cheap model) ──
        try:
            prompt_template = self._load_prompt('quality_check.txt', 'AI_PROMPT_QUALITY_CHECK')
            prompt = prompt_template.format(content=post.content)

            response = self.client.messages.create(
                model=self.review_model,
                max_tokens=1500,
                temperature=0,
                messages=[{"role": "user", "content": prompt}]
            )
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.review_model,
                metadata={"post_source": post.source, "platform": post.platform},
            )

            content_block = response.content[0]
            raw = getattr(content_block, 'text', '')  # type: ignore[union-attr]
            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(json_match.group()) if json_match else json.loads(raw)

            if not result.get('pass', True):
                reason = result.get('reason', 'AI language review failed')
                logger.error(f"❌ Quality gate REJECTED (language): {reason}")
                self._save_error(post, [reason])
                return None

            if result.get('corrected_content'):
                issues = result.get('issues', [])
                logger.info(f"✏️  Quality gate corrected content: {issues}")
                original_content = post.content
                post.content = result['corrected_content']
                post._corrected = True
                self._save_correction(post, original_content, result['corrected_content'], issues)
            else:
                logger.info("✅ Quality gate: content is clean")

        except Exception as e:
            # AI review failure is non-blocking — structural checks already passed
            logger.warning(f"⚠️  Quality gate AI review skipped (error): {e}")

        return post

    @_lf_observe(name="generate_footage_queries")
    def generate_footage_queries(self, title: str, description: str) -> List[str]:
        """Generate Pexels-optimised search queries for stock footage selection.

        Returns up to 3 English queries ordered from most specific to most
        generic. Falls back to an empty list so footage.py uses its own
        extract_search_query as a safety net.
        """
        prompt = (
            "You are selecting stock footage for a Dutch news Instagram Reel.\n\n"
            f"Dutch article headline: {title}\n"
            f"Article context: {description}\n\n"
            "Generate exactly 3 Pexels search queries in English, ordered from most "
            "specific to most generic.\n\n"
            "Rules:\n"
            "- Include 'netherlands' or a specific Dutch city/place when the article "
            "is about the Netherlands or a Dutch institution\n"
            "- Use concrete visual nouns (what would appear on screen)\n"
            "- English only — no Dutch words\n"
            "- 2–4 words per query\n\n"
            'Return ONLY valid JSON: {"queries": ["...", "...", "..."]}'
        )
        try:
            response = self.client.messages.create(
                model=self.footage_model,
                max_tokens=120,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.footage_model,
                metadata={"article_title": title},
            )
            text = getattr(response.content[0], 'text', '')
            match = re.search(r'\{.*\}', text, re.DOTALL)
            data = json.loads(match.group() if match else text)
            queries = [q for q in data.get('queries', []) if isinstance(q, str) and q.strip()]
            if queries:
                logger.info(f"🎬 Footage queries: {queries}")
                return queries[:3]
        except Exception as e:
            logger.warning(f"⚠️  Footage query generation failed, falling back to keyword extraction: {e}")
        return []

    # ── Event methods ──────────────────────────────────────────────────────────

    @_lf_observe(name="score_events")
    def score_events(self, events: List[Dict]) -> List[Dict]:
        """Score each event with Claude Haiku (0–8). Returns events with score ≥ 5.

        Scoring rubric per event (total 8 pts):
          audience_fit   0–3: relevant to English-speaking expats/tourists?
          completeness   0–2: has title + date + location + price?
          public_access  0–2: genuinely public event (not private/corporate)?
          visual_appeal  0–1: photogenic / Instagram-worthy?
        """
        if not events:
            return []

        events_json = json.dumps(
            [{"index": i, "title": e.get("title"), "description": e.get("description"),
              "location": e.get("location"), "start_date": e.get("start_date"),
              "price": e.get("price"), "category": e.get("category"), "source": e.get("source")}
             for i, e in enumerate(events)],
            ensure_ascii=False,
        )

        prompt = (
            "You are evaluating Netherlands events for an English-language Instagram account "
            "targeting expats, international students, and tourists.\n\n"
            "Score each event from 0–8 using this rubric:\n"
            "  audience_fit  (0–3): How relevant to English-speaking non-Dutch audience?\n"
            "  completeness  (0–2): Has usable title + date + location? Price is a bonus.\n"
            "  public_access (0–2): Is it a genuine public event anyone can attend?\n"
            "  visual_appeal (0–1): Is it visually interesting for Instagram?\n\n"
            f"Events (JSON):\n{events_json}\n\n"
            "Return ONLY valid JSON: "
            '{{"scores": [{{"index": 0, "score": 7}}, ...]}}'
        )

        try:
            response = self.client.messages.create(
                model=self.review_model,
                max_tokens=1000,
                temperature=0,
                messages=[{"role": "user", "content": prompt}],
            )
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.review_model,
                metadata={"event_count": len(events)},
            )
            raw = getattr(response.content[0], 'text', '')
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group() if match else raw)

            score_map = {s["index"]: s["score"] for s in result.get("scores", [])}
            passed = [
                {**events[i], "_score": score_map.get(i, 0)}
                for i in range(len(events))
                if score_map.get(i, 0) >= 5
            ]
            logger.info(f"🔍 Event scoring: {len(events)} in → {len(passed)} passed (score ≥ 5)")
            return passed

        except Exception as e:
            logger.warning(f"⚠️  Event scoring failed, using all events unfiltered: {e}")
            return events

    @_lf_observe(name="select_and_format_events")
    def select_and_format_events(
        self, events: List[Dict], date_range: str, max_events: int = 7
    ) -> Optional[Dict]:
        """Select best events and generate Instagram caption + card data.

        Returns dict with keys:
          selected_events: list of event dicts (title, date_label, location, price, emoji, description)
          caption:         full formatted Instagram caption string
          hashtags:        list of hashtag strings

        Returns None on failure.
        """
        if not events:
            return None

        events_text = ""
        for i, ev in enumerate(events, 1):
            events_text += (
                f"\nEVENT {i}:\n"
                f"  Title: {ev.get('title', '')}\n"
                f"  Description: {ev.get('description', '')}\n"
                f"  Start: {ev.get('start_date', '')}\n"
                f"  Location: {ev.get('location', '')}\n"
                f"  Venue: {ev.get('venue', '') or 'N/A'}\n"
                f"  Category: {ev.get('category', '')}\n"
                f"  Price: {ev.get('price') or 'Unknown'}\n"
                f"  Source: {ev.get('source', '')}\n"
                f"  URL: {ev.get('url', '')}\n"
            )

        prompt_template = self._load_prompt('event_selection.txt', 'AI_PROMPT_EVENT_SELECTION')
        prompt = prompt_template.format(
            event_count=len(events),
            max_events=max_events,
            date_range=date_range,
            events_text=events_text,
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=3000,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"event_count": len(events), "max_events": max_events, "date_range": date_range},
            )
            raw = getattr(response.content[0], 'text', '')
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group() if match else raw)

            selected = result.get("selected_events", [])
            caption = result.get("caption", "")
            hashtags = result.get("hashtags", [])

            if not selected or not caption:
                logger.error("❌ Event selection: empty response from AI")
                return None

            logger.info(f"✅ Events selected: {len(selected)} events, caption {len(caption)} chars")
            return {"selected_events": selected, "caption": caption, "hashtags": hashtags}

        except Exception as e:
            logger.error(f"❌ Event selection/formatting failed: {e}")
            return None

    def _save_error(self, post: SocialMediaPost, reasons: List[str]):
        """Save rejected post details to S3 (Lambda) or local errors/ directory."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        error_data = {
            'timestamp': timestamp,
            'rejected_reasons': reasons,
            'original_content': post.content,
            'full_post': post.format_post(),
            'original_title': post.original_title,
            'original_url': post.original_url,
            'source': post.source,
            'emoji': post.emoji,
            'hashtags': post.hashtags,
            'platform': post.platform,
        }

        filename = f'rejected_{timestamp}.json'
        self._persist_json(f'errors/{filename}', error_data)

    def _save_correction(self, post: SocialMediaPost, original: str, corrected: str, issues: List[str]):
        """Save correction details (before/after) to S3 (Lambda) or local errors/ directory."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        correction_data = {
            'timestamp': timestamp,
            'type': 'correction',
            'issues': issues,
            'original_content': original,
            'corrected_content': corrected,
            'original_title': post.original_title,
            'original_url': post.original_url,
            'source': post.source,
            'platform': post.platform,
        }

        filename = f'corrected_{timestamp}.json'
        self._persist_json(f'errors/{filename}', correction_data)

    def _persist_json(self, key: str, data: dict):
        """Write JSON to S3 (when RESULTS_BUCKET is set) or local filesystem."""
        bucket = os.environ.get('RESULTS_BUCKET')

        if bucket and boto3:
            try:
                s3 = boto3.client('s3')
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=json.dumps(data, ensure_ascii=False, indent=2),
                    ContentType='application/json',
                )
                logger.info(f"📝 Saved to S3: s3://{bucket}/{key}")
                return
            except Exception as e:
                logger.warning(f"⚠️  S3 write failed, falling back to local: {e}")

        # Local fallback (development)
        local_path = PROJECT_ROOT / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"📝 Saved locally: {local_path}")
    
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
                except Exception:
                    pass
            return []
    
    @_lf_observe(name="select_and_process_articles")
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
            _lf_ctx.update_current_observation(
                input=prompt,
                output=getattr(response.content[0], 'text', ''),
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"article_count": len(articles), "max_posts": max_posts, "platform": platform},
            )

            content_block = response.content[0]
            result = self._parse_batch_response(getattr(content_block, 'text', ''), articles, platform)  # type: ignore[union-attr]
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

        articles_text = ""
        for i, article in enumerate(articles, 1):
            articles_text += f"""
ARTICLE {i}:
- Title: {article['title']}
- Description: {article['description']}
- Source: {article['source'].upper()}
- Category: {article.get('category', 'general')}
- URL: {article['url']}
"""

        prompt_template = self._load_prompt('batch_selection.txt', 'AI_PROMPT_BATCH_SELECTION')
        hashtag_instruction = 'Include 5-10 relevant hashtags (in English)' if platform == 'instagram' else 'Include 3-5 relevant hashtags (in English)'

        return prompt_template.format(
            article_count=len(articles),
            max_posts=max_posts,
            platform=platform,
            articles_text=articles_text,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )
    
    def _parse_batch_response(self, response_text: str, articles: List[Dict], platform: str) -> List[SocialMediaPost]:
        """Parse batch response and create SocialMediaPost objects"""
        try:
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
                    hook=selected.get('hook', ''),
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
