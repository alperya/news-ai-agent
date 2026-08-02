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
from publishing import build_crossposter, REEL, PHOTO
from video import create_news_video
from event_scraper import EventScraper
from video.event_card import generate_carousel_slides, generate_reels_video
from notifier import send_alert, alert_on_exception, detect_error_type, send_event_summary
from viral_guard import should_skip_next_post

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


def handler_selection_review(event, context):
    """Weekly editorial selection review — EventBridge Sunday 17:00 UTC (19:00 Amsterdam).

    Summarises the week's article picks (chosen + top-7 runner-ups + engagement)
    and emails an AI review (growth/content/platform/commercial lenses) so the
    operator can tune the selection prompt, dedup rule, or schedule.
    """
    logger.info("="*60)
    logger.info("📋 Selection Reviewer — Starting")
    logger.info("="*60)
    try:
        get_secrets()
        from selection_reviewer import SelectionReviewer
        dry_run = isinstance(event, dict) and bool(event.get("dry_run", False))
        result = SelectionReviewer().run(dry_run=dry_run)
        logger.info(f"✅ Selection review complete: {result.get('status')}")
        return {"statusCode": 200, "body": json.dumps(result, default=str)}
    except Exception as e:
        logger.error(f"❌ Selection reviewer failed: {e}", exc_info=True)
        send_alert("Selection reviewer failed", str(e), "GENERAL")
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

    # Feature flag: daily Dutch-fact Instagram Story (default off)
    if 'ENABLE_INSTAGRAM_STORIES' in secret:
        os.environ['ENABLE_INSTAGRAM_STORIES'] = secret['ENABLE_INSTAGRAM_STORIES']
    # Feature flag: weekly NL events post (default off — deprecated for low engagement)
    if 'ENABLE_EVENT_POSTS' in secret:
        os.environ['ENABLE_EVENT_POSTS'] = secret['ENABLE_EVENT_POSTS']
    # Feature flag: weekly Dutch-fact carousel feed post (default off)
    if 'ENABLE_FACT_CAROUSEL' in secret:
        os.environ['ENABLE_FACT_CAROUSEL'] = secret['ENABLE_FACT_CAROUSEL']
    # Feature flag + thresholds: skip one news slot while the previous post is viral
    # (flag default on). Thresholds are read at call time, so editing the secret
    # retunes the guard without a redeploy.
    for key in ('ENABLE_VIRAL_SKIP', 'VIRAL_SKIP_MULTIPLIER', 'VIRAL_MIN_ENGAGEMENT',
                'VIRAL_WINDOW_HOURS', 'MIN_BASELINE_POSTS'):
        if key in secret:
            os.environ[key] = secret[key]
    # Publishing drought watchdog threshold (hours) — same call-time read as above
    if 'PUBLISH_DROUGHT_HOURS' in secret:
        os.environ['PUBLISH_DROUGHT_HOURS'] = secret['PUBLISH_DROUGHT_HOURS']
    # Connected Facebook Page — when set, the Story is cross-posted to FB
    if 'FACEBOOK_PAGE_ID' in secret:
        os.environ['FACEBOOK_PAGE_ID'] = secret['FACEBOOK_PAGE_ID']
    # LinkedIn Company Page — news Reels cross-posting (feature-flagged, default off)
    for key in ('ENABLE_LINKEDIN', 'LINKEDIN_ACCESS_TOKEN', 'LINKEDIN_ORG_ID'):
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
    if 'AI_PROMPT_CAROUSEL_CAPTION' in secret:
        os.environ['AI_PROMPT_CAROUSEL_CAPTION'] = secret['AI_PROMPT_CAROUSEL_CAPTION']
    if 'AI_PROMPT_FOOTAGE_QUERIES' in secret:
        os.environ['AI_PROMPT_FOOTAGE_QUERIES'] = secret['AI_PROMPT_FOOTAGE_QUERIES']

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


# Days within which a footage asset (Pexels clip or news cover photo) is
# considered "recently used" and avoided as a Reel cover. Tunable via env.
FOOTAGE_REUSE_WINDOW_DAYS = int(os.environ.get('FOOTAGE_REUSE_WINDOW_DAYS', '30'))


