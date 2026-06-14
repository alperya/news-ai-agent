"""
AWS Lambda Handler for News AI Agent
Automatically posts to Instagram at scheduled times
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
import boto3
from botocore.exceptions import ClientError

# Add src/ directory to path for imports (locally src/ is a subfolder, in Lambda ZIP all files are flat)
_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_dir, 'src'))
sys.path.insert(0, _dir)

from news_scraper import DutchNewsScraper
from ai_agent import NewsAIAgent
from social_publisher import InstagramPublisher
from video import create_news_video
from event_scraper import EventScraper
from video.event_card import generate_carousel_slides, generate_reels_video
from notifier import send_alert, alert_on_exception, detect_error_type, send_event_summary

# Configure logging for Lambda
logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler_metrics_collector(event, context):
    """Daily metrics collection — EventBridge 02:00 Amsterdam (00:00 UTC)."""
    logger.info("="*60)
    logger.info("📊 Metrics Collector — Starting")
    logger.info("="*60)
    try:
        get_secrets()
        from metrics_collector import MetricsCollector
        dry_run = isinstance(event, dict) and bool(event.get("dry_run", False))
        result = MetricsCollector().collect(dry_run=dry_run)
        logger.info(f"✅ Metrics collected: {result}")
        return {"statusCode": 200, "body": json.dumps(result)}
    except PermissionError as e:
        logger.error(f"❌ Instagram permission error: {e}")
        send_alert("Metrics collector: missing instagram_manage_insights permission", str(e), "GENERAL")
        return {"statusCode": 403, "body": json.dumps({"error": str(e)})}
    except Exception as e:
        logger.error(f"❌ Metrics collector failed: {e}", exc_info=True)
        send_alert("Metrics collector failed", str(e), "GENERAL")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def handler_analytics_engine(event, context):
    """Weekly analytics run — EventBridge Sunday 22:00 Amsterdam (20:00 UTC)."""
    logger.info("="*60)
    logger.info("🧠 Analytics Engine — Starting")
    logger.info("="*60)
    try:
        get_secrets()
        from analytics_engine import AnalyticsEngine
        dry_run = isinstance(event, dict) and bool(event.get("dry_run", False))
        result = AnalyticsEngine().run(dry_run=dry_run)
        logger.info(f"✅ Analytics complete: {result.get('status')}")
        return {"statusCode": 200, "body": json.dumps(result, default=str)}
    except Exception as e:
        logger.error(f"❌ Analytics engine failed: {e}", exc_info=True)
        send_alert("Analytics engine failed", str(e), "GENERAL")
        return {"statusCode": 500, "body": json.dumps({"error": str(e)})}


def get_secrets():
    """
    Load secrets from AWS Secrets Manager
    """
    secret_name = os.environ.get('SECRET_NAME', 'news-ai-agent/credentials')
    region_name = os.environ.get('AWS_REGION', 'eu-central-1')
    
    # Create a Secrets Manager client
    session = boto3.Session()
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
    
    # Set Pexels API key for stock footage (optional)
    if 'PEXELS_API_KEY' in secret:
        os.environ['PEXELS_API_KEY'] = secret['PEXELS_API_KEY']

    # Set ElevenLabs API key for natural TTS voice (optional)
    if 'ELEVENLABS_API_KEY' in secret:
        os.environ['ELEVENLABS_API_KEY'] = secret['ELEVENLABS_API_KEY']
    if 'ELEVENLABS_VOICE_ID' in secret:
        os.environ['ELEVENLABS_VOICE_ID'] = secret['ELEVENLABS_VOICE_ID']

    # Event scraper API keys (optional)
    for key in ('EVENTBRITE_API_KEY', 'TICKETMASTER_API_KEY'):
        if key in secret:
            os.environ[key] = secret[key]

    # Set AI prompts if available
    if 'AI_PROMPT_BATCH_SELECTION' in secret:
        os.environ['AI_PROMPT_BATCH_SELECTION'] = secret['AI_PROMPT_BATCH_SELECTION']
    if 'AI_PROMPT_SINGLE_ARTICLE' in secret:
        os.environ['AI_PROMPT_SINGLE_ARTICLE'] = secret['AI_PROMPT_SINGLE_ARTICLE']
    if 'AI_PROMPT_QUALITY_CHECK' in secret:
        os.environ['AI_PROMPT_QUALITY_CHECK'] = secret['AI_PROMPT_QUALITY_CHECK']
    if 'AI_PROMPT_EVENT_SELECTION' in secret:
        os.environ['AI_PROMPT_EVENT_SELECTION'] = secret['AI_PROMPT_EVENT_SELECTION']

    # LangSmith observability (optional)
    for key in ('LANGCHAIN_API_KEY', 'LANGCHAIN_PROJECT', 'LANGCHAIN_TRACING_V2'):
        if key in secret:
            os.environ[key] = secret[key]

    # Langfuse observability (optional)
    for key in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_HOST', 'LANGFUSE_BASE_URL'):
        if key in secret:
            os.environ[key] = secret[key]

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


def _invoke_youtube_async(lambda_client, s3_video_key, post_content, hook, hashtags, bucket_name, timestamp, context=''):
    """Fire-and-forget YouTube Shorts publish. Failure never blocks Instagram."""
    youtube_fn = os.environ.get('YOUTUBE_PUBLISH_FUNCTION_NAME', 'news-ai-agent-youtube-publish')
    try:
        lambda_client.invoke(
            FunctionName=youtube_fn,
            InvocationType='Event',
            Payload=json.dumps({
                's3_video_key': s3_video_key,
                'post_content': post_content,
                'hook': hook,
                'hashtags': hashtags,
                'bucket_name': bucket_name,
                'timestamp': timestamp,
            }).encode(),
        )
        logger.info(f"✅ YouTube {context}publish delegated to {youtube_fn} (async)")
    except Exception as yt_err:
        logger.warning(f"⚠️ YouTube {context}invoke failed (non-critical): {yt_err}")


def _finish_event_pipeline(
    status: str,
    summary: dict,
    log_key: str,
    bucket_name: str,
    timestamp: str,
    http_code: int = 200,
    error: str = "",
) -> dict:
    """Finalise an event pipeline run: persist summary, send email, return HTTP response."""
    summary["status"] = status
    if error:
        summary["error"] = error
    save_to_s3(dict(summary), log_key, bucket_name)
    send_event_summary(summary)
    return {
        "statusCode": http_code,
        "body": json.dumps({"status": status, "timestamp": timestamp, "error": error}),
    }


def _run_event_pipeline(timestamp: str, bucket_name: str, ai_agent, dry_run: bool = False) -> dict:
    """Weekly events pipeline — runs on Wednesday 18:00 (format: event_post).

    Collects a full run summary, writes detailed logs to S3, and emails
    a digest to the alert address regardless of success or failure.
    """
    logger.info("\n" + "="*60)
    logger.info("📅 EVENT PIPELINE: Weekly NL Events Post")
    logger.info("="*60)

    ts_human = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log_key = f"events/pipeline_{timestamp}.json"

    # Summary dict — built up throughout each stage, sent as email at the end
    summary: dict = {
        "status": "started",
        "timestamp": ts_human,
        "source_results": [],
        "raw_count": 0,
        "unique_count": 0,
        "valid_count": 0,
        "scored_count": 0,
        "all_scored": [],
        "score_prompt": "",
        "score_response": "",
        "selected_events": [],
        "caption": "",
        "selection_prompt": "",
        "selection_response": "",
        "publish_result": None,
        "s3_log_key": log_key,
    }

    def _finish(status: str, http_code: int = 200, error: str = "") -> dict:
        return _finish_event_pipeline(
            status, summary, log_key, bucket_name, timestamp, http_code, error
        )

    try:
        # ── Stage 1: Scrape ──────────────────────────────────────────────────
        logger.info("\n🔍 STAGE 1: Scraping events from 8 sources...")
        scraper = EventScraper()
        raw_events = scraper.scrape_all_sources(days_ahead=7)

        source_results = getattr(scraper, "source_results", [])
        all_raw_count = sum(s.get("count", 0) for s in source_results)
        summary["source_results"] = source_results
        summary["raw_count"] = all_raw_count
        summary["valid_count"] = len(raw_events)

        # Save all raw events to S3 for audit
        save_to_s3(
            [e.to_dict() for e in raw_events],
            f"events/raw_events_{timestamp}.json", bucket_name,
        )
        save_to_s3(dict(summary), log_key, bucket_name)

        if not raw_events:
            logger.warning("⚠️  No valid events found from any source.")
            return _finish("no_events")

        logger.info(f"✅ {len(raw_events)} valid events after scraping")

        # ── Stage 2: AI quality scoring (Haiku) ─────────────────────────────
        logger.info("\n🤖 STAGE 2: AI quality scoring (Haiku)...")
        events_dicts = [e.to_dict() for e in raw_events]
        scored = ai_agent.score_events(events_dicts)

        all_scored = getattr(ai_agent, "_last_all_scored", [])
        summary["scored_count"] = len(scored)
        summary["all_scored"] = all_scored
        summary["score_prompt"] = getattr(ai_agent, "_last_score_prompt", "")
        summary["score_response"] = getattr(ai_agent, "_last_score_response", "")

        save_to_s3(
            {"scored_events": all_scored,
             "prompt": summary["score_prompt"],
             "response": summary["score_response"]},
            f"events/scoring_{timestamp}.json", bucket_name,
        )
        save_to_s3(dict(summary), log_key, bucket_name)

        if len(scored) < 3:
            msg = f"Only {len(scored)} events passed quality gate (need ≥ 3)."
            logger.warning(f"⚠️  {msg}")
            return _finish("quality_gate_failed", error=msg)

        logger.info(f"✅ {len(scored)} events passed quality gate")

        # ── Stage 3: AI selection + caption (Opus) ───────────────────────────
        logger.info("\n✍️  STAGE 3: AI event selection and caption (Opus)...")
        now = datetime.now(timezone.utc)
        week_end = now + timedelta(days=7)
        date_range = f"{now.strftime('%-d')}–{week_end.strftime('%-d %B %Y')}"

        result = ai_agent.select_and_format_events(scored, date_range=date_range, min_events=5, max_events=12)
        if not result:
            return _finish("ai_failed", http_code=500, error="AI selection returned empty result.")

        selected_events = result["selected_events"]
        caption = result["caption"]
        summary["selected_events"] = selected_events
        summary["caption"] = caption
        summary["selection_prompt"] = result.get("_prompt", "")
        summary["selection_response"] = result.get("_raw_response", "")

        save_to_s3(
            {"selected_events": selected_events, "caption": caption,
             "prompt": summary["selection_prompt"],
             "response": summary["selection_response"]},
            f"events/selection_{timestamp}.json", bucket_name,
        )
        save_to_s3(dict(summary), log_key, bucket_name)

        logger.info(f"✅ {len(selected_events)} events selected, caption {len(caption)} chars")

        # ── Stage 4: Generate slides → Reels video ───────────────────────────
        # Cover slide + event list slides → stitched into a single MP4 with music
        logger.info("\n🎨 STAGE 4: Generating Reels video...")
        s3_client = boto3.client("s3")

        local_slides = generate_carousel_slides(
            events=selected_events,
            date_range=date_range,
            tmp_prefix=f"/tmp/event_{timestamp}",
        )
        logger.info(f"✅ {len(local_slides)} slides generated")

        reel_path = f"/tmp/event_{timestamp}_reel.mp4"
        generate_reels_video(local_slides, reel_path)
        logger.info(f"✅ Reels video generated: {reel_path}")

        # Upload video to S3 — dry_run uses a separate prefix so it's easy to find
        reel_s3_key = f"events/{'test' if dry_run else 'reel'}_{timestamp}.mp4"
        with open(reel_path, "rb") as f:
            s3_client.put_object(
                Bucket=bucket_name, Key=reel_s3_key, Body=f, ContentType="video/mp4",
            )
        video_url = s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket_name, "Key": reel_s3_key},
            ExpiresIn=3600,
        )
        logger.info(f"✅ Reels video uploaded to S3: s3://{bucket_name}/{reel_s3_key}")
        logger.info(f"🔗 Pre-signed URL (1h): {video_url[:120]}…")

        # ── Stage 5: Publish as Reels ─────────────────────────────────────────
        logger.info(f"\n📱 STAGE 5: Publishing REELS...")
        publisher = InstagramPublisher()
        publish_result = publisher.publish_reels(
            content=caption, video_url=video_url, dry_run=dry_run,
        )
        logger.info(f"✅ Reels published: {publish_result}")
        summary["publish_result"] = str(publish_result)

        # Final S3 records
        save_to_s3(selected_events, f"events_{timestamp}.json", bucket_name)

        return _finish("success")

    except Exception as e:
        logger.error(f"❌ Event pipeline failed: {e}", exc_info=True)
        return _finish("error", http_code=500, error=str(e))


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

        # Detect run mode early — event_post bypasses the news pipeline entirely
        is_event_post = isinstance(event, dict) and event.get('format') == 'event_post'
        if is_event_post:
            ai_agent = NewsAIAgent()
            dry_run = bool(event.get('dry_run', False))
            return _run_event_pipeline(timestamp, bucket_name, ai_agent, dry_run=dry_run)

        # STAGE 1: Scrape news
        logger.info("\n📰 STAGE 1: Scraping news articles...")
        scraper = DutchNewsScraper()
        articles = scraper.scrape_all_sources(max_articles_per_source=2)
        articles_data = [article.to_dict() for article in articles]
        
        if not articles_data:
            logger.warning("⚠️  No articles found!")
            save_to_s3(
                {'status': 'no_articles', 'message': 'No articles found to process', 'timestamp': timestamp},
                f'pipeline_results_{timestamp}.json', bucket_name,
            )
            send_alert(
                "No articles found",
                f"Scraper returned 0 articles.\nTimestamp: {timestamp}\n"
                f"Possible cause: news sources may be unreachable or changed.",
                "GENERAL",
            )
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
        original_count = len(articles_data)
        
        if published_urls:
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
            save_to_s3(
                {
                    'status': 'no_posts',
                    'message': 'No posts generated from articles',
                    'timestamp': timestamp,
                    'articles_count': len(articles_data),
                },
                f'pipeline_results_{timestamp}.json', bucket_name,
            )
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

        # Quality gate: review posts before publishing (runs for BOTH photo & Reels)
        logger.info("\n🔍 QUALITY GATE: Reviewing posts with Claude Haiku...")
        reviewed = [ai_agent.quality_check(p) for p in posts]
        rejected_count = sum(1 for p in reviewed if p is None)
        posts = [p for p in reviewed if p is not None]

        if rejected_count:
            logger.warning(f"⚠️  {rejected_count} post(s) failed quality gate")
        else:
            logger.info(f"✅ Quality gate passed — {len(posts)} post(s) approved")

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

        # Detect if this is a Reels run (afternoon schedule or manual trigger)
        is_reels = False
        if isinstance(event, dict):
            is_reels = event.get('format') == 'reels' or event.get('mode') == 'reels'

        # STAGE 3: Publish to Instagram
        logger.info(f"\n📱 STAGE 3: Publishing to Instagram {'(REELS)' if is_reels else '(PHOTO)'}...")

        # For Reels, guard against entering video creation with too little Lambda time.
        # Scrape+AI can occasionally run slow; if <7 min remain we cannot safely render.
        if is_reels:
            remaining_ms = context.get_remaining_time_in_millis()
            if remaining_ms < 420_000:
                msg = (
                    f"Insufficient Lambda time for Reels video creation "
                    f"({remaining_ms / 1000:.0f}s remaining, need ≥7 min). "
                    f"Scrape/AI stages may have run unusually slow."
                )
                logger.error(f"⏱️  {msg}")
                send_alert("Reels skipped — Lambda low on time", msg, "PUBLISH_FAILED")
                save_to_s3(
                    {'status': 'timeout_risk', 'message': msg, 'timestamp': timestamp},
                    f'pipeline_results_{timestamp}.json', bucket_name,
                )
                return {
                    'statusCode': 500,
                    'body': json.dumps({'status': 'timeout_risk', 'error': msg, 'timestamp': timestamp}),
                }

        published_count = 0
        for idx, post in enumerate(posts_data, 1):
            try:
                logger.info(f"\n📤 Publishing post {idx}/{len(posts)}...")

                if is_reels:
                    # Log narration preview before the heavy video work
                    from video.tts import clean_for_narration
                    narration_preview = clean_for_narration(post.get('content', ''))
                    logger.info(f"🔊 Narration preview (first 120 chars): {narration_preview[:120]}...")

                    # Generate contextually accurate Pexels search queries
                    logger.info("🎬 Generating footage search queries...")
                    footage_queries = ai_agent.generate_footage_queries(
                        title=post.get('original_title', ''),
                        description=post.get('content', ''),
                    )

                    # Generate video and upload to S3
                    video_path = f'/tmp/reels_{timestamp}.mp4'
                    create_news_video(
                        title=post.get('original_title', ''),
                        content=post.get('content', ''),
                        source=post.get('source', ''),
                        hashtags=post.get('hashtags', []),
                        output_path=video_path,
                        emoji=post.get('emoji', '📰'),
                        image_url=post.get('image_url'),
                        footage_queries=footage_queries,
                        hook=post.get('hook', ''),
                    )

                    s3_video_key = f'reels/reels_{timestamp}.mp4'
                    s3_client = boto3.client('s3')
                    with open(video_path, 'rb') as vf:
                        s3_client.put_object(
                            Bucket=bucket_name,
                            Key=s3_video_key,
                            Body=vf,
                            ContentType='video/mp4',
                        )
                    logger.info(f"✅ Video uploaded to S3: {s3_video_key}")

                    # Hand off Instagram publishing to a dedicated Lambda so this
                    # function is not killed by the 15-minute timeout during Meta's
                    # video processing + polling phase.
                    reels_fn = os.environ.get(
                        'REELS_PUBLISH_FUNCTION_NAME', 'news-ai-agent-reels-publish'
                    )
                    lambda_client = boto3.client('lambda')
                    lambda_client.invoke(
                        FunctionName=reels_fn,
                        InvocationType='Event',  # async — fire and forget
                        Payload=json.dumps({
                            's3_video_key': s3_video_key,
                            'post_content': post['full_post'],
                            'bucket_name': bucket_name,
                            'timestamp': timestamp,
                        }).encode(),
                    )
                    logger.info(f"✅ Reels publish delegated to {reels_fn} (async)")

                    # Publish the same video to YouTube Shorts (async, independent of Instagram).
                    _invoke_youtube_async(
                        lambda_client, s3_video_key,
                        post_content=post.get('full_post', ''),
                        hook=post.get('hook', ''),
                        hashtags=post.get('hashtags', []),
                        bucket_name=bucket_name,
                        timestamp=timestamp,
                    )
                    result = {'status': 'queued', 'publishers': [reels_fn, 'youtube'], 's3_key': s3_video_key}

                else:
                    publisher = InstagramPublisher()
                    result = publisher.publish_post(
                        content=post['full_post'],
                        image_url=post.get('image_url'),
                        dry_run=False,
                    )

                logger.info(f"✅ Post {idx} processed: {result}")
                published_count += 1

            except Exception as e:
                logger.error(f"❌ Failed to process post {idx}: {str(e)}")
                error_type = detect_error_type(e)
                alert_on_exception(
                    f"Post {idx} publish failed",
                    e,
                    error_type,
                )
                # Continue with next post if available
                continue
        
        # Success response
        logger.info("\n" + "="*60)
        logger.info(f"✅ Lambda execution completed successfully!")
        logger.info(f"📊 Published: {published_count}/{len(posts)} posts")
        logger.info("="*60)

        if published_count == 0 and len(posts) > 0:
            send_alert(
                "No posts were published",
                f"{len(posts)} posts were generated but none could be published.\n"
                f"Timestamp: {timestamp}\n"
                f"Possible cause: Instagram token may have expired.",
                "PUBLISH_FAILED",
            )
        
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
        error_type = detect_error_type(e)
        # Write error to S3 first so there is always a trace, then alert via SNS.
        save_to_s3(
            {'status': 'error', 'error': str(e), 'error_type': error_type, 'timestamp': timestamp},
            f'pipeline_results_{timestamp}.json', bucket_name,
        )
        alert_on_exception("Lambda execution failed", e, error_type)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'status': 'error',
                'error': str(e),
                'timestamp': timestamp
            })
        }
