"""Viral-skip guard for the news pipeline.

When the most recent Instagram post is still going viral, publishing the next
scheduled news post splits the follower engagement that fuels the algorithm's
distribution ramp and buries the viral post on the profile grid exactly when
profile-visit→follow conversion peaks. This guard skips a single news slot —
never more — while the viral post rides out its window.

No extra schedule is involved: the check runs inline at the start of each news
run, before anything is scraped or generated.

Decision (evaluated live at the start of each news run):
  skip iff the latest post is younger than VIRAL_WINDOW_HOURS
       and its engagement (likes+comments+shares+saves)
           >= VIRAL_SKIP_MULTIPLIER × median engagement of the last 30 days
       and >= VIRAL_MIN_ENGAGEMENT (absolute floor)
       and this media id has not already caused a skip (one skip per event).

The check must call the Graph API live: the metrics collector runs only once a
day at 00:00 UTC, so DynamoDB never contains same-day engagement. DynamoDB is
used only for the baseline (mature posts from previous days).

Every failure path is fail-open — the guard must never block a news slot.
"""

import json
import logging
import os
import statistics
from datetime import datetime, timedelta, timezone

import boto3
import requests
from botocore.exceptions import ClientError

from metrics_collector import MetricsCollector

logger = logging.getLogger(__name__)

_GRAPH_API = "https://graph.facebook.com/v24.0"
_SKIP_STATE_KEY = "viral/_skip_state.json"

# Defaults for the tunables below. They are read from env at *call* time, not at
# import: get_secrets() copies them out of Secrets Manager during the invoke,
# which happens after this module is imported at container init.
DEFAULTS = {
    # A post younger than this is still in its distribution ramp.
    "VIRAL_WINDOW_HOURS": 24.0,
    # Both must hold: relative to the account's own recent median AND an absolute
    # floor so a small account's 5× median (e.g. 5×30 likes) never triggers.
    "VIRAL_SKIP_MULTIPLIER": 5.0,
    "VIRAL_MIN_ENGAGEMENT": 1000.0,
    # Without enough history the median is noise — never trigger below this.
    "MIN_BASELINE_POSTS": 10.0,
    "BASELINE_WINDOW_DAYS": 30.0,
}


def _tunable(name: str) -> float:
    """Read a threshold from env, falling back to its default if unset or malformed."""
    try:
        return float(os.environ.get(name, DEFAULTS[name]))
    except (TypeError, ValueError):
        logger.warning(f"⚠️  Invalid {name} — using default {DEFAULTS[name]}")
        return DEFAULTS[name]

_ENGAGEMENT_FIELDS = ("likes", "comments", "shares", "saves")


# ── S3 skip-state ─────────────────────────────────────────────────────────────

def _s3():
    return boto3.client("s3")