def get_published_urls(bucket_name):
    """
    Get previously published article URLs from S3, plus recency-windowed dedup data.

    A single S3 scan of all ``posts_*.json`` files yields, per file, its
    timestamp (from the filename) used to apply two windows: a 3-day window for
    semantic title dedup and a FOOTAGE_REUSE_WINDOW_DAYS window for footage dedup.

    Returns:
        tuple[set, list[str], dict]: (published_urls, recent_titles, footage)
            published_urls — all-time URL set for exact-duplicate filtering
            recent_titles  — original_title values from the past 72 h for semantic dedup
            footage        — {'pexels_ids': set[int], 'image_urls': set[str],
                              'cover_hashes': list[int]} used within the reuse window
    """
    published_urls = set()
    recent_titles = []
    recent_pexels_ids = set()
    recent_image_urls = set()
    recent_cover_hashes = []
    s3_client = boto3.client('s3')
    title_cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    footage_cutoff = datetime.now(timezone.utc) - timedelta(days=FOOTAGE_REUSE_WINDOW_DAYS)

    footage = {
        'pexels_ids': recent_pexels_ids,
        'image_urls': recent_image_urls,
        'cover_hashes': recent_cover_hashes,
    }

    try:
        # Get all posts_*.json files from S3
        list_response = s3_client.list_objects_v2(
            Bucket=bucket_name,
            Prefix='posts_'
        )

        if 'Contents' not in list_response:
            logger.info("📋 No previous posts found in S3")
            return published_urls, recent_titles, footage

        logger.info(f"🔍 Checking {len(list_response['Contents'])} posts files for duplicates...")

        for obj in list_response['Contents']:
            key = obj['Key']
            if not key.startswith('posts_') or not key.endswith('.json'):
                continue

            # Parse timestamp from filename "posts_YYYYMMDD_HHMMSS.json" to detect recency
            is_recent = False
            within_footage_window = False
            try:
                ts_str = key[len('posts_'):-len('.json')]  # e.g. "20260619_143200"
                file_dt = datetime.strptime(ts_str, '%Y%m%d_%H%M%S').replace(tzinfo=timezone.utc)
                is_recent = file_dt >= title_cutoff
                within_footage_window = file_dt >= footage_cutoff
            except ValueError:
                pass

            try:
                # Download and parse each posts file
                obj_response = s3_client.get_object(Bucket=bucket_name, Key=key)
                posts_data = json.loads(obj_response['Body'].read().decode('utf-8'))

                # Extract URLs (all-time), titles (3 days), footage (reuse window)
                if isinstance(posts_data, list):
                    for post in posts_data:
                        if isinstance(post, dict):
                            url = post.get('original_url') or post.get('url')
                            if url:
                                published_urls.add(url)
                            if is_recent:
                                title = post.get('original_title')
                                if title:
                                    recent_titles.append(title)
                            if within_footage_window:
                                for pid in post.get('pexels_media_ids') or []:
                                    recent_pexels_ids.add(pid)
                                cover_url = post.get('cover_image_url') or post.get('image_url')
                                if cover_url:
                                    recent_image_urls.add(cover_url)
                                chash = post.get('cover_image_hash')
                                if chash is not None:
                                    recent_cover_hashes.append(chash)

            except Exception as e:
                logger.debug(f"Error reading {key}: {str(e)}")
                continue

        logger.info(
            f"📋 Found {len(published_urls)} published articles "
            f"({len(recent_titles)} in last 3 days; "
            f"{len(recent_pexels_ids)} Pexels ids / {len(recent_cover_hashes)} covers "
            f"in last {FOOTAGE_REUSE_WINDOW_DAYS} days)"
        )
        return published_urls, recent_titles, footage

    except Exception as e:
        logger.error(f"❌ Error getting published URLs: {str(e)}")
        return published_urls, recent_titles, footage


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


