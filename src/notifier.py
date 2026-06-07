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


def send_event_summary(summary: dict) -> bool:
    """Send a run-summary email for the weekly events pipeline.

    The *summary* dict is expected to contain:
      status           "success" | "failed" | "no_events" | "quality_gate_failed" | "ai_failed"
      timestamp        ISO/formatted string
      source_results   list of {source, count, ok, error?}
      raw_count        int
      unique_count     int
      valid_count      int
      scored_count     int
      all_scored       list of {title, location, _score, ...}
      score_prompt     str (prompt sent to Haiku)
      score_response   str (raw Haiku response)
      selected_events  list of {title, date_label, location, price, emoji}
      caption          str (full Instagram caption)
      selection_prompt str
      selection_response str
      publish_result   dict | str | None
      error            str (only on failure)
      s3_log_key       str (S3 path for full log)
    """
    topic_arn = os.environ.get("SNS_ALERT_TOPIC_ARN")
    if not topic_arn:
        logger.warning("⚠️  SNS_ALERT_TOPIC_ARN not set — event summary skipped")
        return False

    status = summary.get("status", "unknown")
    ts = summary.get("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    ok = status == "success"
    status_icon = "✅" if ok else "❌"
    subject_line = f"[Events] {status_icon} Weekly NL Events — {status.upper()} — {ts[:10]}"

    lines = [
        f"📅 News AI Agent — Weekly Events Pipeline",
        "=" * 55,
        "",
        f"⏰ Time:    {ts}",
        f"📊 Status:  {status_icon} {status.upper()}",
        "",
    ]

    # ── Source scraping ──
    source_results = summary.get("source_results", [])
    if source_results:
        ok_count = sum(1 for s in source_results if s.get("ok"))
        lines += [
            "━" * 55,
            f"📡 STAGE 1 — EVENT SCRAPING ({ok_count}/{len(source_results)} sources OK)",
            "━" * 55,
            "",
        ]
        for sr in source_results:
            icon = "✅" if sr.get("ok") else "❌"
            count = sr.get("count", 0)
            err = f"  → {sr['error']}" if not sr.get("ok") and sr.get("error") else ""
            lines.append(f"  {icon} {sr['source']:<18} {count:>3} events{err}")
        lines += [
            "",
            f"  Raw: {summary.get('raw_count', '?')}  →  "
            f"Unique: {summary.get('unique_count', '?')}  →  "
            f"Valid (NL, next 7 days): {summary.get('valid_count', '?')}",
            "",
        ]

    # ── Scoring ──
    all_scored = summary.get("all_scored", [])
    scored_count = summary.get("scored_count", 0)
    if all_scored:
        lines += [
            "━" * 55,
            f"🤖 STAGE 2 — AI QUALITY SCORING (Haiku) — {scored_count} passed (≥5/8)",
            "━" * 55,
            "",
        ]
        top = sorted(all_scored, key=lambda x: x.get("_score", 0), reverse=True)[:8]
        for ev in top:
            score = ev.get("_score", "?")
            lines.append(
                f"  [{score}/8] {ev.get('title', '')[:50]:<50}  "
                f"{ev.get('location', '')[:15]} | {ev.get('start_date', '')[:10]}"
            )
        score_prompt = summary.get("score_prompt", "")
        if score_prompt:
            lines += ["", f"  Prompt sent to Haiku ({len(score_prompt)} chars):"]
            lines += [f"    {line}" for line in score_prompt[:800].splitlines()]
            if len(score_prompt) > 800:
                lines.append(f"    ... [{len(score_prompt) - 800} more chars]")
        score_response = summary.get("score_response", "")
        if score_response:
            lines += ["", f"  Haiku raw response ({len(score_response)} chars):"]
            lines += [f"    {line}" for line in score_response[:500].splitlines()]
        lines.append("")

    # ── Selection + caption ──
    selected_events = summary.get("selected_events", [])
    if selected_events:
        lines += [
            "━" * 55,
            f"✍️  STAGE 3 — AI SELECTION & CAPTION (Opus) — {len(selected_events)} events selected",
            "━" * 55,
            "",
        ]
        for ev in selected_events:
            emoji = ev.get("emoji", "📍")
            price = ev.get("price") or "?"
            lines.append(
                f"  {emoji} {ev.get('title', '')[:45]:<45}  "
                f"{ev.get('location', '')[:12]} | {ev.get('date_label', '')} | {price}"
            )
        caption = summary.get("caption", "")
        if caption:
            lines += ["", "  Caption preview (first 400 chars):"]
            lines += [f"    {line}" for line in caption[:400].splitlines()]
            if len(caption) > 400:
                lines.append(f"    ... [{len(caption) - 400} more chars]")
        sel_prompt = summary.get("selection_prompt", "")
        if sel_prompt:
            lines += ["", f"  Prompt sent to Opus ({len(sel_prompt)} chars):"]
            lines += [f"    {line}" for line in sel_prompt[:600].splitlines()]
            if len(sel_prompt) > 600:
                lines.append(f"    ... [{len(sel_prompt) - 600} more chars]")
        sel_response = summary.get("selection_response", "")
        if sel_response:
            lines += ["", f"  Opus raw response ({len(sel_response)} chars):"]
            lines += [f"    {line}" for line in sel_response[:400].splitlines()]
        lines.append("")

    # ── Publish result ──
    lines += [
        "━" * 55,
        "📱 STAGE 4-6 — GENERATE CARD & PUBLISH",
        "━" * 55,
        "",
    ]
    publish_result = summary.get("publish_result")
    if ok and publish_result:
        lines.append(f"  ✅ Published: {publish_result}")
    elif status == "success":
        lines.append("  ✅ Published successfully")
    else:
        lines.append(f"  ❌ Not published — status: {status}")
        if summary.get("error"):
            lines.append(f"  Error: {summary['error']}")
    lines.append("")

    # ── S3 log + CloudWatch ──
    s3_key = summary.get("s3_log_key", "")
    if s3_key:
        lines += [
            "━" * 55,
            "📂 FULL LOGS",
            "━" * 55,
            "",
            f"  S3:          s3://news-ai-agent-results-645949963620/{s3_key}",
        ]
    lines += [
        f"  CloudWatch:  {CLOUDWATCH_URL}",
        "",
    ]

    body = "\n".join(lines)

    try:
        sns = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "eu-central-1"))
        sns.publish(
            TopicArn=topic_arn,
            Subject=subject_line[:100],
            Message=body,
        )
        logger.info(f"📧 Event summary email sent: {subject_line}")
        return True
    except ClientError as exc:
        logger.error(f"❌ Failed to send event summary: {exc}")
        return False


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