def _read_json(bucket: str, key: str, default):
    try:
        obj = _s3().get_object(Bucket=bucket, Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NoSuchBucket"):
            return default
        raise
    except Exception as e:  # malformed JSON etc. — fall back gracefully
        logger.warning(f"⚠️  Could not read s3://{bucket}/{key}: {e}")
        return default


def _write_json(bucket: str, key: str, data) -> None:
    _s3().put_object(
        Bucket=bucket, Key=key,
        Body=json.dumps(data, indent=2, ensure_ascii=False),
        ContentType="application/json",
    )


# ── Live Instagram lookups ────────────────────────────────────────────────────

def _latest_media(collector: MetricsCollector) -> dict:
    """Fetch the most recent post from the media edge (Stories never appear here)."""
    resp = requests.get(
        f"{_GRAPH_API}/{collector.account_id}/media",
        params={
            "fields": "id,timestamp,media_type,media_product_type",
            "limit": 1,
            "access_token": collector.access_token,
        },
        timeout=15,
    )
    resp.raise_for_status()
    items = resp.json().get("data", [])
    return items[0] if items else {}


def _live_engagement(collector: MetricsCollector, media_id: str, is_video: bool) -> int | None:
    """Current likes+comments+shares+saves of a single post, or None if unavailable."""
    insights = collector._post_insights(media_id, is_video)
    if not insights:
        return None
    return sum(int(insights.get(k, 0) or 0) for k in ("likes", "comments", "shares", "saved"))


# ── Baseline from DynamoDB ────────────────────────────────────────────────────

def _baseline_engagements(collector: MetricsCollector, exclude_id: str) -> list[int]:
    """Per-post engagement of mature posts in the baseline window (values are stored as strings)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_tunable("BASELINE_WINDOW_DAYS"))).isoformat()
    scan_kwargs = {
        "FilterExpression": "published_at >= :cutoff",
        "ExpressionAttributeValues": {":cutoff": cutoff},
    }
    items = []
    response = collector._table.scan(**scan_kwargs)
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = collector._table.scan(**scan_kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))

    engagements = []
    for item in items:
        if item.get("post_id") == exclude_id:
            continue
        try:
            engagements.append(sum(int(float(item.get(k, 0) or 0)) for k in _ENGAGEMENT_FIELDS))
        except (TypeError, ValueError):
            continue
    return engagements


# ── Decision ──────────────────────────────────────────────────────────────────

def should_skip_next_post(bucket_name: str) -> tuple[bool, dict]:
    """Decide whether the previous post's virality warrants skipping this news slot.

    Returns (skip, payload) where payload carries the reason and telemetry.
    Fail-open: any error means (False, ...) — the guard never blocks news.
    """
    if os.environ.get("ENABLE_VIRAL_SKIP", "true").lower() != "true":
        return False, {"reason": "flag_disabled"}
    try:
        return _evaluate(bucket_name)
    except Exception as e:
        logger.warning(f"⚠️  Viral guard failed — posting normally (fail-open): {e}")
        return False, {"reason": "guard_error", "error": str(e)}


def _evaluate(bucket_name: str) -> tuple[bool, dict]:
    collector = MetricsCollector()

    latest = _latest_media(collector)
    if not latest:
        return False, {"reason": "no_media"}
    media_id = latest["id"]

    published = datetime.fromisoformat(latest["timestamp"].replace("Z", "+00:00"))
    age_hours = (datetime.now(timezone.utc) - published).total_seconds() / 3600
    if age_hours >= _tunable("VIRAL_WINDOW_HOURS"):
        return False, {"reason": "post_too_old", "media_id": media_id, "age_hours": round(age_hours, 1)}

    # One skip per viral event — never lose two consecutive slots.
    state = _read_json(bucket_name, _SKIP_STATE_KEY, {})
    if state.get("last_skipped_media_id") == media_id:
        return False, {"reason": "already_skipped_once", "media_id": media_id}

    engagement = _live_engagement(collector, media_id, is_video=latest.get("media_type") == "VIDEO")
    if engagement is None:
        return False, {"reason": "no_insights", "media_id": media_id}

    baseline = _baseline_engagements(collector, exclude_id=media_id)
    if len(baseline) < _tunable("MIN_BASELINE_POSTS"):
        return False, {"reason": "insufficient_baseline", "media_id": media_id,
                       "baseline_posts": len(baseline)}

    median_engagement = statistics.median(baseline)
    payload = {
        "media_id": media_id,
        "age_hours": round(age_hours, 1),
        "engagement": engagement,
        "median_engagement": median_engagement,
        "multiplier": round(engagement / median_engagement, 1) if median_engagement else None,
        "baseline_posts": len(baseline),
    }

    is_viral = (engagement >= _tunable("VIRAL_SKIP_MULTIPLIER") * median_engagement
                and engagement >= _tunable("VIRAL_MIN_ENGAGEMENT"))
    if not is_viral:
        return False, {**payload, "reason": "below_threshold"}

    _write_json(bucket_name, _SKIP_STATE_KEY, {
        "last_skipped_media_id": media_id,
        "skipped_at": datetime.now(timezone.utc).isoformat(),
        "engagement": engagement,
        "median_engagement": median_engagement,
    })
    return True, {**payload, "reason": "previous_post_viral"}