def _run_daily_fact_pipeline(timestamp: str, bucket_name: str, dry_run: bool = False) -> dict:
    """Daily "Did you know?" Dutch-fact Story — runs at 07:00 (format: daily_fact).

    Story-only (no Reel, no YouTube). Gated by ENABLE_INSTAGRAM_STORIES — when
    disabled, returns immediately WITHOUT generating any content (no fact
    selection, no Pexels calls, no render), so nothing is wasted.
    """
    logger.info("\n" + "=" * 60)
    logger.info("💡 DAILY FACT PIPELINE: Dutch-fact Story")
    logger.info("=" * 60)

    if os.environ.get('ENABLE_INSTAGRAM_STORIES', 'false').lower() != 'true':
        logger.info("⏸️  ENABLE_INSTAGRAM_STORIES disabled — skipping (no content generated)")
        return {'statusCode': 200,
                'body': json.dumps({'status': 'skipped', 'reason': 'flag_disabled', 'timestamp': timestamp})}

    try:
        from dutch_facts import get_fact_for_today
        from video import create_fact_video
        from video.config import FACT_STORY_MUSIC

        # Pick today's fact (least-recently-used rotation, state in S3)
        fact = get_fact_for_today(bucket_name)

        # Render short vertical video (text + Dutch B-roll + music, no narration)
        video_path = f'/tmp/fact_{timestamp}.mp4'
        create_fact_video(
            fact_text=fact['text'],
            footage_queries=fact.get('footage_queries'),
            music_path=str(FACT_STORY_MUSIC),
            output_path=video_path,
        )

        # Upload to S3
        s3_video_key = f'facts/fact_{timestamp}.mp4'
        s3_client = boto3.client('s3')
        with open(video_path, 'rb') as vf:
            s3_client.put_object(
                Bucket=bucket_name, Key=s3_video_key, Body=vf, ContentType='video/mp4',
            )
        logger.info(f"✅ Fact video uploaded to S3: {s3_video_key}")

        if dry_run:
            logger.info("[DRY RUN] Skipping Story publish; video kept in S3 for inspection")
            return {'statusCode': 200,
                    'body': json.dumps({'status': 'dry_run', 'fact_id': fact['id'],
                                        's3_key': s3_video_key, 'timestamp': timestamp})}

        # Hand off Story publishing to the async worker (video processing +
        # container polling happen there, off the main Lambda's clock).
        reels_fn = os.environ.get('REELS_PUBLISH_FUNCTION_NAME', 'news-ai-agent-reels-publish')
        lambda_client = boto3.client('lambda')
        lambda_client.invoke(
            FunctionName=reels_fn,
            InvocationType='Event',  # async — fire and forget
            Payload=json.dumps({
                'publish_reel': False,
                'publish_story': True,
                's3_video_key': s3_video_key,
                'bucket_name': bucket_name,
                'timestamp': timestamp,
            }).encode(),
        )
        logger.info(f"✅ Story publish delegated to {reels_fn} (async)")

        return {'statusCode': 200,
                'body': json.dumps({'status': 'queued', 'fact_id': fact['id'],
                                    's3_key': s3_video_key, 'timestamp': timestamp})}

    except Exception as e:
        logger.error(f"❌ Daily fact pipeline failed: {e}", exc_info=True)
        error_type = detect_error_type(e)
        alert_on_exception("Daily fact story failed", e, error_type)
        return {'statusCode': 500,
                'body': json.dumps({'status': 'error', 'error': str(e), 'timestamp': timestamp})}


