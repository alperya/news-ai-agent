"""
Instagram Metrics Collector

Fetches engagement metrics for the last 30 days of posts, normalizes by
followers_at_publish (removes growth bias), writes to DynamoDB.
Also saves a daily account snapshot to S3 for Athena queries.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import boto3
import requests
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_REGION = os.environ.get("AWS_REGION", "eu-central-1")
_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "news-ai-agent-results-645949963620")
_METRICS_TABLE = os.environ.get("METRICS_TABLE", "news-ai-agent-post-metrics")
_GRAPH_API = "https://graph.facebook.com/v24.0"


class MetricsCollector:
    def __init__(self):
        self.access_token = os.environ["INSTAGRAM_ACCESS_TOKEN"]
        self.account_id = os.environ["INSTAGRAM_ACCOUNT_ID"]
        self._dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        self._s3 = boto3.client("s3", region_name=_REGION)
        self._table = self._dynamodb.Table(_METRICS_TABLE)

    # ── Account snapshot ────────────────────────────────────────────────────

    def collect_account_metrics(self) -> dict:
        """Fetch today's follower count and save to S3."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Followers via basic account fields (simpler than insights endpoint)
        resp = requests.get(
            f"{_GRAPH_API}/{self.account_id}",
            params={"fields": "followers_count,media_count", "access_token": self.access_token},
            timeout=15,
        )
        resp.raise_for_status()
        body = resp.json()

        snapshot = {
            "date": today,
            "followers_count": body.get("followers_count"),
            "media_count": body.get("media_count"),
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        self._s3.put_object(
            Bucket=_RESULTS_BUCKET,
            Key=f"metrics/account/{today}.json",
            Body=json.dumps(snapshot),
            ContentType="application/json",
        )
        logger.info(f"✅ Account snapshot saved: {snapshot['followers_count']} followers")
        return snapshot

    # ── Follower lookup ──────────────────────────────────────────────────────

    def _followers_on_date(self, date_str: str) -> Optional[int]:
        """Read follower count from S3 account snapshot for a given date."""
        try:
            obj = self._s3.get_object(Bucket=_RESULTS_BUCKET, Key=f"metrics/account/{date_str}.json")
            return json.loads(obj["Body"].read()).get("followers_count")
        except ClientError:
            return None

    # ── Media list ───────────────────────────────────────────────────────────

    def _recent_media(self, days: int = 30) -> list:
        """Return media items published within the last `days` days, newest first."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        items = []
        url = f"{_GRAPH_API}/{self.account_id}/media"
        params = {
            "fields": "id,timestamp,media_type,media_product_type,caption",
            "limit": 100,
            "access_token": self.access_token,
        }
        while url:
            resp = requests.get(url, params=params, timeout=15)
            if not resp.ok:
                logger.error(f"Media list failed: {resp.status_code} {resp.text[:200]}")
                break
            body = resp.json()
            for item in body.get("data", []):
                ts = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                if ts < cutoff:
                    return items  # items are newest-first; stop once past the window
                items.append(item)
            url = body.get("paging", {}).get("next")
            params = {}  # next-page URL already carries all params
        logger.info(f"📋 {len(items)} media items found in last {days} days")
        return items

    # ── Post insights ────────────────────────────────────────────────────────

    def _post_insights(self, media_id: str, is_video: bool) -> dict:
        """Fetch engagement metrics for a single post.

        API v22.0+ changes:
        - 'impressions' removed for feed posts (photos)
        - For Reels: use likes,comments,shares,saved,reach,views,total_interactions
        - For Photos: use reach,saved,shares,likes,comments,total_interactions
        - Likes and comments now available directly in insights (no separate media fields call needed)
        """
        if is_video:
            metrics_param = "likes,comments,shares,saved,reach,views,ig_reels_avg_watch_time,total_interactions"
        else:
            metrics_param = "reach,saved,shares,likes,comments,total_interactions"

        ins = requests.get(
            f"{_GRAPH_API}/{media_id}/insights",
            params={"metric": metrics_param, "access_token": self.access_token},
            timeout=15,
        )
        result: dict = {}
        if ins.ok:
            for item in ins.json().get("data", []):
                val = item.get("value") or (item.get("values") or [{}])[0].get("value", 0)
                result[item["name"]] = val or 0
        elif ins.status_code in (400, 403):
            err = ins.json().get("error", {})
            if "manage_insights" in err.get("message", "") or err.get("code") in (200, 10):
                raise PermissionError(
                    "Token is missing 'instagram_manage_insights' permission. "
                    "Add it in Meta App Dashboard and regenerate the token."
                )
            logger.warning(f"Insights unavailable for {media_id}: {err.get('message')}")

        # Normalize field names: 'views' (Reels API name) → stored as 'video_views' for consistency
        if "views" in result:
            result["video_views"] = result.pop("views")

        return result

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _post_type(media_type: str, product_type: str, caption: str) -> str:
        if media_type != "VIDEO":
            return "photo"
        if product_type == "REELS":
            cap = caption.lower()
            if "events" in cap or "🗓" in cap or "this week" in cap or "festival" in cap:
                return "events_reel"
            return "reel"
        return "photo"

    @staticmethod
    def _topic(caption: str) -> str:
        if not caption:
            return "World"
        cap = caption.lower()
        topics = [
            (["netherlands", "amsterdam", "rotterdam", "den haag", "nederland", "dutch", "nl "], "Netherlands"),
            (["ukraine", "russia", "europe", "eu ", "nato"], "Europe"),
            (["climate", "energy", "environment", "solar", "wind"], "Climate/Energy"),
            (["economy", "inflation", "market", "stock", "trade"], "Economy"),
            (["tech", "ai ", "artificial intelligence", "software", "startup"], "Technology"),
            (["sport", "football", "soccer", "eredivisie", "olympic"], "Sports"),
            (["festival", "events", "concert", "museum", "exhibition"], "Events"),
            (["health", "covid", "hospital", "medicine", "vaccine"], "Health"),
        ]
        for keywords, label in topics:
            if any(k in cap for k in keywords):
                return label
        return "World"

    # ── Main ─────────────────────────────────────────────────────────────────

    def collect(self, dry_run: bool = False) -> dict:
        """Run full collection cycle: account snapshot + per-post metrics."""
        account = self.collect_account_metrics()
        media_items = self._recent_media(days=30)
        if not media_items:
            return {"collected": 0, "errors": 0, "followers": account.get("followers_count")}

        collected = errors = 0
        for item in media_items:
            media_id = item["id"]
            try:
                published_at = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
                date_str = published_at.strftime("%Y-%m-%d")
                caption = item.get("caption") or ""
                media_type = item.get("media_type", "IMAGE")
                is_video = media_type == "VIDEO"
                post_type = self._post_type(media_type, item.get("media_product_type", ""), caption)
                topic = self._topic(caption)

                insights = self._post_insights(media_id, is_video)
                likes = insights.get("likes", 0)
                comments = insights.get("comments", 0)
                shares = insights.get("shares", 0)
                saves = insights.get("saved", 0)
                reach = insights.get("reach", 0)

                total_engagement = likes + comments + shares + saves
                followers_at_publish = self._followers_on_date(date_str)
                followers = followers_at_publish or 0

                # reach_amplification: how far beyond the follower base this post traveled
                # Reels scoring much higher here is expected and desirable for growth
                reach_amplification = round(reach / followers, 3) if followers > 0 else 0.0

                # save_rate: % of people who saw the post and saved it — strongest algorithmic signal
                # Works fairly across post types since both numerator and denominator scale with distribution
                save_rate = round(saves / reach * 100, 4) if reach > 0 else 0.0

                # avg_watch_time stored raw (seconds) — higher = algorithm pushes Reel further
                avg_watch_time = float(insights.get("ig_reels_avg_watch_time", 0) or 0)

                # NER kept for backward-compat and same-type temporal comparisons only
                # Do NOT use NER for Reels vs Photo cross-type comparisons
                normalized_engagement_rate = round(total_engagement / followers * 100, 4) if followers > 0 else 0.0
                raw_engagement_rate = round(total_engagement / reach * 100, 4) if reach > 0 else 0.0

                record = {
                    "post_id": media_id,
                    "published_at": item["timestamp"],
                    "post_type": post_type,
                    "topic": topic,
                    "caption_preview": caption[:150],
                    "hashtags_count": caption.count("#"),
                    "hour_published": str(published_at.hour),
                    "day_of_week": str(published_at.weekday()),  # 0=Monday
                    "likes": str(likes),
                    "comments": str(comments),
                    "shares": str(shares),
                    "saves": str(saves),
                    "reach": str(reach),
                    "video_views": str(insights.get("video_views", 0)),
                    "avg_watch_time": str(avg_watch_time),
                    "followers_at_publish": str(followers),
                    "reach_amplification": str(reach_amplification),
                    "save_rate": str(save_rate),
                    "normalized_engagement_rate": str(normalized_engagement_rate),
                    "raw_engagement_rate": str(raw_engagement_rate),
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }

                if not dry_run:
                    self._table.put_item(Item=record)

                logger.info(json.dumps({
                    "event": "post_metrics_updated",
                    "post_id": media_id,
                    "post_type": post_type,
                    "topic": topic,
                    "hour_published": published_at.hour,
                    "day_of_week": published_at.weekday(),
                    "hashtags_count": caption.count("#"),
                    "reach_amplification": reach_amplification,
                    "save_rate": save_rate,
                    "avg_watch_time": avg_watch_time,
                    "normalized_engagement_rate": normalized_engagement_rate,
                    "likes": likes,
                    "saves": saves,
                    "reach": reach,
                    "followers_at_publish": followers,
                }))
                logger.info(
                    f"  {'[dry]' if dry_run else '✅'} {media_id} ({post_type}) "
                    f"RA={reach_amplification:.2f}x save_rate={save_rate:.2f}% likes={likes} reach={reach}"
                )
                collected += 1

            except PermissionError:
                raise
            except Exception as exc:
                logger.warning(f"  ⚠️ {media_id} failed: {exc}")
                errors += 1

        logger.info(f"✅ Collection done: {collected} posts, {errors} errors")
        return {"collected": collected, "errors": errors, "followers": account.get("followers_count")}
