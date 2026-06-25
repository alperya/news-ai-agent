"""Main Pipeline - News AI Agent with Twitter Integration"""

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()

import logging
import argparse
import json
from datetime import datetime
from pathlib import Path

from news_scraper import DutchNewsScraper, save_articles_json
from ai_agent import NewsAIAgent, save_posts_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class NewsAIPipeline:
    def __init__(self, config: dict = None):
        # Default config; can be overridden via CLI
        self.config = config or {
            'output_dir': 'output',
            'max_articles_per_source': 3,
            'max_posts': 1,  # Post only one article per run
            'dry_run': True,
            'platform': 'instagram',  # 'twitter' or 'instagram'
            'use_existing_today': False,  # Use existing scraped articles from today if available
        }
        self.scraper = DutchNewsScraper()
        self.ai_agent = NewsAIAgent()
        self.output_dir = Path(self.config['output_dir'])
        self.output_dir.mkdir(exist_ok=True)
    
    def run(self, dry_run: bool = None):
        dry_run = dry_run if dry_run is not None else self.config['dry_run']
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        logger.info("="*60)
        logger.info("🚀 Starting News AI Agent Pipeline")
        logger.info("="*60)
        
        results = {'timestamp': timestamp, 'stages': {}}
        
        try:
            # STAGE 1: Scraping
            logger.info("\n📰 STAGE 1: Scraping news articles...")
            
            # Check if we should use existing articles from today
            if self.config.get('use_existing_today', False):
                existing_articles = self._find_today_articles()
                if existing_articles:
                    logger.info(f"📋 Found {len(existing_articles)} articles from today's existing files")
                    logger.info("⏭️  Using existing articles (skipping new scrape)")
                    articles = existing_articles
                else:
                    logger.info("🆕 No existing articles found for today, scraping new articles...")
                    articles = self.scraper.scrape_all_sources(self.config['max_articles_per_source'])
                    # Sort articles so the most recent (and thus typically most relevant) come first
                    articles = self._sort_articles_by_recency(articles)
            else:
                articles = self.scraper.scrape_all_sources(self.config['max_articles_per_source'])
                # Sort articles so the most recent (and thus typically most relevant) come first
                articles = self._sort_articles_by_recency(articles)
            
            save_articles_json(articles, str(self.output_dir / f'articles_{timestamp}.json'))
            results['stages']['scraping'] = {'success': True, 'count': len(articles)}
            logger.info(f"Scraped {len(articles)} articles")
            
            if not articles:
                logger.warning("No articles found!")
                return results
            
            # STAGE 2: AI Processing
            logger.info("\n🤖 STAGE 2: Processing with AI...")
            platform = self.config.get('platform', 'twitter')
            
            # Check if we should use existing posts from today
            posts = None
            if self.config.get('use_existing_today', False):
                existing_posts = self._find_today_posts(platform)
                if existing_posts:
                    logger.info(f"📋 Found {len(existing_posts)} existing posts from today")
                    logger.info("⏭️  Using existing posts (skipping AI API call)")
                    posts = existing_posts
            
            # If no existing posts found, process with AI
            if not posts:
                logger.info("🆕 No existing posts found, processing with AI...")
                
                # Filter out already posted articles (from ANY previous run)
                posted_urls = self._get_posted_urls()
                if posted_urls:
                    logger.info(f"🔍 Found {len(posted_urls)} previously posted articles across all runs")
                    original_count = len(articles)
                    articles = [a for a in articles if a.url not in posted_urls]
                    filtered_count = len(articles)
                    if filtered_count < original_count:
                        logger.info(f"📋 Filtered out {original_count - filtered_count} duplicate articles")
                        logger.info(f"📰 Remaining NEW articles: {filtered_count}")
                    else:
                        logger.info(f"✅ All {filtered_count} articles are NEW (no duplicates found)")
                    
                    if not articles:
                        logger.warning("⚠️  All articles have already been posted!")
                        logger.info("💡 No new articles to post. Exiting.")
                        results['stages']['ai_processing'] = {'success': False, 'count': 0, 'reason': 'all_articles_already_posted'}
                        return results
                
                articles_dict = [a.to_dict() for a in articles]
                posts = self.ai_agent.process_batch(articles_dict, self.config['max_posts'], platform=platform)
            
            save_posts_json(posts, str(self.output_dir / f'posts_{timestamp}.json'))
            results['stages']['ai_processing'] = {'success': True, 'count': len(posts)}
            logger.info(f"Generated {len(posts)} posts")

            # Quality gate: review posts before publishing
            if posts:
                logger.info("\n🔍 QUALITY GATE: Reviewing posts...")
                reviewed = [self.ai_agent.quality_check(p) for p in posts]
                rejected_count = sum(1 for p in reviewed if p is None)
                posts = [p for p in reviewed if p is not None]
                if rejected_count:
                    logger.warning(f"⚠️  {rejected_count} post(s) failed quality gate (see errors/ folder)")

            if posts:
                logger.info("\nSample post:")
                logger.info("-" * 60)
                logger.info(posts[0].format_post())
                logger.info("-" * 60)
            else:
                logger.warning("⚠️  All posts failed quality gate, skipping publish.")
                results['stages']['publishing'] = {'success': False, 'reason': 'quality_gate_rejected_all'}
                self._save_results(results, timestamp)
                return results
            
            # STAGE 3: Publishing
            logger.info("\n📱 STAGE 3: Publishing to social media...")
            
            publish_results = self._publish_posts(posts, dry_run)
            results['stages']['publishing'] = {
                'success': True,
                'dry_run': dry_run,
                'results': publish_results
            }
            
            # Save complete results
            self._save_results(results, timestamp)
            
            logger.info("\n" + "="*60)
            logger.info("✅ Pipeline completed successfully!")
            logger.info("="*60)
            
            return results
            
        except Exception as e:
            logger.error(f"❌ Pipeline failed: {str(e)}", exc_info=True)
            results['error'] = str(e)
            return results
    
    def _sort_articles_by_recency(self, articles: list):
        """Sort articles so newest ones are first"""
        def _parse_date(art):
            value = getattr(art, 'published_date', '') or ''
            # Try ISO format first (what we generate when possible)
            try:
                return datetime.fromisoformat(value)
            except Exception:
                # If parsing fails, push it to the end
                return datetime.min

        return sorted(articles, key=_parse_date, reverse=True)
    
    def _find_today_articles(self) -> list:
        """Find articles from today's existing output files"""
        today_prefix = datetime.now().strftime('%Y%m%d')
        
        # Look for articles files from today
        article_files = list(self.output_dir.glob(f'articles_{today_prefix}*.json'))
        
        if not article_files:
            return []
        
        # Use the most recent file from today
        latest_file = max(article_files, key=lambda p: p.stat().st_mtime)
        logger.info(f"📂 Loading articles from: {latest_file.name}")
        
        try:
            with open(latest_file, 'r', encoding='utf-8') as f:
                articles_data = json.load(f)
            
            # Convert dict back to NewsArticle objects
            from news_scraper import NewsArticle
            articles = []
            for article_dict in articles_data:
                try:
                    article = NewsArticle(
                        title=article_dict.get('title', ''),
                        description=article_dict.get('description', ''),
                        url=article_dict.get('url', ''),
                        published_date=article_dict.get('published_date', ''),
                        source=article_dict.get('source', ''),
                        category=article_dict.get('category'),
                        image_url=article_dict.get('image_url')
                    )
                    articles.append(article)
                except Exception as e:
                    logger.warning(f"Failed to load article: {str(e)}")
                    continue
            
            return articles
        except Exception as e:
            logger.error(f"❌ Failed to load articles from {latest_file}: {str(e)}")
            return []
    
    def _find_today_posts(self, platform: str) -> list:
        """Find posts from today's existing output files"""
        from ai_agent import SocialMediaPost
        
        today_prefix = datetime.now().strftime('%Y%m%d')
        
        # Look for posts files from today
        post_files = list(self.output_dir.glob(f'posts_{today_prefix}*.json'))
        
        if not post_files:
            return []
        
        # Use the most recent file from today
        latest_file = max(post_files, key=lambda p: p.stat().st_mtime)
        logger.info(f"📂 Loading posts from: {latest_file.name}")
        
        try:
            with open(latest_file, 'r', encoding='utf-8') as f:
                posts_data = json.load(f)
            
            # Convert dict back to SocialMediaPost objects
            posts = []
            for post_dict in posts_data:
                try:
                    # Filter by platform if specified
                    post_platform = post_dict.get('platform', 'twitter')
                    if platform and post_platform != platform:
                        continue
                    
                    post = SocialMediaPost(
                        original_title=post_dict.get('original_title', ''),
                        original_url=post_dict.get('original_url', ''),
                        source=post_dict.get('source', ''),
                        content=post_dict.get('content', ''),
                        hashtags=post_dict.get('hashtags', []),
                        emoji=post_dict.get('emoji', '📰'),
                        platform=post_platform,
                        image_url=post_dict.get('image_url')
                    )
                    posts.append(post)
                except Exception as e:
                    logger.warning(f"Failed to load post: {str(e)}")
                    continue
            
            return posts
        except Exception as e:
            logger.error(f"❌ Failed to load posts from {latest_file}: {str(e)}")
            return []

    def _publish_posts(self, posts: list, dry_run: bool) -> dict:
        """Stage 3: Publish posts to social media"""
        
        if dry_run:
            logger.info("🧪 Running in DRY RUN mode (no actual posting)")
            for i, post in enumerate(posts, 1):
                logger.info(f"[DRY RUN {i}/{len(posts)}] Would post:")
                logger.info(post.format_post()[:150] + "...")
            return {'dry_run': True, 'count': len(posts)}
        
        # LIVE MODE - Actually post to social media
        platform = self.config.get('platform', 'twitter')
        platform_name = 'Instagram' if platform == 'instagram' else 'Twitter'
        logger.info(f"⚠️  LIVE MODE - Will actually post to {platform_name}!")
        
        try:
            # Local CLI publishes to Instagram only. Other channels run in the
            # Lambda pipeline via the CrossPoster (src/publishing.py).
            from social_publisher import InstagramPublisher
            publisher = InstagramPublisher()
            logger.info("✅ Instagram client initialized")

            posted_results = []
            
            # Post only the first post (one post per run)
            if posts:
                post = posts[0]
                try:
                    logger.info(f"\n📤 Posting to {platform_name}...")
                    
                    # Get image URL from post if available (for Instagram)
                    image_url = None
                    if platform == 'instagram':
                        image_url = getattr(post, 'image_url', None)
                        if not image_url:
                            error_msg = (
                                "❌ Instagram post requires an image_url but none was found.\n"
                                f"   Post title: {post.original_title}\n"
                                f"   Post URL: {post.original_url}\n"
                                "   Please ensure the RSS feed provides image URLs or skip this post."
                            )
                            logger.error(error_msg)
                            raise ValueError("Instagram post cannot be published without image_url")
                    
                    # Post to social media
                    if platform == 'instagram':
                        logger.info(f"   Image URL: {image_url}")
                        result = publisher.publish_post(post.format_post(), image_url=image_url, dry_run=False)
                    else:
                        result = publisher.publish_post(post.format_post(), dry_run=False)
                    
                    if result and 'id' in result:
                        if platform == 'instagram':
                            post_url = result.get('url', f"https://www.instagram.com/p/{result['id']}/")
                            logger.info(f"✅ SUCCESS! Instagram post published!")
                            logger.info(f"   Post ID: {result['id']}")
                            logger.info(f"   URL: {post_url}")
                            
                            posted_results.append({
                                'post_id': result['id'],
                                'url': post_url,
                                'original_title': post.original_title,
                                'original_url': post.original_url,
                                'status': 'success'
                            })
                        else:
                            tweet_url = f"https://twitter.com/i/web/status/{result['id']}"
                            logger.info(f"✅ SUCCESS! Tweet posted!")
                            logger.info(f"   Tweet ID: {result['id']}")
                            logger.info(f"   URL: {tweet_url}")
                            
                            posted_results.append({
                                'tweet_id': result['id'],
                                'url': tweet_url,
                                'original_title': post.original_title,
                                'original_url': post.original_url,
                                'status': 'success'
                            })
                    else:
                        logger.warning(f"❌ Failed to post to {platform_name}")
                        posted_results.append({
                            'status': 'failed',
                            'original_title': post.original_title
                        })
                
                except Exception as e:
                    logger.error(f"❌ Error posting to {platform_name}: {str(e)}")
                    posted_results.append({
                        'status': 'error',
                        'error': str(e),
                        'original_title': post.original_title
                    })
            
            success_count = sum(1 for r in posted_results if r.get('status') == 'success')
            logger.info(f"\n📊 Posted {success_count}/1 {platform_name.lower()} post successfully")
            
            return {
                'posted': success_count,
                'total': 1,
                'results': posted_results
            }
            
        except ValueError as e:
            platform = self.config.get('platform', 'twitter')
            if platform == 'instagram':
                logger.error(f"❌ Instagram credentials missing: {str(e)}")
                logger.info("💡 Add INSTAGRAM_ACCESS_TOKEN and INSTAGRAM_ACCOUNT_ID to .env file")
            else:
                logger.error(f"❌ Twitter credentials missing: {str(e)}")
                logger.info("💡 Add Twitter API keys to .env file to enable posting")
            return {'error': f'Missing {platform} credentials', 'dry_run': True}
        
        except Exception as e:
            logger.error(f"❌ Publishing error: {str(e)}", exc_info=True)
            return {'error': str(e)}
    
    
    def _get_posted_urls(self) -> set:
        """Get URLs of all previously posted articles from all posts_*.json files"""
        posted_urls = set()
        
        # Look for all posts_*.json files (which contain successfully posted articles)
        posts_files = list(self.output_dir.glob('posts_*.json'))
        
        logger.debug(f"Checking {len(posts_files)} posts files for duplicate detection")
        
        for file in posts_files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    posts_data = json.load(f)
                    
                # posts_*.json contains a list of SocialMediaPost objects
                if isinstance(posts_data, list):
                    for post in posts_data:
                        if isinstance(post, dict):
                            original_url = post.get('original_url') or post.get('url')
                            if original_url:
                                posted_urls.add(original_url)
                elif isinstance(posts_data, dict):
                    # Sometimes it might be wrapped in a dict
                    posts_list = posts_data.get('posts', [])
                    if isinstance(posts_list, list):
                        for post in posts_list:
                            if isinstance(post, dict):
                                original_url = post.get('original_url') or post.get('url')
                                if original_url:
                                    posted_urls.add(original_url)
                                
            except Exception as e:
                logger.debug(f"Error reading {file}: {str(e)}")
                continue
        
        return posted_urls
    
    def _save_results(self, results: dict, timestamp: str):
        """Save complete pipeline results"""
        results_file = self.output_dir / f'pipeline_results_{timestamp}.json'
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"💾 Saved pipeline results to {results_file}")