def _run_fact_carousel_pipeline(timestamp: str, bucket_name: str, dry_run: bool = False) -> dict:
    """Weekly Dutch-fact carousel — runs Sunday 12:00 Amsterdam (format: fact_carousel).

    Collects the facts shown in Stories over the last 7 days and publishes them
    as one Instagram carousel (album) feed post — a discovery + save surface the
    Story cannot reach (non-followers see the feed/Explore). Gated by
    ENABLE_FACT_CAROUSEL — when off, returns immediately without generating
    anything. Instagram-only (no cross-post, no YouTube).
    """
    logger.info("\n" + "=" * 60)
    logger.info("🗂️  FACT CAROUSEL PIPELINE: Weekly Dutch-fact feed post")
    logger.info("=" * 60)

    if os.environ.get('ENABLE_FACT_CAROUSEL', 'true').lower() != 'true':
        logger.info("⏸️  ENABLE_FACT_CAROUSEL disabled — skipping (no content generated)")
        return {'statusCode': 200,
                'body': json.dumps({'status': 'skipped', 'reason': 'flag_disabled', 'timestamp': timestamp})}

    try:
        from dutch_facts import get_weekly_facts, MIN_CAROUSEL_FACTS
        from video import render_fact_carousel
        from video.footage import fetch_pexels_photo
        from social_publisher import InstagramPublisher

        facts = get_weekly_facts(bucket_name)
        if len(facts) < MIN_CAROUSEL_FACTS:
            logger.info(f"⏭️  Only {len(facts)} facts in the last 7 days "
                        f"(need ≥{MIN_CAROUSEL_FACTS}) — skipping this week's carousel")
            return {'statusCode': 200,
                    'body': json.dumps({'status': 'skipped', 'reason': 'not_enough_facts',
                                        'count': len(facts), 'timestamp': timestamp})}

        out_dir = f"/tmp/carousel_{timestamp}"
        os.makedirs(out_dir, exist_ok=True)

        # Fetch a relevant Pexels photo per fact (same footage_queries the Story
        # video uses) to use as the card background. None → solid-colour fallback.
        bg_paths = []
        for i, fact in enumerate(facts):
            path = None
            for query in (fact.get('footage_queries') or []):
                path = fetch_pexels_photo(query, os.path.join(out_dir, f"bg_{i:02d}.jpg"))
                if path:
                    break
            bg_paths.append(path)

        # Special closing image for the follow slide + brand logo (bundled or env).
        from video.config import brand_logo_path
        cta_bg = fetch_pexels_photo(
            "amsterdam canal houses evening", os.path.join(out_dir, "cta_bg.jpg"))
        logo_path = brand_logo_path()

        # Render 4:5 cards → local PNGs
        card_paths = render_fact_carousel(
            facts, out_dir, bg_paths=bg_paths, cta_bg_path=cta_bg, logo_path=logo_path)

        # Upload each card to S3 and presign for Instagram to fetch
        s3_client = boto3.client('s3')
        prefix = f"facts/carousel/{'test' if dry_run else 'post'}_{timestamp}"
        image_urls = []
        for i, path in enumerate(card_paths):
            key = f"{prefix}/card_{i + 1:02d}.png"
            with open(path, 'rb') as fh:
                s3_client.put_object(Bucket=bucket_name, Key=key, Body=fh, ContentType='image/png')
            image_urls.append(s3_client.generate_presigned_url(
                'get_object', Params={'Bucket': bucket_name, 'Key': key}, ExpiresIn=3600,
            ))
        logger.info(f"✅ Uploaded {len(image_urls)} carousel cards → s3://{bucket_name}/{prefix}/")

        # Caption (AI with template fallback)
        caption = NewsAIAgent().generate_carousel_caption([f['text'] for f in facts])

        if dry_run:
            logger.info("[DRY RUN] Skipping carousel publish; cards kept in S3 for inspection")
            return {'statusCode': 200,
                    'body': json.dumps({'status': 'dry_run', 'slides': len(image_urls),
                                        's3_prefix': prefix, 'timestamp': timestamp})}

        result = InstagramPublisher().publish_carousel(image_urls, caption=caption, dry_run=False)
        logger.info(f"✅ Carousel published: {result.get('url')}")
        return {'statusCode': 200,
                'body': json.dumps({'status': 'success', 'slides': len(image_urls),
                                    'media_id': result.get('id'), 'timestamp': timestamp})}

    except Exception as e:
        logger.error(f"❌ Fact carousel pipeline failed: {e}", exc_info=True)
        error_type = detect_error_type(e)
        alert_on_exception("Weekly fact carousel failed", e, error_type)
        return {'statusCode': 500,
                'body': json.dumps({'status': 'error', 'error': str(e), 'timestamp': timestamp})}


