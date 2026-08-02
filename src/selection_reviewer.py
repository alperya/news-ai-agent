"""
Weekly Selection Reviewer

Scans the last 7 days of editorial decisions — the candidate pool
(`articles_*.json`) paired with what was actually published (`posts_*.json`,
including the new `selection_reason` / `runner_ups` transparency fields) — joins
each pick to its Instagram engagement (DynamoDB `post-metrics`, best-effort),
then asks the most advanced Claude model to review the week through a panel of
growth / content-strategy / platform / commercial lenses and propose concrete
revisions. The news Reel is published to Instagram, YouTube Shorts AND Facebook,
so the review judges cross-platform reach. The digest and the AI review are
emailed via SNS so the operator can decide whether to adjust the selection
prompt, the dedup rule, the Tier weighting, or the schedule.

North star: grow followers as fast as possible and raise engagement/attraction
to make the account commercial (monetizable, sponsorship-ready).

Read-only with respect to prompts/secrets — it only reports.
"""

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import anthropic
import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_REGION = os.environ.get("AWS_REGION", "eu-central-1")
_RESULTS_BUCKET = os.environ.get("RESULTS_BUCKET", "news-ai-agent-results-645949963620")
_METRICS_TABLE = os.environ.get("METRICS_TABLE", "news-ai-agent-post-metrics")

# Most advanced Claude model — review quality matters more than cost here (run
# once a week). Overridable so the id can be bumped without a code change.
_REVIEW_MODEL = os.environ.get("SELECTION_REVIEW_MODEL", "claude-opus-5")

# A multi-lens expert panel, chosen for the stated goal: fastest follower growth
# and rising engagement toward commercialisation. Performance is framed as
# ORGANIC retention/share optimisation (no paid ad budget in this pipeline), not
# paid-media buying.
_REVIEW_SYSTEM = (
    "You are a panel of senior experts advising a Dutch news short-video channel "
    "that publishes 2 Reels per day to Instagram, YouTube Shorts AND Facebook. "
    "You reason through four lenses at once:\n"
    "(1) GROWTH strategist — fastest possible follower acquisition, shareability "
    "and virality loops;\n"
    "(2) CONTENT strategist — editorial angles, hooks and narratives that travel "
    "and that a Netherlands audience forwards;\n"
    "(3) PLATFORM / retention specialist — Reels/Shorts/Facebook mechanics: the "
    "first-second hook, watch-time, saves and shares (organic optimisation, NOT "
    "paid media — there is no ad budget);\n"
    "(4) commercially-minded PRODUCT MANAGER — north star is turning the account "
    "into a monetizable, brand-safe property: consistent news niche, YouTube "
    "Shorts monetization eligibility, sponsorship readiness.\n"
    "Overarching goal: grow followers as fast as possible and raise "
    "engagement/attraction to make the account commercial. Be direct and "
    "specific — name weak picks and the better candidates passed over, and always "
    "tie advice back to growth and monetization."
)

_FILENAME_TS = re.compile(r"_(\d{8}_\d{6})\.json$")


