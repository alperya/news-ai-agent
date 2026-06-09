# News AI Agent — Project Context

Dutch-language Instagram automation: scrapes NOS/RTL news + 8 NL event sources, generates AI content with Claude, publishes photo posts and Reels to Instagram 3x/day + twice weekly for events.

---

## Lambda Functions & Handlers

| Handler | File | Schedule | Purpose |
|---|---|---|---|
| `lambda_handler` | `lambda_handler.py` | EventBridge 3x/day | Main pipeline: scrape → AI → publish photo or Reels |
| `reels_worker` | `src/reels_worker.py` | Invoked async by main handler | Publishes Reels to Instagram (polls container status) |
| `token_refresher` | `token_refresher.py` | EventBridge every 30 days | Refreshes Instagram 60-day token before expiry |
| `handler_metrics_collector` | `lambda_handler.py` | EventBridge daily 02:00 AMS | Fetches Instagram Insights, writes to DynamoDB |
| `handler_analytics_engine` | `lambda_handler.py` | EventBridge Sunday 22:00 AMS | Claude analytics + prompt auto-update + SNS email |

**Run mode detection** in `lambda_handler`: `event.get('format') == 'event_post'` → events pipeline; `event.get('format') == 'reels'` → Reels mode; otherwise → photo post.

---

## Key Architectural Decisions

**Reels publishes via separate Lambda (`reels_worker`)**
Meta's video processing takes up to 10 minutes. Main Lambda has 15-min hard limit. To avoid timeout, main handler generates video, uploads to S3, then invokes `reels_worker` asynchronously (fire-and-forget). `reels_worker` polls the Instagram container status up to 80×8 seconds.

**Two Claude models**
- `claude-opus-4-6` / `claude-opus-4-7` → content generation (single_article, batch_selection, event_selection, footage queries). Quality matters here.
- `claude-haiku-4-5` → quality gate reviews and event scoring. Speed + cost matter; results are structural/language checks only.

**Prompts stored in Secrets Manager**
All four prompts (`AI_PROMPT_BATCH_SELECTION`, `AI_PROMPT_SINGLE_ARTICLE`, `AI_PROMPT_QUALITY_CHECK`, `AI_PROMPT_EVENT_SELECTION`) are stored in Secrets Manager alongside API keys. `get_secrets()` in `lambda_handler.py` loads them as env vars at startup. `ai_agent.py` reads from env, falls back to `prompts/` directory files. This allows hot-updating prompts without redeploying Lambda.

**Token refresh is a separate Lambda**
Instagram access tokens have a 60-day TTL. A dedicated Lambda runs every 30 days to exchange the token via Meta Graph API and write the new token back to Secrets Manager.

**Duplicate detection**
On every run, main handler reads all `posts_*.json` files from S3 to build a set of published URLs and filters them out before AI processing. No database needed — S3 scan is acceptable at this volume.

---

## Source Files

| File | Purpose |
|---|---|
| `lambda_handler.py` | Lambda entry points, pipeline orchestration, S3 I/O |
| `src/news_scraper.py` | RSS scraping — NOS + RTL Nieuws → `NewsArticle` objects |
| `src/ai_agent.py` | Claude API calls — content gen, quality gate, event scoring/selection, footage queries |
| `src/social_publisher.py` | Instagram Graph API v24.0 — photo posts + Reels container upload/publish |
| `src/event_scraper.py` | 8-source NL event scraping (Ticketmaster, RSS, HTML) → `EventItem` objects |
| `src/notifier.py` | AWS SNS email alerts — `send_alert()`, `send_event_summary()` |
| `src/reels_worker.py` | Async Instagram Reels publishing + container status polling |
| `src/video/creator.py` | Reels orchestrator: TTS → footage → effects → audio → 1080×1920 MP4 |
| `src/video/tts.py` | ElevenLabs TTS (premium) with edge-tts fallback (free) |
| `src/video/footage.py` | Pexels video/photo API |
| `src/video/effects.py` | Ken Burns zoom, hook overlay (first 3s), orange subtitle clip |
| `src/video/audio.py` | Narration + mood-based background music mixing |
| `src/video/event_card.py` | PIL event infographic slides + video stitching |
| `token_refresher.py` | Instagram token refresh Lambda handler |
| `infrastructure/terraform/main.tf` | All AWS infrastructure (Lambda, S3, EventBridge, SNS, Secrets, IAM) |
| `infrastructure/terraform/analytics.tf` | Analytics infrastructure (DynamoDB x2, Glue, Athena, CloudWatch Dashboard) |

---

## Analytics System (added 2026-06)

Collects per-post Instagram engagement metrics, normalizes them against follower count at time of publishing (to remove growth bias), runs weekly Claude analysis, auto-updates prompts, and surfaces everything in CloudWatch.

**New DynamoDB Tables**
- `news-ai-agent-post-metrics` — per-post engagement (normalized_engagement_rate = engagement/followers_at_publish×100)
- `news-ai-agent-prompt-versions` — versioned prompt history with change_reason + analytics_ref. Rollback = write an old version's `content` back to Secrets Manager.

**New Lambda handlers** (in `lambda_handler.py`)
- `handler_metrics_collector` — daily, fetches last 30 days of media + Insights from Instagram Graph API, writes to DynamoDB. Requires `instagram_manage_insights` permission on access token.
- `handler_analytics_engine` — weekly Sunday, queries DynamoDB, sends normalized data to Claude Sonnet for pattern analysis, auto-applies high-confidence prompt changes (>0.80), queues low-confidence ones for dashboard display, sends SNS diff email.

**Auto-decision thresholds**: prompt updates apply if confidence >0.75–0.85 AND min data ≥5–10 posts. Schedule/content-ratio changes are NEVER auto-applied — surfaced in CloudWatch Dashboard only.

**Dashboard**: CloudWatch Logs Insights queries pinned as dashboard widgets. Structured JSON logs written via `aws_lambda_powertools.Logger` in `social_publisher.py` and `ai_agent.py` with event keys `post_published`, `quality_gate_rejected`, `prompt_updated`.

**Source files**: `src/metrics_collector.py`, `src/analytics_engine.py`

---

## Credentials & Configuration

All credentials are in AWS Secrets Manager secret `news-ai-agent/credentials`. See `.env.example` for the full key list. Required: `ANTHROPIC_API_KEY`, `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_ACCOUNT_ID`, `META_APP_ID`, `META_APP_SECRET`. Optional: `ELEVENLABS_API_KEY/VOICE_ID`, `PEXELS_API_KEY`, `TICKETMASTER_API_KEY`, `ALERT_EMAIL`, LangSmith + Langfuse keys.

For local development, copy `.env.example` to `.env` and run `src/main.py`.

---

## Known Gotchas

- **Lambda import path**: `sys.path` inserts both `.` and `./src` at top of `lambda_handler.py` — necessary because local and Lambda ZIP have different structures.
- **Reels container polling**: Can take 8–10 minutes. Always happens in `reels_worker`, never in the main handler.
- **EventBridge cron times are UTC**: All schedules in Terraform are UTC. Amsterdam = UTC+1 (winter) / UTC+2 (summer, CEST). The current crons assume CEST (+2) — may be off by 1 hour in winter.
- **Pexels lazy-loading**: `footage.py` uses pagination with lazy fetching; don't call `len()` on the generator directly.
- **Instagram Insights requires separate permission**: `instagram_manage_insights` must be added to the Meta App for `handler_metrics_collector` to work.
- **S3 bucket name**: hardcoded fallback in `lambda_handler.py` as `news-ai-agent-results-645949963620`. Override via `RESULTS_BUCKET` env var.