def _run_event_pipeline(timestamp: str, bucket_name: str, ai_agent, dry_run: bool = False) -> dict:
    """Weekly events pipeline — runs on Wednesday 18:00 (format: event_post).

    Collects a full run summary, writes detailed logs to S3, and emails
    a digest to the alert address regardless of success or failure.
    """
    logger.info("\n" + "="*60)
    logger.info("📅 EVENT PIPELINE: Weekly NL Events Post")
    logger.info("="*60)

    # Feature flag (default off) — events are deprecated for low engagement. When
    # disabled, return immediately WITHOUT scraping, scoring, or generating
    # anything, so the weekly cron firing costs nothing.
    if os.environ.get('ENABLE_EVENT_POSTS', 'false').lower() != 'true':
        logger.info("⏸️  ENABLE_EVENT_POSTS disabled — skipping (no content generated)")
        return {'statusCode': 200,
                'body': json.dumps({'status': 'skipped', 'reason': 'flag_disabled', 'timestamp': timestamp})}

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

        local_slides, slide_durations = generate_carousel_slides(
            events=selected_events,
            date_range=date_range,
            tmp_prefix=f"/tmp/event_{timestamp}",
        )
        logger.info(f"✅ {len(local_slides)} slides generated ({sum(slide_durations):.0f}s read-once)")

        reel_path = f"/tmp/event_{timestamp}_reel.mp4"
        generate_reels_video(local_slides, reel_path, slide_durations=slide_durations)
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

        # ── Stage 5: Publish as Reels (Instagram primary + Facebook best-effort;
        #    LinkedIn excluded — it takes news Reels only) ─
        logger.info(f"\n📱 STAGE 5: Publishing REELS...")
        outcome = build_crossposter(content_source="event").publish(
            REEL, media_url=video_url, caption=caption, dry_run=dry_run,
        )
        if outcome['primary_error']:
            raise outcome['primary_error']
        logger.info(f"✅ Reels published: {outcome['results']}")
        summary["publish_result"] = str(outcome['results'])

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

        # Post-deploy smoke test (CI invokes this right after terraform apply). Reaching
        # this line already proves every module imported and the secret is readable —
        # the failure class a bad commit produces most often. Deliberately a dedicated
        # payload rather than a flag-disabled pipeline: a flag flip must never turn the
        # smoke test into a real publish.
        if isinstance(event, dict) and event.get('health_check'):
            logger.info("🩺 Health check — imports and secrets OK")
            return {'statusCode': 200,
                    'body': json.dumps({'status': 'healthy', 'timestamp': timestamp})}

        # Detect run mode early — event_post bypasses the news pipeline entirely
        is_event_post = isinstance(event, dict) and event.get('format') == 'event_post'
        if is_event_post:
            # Feature flag (default off) — deprecated for low engagement. Short-circuit
            # before building the AI agent so a flag-off cron firing costs nothing.
            if os.environ.get('ENABLE_EVENT_POSTS', 'false').lower() != 'true':
                logger.info("⏸️  ENABLE_EVENT_POSTS disabled — skipping event pipeline")
                return {'statusCode': 200,
                        'body': json.dumps({'status': 'skipped', 'reason': 'flag_disabled', 'timestamp': timestamp})}
            ai_agent = NewsAIAgent()
            dry_run = bool(event.get('dry_run', False))
            return _run_event_pipeline(timestamp, bucket_name, ai_agent, dry_run=dry_run)

        # Daily Dutch-fact Story run — bypasses the news pipeline entirely
        is_daily_fact = isinstance(event, dict) and event.get('format') == 'daily_fact'
        if is_daily_fact:
            dry_run = bool(event.get('dry_run', False))
            return _run_daily_fact_pipeline(timestamp, bucket_name, dry_run=dry_run)

        # Weekly Dutch-fact carousel feed post — bypasses the news pipeline entirely
        is_fact_carousel = isinstance(event, dict) and event.get('format') == 'fact_carousel'
        if is_fact_carousel:
            dry_run = bool(event.get('dry_run', False))
            return _run_fact_carousel_pipeline(timestamp, bucket_name, dry_run=dry_run)

        # Viral-skip guard — if the previous post is still going viral, give it the
        # slot instead of competing with it. Checked here so a skip costs nothing:
        # no scrape, no Claude call, no render. Fail-open by construction.
        skip_run, viral_info = should_skip_next_post(bucket_name)
        if skip_run:
            logger.info(
                f"⏸️  Previous post is viral ({viral_info['engagement']} engagements, "
                f"{viral_info['multiplier']}× median) — skipping this news slot"
            )
            logger.info(json.dumps({"event": "viral_skip", **viral_info}))
            send_alert(
                "News slot skipped — previous post is viral",
                f"Post {viral_info['media_id']} has {viral_info['engagement']} engagements "
                f"({viral_info['multiplier']}× the {viral_info['median_engagement']} median) "
                f"after {viral_info['age_hours']}h.\n\n"
                f"This slot was skipped so the viral post keeps the audience's attention. "
                f"Only one slot is ever skipped per viral post — the next run publishes normally.",
                "GENERAL",
            )
            return {'statusCode': 200,
                    'body': json.dumps({'status': 'skipped', **viral_info, 'timestamp': timestamp})}

        # STAGE 1: Scrape news
        logger.info("\n📰 STAGE 1: Scraping news articles...")
        scraper = DutchNewsScraper()
        articles = scraper.scrape_all_sources(max_articles_per_source=3)
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
        published_urls, recent_titles, recent_footage = get_published_urls(bucket_name)
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

            send_alert(
                "Slot missed — every scraped article was already published",
                f"All {original_count} scraped articles were filtered out as duplicates, "
                f"so this slot published nothing.\nTimestamp: {timestamp}\n\n"
                f"Occasional occurrences are normal on a quiet news day. Repeated ones point "
                f"at the scraper returning a stale feed or the dedup scan over-matching.",
                "GENERAL",
            )

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
            platform='instagram',
            recently_published=recent_titles,
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
            send_alert(
                "Slot missed — AI generated no posts",
                f"{len(articles_data)} article(s) were available but the AI returned no post, "
                f"so this slot published nothing.\nTimestamp: {timestamp}\n\n"
                f"Check the batch-selection prompt, the Anthropic API status, and whether the "
                f"model refused (a refusal returns empty text, not an error).",
                "GENERAL",
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
            send_alert(
                "Slot missed — quality gate rejected every post",
                f"All {rejected_count} generated post(s) failed the quality gate, so this slot "
                f"published nothing.\nTimestamp: {timestamp}\n\n"
                f"Repeated occurrences usually mean the quality-check prompt became too strict "
                f"or the content prompt regressed — compare both in Secrets Manager.",
                "GENERAL",
            )
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
                    footage_queries, avoid_terms, footage_plan = ai_agent.generate_footage_queries(
                        title=post.get('original_title', ''),
                        description=post.get('content', ''),
                    )

                    # Generate video and upload to S3.
                    # Capture which footage was used so future runs avoid reusing
                    # the same cover (Pexels clip or news photo) within the window.
                    used_media_ids = []
                    cover_meta = {}
                    footage_audit = []
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
                        avoid_terms=avoid_terms,
                        ai_agent=ai_agent,
                        exclude_media_ids=recent_footage['pexels_ids'],
                        recent_image_urls=recent_footage['image_urls'],
                        recent_cover_hashes=recent_footage['cover_hashes'],
                        used_media_ids=used_media_ids,
                        cover_meta_out=cover_meta,
                        footage_plan=footage_plan,
                        footage_audit_out=footage_audit,
                    )
                    # Persist footage fingerprints onto the post record (saved to
                    # posts_*.json below) for the next run's dedup scan.
                    post['pexels_media_ids'] = used_media_ids
                    post.update(cover_meta)
                    # Persist the geographic decision + per-candidate trail. Without
                    # this the footage choices are invisible after publish, which is
                    # why the wrong-city problem went unmeasured for months.
                    post['footage_plan'] = footage_plan
                    post['footage_audit'] = footage_audit

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
                    # Instagram primary (+ Facebook best-effort) photo post
                    outcome = build_crossposter().publish(
                        PHOTO, media_url=post.get('image_url'), caption=post['full_post'],
                    )
                    if outcome['primary_error']:
                        raise outcome['primary_error']
                    result = outcome['results']

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
        # Re-raise so AWS records a failed invocation. Returning a 500 dict here looked
        # like a success to Lambda, which zeroed the Errors metric, disarmed the
        # news-ai-agent-lambda-errors alarm and stopped the on-failure destination from
        # firing — leaving the in-code SNS email above as the only notification path for
        # every runtime error. Raising restores those three AWS-side signals as an
        # independent channel that does not depend on our own alerting still working.
        # Safe here because this function runs with maximum_retry_attempts = 0, so there
        # is no retry amplification and no double-publish risk. The async workers keep
        # returning 500 instead: they retry twice, and a failure after Instagram accepted
        # the container would republish.
        raise
