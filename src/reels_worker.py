"""
Lambda handler for the Reels publishing step.

Invoked asynchronously by the main news-ai-agent Lambda once the video
has been rendered and uploaded to S3.  This handler's only job is to
create the Instagram media container, poll until Meta finishes processing
the video, and then publish the Reel.

Expected event payload:
    {
        "s3_video_key":  "reels/reels_20260518_123000.mp4",
        "post_content":  "Instagram caption...",
        "bucket_name":   "news-ai-agent-results-...",
        "timestamp":     "20260518_123000"
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

from social_publisher import InstagramPublisher, FacebookPublisher
from notifier import send_alert, alert_on_exception, detect_error_type

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_PRESIGNED_URL_EXPIRY = 3600  # seconds — must cover Meta's download + processing time


def _load_secrets():
    """Load Instagram credentials from Secrets Manager into env vars."""
    secret_name = os.environ.get('SECRET_NAME', 'news-ai-agent/credentials')
    region = os.environ.get('AWS_REGION', 'eu-central-1')
    client = boto3.session.Session().client('secretsmanager', region_name=region)
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        logger.error(f"❌ Failed to load secrets: {e}")
        raise
    secret = json.loads(resp['SecretString'])
    os.environ['INSTAGRAM_ACCESS_TOKEN'] = secret['INSTAGRAM_ACCESS_TOKEN']
    os.environ['INSTAGRAM_ACCOUNT_ID'] = secret['INSTAGRAM_ACCOUNT_ID']
    if 'ENABLE_INSTAGRAM_STORIES' in secret:
        os.environ['ENABLE_INSTAGRAM_STORIES'] = secret['ENABLE_INSTAGRAM_STORIES']
    if 'FACEBOOK_PAGE_ID' in secret:
        os.environ['FACEBOOK_PAGE_ID'] = secret['FACEBOOK_PAGE_ID']
    logger.info("✅ Secrets loaded")


def lambda_handler(event, context):
    timestamp = event.get('timestamp', 'unknown')
    s3_video_key = event.get('s3_video_key')
    post_content = event.get('post_content')
    bucket_name = event.get('bucket_name') or os.environ.get('RESULTS_BUCKET', '')
    # Which surfaces to publish this video to. Defaults preserve the original
    # behaviour (Reel only); the daily-fact pipeline invokes with
    # publish_reel=False, publish_story=True.
    publish_reel = bool(event.get('publish_reel', True))
    publish_story = bool(event.get('publish_story', False))

    logger.info("=" * 60)
    logger.info("📱 Reels Publisher — Starting")
    logger.info(f"📅 Timestamp: {timestamp}")
    logger.info(f"🎬 Video key: {s3_video_key}")
    logger.info(f"🎯 Targets: reel={publish_reel} story={publish_story}")
    logger.info("=" * 60)

    # post_content (caption) is only needed for the Reel; Stories take no caption.
    missing_content = publish_reel and not post_content
    if not s3_video_key or not bucket_name or missing_content:
        msg = (
            f"Missing required payload fields — "
            f"s3_video_key={'set' if s3_video_key else 'MISSING'}, "
            f"post_content={'set' if post_content else 'MISSING'}, "
            f"bucket={'set' if bucket_name else 'MISSING'}"
        )
        logger.error(f"❌ {msg}")
        send_alert("Reels publish aborted — bad payload", msg, "PUBLISH_FAILED")
        return {'statusCode': 400, 'body': json.dumps({'status': 'error', 'error': msg})}

    try:
        _load_secrets()
        stories_enabled = os.environ.get('ENABLE_INSTAGRAM_STORIES', 'false').lower() == 'true'

        s3 = boto3.client('s3')
        video_url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': bucket_name, 'Key': s3_video_key},
            ExpiresIn=_PRESIGNED_URL_EXPIRY,
        )
        logger.info("✅ Pre-signed URL generated")

        publisher = InstagramPublisher()
        results: dict = {}
        errors: list = []

        # ── Reel ──
        if publish_reel:
            try:
                results['reel'] = publisher.publish_reels(
                    content=post_content, video_url=video_url, dry_run=False,
                )
                logger.info(f"✅ Reel published: {results['reel'].get('url', results['reel'])}")
            except Exception as e:
                logger.error(f"❌ Reel publish failed: {e}", exc_info=True)
                alert_on_exception("Reels publish failed", e, detect_error_type(e))
                errors.append(f"reel: {e}")

        # ── Story ── (independent of the Reel; gated by feature flag)
        if publish_story:
            if not stories_enabled:
                logger.info("⏸️  ENABLE_INSTAGRAM_STORIES disabled — skipping story")
            else:
                try:
                    results['story'] = publisher.publish_story(video_url=video_url, dry_run=False)
                    logger.info(f"✅ Story published: {results['story']}")
                except Exception as e:
                    logger.error(f"❌ Story publish failed: {e}", exc_info=True)
                    alert_on_exception("Story publish failed", e, detect_error_type(e))
                    errors.append(f"story: {e}")

                # Cross-post the same video to the connected Facebook Page Story.
                # Best-effort: a Facebook failure never fails the run (Instagram
                # is the primary surface). Only runs when FACEBOOK_PAGE_ID is set.
                if os.environ.get('FACEBOOK_PAGE_ID'):
                    try:
                        results['facebook_story'] = FacebookPublisher().publish_story(
                            video_url=video_url, dry_run=False,
                        )
                        logger.info(f"✅ Facebook Story published: {results['facebook_story']}")
                    except Exception as e:
                        logger.error(f"❌ Facebook Story publish failed (non-critical): {e}", exc_info=True)
                        alert_on_exception("Facebook Story publish failed", e, detect_error_type(e))

        if errors:
            return {
                'statusCode': 500,
                'body': json.dumps({'status': 'partial_error', 'errors': errors,
                                    'result': results, 'timestamp': timestamp}),
            }
        return {
            'statusCode': 200,
            'body': json.dumps({'status': 'success', 'result': results, 'timestamp': timestamp}),
        }

    except Exception as e:
        logger.error(f"❌ Publish worker failed: {e}", exc_info=True)
        error_type = detect_error_type(e)
        alert_on_exception("Publish worker failed", e, error_type)
        return {
            'statusCode': 500,
            'body': json.dumps({'status': 'error', 'error': str(e), 'timestamp': timestamp}),
        }
