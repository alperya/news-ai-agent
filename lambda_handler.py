"""
AWS Lambda Handler for News AI Agent
Automatically posts to Instagram at scheduled times
"""

import json
import logging
import os
import sys
from datetime import datetime
import boto3
from botocore.exceptions import ClientError

# Add src/ directory to path for imports (locally src/ is a subfolder, in Lambda ZIP all files are flat)
_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_dir, 'src'))
sys.path.insert(0, _dir)

from news_scraper import DutchNewsScraper
from ai_agent import NewsAIAgent
from social_publisher import InstagramPublisher

# Configure logging for Lambda
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def get_secrets():
    """
    Load secrets from AWS Secrets Manager
    """
    secret_name = os.environ.get('SECRET_NAME', 'news-ai-agent/credentials')
    region_name = os.environ.get('AWS_REGION', 'eu-central-1')
    
    # Create a Secrets Manager client
    session = boto3.session.Session()
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name
    )
    
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name
        )
    except ClientError as e:
        logger.error(f"❌ Error retrieving secrets: {str(e)}")
        raise e
    
    # Parse and return secrets
    secret = json.loads(get_secret_value_response['SecretString'])
    
    # Set environment variables for the application
    os.environ['ANTHROPIC_API_KEY'] = secret['ANTHROPIC_API_KEY']
    os.environ['INSTAGRAM_ACCESS_TOKEN'] = secret['INSTAGRAM_ACCESS_TOKEN']
    os.environ['INSTAGRAM_ACCOUNT_ID'] = secret['INSTAGRAM_ACCOUNT_ID']
    
    # Set AI prompts if available
    if 'AI_PROMPT_BATCH_SELECTION' in secret:
        os.environ['AI_PROMPT_BATCH_SELECTION'] = secret['AI_PROMPT_BATCH_SELECTION']
    if 'AI_PROMPT_SINGLE_ARTICLE' in secret:
        os.environ['AI_PROMPT_SINGLE_ARTICLE'] = secret['AI_PROMPT_SINGLE_ARTICLE']
    if 'AI_PROMPT_QUALITY_CHECK' in secret:
        os.environ['AI_PROMPT_QUALITY_CHECK'] = secret['AI_PROMPT_QUALITY_CHECK']
    
    logger.info("✅ Secrets loaded successfully")
    return secret


def save_to_s3(data, filename, bucket_name):
    """
    Save data to S3 bucket
    
    Args:
        data: Data to save (will be JSON serialized)
        filename: Name of the file
        bucket_name: S3 bucket name
    """
    try:
        s3_client = boto3.client('s3')
        s3_client.put_object(
            Bucket=bucket_name,
            Key=filename,
            Body=json.dumps(data, indent=2, ensure_ascii=False),
            ContentType='application/json'
        )
        logger.info(f"✅ Saved to S3: s3://{bucket_name}/{filename}")
    except Exception as e:
        logger.error(f"❌ Failed to save to S3: {str(e)}")


def get_published_urls(bucket_name):
    """
    Get set of previously published article URLs from S3
    
    Args:
        bucket_name: S3 bucket name
        
    Returns:
        set of URLs that have been published
    """
    published_urls = set()
    s3_client = boto3.client('s3')
    
    try:
        # Get all posts_*.json files from S3
        list_response = s3_client.list_objects_v2(
            Bucket=bucket_name,
            Prefix='posts_'
        )
        
        if 'Contents' not in list_response:
            logger.info("📋 No previous posts found in S3")
            return published_urls
        
        logger.info(f"🔍 Checking {len(list_response['Contents'])} posts files for duplicates...")
        
        for obj in list_response['Contents']:
            key = obj['Key']
            if not key.startswith('posts_') or not key.endswith('.json'):
                continue
                
            try:
                # Download and parse each posts file
                obj_response = s3_client.get_object(Bucket=bucket_name, Key=key)
                posts_data = json.loads(obj_response['Body'].read().decode('utf-8'))
                
                # Extract URLs from posts
                if isinstance(posts_data, list):
                    for post in posts_data:
                        if isinstance(post, dict):
                            url = post.get('original_url') or post.get('url')
                            if url:
                                published_urls.add(url)
                                
            except Exception as e:
                logger.debug(f"Error reading {key}: {str(e)}")
                continue
        
        logger.info(f"📋 Found {len(published_urls)} previously published articles")
        return published_urls
        
    except Exception as e:
        logger.error(f"❌ Error getting published URLs: {str(e)}")
        return published_urls