class SelectionReviewer:
    def __init__(self):
        self._s3 = boto3.client("s3", region_name=_REGION)
        self._dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        self._metrics_table = self._dynamodb.Table(_METRICS_TABLE)
        self._claude = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ── Public entry point ────────────────────────────────────────────────────

    def run(self, dry_run: bool = False, days: int = 7) -> dict:
        runs = self._collect_runs(days=days)
        if not runs:
            logger.info("No news selection runs found in the last %d days", days)
            return {"status": "empty", "runs": 0}

        metrics = self._load_metrics(days=days + 3)
        self._attach_engagement(runs, metrics)

        review = self._ai_review(runs)
        body = self._format_email(runs, review, days)

        if dry_run:
            logger.info("[dry-run] selection review:\n%s", body)
            return {"status": "dry_run", "runs": len(runs), "body": body, "review": review}

        sent = self._send_email(body)
        return {"status": "sent" if sent else "send_failed", "runs": len(runs)}

    # ── Data collection ───────────────────────────────────────────────────────

    def _list_recent(self, prefix: str, cutoff: datetime) -> dict:
        """Return {timestamp_str: s3_key} for `prefix` files newer than cutoff."""
        out: dict[str, str] = {}
        token = None
        while True:
            kwargs = {"Bucket": _RESULTS_BUCKET, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                m = _FILENAME_TS.search(obj["Key"])
                if not m:
                    continue
                ts = m.group(1)
                try:
                    when = datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if when >= cutoff:
                    out[ts] = obj["Key"]
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
        return out

    def _read_json(self, key: str):
        try:
            obj = self._s3.get_object(Bucket=_RESULTS_BUCKET, Key=key)
            return json.loads(obj["Body"].read())
        except (ClientError, json.JSONDecodeError) as exc:
            logger.warning("Could not read %s: %s", key, exc)
            return None

    def _collect_runs(self, days: int) -> list[dict]:
        """Pair each posts_*.json (what was published) with its articles_*.json
        (the candidate pool) by shared filename timestamp."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        post_files = self._list_recent("posts_", cutoff)
        article_files = self._list_recent("articles_", cutoff)

        runs: list[dict] = []
        for ts, post_key in sorted(post_files.items()):
            posts = self._read_json(post_key)
            if not posts:
                continue
            post = posts[0]  # news pipeline publishes exactly one post per run

            pool_titles: list[str] = []
            article_key = article_files.get(ts)
            if article_key:
                articles = self._read_json(article_key) or []
                pool_titles = [a.get("title", "") for a in articles if a.get("title")]

            runs.append({
                "timestamp": ts,
                "when": datetime.strptime(ts, "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc),
                "pool_size": len(pool_titles),
                "pool_titles": pool_titles,
                "chosen_title": post.get("original_title", ""),
                "selection_reason": post.get("selection_reason", ""),
                "runner_ups": post.get("runner_ups", []) or [],
                "full_post": post.get("full_post", ""),
                "content": post.get("content", ""),
                "engagement": None,  # filled by _attach_engagement
            })
        return runs

    # ── Engagement join (best-effort) ─────────────────────────────────────────

    def _load_metrics(self, days: int) -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        items: list[dict] = []
        resp = self._metrics_table.scan(
            FilterExpression="published_at >= :c",
            ExpressionAttributeValues={":c": cutoff},
        )
        items.extend(resp.get("Items", []))
        while "LastEvaluatedKey" in resp:
            resp = self._metrics_table.scan(
                FilterExpression="published_at >= :c",
                ExpressionAttributeValues={":c": cutoff},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))
        return items

    @staticmethod
    def _norm(text: str) -> str:
        """Lowercase, drop non-alphanumerics — for fuzzy caption matching."""
        return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()

    def _attach_engagement(self, runs: list[dict], metrics: list[dict]) -> None:
        """Match each run to its Instagram metric. Primary: caption-prefix
        overlap (the metric stores the first 150 chars of the live caption).
        Fallback: nearest publish time the same day."""
        for run in runs:
            run_key = self._norm(run["content"])[:60] or self._norm(run["full_post"])[:60]
            best = None
            for m in metrics:
                cap = self._norm(m.get("caption_preview", ""))
                if run_key and (run_key in cap or cap[:60] in self._norm(run["full_post"])):
                    best = m
                    break
            if best is None:
                # Fallback: closest metric within ~6h of the run timestamp
                best = self._nearest_by_time(run["when"], metrics)
            if best is not None:
                run["engagement"] = {
                    "normalized_engagement_rate": best.get("normalized_engagement_rate"),
                    "reach": best.get("reach"),
                    "saves": best.get("saves"),
                    "comments": best.get("comments"),
                    "post_type": best.get("post_type"),
                }

    @staticmethod
    def _nearest_by_time(when: datetime, metrics: list[dict]) -> Optional[dict]:
        best, best_delta = None, timedelta(hours=6)
        for m in metrics:
            raw = m.get("published_at")
            if not raw:
                continue
            try:
                pub = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
            delta = abs(pub - when)
            if delta <= best_delta:
                best, best_delta = m, delta
        return best

    # ── AI review ─────────────────────────────────────────────────────────────

    def _ai_review(self, runs: list[dict]) -> str:
        digest = [
            {
                "date": r["when"].strftime("%Y-%m-%d %H:%M UTC"),
                "pool_size": r["pool_size"],
                "chosen": r["chosen_title"],
                "selection_reason": r["selection_reason"],
                "runner_ups": r["runner_ups"],
                "engagement": r["engagement"],
                "candidate_pool": r["pool_titles"],
            }
            for r in runs
        ]
        prompt = (
            "Below is one week of automated editorial decisions for a Dutch news "
            "short-video channel (each Reel is published to Instagram, YouTube "
            "Shorts AND Facebook). Each run picked exactly ONE article from a "
            "candidate pool; `runner_ups` are the strongest candidates the AI "
            "passed over; `engagement.normalized_engagement_rate` is engagement / "
            "followers × 100 (null = metrics not yet mature, ignore those for "
            "performance judgement).\n\n"
            f"{json.dumps(digest, ensure_ascii=False, indent=2)}\n\n"
            "Write a concise review (plain text, no markdown headers) covering:\n"
            "1. OVERALL: how strong were this week's picks for FAST follower growth "
            "and engagement across Instagram, YouTube Shorts and Facebook?\n"
            "2. WEAK PICKS: name specific runs where a runner-up or a pool article "
            "would likely have driven more reach/follows/shares, and why.\n"
            "3. PATTERNS: any recurring blind spot (e.g. missing escalating weather "
            "follow-ups, over-favouring low-visual stories, timing) that caps growth.\n"
            "4. REVISION SUGGESTIONS: concrete changes to the selection prompt, the "
            "dedup rule, the Tier weighting, or the posting schedule — each tied to "
            "follower growth and the path to monetization (consistent news niche, "
            "YouTube Shorts eligibility, sponsorship readiness). Be specific enough "
            "to act on. If picks were good, say so plainly."
        )
        try:
            msg = self._claude.messages.create(
                model=_REVIEW_MODEL,
                max_tokens=8000,  # thinking counts against max_tokens on Opus 5
                system=_REVIEW_SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            return "".join(
                getattr(b, "text", "") for b in msg.content
            ).strip() or "(empty review)"
        except Exception as exc:
            logger.error("Claude review failed: %s", exc)
            return f"(AI review unavailable: {exc})"

    # ── Email ─────────────────────────────────────────────────────────────────

    def _format_email(self, runs: list[dict], review: str, days: int) -> str:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            "📋 News AI Agent — Weekly Selection Review",
            "=" * 55,
            "",
            f"⏰ Period: last {days} days  |  Report date: {today}",
            f"🗞️  Runs reviewed: {len(runs)}  |  Channels: Instagram · YouTube Shorts · Facebook",
            "",
            "━" * 55,
            "🧠 AI REVIEW (growth · content · platform · product/commercial)",
            "━" * 55,
            "",
            review,
            "",
            "━" * 55,
            "🗂️  SELECTION LOG",
            "━" * 55,
            "",
        ]
        for r in runs:
            ner = "—"
            if r["engagement"] and r["engagement"].get("normalized_engagement_rate") is not None:
                ner = f"{r['engagement']['normalized_engagement_rate']}% NER"
            lines += [
                f"📅 {r['when'].strftime('%a %Y-%m-%d %H:%M UTC')}  "
                f"(pool: {r['pool_size']}  |  {ner})",
                f"   ✅ CHOSEN: {r['chosen_title']}",
                f"   💬 Why:    {r['selection_reason'] or '(no reason logged — pre-update run)'}",
            ]
            if r["runner_ups"]:
                lines.append("   🥈 Runner-ups (passed over):")
                for ru in r["runner_ups"][:6]:
                    title = ru.get("title", "?") if isinstance(ru, dict) else str(ru)
                    tier = ru.get("tier", "") if isinstance(ru, dict) else ""
                    why = ru.get("reason_not_selected", "") if isinstance(ru, dict) else ""
                    lines.append(f"      • [{tier}] {title} — {why}")
            lines.append("")

        lines += ["━" * 55, "📂 Source: posts_*.json / articles_*.json in S3", ""]
        return "\n".join(lines)

    def _send_email(self, body: str) -> bool:
        topic_arn = os.environ.get("SNS_ALERT_TOPIC_ARN")
        if not topic_arn:
            logger.warning("SNS_ALERT_TOPIC_ARN not set — selection review email skipped")
            return False
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            boto3.client("sns", region_name=_REGION).publish(
                TopicArn=topic_arn,
                Subject=f"[News AI Agent] 📋 Weekly Selection Review — {today}"[:100],
                Message=body,
            )
            logger.info("📧 Weekly selection review email sent")
            return True
        except Exception as exc:
            logger.error("SNS send failed: %s", exc)
            return False
