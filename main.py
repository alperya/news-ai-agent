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


class NewsAIPipeline:
    def __init__(self, config: dict = None):
        self.config = config or {
            'output_dir': 'output',
            'max_articles_per_source': 2,
            'max_posts': 5,
            'dry_run': True
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
            articles = self.scraper.scrape_all_sources(self.config['max_articles_per_source'])
            save_articles_json(articles, str(self.output_dir / f'articles_{timestamp}.json'))
            results['stages']['scraping'] = {'success': True, 'count': len(articles)}
            logger.info(f"Scraped {len(articles)} articles")
            
            if not articles:
                logger.warning("No articles found!")
                return results
            
            # STAGE 2: AI Processing
            logger.info("\n🤖 STAGE 2: Processing with AI...")
            posts = self.ai_agent.process_batch([a.to_dict() for a in articles], self.config['max_posts'])
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
    
    def _publish_posts(self, posts: list, dry_run: bool) -> dict:
        """Stage 3: Publish posts to social media"""
        
        if dry_run:
            logger.info("🧪 Running in DRY RUN mode (no actual posting)")
            for i, post in enumerate(posts, 1):
                logger.info(f"[DRY RUN {i}/{len(posts)}] Would post:")
                logger.info(post.format_post()[:150] + "...")
            return {'dry_run': True, 'count': len(posts)}
        
        # LIVE MODE - Actually post to Twitter
        logger.info("⚠️  LIVE MODE - Will actually post to Twitter!")
        
        try:
            from social_publisher import TwitterPublisher
            
            publisher = TwitterPublisher()
            logger.info("✅ Twitter client initialized")
            
            posted_results = []
            
            for i, post in enumerate(posts, 1):
                try:
                    logger.info(f"\n📤 Posting {i}/{len(posts)}...")
                    
                    # Post tweet
                    result = publisher.publish_post(post.format_post(), dry_run=False)
                    
                    if result and 'id' in result:
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
                        logger.warning(f"❌ Failed to post tweet {i}")
                        posted_results.append({
                            'status': 'failed',
                            'original_title': post.original_title
                        })
                    
                    # Rate limiting - wait between tweets
                    if i < len(posts):
                        delay = 60  # 60 seconds between tweets
                        logger.info(f"⏳ Waiting {delay} seconds before next tweet...")
                        time.sleep(delay)
                
                except Exception as e:
                    logger.error(f"❌ Error posting tweet {i}: {str(e)}")
                    posted_results.append({
                        'status': 'error',
                        'error': str(e),
                        'original_title': post.original_title
                    })
            
            success_count = sum(1 for r in posted_results if r.get('status') == 'success')
            logger.info(f"\n📊 Posted {success_count}/{len(posts)} tweets successfully")
            
            return {
                'posted': success_count,
                'total': len(posts),
                'results': posted_results
            }
            
        except ValueError as e:
            logger.error(f"❌ Twitter credentials missing: {str(e)}")
            logger.info("💡 Add Twitter API keys to .env file to enable posting")
            return {'error': 'Missing Twitter credentials', 'dry_run': True}
        
        except Exception as e:
            logger.error(f"❌ Publishing error: {str(e)}", exc_info=True)
            return {'error': str(e)}
    
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
    parser.add_argument('--max-posts', type=int, default=5, help='Maximum posts to generate')
    parser.add_argument('--output-dir', type=str, default='output', help='Output directory')
    
    args = parser.parse_args()
    
    # Determine dry_run mode
    if args.no_dry_run:
        dry_run = False
    elif args.dry_run:
        dry_run = True
    else:
        dry_run = True  # Default to safe mode
    
    config = {
        'output_dir': args.output_dir,
        'max_articles_per_source': 2,
        'max_posts': args.max_posts,
        'dry_run': dry_run
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