def lambda_handler(event, context):
    """
    AWS Lambda handler function
    
    Args:
        event: Lambda event (from EventBridge)
        context: Lambda context
        
    Returns:
        Response with status and details
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    bucket_name = os.environ.get('RESULTS_BUCKET') or os.environ.get('S3_BUCKET_NAME', 'news-ai-agent-results-645949963620')
    
    logger.info("="*60)
    logger.info("🚀 News AI Agent Lambda - Starting")
    logger.info(f"📅 Timestamp: {timestamp}")
    logger.info("="*60)
    
    try:
        # Load secrets from AWS Secrets Manager
        logger.info("\n🔐 Loading secrets from AWS Secrets Manager...")
        get_secrets()
        
        # STAGE 1: Scrape news
        logger.info("\n📰 STAGE 1: Scraping news articles...")
        scraper = DutchNewsScraper()
        articles = scraper.scrape_all_sources(max_articles_per_source=2)
        articles_data = [article.to_dict() for article in articles]
        
        if not articles_data:
            logger.warning("⚠️  No articles found!")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'no_articles',
                    'message': 'No articles found to process',
                    'timestamp': timestamp
                })
            }
        
        logger.info(f"✅ Scraped {len(articles_data)} articles")
        
        # Filter out already published articles
        logger.info("\n🔍 DUPLICATE CHECK: Checking for previously published articles...")
        published_urls = get_published_urls(bucket_name)
        
        if published_urls:
            original_count = len(articles_data)
            articles_data = [a for a in articles_data if a.get('url') not in published_urls]
            filtered_count = len(articles_data)
            
            if filtered_count < original_count:
                logger.info(f"📋 Filtered out {original_count - filtered_count} duplicate article(s)")
                logger.info(f"📰 Remaining NEW articles: {filtered_count}")
            else:
                logger.info(f"✅ All {filtered_count} articles are NEW (no duplicates found)")
        
        if not articles_data:
            logger.warning("⚠️  All articles have already been posted!")
            save_to_s3({
                'status': 'all_duplicates',
                'message': 'All articles have been previously published',
                'timestamp': timestamp,
                'original_count': original_count
            }, f'pipeline_results_{timestamp}.json', bucket_name)
            
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'all_duplicates',
                    'message': 'All articles already published',
                    'timestamp': timestamp
                })
            }
        
        # STAGE 2: Process with AI
        logger.info("\n🤖 STAGE 2: Processing with AI...")
        ai_agent = NewsAIAgent()
        posts = ai_agent.process_batch(
            articles=articles_data,
            max_posts=1,
            platform='instagram'
        )
        
        if not posts:
            logger.warning("⚠️  No posts generated!")
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'no_posts',
                    'message': 'No posts generated from articles',
                    'timestamp': timestamp,
                    'articles_count': len(articles_data)
                })
            }
        
        # Convert SocialMediaPost objects to dicts
        posts_data = [post.to_dict() for post in posts]
        logger.info(f"✅ Generated {len(posts_data)} post(s)")

        # Quality gate: review posts before publishing
        logger.info("\n🔍 QUALITY GATE: Reviewing posts...")
        reviewed = [ai_agent.quality_check(p) for p in posts]
        rejected_count = sum(1 for p in reviewed if p is None)
        posts = [p for p in reviewed if p is not None]

        if rejected_count:
            logger.warning(f"⚠️  {rejected_count} post(s) failed quality gate")
            # Upload local error/correction files to S3
            from pathlib import Path
            errors_dir = Path(__file__).parent / 'errors'
            if errors_dir.exists():
                for err_file in errors_dir.glob('*.json'):
                    try:
                        err_data = json.loads(err_file.read_text(encoding='utf-8'))
                        save_to_s3(err_data, f'errors/{err_file.name}', bucket_name)
                    except Exception:
                        pass

        if not posts:
            logger.warning("⚠️  All posts failed quality gate, skipping publish.")
            save_to_s3({
                'status': 'quality_gate_rejected',
                'message': 'All posts failed quality gate',
                'timestamp': timestamp,
                'rejected_count': rejected_count
            }, f'pipeline_results_{timestamp}.json', bucket_name)
            return {
                'statusCode': 200,
                'body': json.dumps({
                    'status': 'quality_gate_rejected',
                    'message': 'All posts failed quality gate',
                    'timestamp': timestamp
                })
            }

        posts_data = [post.to_dict() for post in posts]

        # STAGE 3: Publish to Instagram
        logger.info("\n📱 STAGE 3: Publishing to Instagram...")
        publisher = InstagramPublisher()
        
        published_count = 0
        for idx, post in enumerate(posts_data, 1):
            try:
                logger.info(f"\n📤 Publishing post {idx}/{len(posts)}...")
                
                result = publisher.publish_post(
                    content=post['full_post'],
                    image_url=post.get('image_url'),
                    dry_run=False
                )
                
                logger.info(f"✅ Post {idx} published: {result}")
                published_count += 1
                
            except Exception as e:
                logger.error(f"❌ Failed to publish post {idx}: {str(e)}")
                # Continue with next post if available
                continue
        
        # Success response
        logger.info("\n" + "="*60)
        logger.info(f"✅ Lambda execution completed successfully!")
        logger.info(f"📊 Published: {published_count}/{len(posts)} posts")
        logger.info("="*60)
        
        # Save results to S3
        results = {
            'status': 'success',
            'articles_scraped': len(articles_data),
            'posts_generated': len(posts),
            'posts_published': published_count,
            'timestamp': timestamp,
            'articles': articles_data,
            'posts': posts_data
        }
        save_to_s3(results, f'pipeline_results_{timestamp}.json', bucket_name)
        save_to_s3(articles_data, f'articles_{timestamp}.json', bucket_name)
        save_to_s3(posts_data, f'posts_{timestamp}.json', bucket_name)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'status': 'success',
                'articles_scraped': len(articles_data),
                'posts_generated': len(posts),
                'posts_published': published_count,
                'timestamp': timestamp
            })
        }
        
    except Exception as e:
        logger.error(f"\n❌ Lambda execution failed: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'status': 'error',
                'error': str(e),
                'timestamp': timestamp
            })
        }
