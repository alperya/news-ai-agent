"""
Lambda handler for the YouTube Shorts publishing step.

Invoked asynchronously by the main news-ai-agent Lambda once the video
has been rendered and uploaded to S3. Downloads the video from S3 and
uploads it as a YouTube Short using the Data API v3.

If YouTube credentials (YOUTUBE_CLIENT_ID / SECRET / REFRESH_TOKEN) are
not yet configured in Secrets Manager the handler exits cleanly with
status 'skipped' — no alert is sent. Add the credentials to enable publishing.

Expected event payload:
    {
        "s3_video_key":  "reels/reels_20260614_090000.mp4",
        "post_content":  "Full caption / description text...",
        "hook":          "Dit is wat er vandaag in Nederland gebeurde",
        "hashtags":      ["#Nederland", "#Nieuws"],
        "timestamp":     "20260614_090000"
    }
"""

import json
import logging
import os
import sys

import boto3
from botocore.exceptions import ClientError

_dir = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_dir, 'src'))
sys.path.insert(0, _dir)

from youtube_publisher import YouTubePublisher
from notifier import send_alert, alert_on_exception, detect_error_type

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PRESIGNED_URL_EXPIRY = 3600
_STATIC_TAGS = ['nieuws', 'nederland', 'shorts', 'nl nieuws', 'hollands nieuws']

_YT_CRED_KEYS = ('YOUTUBE_CLIENT_ID', 'YOUTUBE_CLIENT_SECRET', 'YOUTUBE_REFRESH_TOKEN')


def _load_secrets() -> bool:
    """Load credentials from Secrets Manager into env vars.

    Returns True if YouTube credentials were found, False otherwise.
    """
    secret_name = os.environ.get('SECRET_NAME', 'news-ai-agent/credentials')
    region = os.environ.get('AWS_REGION', 'eu-central-1')
    client = boto3.session.Session().client('secretsmanager', region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        logger.error(f"❌ Failed to load secrets: {e}")
        raise
    secret = json.loads(resp['SecretString'])
    for key in _YT_CRED_KEYS:
        if key in secret:
            os.environ[key] = secret[key]
    found = all(k in secret for k in _YT_CRED_KEYS)
    if found:
        logger.info("✅ YouTube credentials loaded")
    else:
        logger.warning("⚠️  YouTube credentials not yet in Secrets Manager — skipping upload")
    return found


def _build_title(hook: str, timestamp: str) -> str:
    base = hook.strip() or f"NL Nieuws {timestamp}"
    title = f"{base} #Shorts"
    return title[:100]


def _build_description(post_content: str, hashtags: list) -> str:
    hashtag_str = ' '.join(h if h.startswith('#') else f'#{h}' for h in hashtags)
    parts = [p for p in [post_content, hashtag_str] if p]
    return '\n\n'.join(parts)


def _build_tags(hashtags: list) -> list:
    tags = [h.lstrip('#') for h in hashtags if h]
    return list(dict.fromkeys(tags + _STATIC_TAGS))


def lambda_handler(event, context):
    timestamp = event.get('timestamp', 'unknown')
    s3_video_key = event.get('s3_video_key')
    post_content = event.get('post_content', '')
    hook = event.get('hook', '')
    hashtags = event.get('hashtags', [])
    # Always use the env var injected by Lambda — never trust event payload for bucket writes
    bucket_name = os.environ.get('RESULTS_BUCKET', '')

    logger.info("=" * 60)
    logger.info("▶️  YouTube Worker — Starting")
    logger.info(f"📅 Timestamp: {timestamp}")
    logger.info(f"🎬 Video key: {s3_video_key}")
    logger.info("=" * 60)

    if not s3_video_key or not bucket_name:
        msg = (
            f"Missing required payload fields — "
            f"s3_video_key={'set' if s3_video_key else 'MISSING'}, "
            f"bucket={'set' if bucket_name else 'MISSING'}"
        )
        logger.error(f"❌ {msg}")
        send_alert("YouTube publish aborted — bad payload", msg, "PUBLISH_FAILED")
        return {'statusCode': 400, 'body': json.dumps({'status': 'error', 'error': msg})}

    try:
        has_credentials = _load_secrets()
        if not has_credentials:
            return {
                'statusCode': 200,
                'body': json.dumps({'status': 'skipped', 'reason': 'credentials_not_configured',
                                    'timestamp': timestamp}),
            }

        s3 = boto3.client('s3')
        video_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': s3_video_key},
            ExpiresIn=_PRESIGNED_URL_EXPIRY,
        )
        logger.info("✅ Pre-signed URL generated")

        title = _build_title(hook, timestamp)
        description = _build_description(post_content, hashtags)
        tags = _build_tags(hashtags)

        logger.info(f"📝 Title: {title}")
        logger.info(f"🏷️  Tags: {tags[:8]}")

        publisher = YouTubePublisher()
        result = publisher.upload_short(
            video_url=video_url,
            title=title,
            description=description,
            tags=tags,
        )

        s3.put_object(
            Bucket=bucket_name,
            Key=f"youtube_results/{timestamp}.json",
            Body=json.dumps(
                {'status': 'success', 'result': result, 'timestamp': timestamp},
                ensure_ascii=False,
            ).encode(),
            ContentType='application/json',
        )

        logger.info(f"✅ YouTube Short published: {result.get('url')}")
        return {
            'statusCode': 200,
            'body': json.dumps({'status': 'success', 'result': result, 'timestamp': timestamp}),
        }

    except Exception as e:
        logger.error(f"❌ YouTube publish failed: {e}", exc_info=True)
        error_type = detect_error_type(e)
        alert_on_exception("YouTube publish failed", e, error_type)
        return {
            'statusCode': 500,
            'body': json.dumps({'status': 'error', 'error': str(e), 'timestamp': timestamp}),
        }
