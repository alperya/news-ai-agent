"""
Analytics Engine

Weekly: queries DynamoDB post-metrics, sends normalized data to Claude Sonnet
for pattern analysis, auto-updates high-confidence prompt changes in Secrets Manager
(with version history in DynamoDB), and sends a weekly diff email via SNS.
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_REGION = os.environ.get("AWS_REGION", "eu-central-1")
_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "news-ai-agent-results-645949963620")
_METRICS_TABLE = os.environ.get("METRICS_TABLE", "news-ai-agent-post-metrics")
_VERSIONS_TABLE = os.environ.get("PROMPT_VERSIONS_TABLE", "news-ai-agent-prompt-versions")
_SECRET_NAME = os.environ.get("SECRET_NAME", "news-ai-agent/credentials")
_CW_NAMESPACE = "NewsAIAgent"

# Auto-apply thresholds: prompt changes require confidence above these values
# AND minimum post counts before a recommendation is trusted
_THRESHOLDS = {
    "caption_hook": {"confidence": 0.80, "min_posts": 10},
    "hashtag_strategy": {"confidence": 0.75, "min_posts": 8},
    "event_curation": {"confidence": 0.85, "min_posts": 6},
}

# Maps recommendation category → Secrets Manager key
_PROMPT_KEYS = {
    "caption_hook": "AI_PROMPT_SINGLE_ARTICLE",
    "hashtag_strategy": "AI_PROMPT_SINGLE_ARTICLE",
    "event_curation": "AI_PROMPT_EVENT_SELECTION",
}

_DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_ANALYTICS_MODEL = os.environ.get("ANALYTICS_MODEL", "claude-opus-5")


class AnalyticsEngine:
    def __init__(self):
        self._dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        self._s3 = boto3.client("s3", region_name=_REGION)
        self._secrets = boto3.client("secretsmanager", region_name=_REGION)
        self._cw = boto3.client("cloudwatch", region_name=_REGION)
        self._metrics_table = self._dynamodb.Table(_METRICS_TABLE)
        self._versions_table = self._dynamodb.Table(_VERSIONS_TABLE)
        self._claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Data retrieval ────────────────────────────────────────────────────────

    def _load_posts(self, days: int = 30) -> list[dict]:
        """Scan DynamoDB for posts published in the last `days` days."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        response = self._metrics_table.scan(
            FilterExpression="published_at >= :cutoff",
            ExpressionAttributeValues={":cutoff": cutoff},
        )
        posts = response.get("Items", [])
        # Handle DynamoDB pagination
        while "LastEvaluatedKey" in response:
            response = self._metrics_table.scan(
                FilterExpression="published_at >= :cutoff",
                ExpressionAttributeValues={":cutoff": cutoff},
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            posts.extend(response.get("Items", []))
        logger.info(f"📊 Loaded {len(posts)} posts from DynamoDB (last {days} days)")
        return posts

    def _load_account_snapshots(self, days: int = 30) -> list[dict]:
        """Load daily account snapshots from S3 for follower growth analysis."""
        snapshots = []
        for i in range(days):
            date_str = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
            try:
                obj = self._s3.get_object(
                    Bucket=_RESULTS_BUCKET, Key=f"metrics/account/{date_str}.json"
                )
                snapshots.append(json.loads(obj["Body"].read()))
            except ClientError:
                continue
        return sorted(snapshots, key=lambda x: x["date"])

    def _load_previous_analysis(self) -> Optional[dict]:
        """Load last week's analysis result from S3 for diff comparison."""
        try:
            response = self._s3.list_objects_v2(
                Bucket=_RESULTS_BUCKET, Prefix="analytics/weekly_", MaxKeys=10
            )
            objects = sorted(
                response.get("Contents", []), key=lambda x: x["LastModified"], reverse=True
            )
            if len(objects) < 2:
                return None
            previous_key = objects[1]["Key"]
            obj = self._s3.get_object(Bucket=_RESULTS_BUCKET, Key=previous_key)
            return json.loads(obj["Body"].read())
        except Exception:
            return None

    # ── Claude analysis ───────────────────────────────────────────────────────

    def _build_analysis_prompt(self, posts: list[dict], snapshots: list[dict]) -> str:
        total_posts = len(posts)
        if total_posts == 0:
            return ""

        def avg(lst): return round(sum(lst) / len(lst), 3) if lst else 0

        # Growth metrics: reach_amplification and save_rate are the primary KPIs
        # reach_amplification = reach / followers — measures distribution beyond follower base
        # save_rate = saves / reach × 100 — strongest algorithmic signal, works fairly across post types
        by_type: dict = {}
        by_hour: dict = {}
        by_day: dict = {}
        by_topic: dict = {}

        for p in posts:
            ra = float(p.get("reach_amplification", 0) or 0)
            sr = float(p.get("save_rate", 0) or 0)
            wt = float(p.get("avg_watch_time", 0) or 0)
            reach = int(p.get("reach", 0) or 0)
            likes = int(p.get("likes", 0) or 0)
            pt = p.get("post_type", "unknown")
            hour = str(p.get("hour_published", "?"))
            dow = int(p.get("day_of_week", 0) or 0)
            topic = p.get("topic", "unknown")

            if pt not in by_type:
                by_type[pt] = {"ra": [], "sr": [], "wt": [], "reach": [], "likes": [], "count": 0}
            by_type[pt]["ra"].append(ra)
            by_type[pt]["sr"].append(sr)
            by_type[pt]["wt"].append(wt)
            by_type[pt]["reach"].append(reach)
            by_type[pt]["likes"].append(likes)
            by_type[pt]["count"] += 1

            if hour not in by_hour:
                by_hour[hour] = {"ra": [], "sr": []}
            by_hour[hour]["ra"].append(ra)
            by_hour[hour]["sr"].append(sr)

            if topic not in by_topic:
                by_topic[topic] = {"ra": [], "sr": []}
            by_topic[topic]["ra"].append(ra)
            by_topic[topic]["sr"].append(sr)

            day_name = _DAYS_OF_WEEK[dow]
            if day_name not in by_day:
                by_day[day_name] = {"ra": [], "sr": []}
            by_day[day_name]["ra"].append(ra)
            by_day[day_name]["sr"].append(sr)

        type_stats = {
            pt: {
                "count": d["count"],
                "avg_reach_amplification": avg(d["ra"]),
                "avg_save_rate_pct": avg(d["sr"]),
                "avg_reach_absolute": round(avg(d["reach"]), 0),
                "avg_likes_absolute": round(avg(d["likes"]), 1),
                "avg_watch_time_sec": avg(d["wt"]),
            }
            for pt, d in by_type.items()
        }
        hour_stats = {
            h: {"avg_reach_amplification": avg(d["ra"]), "avg_save_rate_pct": avg(d["sr"]), "count": len(d["ra"])}
            for h, d in sorted(by_hour.items())
        }
        day_stats = {
            day: {"avg_reach_amplification": avg(d["ra"]), "avg_save_rate_pct": avg(d["sr"]), "count": len(d["ra"])}
            for day, d in by_day.items()
        }
        topic_stats = sorted(
            [{"topic": t, "avg_reach_amplification": avg(d["ra"]), "avg_save_rate_pct": avg(d["sr"]), "count": len(d["ra"])} for t, d in by_topic.items()],
            key=lambda x: x["avg_reach_amplification"], reverse=True
        )

        # Top/bottom posts ranked by reach_amplification (primary growth signal)
        def _clean(s): return (s or "")[:100].replace("\n", " ").replace('"', "'")
        sorted_posts = sorted(posts, key=lambda p: float(p.get("reach_amplification", 0) or 0), reverse=True)
        top5 = [{"caption": _clean(p.get("caption_preview", "")), "reach_amplification": float(p.get("reach_amplification", 0) or 0), "save_rate": float(p.get("save_rate", 0) or 0), "type": p.get("post_type")} for p in sorted_posts[:5]]
        bot5 = [{"caption": _clean(p.get("caption_preview", "")), "reach_amplification": float(p.get("reach_amplification", 0) or 0), "save_rate": float(p.get("save_rate", 0) or 0), "type": p.get("post_type")} for p in sorted_posts[-5:]]

        # Follower growth
        follower_start = snapshots[0].get("followers_count") if snapshots else None
        follower_end = snapshots[-1].get("followers_count") if snapshots else None
        growth = None
        if follower_start and follower_end and follower_start > 0:
            growth = round((follower_end - follower_start) / follower_start * 100, 2)

        prompt = f"""You are analyzing Instagram performance data for a Dutch news & events account that posts in English.
The account is in early growth phase ({follower_end or 'unknown'} followers). The primary goal is FOLLOWER GROWTH.

## Metric definitions
- **reach_amplification** = reach / followers_at_publish
  → How far beyond the follower base a post distributed. A Reel with reach_amplification=10 reached 10x the follower count.
  → This is the PRIMARY growth metric. High values mean the algorithm is distributing content to non-followers.

- **save_rate** = saves / reach × 100 (%)
  → % of people who saw the post and saved it. Strongest signal that Instagram uses to promote content further.
  → Works fairly across all post types — denominator scales with the actual distribution.

- **avg_watch_time** (seconds, Reels only)
  → Longer watch time = algorithm shows the Reel to more people. Correlates with reach_amplification for Reels.

Total posts analyzed: {total_posts}
Analysis period: last 30 days
Current followers: {follower_end or 'unknown'}
Follower growth this period: {f'{growth}%' if growth is not None else 'unknown'}

## Performance by post type (ALWAYS judge primarily by reach_amplification, secondarily by save_rate)
{json.dumps(type_stats, indent=2)}

## Performance by hour published (Amsterdam time)
{json.dumps(hour_stats, indent=2)}

## Performance by day of week
{json.dumps(day_stats, indent=2)}

## Performance by topic
{json.dumps(topic_stats[:10], indent=2)}

## Top 5 posts (highest reach_amplification)
{json.dumps(top5, indent=2)}

## Bottom 5 posts (lowest reach_amplification)
{json.dumps(bot5, indent=2)}

---

Please analyze this data focusing on WHAT DRIVES FOLLOWER GROWTH and return a JSON object:

{{
  "summary": "2-3 sentence plain-English summary of the account's performance and growth trajectory",
  "top_insights": [
    "Insight 1 (specific, data-backed, growth-focused)",
    "Insight 2",
    "Insight 3"
  ],
  "recommendations": [
    {{
      "category": "caption_hook|hashtag_strategy|event_curation|posting_schedule|content_type_ratio",
      "title": "Short action title",
      "description": "What to change and why, referencing reach_amplification or save_rate data",
      "confidence": 0.0-1.0,
      "auto_apply": true|false,
      "prompt_instruction": "If auto_apply=true for caption_hook or hashtag_strategy: exact instruction to append to the content generation prompt. Otherwise null.",
      "data_points_used": 0
    }}
  ],
  "growth_analysis": "What is driving or blocking follower growth? What content type/topic/time should be doubled down on?"
}}

Rules:
- Only set auto_apply=true for categories: caption_hook, hashtag_strategy, event_curation
- Only set auto_apply=true if confidence >= 0.75 AND data_points_used >= 5
- For posting_schedule and content_type_ratio: always set auto_apply=false
- Do NOT recommend reducing Reels unless save_rate and reach_amplification are both very low — Reels are the primary growth vehicle
- prompt_instruction must be a specific, actionable instruction
- Return ONLY valid JSON, no markdown fences"""

        return prompt

    def _call_claude(self, prompt: str) -> Optional[dict]:
        """Call Claude and parse the JSON response."""
        if not prompt:
            return None
        try:
            msg = self._claude.messages.create(
                model=_ANALYTICS_MODEL,
                max_tokens=8000,  # thinking counts against max_tokens on Opus 5
                messages=[{"role": "user", "content": prompt}],
            )
            # Thinking blocks precede text on Opus 5 — never index content[0]
            raw = next(
                (b.text for b in msg.content if getattr(b, "type", None) == "text"), ""
            ).strip()
            # Strip any accidental markdown fences
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return json.loads(raw.strip())
        except Exception as exc:
            logger.error(f"Claude analysis failed: {exc}")
            return None

    # ── Prompt versioning & Secrets Manager ───────────────────────────────────

    def _load_secret(self) -> dict:
        resp = self._secrets.get_secret_value(SecretId=_SECRET_NAME)
        return json.loads(resp["SecretString"])

    def _save_secret(self, secret: dict) -> None:
        self._secrets.put_secret_value(
            SecretId=_SECRET_NAME,
            SecretString=json.dumps(secret),
        )

    def _version_prompt(
        self,
        prompt_name: str,
        new_content: str,
        change_reason: str,
        analytics_ref: str,
        confidence: float,
    ) -> None:
        """Save new prompt version to DynamoDB and deactivate previous active version."""
        version = datetime.now(timezone.utc).isoformat()
        # Deactivate current active version
        try:
            resp = self._versions_table.query(
                KeyConditionExpression="prompt_name = :n",
                FilterExpression="is_active = :t",
                ExpressionAttributeValues={":n": prompt_name, ":t": True},
            )
            for item in resp.get("Items", []):
                self._versions_table.update_item(
                    Key={"prompt_name": prompt_name, "version": item["version"]},
                    UpdateExpression="SET is_active = :f",
                    ExpressionAttributeValues={":f": False},
                )
        except Exception as exc:
            logger.warning(f"Could not deactivate previous version: {exc}")

        self._versions_table.put_item(Item={
            "prompt_name": prompt_name,
            "version": version,
            "content": new_content,
            "applied_at": version,
            "change_reason": change_reason,
            "analytics_ref": analytics_ref,
            "confidence_score": str(confidence),
            "is_active": True,
        })
        logger.info(f"📝 Prompt version saved: {prompt_name}@{version}")

    def _apply_prompt_instruction(
        self,
        secret_key: str,
        instruction: str,
        recommendation: dict,
        analytics_ref: str,
        secret: dict,
    ) -> Optional[str]:
        """Append instruction to existing prompt and update Secrets Manager."""
        current_prompt = secret.get(secret_key, "")
        if not current_prompt:
            logger.warning(f"No current prompt found for {secret_key}, skipping auto-apply")
            return None

        # Append the optimization instruction as a new guidance section
        new_prompt = (
            current_prompt.rstrip()
            + f"\n\n[Analytics Optimization — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}]\n"
            + instruction
        )

        # Version in DynamoDB
        self._version_prompt(
            prompt_name=secret_key,
            new_content=new_prompt,
            change_reason=recommendation.get("description", ""),
            analytics_ref=analytics_ref,
            confidence=float(recommendation.get("confidence", 0)),
        )

        # Update Secrets Manager
        secret[secret_key] = new_prompt
        return new_prompt

    # ── CloudWatch custom metric ───────────────────────────────────────────────

    def _push_cw_metrics(self, posts: list[dict], followers: Optional[int]) -> None:
        """Push weekly aggregate metrics to CloudWatch for dashboard widgets."""
        if not posts:
            return
        ners = [float(p.get("normalized_engagement_rate", 0) or 0) for p in posts]
        avg_ner = sum(ners) / len(ners) if ners else 0
        metric_data = [
            {"MetricName": "AverageNormalizedEngagementRate", "Value": avg_ner, "Unit": "Percent"},
            {"MetricName": "PostsAnalyzed", "Value": len(posts), "Unit": "Count"},
        ]
        if followers:
            metric_data.append({"MetricName": "FollowerCount", "Value": followers, "Unit": "Count"})
        try:
            self._cw.put_metric_data(Namespace=_CW_NAMESPACE, MetricData=metric_data)
            logger.info(f"📈 CloudWatch metrics pushed: avg_ner={avg_ner:.3f}%")
        except Exception as exc:
            logger.warning(f"CloudWatch push failed: {exc}")

    # ── SNS email ─────────────────────────────────────────────────────────────

    def _send_weekly_email(
        self,
        analysis: dict,
        applied_changes: list[dict],
        pending_changes: list[dict],
        prev_analysis: Optional[dict],
        posts: list[dict],
        followers: Optional[int],
    ) -> None:
        topic_arn = os.environ.get("SNS_ALERT_TOPIC_ARN")
        if not topic_arn:
            logger.warning("SNS_ALERT_TOPIC_ARN not set — email skipped")
            return

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            "📊 News AI Agent — Weekly Analytics Report",
            "=" * 55,
            "",
            f"⏰ Period:    Last 30 days  |  Report date: {today}",
            f"👥 Followers: {followers or 'unknown'}",
            "",
        ]

        # Summary
        lines += [
            "━" * 55,
            "🔍 ANALYSIS SUMMARY",
            "━" * 55,
            "",
            analysis.get("summary", ""),
            "",
        ]

        # Top insights
        insights = analysis.get("top_insights", [])
        if insights:
            lines += ["📌 Key Insights:", ""]
            for i, ins in enumerate(insights, 1):
                lines.append(f"  {i}. {ins}")
            lines.append("")

        # Growth analysis
        growth = analysis.get("growth_analysis", "")
        if growth:
            lines += ["📈 Growth Analysis:", f"  {growth}", ""]

        # Auto-applied changes
        if applied_changes:
            lines += [
                "━" * 55,
                f"✅ AUTO-APPLIED CHANGES ({len(applied_changes)})",
                "━" * 55,
                "",
            ]
            for ch in applied_changes:
                lines += [
                    f"  [{ch['category']}] {ch['title']}",
                    f"  Confidence: {ch['confidence']}",
                    "  Instruction added:",
                    f"    → {ch.get('prompt_instruction', '')}",
                    "",
                ]

        # Pending (manual) recommendations
        if pending_changes:
            lines += [
                "━" * 55,
                f"⏳ AWAITING MANUAL REVIEW ({len(pending_changes)})",
                "━" * 55,
                "",
            ]
            for ch in pending_changes:
                lines += [
                    f"  [{ch['category']}] {ch['title']}",
                    f"  {ch['description']}",
                    f"  Confidence: {ch['confidence']} | Data points: {ch.get('data_points_used', '?')}",
                    "",
                ]

        # Engagement diff vs previous week
        if prev_analysis:
            prev_ner = prev_analysis.get("metrics_summary", {}).get("avg_normalized_engagement_rate")
            curr_ners = [float(p.get("normalized_engagement_rate", 0) or 0) for p in posts]
            curr_ner = round(sum(curr_ners) / len(curr_ners), 3) if curr_ners else 0
            if prev_ner is not None:
                diff = round(curr_ner - float(prev_ner), 3)
                sign = "+" if diff >= 0 else ""
                lines += [
                    "━" * 55,
                    "📊 WEEK-OVER-WEEK",
                    "━" * 55,
                    "",
                    f"  Avg NER this period:  {curr_ner:.3f}%",
                    f"  Avg NER last period:  {prev_ner:.3f}%",
                    f"  Change:               {sign}{diff:.3f}%",
                    "",
                ]

        lines += [
            "━" * 55,
            "📂 Full analysis saved to S3 under analytics/",
            "",
        ]

        body = "\n".join(lines)
        status_icon = "✅" if applied_changes or not pending_changes else "⏳"
        subject = f"[News AI Agent] {status_icon} Weekly Analytics — {today}"

        try:
            boto3.client("sns", region_name=_REGION).publish(
                TopicArn=topic_arn,
                Subject=subject[:100],
                Message=body,
            )
            logger.info("📧 Weekly analytics email sent")
        except Exception as exc:
            logger.error(f"SNS send failed: {exc}")

    # ── Main ─────────────────────────────────────────────────────────────────

    def run(self, dry_run: bool = False) -> dict:
        """Run the full analytics cycle."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        analytics_ref = f"analytics/weekly_{today}.json"

        posts = self._load_posts(days=30)
        snapshots = self._load_account_snapshots(days=30)
        prev_analysis = self._load_previous_analysis()

        current_followers = snapshots[-1].get("followers_count") if snapshots else None

        # Push CloudWatch metrics (before Claude analysis so dashboard is always fresh)
        self._push_cw_metrics(posts, current_followers)

        if len(posts) < 5:
            logger.warning(f"Only {len(posts)} posts — too few for reliable analysis (need ≥5)")
            result = {"status": "insufficient_data", "post_count": len(posts)}
            if not dry_run:
                self._s3.put_object(
                    Bucket=_RESULTS_BUCKET,
                    Key=analytics_ref,
                    Body=json.dumps(result),
                    ContentType="application/json",
                )
            return result

        # Claude analysis
        prompt = self._build_analysis_prompt(posts, snapshots)
        analysis = self._call_claude(prompt)
        if not analysis:
            logger.error("Claude analysis returned no result")
            return {"status": "analysis_failed"}

        # Process recommendations
        applied_changes = []
        pending_changes = []

        if not dry_run:
            secret = self._load_secret()

        events_enabled = os.environ.get("ENABLE_EVENT_POSTS", "false").lower() == "true"

        for rec in analysis.get("recommendations", []):
            category = rec.get("category", "")
            # Events are deprecated (feature-flagged off) — never auto-tune the
            # event prompt, otherwise the engine keeps appending instructions to a
            # prompt that no longer runs (the weekly cron is gated off in code).
            if category == "event_curation" and not events_enabled:
                logger.info("Skipping event_curation recommendation — event posts are disabled")
                continue
            confidence = float(rec.get("confidence", 0))
            data_points = int(rec.get("data_points_used", 0))
            threshold = _THRESHOLDS.get(category, {})
            instruction = rec.get("prompt_instruction")
            secret_key = _PROMPT_KEYS.get(category)

            can_auto_apply = (
                rec.get("auto_apply", False)
                and confidence >= threshold.get("confidence", 999)
                and data_points >= threshold.get("min_posts", 999)
                and instruction
                and secret_key
            )

            if can_auto_apply:
                if dry_run:
                    logger.info(f"[dry] Would auto-apply: {category} (confidence={confidence})")
                    applied_changes.append(rec)
                else:
                    new_prompt = self._apply_prompt_instruction(
                        secret_key, instruction, rec, analytics_ref, secret
                    )
                    if new_prompt:
                        rec["_applied"] = True
                        applied_changes.append(rec)
            else:
                pending_changes.append(rec)

        # Write updated secrets if any prompts changed
        if applied_changes and not dry_run:
            self._save_secret(secret)
            logger.info(f"✅ Secrets Manager updated with {len(applied_changes)} prompt change(s)")

        # Compute summary metrics
        ners = [float(p.get("normalized_engagement_rate", 0) or 0) for p in posts]
        metrics_summary = {
            "post_count": len(posts),
            "avg_normalized_engagement_rate": round(sum(ners) / len(ners), 4) if ners else 0,
            "followers": current_followers,
            "date": today,
        }

        result = {
            "status": "success",
            "period": f"last 30 days ending {today}",
            "metrics_summary": metrics_summary,
            "analysis": analysis,
            "applied_changes": applied_changes,
            "pending_changes": pending_changes,
        }

        if not dry_run:
            self._s3.put_object(
                Bucket=_RESULTS_BUCKET,
                Key=analytics_ref,
                Body=json.dumps(result, default=str),
                ContentType="application/json",
            )
            logger.info(f"✅ Analysis saved to S3: {analytics_ref}")

            self._send_weekly_email(
                analysis=analysis,
                applied_changes=applied_changes,
                pending_changes=pending_changes,
                prev_analysis=prev_analysis,
                posts=posts,
                followers=current_followers,
            )

        logger.info(
            f"✅ Analytics run complete: {len(applied_changes)} auto-applied, "
            f"{len(pending_changes)} pending"
        )
        return result
