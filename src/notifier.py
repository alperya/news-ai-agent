"""Error notification via AWS SNS.

Sends email alerts when the Lambda encounters critical errors
such as OOM, token expiration, or publish failures.
"""

import logging
import os
import traceback
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

CLOUDWATCH_URL = (
    "https://eu-central-1.console.aws.amazon.com/cloudwatch/home"
    "?region=eu-central-1#logsV2:log-groups/log-group/"
    "$252Faws$252Flambda$252Fnews-ai-agent"
)

_EMOJI = {
    "OOM": "💥",
    "TOKEN_EXPIRED": "🔑",
    "PUBLISH_FAILED": "📤",
    "VIDEO_ERROR": "🎬",
    "GENERAL": "🚨",
}


def send_alert(
    subject: str,
    message: str,
    error_type: str = "GENERAL",
) -> bool:
    """Send an alert email via SNS.

    Args:
        subject: Short description (max ~90 chars).
        message: Detailed error body.
        error_type: OOM | TOKEN_EXPIRED | PUBLISH_FAILED | VIDEO_ERROR | GENERAL

    Returns:
        True if the alert was published successfully.
    """
    topic_arn = os.environ.get("SNS_ALERT_TOPIC_ARN")
    if not topic_arn:
        logger.warning("⚠️  SNS_ALERT_TOPIC_ARN not set — alert skipped")
        return False

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    emoji = _EMOJI.get(error_type, "🚨")

    body = (
        f"{emoji} News AI Agent — Error Alert\n"
        f"{'=' * 50}\n\n"
        f"⏰ Time:  {timestamp}\n"
        f"🏷️ Type:  {error_type}\n\n"
        f"📝 {subject}\n\n"
        f"{'─' * 50}\n"
        f"{message}\n"
        f"{'─' * 50}\n\n"
        f"📊 CloudWatch Logs:\n{CLOUDWATCH_URL}\n"
    )

    try:
        sns = boto3.client(
            "sns",
            region_name=os.environ.get("AWS_REGION", "eu-central-1"),
        )
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"[News AI Agent] {emoji} {subject}"[:100],
            Message=body,
        )
        logger.info(f"📧 Alert sent: {subject}")
        return True
    except ClientError as exc:
        logger.error(f"❌ Failed to send alert: {exc}")
        return False


def alert_on_exception(
    subject: str,
    exc: Exception,
    error_type: str = "GENERAL",
) -> bool:
    """Convenience wrapper: format an exception and send alert."""
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    message = (
        f"Exception: {type(exc).__name__}: {exc}\n\n"
        f"Traceback:\n{''.join(tb[-5:])}"
    )
    return send_alert(subject, message, error_type)


def detect_error_type(error: Exception) -> str:
    """Classify an error for alerting purposes."""
    msg = str(error).lower()

    if any(k in msg for k in ("memory", "oom", "cannot allocate", "enomem")):
        return "OOM"

    if any(k in msg for k in (
        "oauthexception", "token has expired", "token is invalid",
        "error validating access token", "session has expired",
        "code 190",
    )):
        return "TOKEN_EXPIRED"

    if any(k in msg for k in ("publish", "reels", "instagram", "media container")):
        return "PUBLISH_FAILED"

    if any(k in msg for k in ("video", "ffmpeg", "render", "moviepy")):
        return "VIDEO_ERROR"

    return "GENERAL"