def main():
    parser = argparse.ArgumentParser(description='News AI Agent Pipeline')
    parser.add_argument('--dry-run', action='store_true', help='Simulate without posting')
    parser.add_argument('--no-dry-run', action='store_true', help='Actually post to Twitter')
    parser.add_argument('--max-posts', type=int, default=1, help='Maximum posts to generate (default: 1)')
    parser.add_argument('--output-dir', type=str, default='output', help='Output directory')
    parser.add_argument(
        '--platform',
        type=str,
        choices=['twitter', 'instagram'],
        default='instagram',
        help='Social media platform (default: instagram)',
    )
    parser.add_argument(
        '--use-existing-today',
        action='store_true',
        help='Use existing scraped articles from today if available (skip new scraping)',
    )
    
    args = parser.parse_args()
    
    # Default to safe mode (dry_run=True) unless explicitly disabled
    dry_run = not args.no_dry_run
    
    config = {
        'output_dir': args.output_dir,
        'max_articles_per_source': 2,
        'max_posts': args.max_posts,
        'dry_run': dry_run,
        'platform': args.platform,
        'use_existing_today': args.use_existing_today,
    }
    
    pipeline = NewsAIPipeline(config)
    results = pipeline.run()
    
    # Print summary
    print("\n" + "="*60)
    print("📊 PIPELINE SUMMARY")
    print("="*60)
    for stage, data in results.get('stages', {}).items():
        print(f"\n{stage.upper()}:")
        for key, value in data.items():
            if key != 'results':
                print(f"  {key}: {value}")
    print("\n" + "="*60)


if __name__ == "__main__":
    main()
