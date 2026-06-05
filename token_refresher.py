"""
Instagram Token Auto-Refresher — Lambda handler.

Refreshes the Instagram long-lived access token (60-day TTL) via the
Meta Graph API and writes the new token back to AWS Secrets Manager.
Triggered by EventBridge every 30 days so the token never expires.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import urllib.request
import urllib.parse

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

_SECRET_NAME = os.environ.get("SECRET_NAME", "news-ai-agent/credentials")
_REGION = os.environ.get("AWS_REGION", "eu-central-1")
_SNS_TOPIC_ARN = os.environ.get("SNS_ALERT_TOPIC_ARN", "")

_REFRESH_URL = "https://graph.facebook.com/oauth/access_token"


def lambda_handler(event, context):
    logger.info("🔄 Starting Instagram token refresh...")

    try:
        secret = _get_secret()
        current_token = secret.get("INSTAGRAM_ACCESS_TOKEN", "")
        app_id = secret.get("META_APP_ID", "")
        app_secret = secret.get("META_APP_SECRET", "")

        if not all([current_token, app_id, app_secret]):
            raise ValueError(
                "Missing required secrets: INSTAGRAM_ACCESS_TOKEN, META_APP_ID, or META_APP_SECRET"
            )

        new_token, expires_in = _refresh_token(current_token, app_id, app_secret)

        secret["INSTAGRAM_ACCESS_TOKEN"] = new_token
        _put_secret(secret)

        expiry_days = expires_in // 86400
        logger.info(f"✅ Token refreshed — expires in {expiry_days} days")

        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": f"Token refreshed, expires in {expiry_days} days",
                "refreshed_at": datetime.now(timezone.utc).isoformat(),
            }),
        }

    except Exception as exc:
        logger.error(f"❌ Token refresh failed: {exc}")
        _alert(str(exc))
        raise


def _get_secret() -> dict:
    client = boto3.client("secretsmanager", region_name=_REGION)
    resp = client.get_secret_value(SecretId=_SECRET_NAME)
    return json.loads(resp["SecretString"])


def _put_secret(payload: dict) -> None:
    client = boto3.client("secretsmanager", region_name=_REGION)
    client.put_secret_value(
        SecretId=_SECRET_NAME,
        SecretString=json.dumps(payload, ensure_ascii=False),
    )
    logger.info(f"✅ Secrets Manager updated ({_SECRET_NAME})")


def _refresh_token(current_token: str, app_id: str, app_secret: str) -> tuple[str, int]:
    """Exchange current token for a fresh 60-day token via Meta Graph API."""
    params = urllib.parse.urlencode({
        "grant_type": "fb_exchange_token",
        "client_id": app_id,
        "client_secret": app_secret,
        "fb_exchange_token": current_token,
    })
    url = f"{_REFRESH_URL}?{params}"

    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read())

    if "error" in data:
        raise RuntimeError(f"Meta API error: {data['error']}")

    new_token = data["access_token"]
    expires_in = data.get("expires_in", 5183944)  # default ~60 days in seconds
    return new_token, expires_in


def _alert(message: str) -> None:
    if not _SNS_TOPIC_ARN:
        return
    try:
        sns = boto3.client("sns", region_name=_REGION)
        sns.publish(
            TopicArn=_SNS_TOPIC_ARN,
            Subject="❌ Instagram token refresh FAILED",
            Message=(
                f"Automatic Instagram token refresh failed.\n\n"
                f"Error: {message}\n\n"
                f"Action required: manually obtain a new long-lived token and run:\n"
                f"  python scripts/update_secrets.py"
            ),
        )
    except Exception as e:
        logger.warning(f"Failed to send SNS alert: {e}")
