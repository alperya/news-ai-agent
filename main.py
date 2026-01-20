"""Main Pipeline - News AI Agent with Twitter Integration"""

# Load environment variables from .env file
from dotenv import load_dotenv
load_dotenv()



import logging
import argparse
import json
from datetime import datetime
from pathlib import Path
import time

from news_scraper import DutchNewsScraper, save_articles_json
from ai_agent import NewsAIAgent, save_posts_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Best times for Instagram engagement (CET timezone)
INSTAGRAM_OPTIMAL_HOURS = [11, 15, 19]  # 11:00, 15:00, 19:00


class NewsAIPipeline:
    def __init__(self, config: dict = None):
        # Default config; can be overridden via CLI
        self.config = config or {
            'output_dir': 'output',
            'max_articles_per_source': 2,
            'max_posts': 3,  # Maximum 3 posts per day
            'dry_run': True,
            # Default: 4 hours between posts (in seconds) - to space out 3 posts per day
            'publish_interval_seconds': 4 * 60 * 60,
            'platform': 'instagram',  # 'twitter' or 'instagram'
            'use_existing_today': False,  # Use existing scraped articles from today if available
            'max_posts_per_day': 3,  # Daily limit
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
        
        # Check daily post limit
        if not dry_run:
            posts_today = self._count_posts_today()
            max_daily = self.config.get('max_posts_per_day', 3)
            if posts_today >= max_daily:
                logger.warning(f"⚠️  Daily limit reached! Already posted {posts_today}/{max_daily} times today.")
                logger.info("💡 Skipping pipeline run to respect daily limit.")
                return {'error': 'Daily post limit reached', 'posts_today': posts_today}
            logger.info(f"📊 Posts today: {posts_today}/{max_daily}")
        
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
            articles_dict = [a.to_dict() for a in articles]
            posts = self.ai_agent.process_batch(articles_dict, self.config['max_posts'], platform=platform)
            save_posts_json(posts, str(self.output_dir / f'posts_{timestamp}.json'))
            results['stages']['ai_processing'] = {'success': True, 'count': len(posts)}
            logger.info(f"Generated {len(posts)} posts")
            
            if posts:
                logger.info("\nSample post:")
                logger.info("-" * 60)
                logger.info(posts[0].format_post())
                logger.info("-" * 60)
            
            # STAGE 3: Publishing
            logger.info("\n📱 STAGE 3: Publishing to social media...")
            
            # Check if it's optimal time for Instagram
            if not dry_run and self.config.get('platform') == 'instagram':
                if not self._should_post_now():
                    next_time = self._get_next_optimal_time()
                    logger.warning(f"⏰ Not optimal time for Instagram posting!")
                    logger.info(f"💡 Best posting times: {', '.join([f'{h}:00' for h in INSTAGRAM_OPTIMAL_HOURS])}")
                    logger.info(f"📅 Next optimal time: {next_time.strftime('%Y-%m-%d %H:%M')}")
                    logger.info("🧪 Switching to DRY RUN mode")
                    dry_run = True
            
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
            if platform == 'instagram':
                from social_publisher import InstagramPublisher
                publisher = InstagramPublisher()
                logger.info("✅ Instagram client initialized")
            else:
                from social_publisher import TwitterPublisher
                publisher = TwitterPublisher()
                logger.info("✅ Twitter client initialized")
            
            posted_results = []
            
            for i, post in enumerate(posts, 1):
                try:
                    logger.info(f"\n📤 Posting {i}/{len(posts)} to {platform_name}...")
                    
                    # Get image URL from post if available (for Instagram)
                    image_url = None
                    if platform == 'instagram':
                        image_url = getattr(post, 'image_url', None)
                        if not image_url:
                            logger.warning(f"⚠️  No image URL for post {i}, Instagram post may fail")
                    
                    # Post to social media
                    if platform == 'instagram':
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
                                'status': 'success'
                            })
                    else:
                        logger.warning(f"❌ Failed to post {platform_name} post {i}")
                        posted_results.append({
                            'status': 'failed',
                            'original_title': post.original_title
                        })
                    
                    # Rate limiting - wait between tweets
                    if i < len(posts):
                        # Wait between tweets (configured, default ~2 hours)
                        delay = self.config.get('publish_interval_seconds', 2 * 60 * 60)
                        logger.info(f"⏳ Waiting {delay} seconds (~{delay/3600:.1f} hours) before next tweet...")
                        time.sleep(delay)
                
                except Exception as e:
                    logger.error(f"❌ Error posting {platform_name} post {i}: {str(e)}")
                    posted_results.append({
                        'status': 'error',
                        'error': str(e),
                        'original_title': post.original_title
                    })
            
            success_count = sum(1 for r in posted_results if r.get('status') == 'success')
            logger.info(f"\n📊 Posted {success_count}/{len(posts)} {platform_name.lower()} posts successfully")
            
            return {
                'posted': success_count,
                'total': len(posts),
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
    
    def _count_posts_today(self) -> int:
        """Count how many posts were successfully made today"""
        today_prefix = datetime.now().strftime('%Y%m%d')
        result_files = list(self.output_dir.glob(f'pipeline_results_{today_prefix}*.json'))
        
        total_posts = 0
        for file in result_files:
            try:
                with open(file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    publishing = data.get('stages', {}).get('publishing', {})
                    if not publishing.get('dry_run', True):
                        total_posts += publishing.get('posted', 0)
            except Exception as e:
                logger.warning(f"Error reading {file}: {str(e)}")
        
        return total_posts
    
    def _get_next_optimal_time(self) -> datetime:
        """Get next optimal posting time for Instagram"""
        now = datetime.now()
        current_hour = now.hour
        
        # Find next optimal hour
        for hour in INSTAGRAM_OPTIMAL_HOURS:
            if hour > current_hour:
                next_time = now.replace(hour=hour, minute=0, second=0, microsecond=0)
                return next_time
        
        # If no more optimal times today, return first optimal time tomorrow
        tomorrow = now.replace(hour=INSTAGRAM_OPTIMAL_HOURS[0], minute=0, second=0, microsecond=0)
        tomorrow = tomorrow.replace(day=tomorrow.day + 1)
        return tomorrow
    
    def _should_post_now(self) -> bool:
        """Check if current time is optimal for posting"""
        if self.config.get('platform') != 'instagram':
            return True  # No time restrictions for Twitter
        
        current_hour = datetime.now().hour
        # Allow posting within 30 minutes of optimal hours
        for optimal_hour in INSTAGRAM_OPTIMAL_HOURS:
            if abs(current_hour - optimal_hour) < 1:
                return True
        
        return False
    
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
    parser.add_argument('--max-posts', type=int, default=3, help='Maximum posts to generate (default: 3)')
    parser.add_argument('--output-dir', type=str, default='output', help='Output directory')
    parser.add_argument(
        '--interval-hours',
        type=float,
        default=4.0,
        help='Interval between posts in hours (default: 4.0 for 3 posts/day)',
    )
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
    
    # Determine dry_run mode
    if args.no_dry_run:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = True  # Default to safe mode
    
    publish_interval_seconds = int(args.interval_hours * 60 * 60)

    config = {
        'output_dir': args.output_dir,
        'max_articles_per_source': 2,
        'max_posts': args.max_posts,
        'dry_run': dry_run,
        'publish_interval_seconds': publish_interval_seconds,
        'platform': args.platform,
        'use_existing_today': args.use_existing_today,
        'max_posts_per_day': 3,  # Daily limit
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
