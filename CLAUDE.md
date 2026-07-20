# News AI Agent — Project Context

Dutch-language social media automation: scrapes NOS/RTL news + 8 NL event sources, generates AI content with Claude, publishes photo posts and Reels to **Instagram** and the same Reels as **YouTube Shorts** — 2× daily for news, weekly for events on Thursday (events go to Instagram + Facebook, but not YouTube). **The weekly events post is currently disabled** (feature flag `ENABLE_EVENT_POSTS`, default off — deprecated for low engagement); see the flag note below.

---

## Lambda Functions & Handlers

| Handler | File | Schedule | Purpose |
|---|---|---|---|
| `lambda_handler` | `lambda_handler.py` | EventBridge 2×/day + events | Main pipeline: scrape → AI → publish photo or Reels |
| `reels_worker` | `src/reels_worker.py` | Invoked async by main handler | Publishes Reels to Instagram (polls container status) |
| `youtube_worker` | `src/youtube_worker.py` | Invoked async by main handler | Uploads same Reels video as YouTube Short |
| `token_refresher` | `token_refresher.py` | EventBridge every 30 days | Refreshes Instagram 60-day token before expiry |
| `handler_metrics_collector` | `lambda_handler.py` | EventBridge daily 02:00 AMS | Fetches Instagram Insights, writes to DynamoDB |
| `handler_analytics_engine` | `lambda_handler.py` | EventBridge Sunday 22:00 AMS | Claude analytics + prompt auto-update + SNS email |
| `handler_selection_review` | `lambda_handler.py` | EventBridge Sunday 19:00 AMS | Weekly editorial review: digests the week's article picks + top-7 runner-ups + engagement, emails an Opus growth/commercial review |

**Run mode detection** in `lambda_handler`: `event.get('format') == 'event_post'` → events pipeline (**gated by `ENABLE_EVENT_POSTS`, default off** — returns `skipped` without building the AI agent or scraping when disabled); `event.get('format') == 'daily_fact'` → daily Dutch-fact Story pipeline; `event.get('format') == 'reels'` → Reels mode; otherwise → photo post.

---

## Deployment (CI/CD)

**Every push to `main` auto-deploys to AWS.** `.github/workflows/deploy.yml` runs on push: it first runs the test job (`pytest tests/`), then — only on push to `main` — builds the ZIP (`scripts/build_lambda.sh`) and runs `terraform apply -auto-approve` (AWS auth via OIDC). There is no separate manual deploy step; merging/pushing **is** the deploy. A deploy typically lands a few minutes after push — confirm via the function's `LastModified` (`aws lambda get-function-configuration --function-name news-ai-agent --query LastModified`). Pull requests run the tests only (no deploy). So: never push to `main` expecting code not to ship, and if a change breaks a scheduled run, the fix must be pushed (and the deploy confirmed) before re-triggering the Lambda.

---

## Key Architectural Decisions

**Reels publishes via separate Lambda (`reels_worker`)**
Meta's video processing takes up to 10 minutes. Main Lambda has 15-min hard limit. To avoid timeout, main handler generates video, uploads to S3, then invokes `reels_worker` asynchronously (fire-and-forget). `reels_worker` polls the Instagram container status up to 80×8 seconds.

**YouTube Shorts via separate Lambda (`youtube_worker`)**
The same MP4 already in S3 is also published to YouTube Shorts. Main handler invokes `youtube_worker` asynchronously immediately after invoking `reels_worker` — both are fire-and-forget. YouTube failure never blocks Instagram: the invoke is wrapped in its own try/except. Events content is intentionally excluded (static slideshow format performs poorly on YouTube Shorts; misaligns with the news-channel identity needed for monetization).

**News Reel render — single ffmpeg pass, not moviepy (`src/video/ffcompose.py`)**
The final video assembly for news Reels is done in **one ffmpeg `filter_complex` pass**, not moviepy's `write_videofile`. moviepy composited every frame in pure Python (single-thread) and took ~9 min for a ~35 s clip — ~85% of pipeline time and the main cause of 15-min Lambda timeouts. Key insight: every visual layer is a **static image over a time window** (gradient is time-invariant; each subtitle chunk and the hook are fixed PIL PNGs; all full-width, x=0). So `_build_background` (`creator.py`) returns a background **mp4 path** (stock → `_stitch_clips_to_file`; photo/Ken Burns → `render_ken_burns_mp4`; fallback → `render_fallback_bg_mp4` — all ffmpeg), `effects.build_subtitle_overlays`/`build_hook_overlays`/`render_gradient_png` emit RGBA PNGs as `OverlaySpec(png_path, y, start, end)`, `mix_audio` is written to a file, and `ffcompose.assemble_reel` burns them with `overlay=0:y:enable='between(t,s,e)'` + one x264 encode (same settings as before → pixel-equivalent, no quality loss). Render drops to ~1-2 min. **Only the news Reel path uses this**; `create_fact_video` (daily Story) still uses moviepy (`compose_stock_scenes`/`make_ken_burns_clip`/`make_fact_overlay` are kept for it) — shorter clips, lower risk; can migrate later (helpers are reusable).

**Multi-channel publishing — Publisher adapter + CrossPoster (`src/publishing.py`)**
Whatever is published to Instagram is mirrored to every other configured channel via a single dispatcher. `ChannelPublisher` is the common interface (`name`, `supports(kind)`, `publish(kind, media_url, caption, dry_run)`); `kind ∈ {REEL, PHOTO, STORY}`. Concrete adapters live in `src/social_publisher.py`: `InstagramPublisher` (primary), `FacebookPublisher` (secondary), and `LinkedInPublisher` (secondary, REEL only). `CrossPoster.publish(kind, …)` fans out to the primary (failure surfaced via `primary_error`) + every secondary that `supports(kind)` **best-effort** (logged + alerted, never blocks); it returns `{"results": {channel: result}, "primary_error": Exception|None}`. `build_crossposter(content_source="news")` registers the primary plus any secondary whose creds are present (`FACEBOOK_PAGE_ID` → Facebook; `ENABLE_LINKEDIN=true` **and** `LINKEDIN_ACCESS_TOKEN` + `LINKEDIN_ORG_ID` → LinkedIn — the flag lets the code ship dark, default off). **Adding a channel = write one `ChannelPublisher` adapter + one line in `build_crossposter()`; call sites never change.** Call sites: `reels_worker` (Reel + Story), `_run_event_pipeline` (event Reel — passes `content_source="event"`), photo branch in `lambda_handler`. Facebook: news/event Reels → **FB Reel** (`video_reels`), photos → **FB photo** (`/photos`), daily Story → **FB Story** (`video_stories`); Page token derived at runtime from the user token (`pages_manage_posts` scope). Meta video flows follow Meta's two-phase upload: `start` → hosted `file_url` upload → poll status → `finish`. **LinkedIn: news Reels only** (event slideshows excluded, same rationale as the YouTube exclusion — the event pipeline opts out via `content_source="event"`); posts to a Company Page (`urn:li:organization:<id>`, `w_organization_social` scope). Unlike Meta, LinkedIn's Videos API needs the raw **bytes**, so the adapter downloads the presigned S3 URL into memory then does `initializeUpload` → PUT byte ranges (collect ETags) → `finalizeUpload` → poll until `AVAILABLE` → create `/posts`. **When new channel SDKs push the Lambda ZIP past 250 MB, migrate to a Lambda Container Image (ECR) — not microservices.**

**Daily Dutch-fact Story (story-only, feature-flagged)**
Every day at 07:00 Amsterdam (EventBridge `{"format":"daily_fact"}` → `_run_daily_fact_pipeline`), a curated "Did you know?" Dutch fact is rendered as a short vertical video (Pexels Dutch B-roll + on-screen text + `FACT_STORY_MUSIC`, **no TTS**, no baked-in branding since IG shows the account in the Story's top-left) and published as an **Instagram Story only** — no Reel, no YouTube. Duration = reading time of the text (`reading_seconds()` in `video/config.py`) so it's short enough to watch to completion. Deliberately not a Reel: news already posts 2 Reels/day and a 3rd would cannibalize reach; Stories are a separate surface. Story video is published via `reels_worker` (async, `publish_story=True, publish_reel=False`) so Meta's video processing happens off the main Lambda's clock. When `FACEBOOK_PAGE_ID` is set, the same video is also cross-posted as a **Facebook Page Story** (`FacebookPublisher.publish_story` → Graph API `video_stories` start→file_url upload→finish; Page token derived at runtime from the user token, requires `pages_manage_posts`). The Facebook leg is best-effort — a Facebook failure never blocks the Instagram Story. Gated by `ENABLE_INSTAGRAM_STORIES` (Secrets Manager) — when off, the pipeline returns immediately and generates **nothing** (no fact pick, no Pexels, no render). Fact pool lives in S3 `facts/pool.json` (hot-editable, seeded from `DEFAULT_FACTS` in `src/dutch_facts.py`); least-recently-used rotation in `facts/_rotation.json` means a fact repeats only after ~the whole pool cycles (~90 days). When the pool nearly cycles, a one-time SNS refill email is sent to `ALERT_EMAIL`; if ignored, facts simply repeat (system never stops, AI never auto-generates facts).

**Two Claude models**
- `claude-opus-4-6` / `claude-opus-4-7` → content generation (single_article, batch_selection, event_selection, footage queries). Quality matters here.
- `claude-haiku-4-5` → quality gate reviews, event scoring, and footage thumbnail validation (vision). Speed + cost matter; results are structural/language checks only.

**Prompts stored in Secrets Manager**
All four prompts (`AI_PROMPT_BATCH_SELECTION`, `AI_PROMPT_SINGLE_ARTICLE`, `AI_PROMPT_QUALITY_CHECK`, `AI_PROMPT_EVENT_SELECTION`) are stored in Secrets Manager alongside API keys. `get_secrets()` in `lambda_handler.py` loads them as env vars at startup. `ai_agent.py` reads from env, falls back to `prompts/` directory files. This allows hot-updating prompts without redeploying Lambda.

**Token refresh is a separate Lambda**
Instagram access tokens have a 60-day TTL. A dedicated Lambda runs every 30 days to exchange the token via Meta Graph API and write the new token back to Secrets Manager.

**Duplicate detection**
On every run, main handler reads all `posts_*.json` files from S3 to build a set of published URLs and filters them out before AI processing. No database needed — S3 scan is acceptable at this volume. Additionally, `original_title` values from posts published in the last 3 days are passed to `batch_selection` as context so the AI skips articles covering already-published topics even when they come from different sources (cross-source semantic dedup). This is a **soft prompt instruction**, not a hard filter. The instruction targets the **same specific event** (the same match result/accident/announcement re-reported), and explicitly **permits follow-ups on an escalating situation** — new damage, casualties, a distinct phenomenon — so e.g. an already-covered heatwave does not suppress the storms/lightning damage that end it (those are separate, newsworthy events). Refined after a storm/windmill story was wrongly skipped as "same as" a prior heat story.

**Editorial selection transparency + weekly review**
`batch_selection` returns, alongside the chosen article, a `selection_reason` and a `runner_ups` array — together the **top-7** pool candidates with their Tier and why-not — persisted into `posts_*.json` and emitted as a structured `selection_decision` CloudWatch log (survives S3 pruning; back-compat: absent on pre-update runs). A separate weekly Lambda (`handler_selection_review` → `src/selection_reviewer.py`, Sunday 19:00 AMS) pairs each week's pool (`articles_*.json`) with its pick, joins Instagram engagement (DynamoDB `post-metrics`, best-effort caption/time match), and emails the most advanced Claude (`SELECTION_REVIEW_MODEL`, default `claude-opus-4-8`) acting as a growth/content/platform/commercial panel — surfacing weak picks, missed high-engagement candidates, and concrete prompt/dedup/schedule revisions to drive follower growth toward monetization. Reports only; never mutates prompts.

**Cover de-duplication (Reels)**
Same problem, visual layer: similar news (heatwaves, weather) used to reuse the identical Pexels cover clip. Two defenses, both fed by the same `get_published_urls()` S3 scan (now also returns footage fingerprints from posts in the last `FOOTAGE_REUSE_WINDOW_DAYS`, default 30):
1. **Real photo preferred as cover.** When the article's own image is *fresh* it becomes the cover (Ken Burns) with Pexels stock filling the body — `creator._build_background` hybrid. Freshness = its URL wasn't used recently **and** it isn't a perceptual near-duplicate of a recent cover (`effects.compute_dhash`/`hamming`, threshold ~10) — this catches NOS's recurring "weekdienst" template card even at a different URL.
2. **Pexels id fresh-first.** When stock clips are used, ids from the reuse window are moved to the back of the candidate list (not hard-filtered) so the cover is fresh but the body never degrades to a gradient when the pool is small. `footage.fetch_stock_clips` records the ids actually used.
Each Reel post persists `pexels_media_ids`, `cover_image_url`, `cover_image_hash` into `posts_*.json` for the next run's scan. News Reels only (events/daily-fact opt out).

---

## Source Files

| File | Purpose |
|---|---|
| `lambda_handler.py` | Lambda entry points, pipeline orchestration, S3 I/O |
| `src/news_scraper.py` | RSS scraping — NOS + RTL Nieuws → `NewsArticle` objects |
| `src/dutch_facts.py` | Daily "Did you know?" fact pool (`DEFAULT_FACTS` seed) + S3-backed LRU rotation (`get_fact_for_today`) + refill SNS alert |
| `src/ai_agent.py` | Claude API calls — content gen, quality gate, event scoring/selection, footage queries |
| `src/publishing.py` | `ChannelPublisher` interface + `CrossPoster` dispatcher + `build_crossposter()` — fans one artifact out to all configured channels |
| `src/social_publisher.py` | Channel adapters: `InstagramPublisher` (Graph API v24.0 — photo/Reels/Story) + `FacebookPublisher` (Page Reels/photos/Stories) + `LinkedInPublisher` (Company Page news Reels via REST Videos+Posts API) |
| `src/youtube_publisher.py` | YouTube Data API v3 — OAuth 2.0 refresh token flow, resumable upload as Short |
| `src/youtube_worker.py` | Async YouTube Shorts Lambda handler (mirrored pattern from `reels_worker`) |
| `src/event_scraper.py` | 8-source NL event scraping (Ticketmaster, RSS, HTML) → `EventItem` objects |
| `src/notifier.py` | AWS SNS email alerts — `send_alert()`, `send_event_summary()` |
| `src/selection_reviewer.py` | Weekly editorial review: pairs `articles_*.json` pools with `posts_*.json` picks, joins engagement (DynamoDB), emails an Opus growth/content/platform/commercial review |
| `src/reels_worker.py` | Async Instagram Reels publishing + container status polling |
| `src/video/creator.py` | Reels orchestrator: TTS → footage → effects → audio → 1080×1920 MP4 |
| `src/video/tts.py` | ElevenLabs TTS (premium) with edge-tts fallback (free) |
| `src/video/footage.py` | Pexels video/photo API |
| `src/video/effects.py` | Ken Burns zoom, gradient/hook/subtitle overlays (moviepy clips for fact video + RGBA `OverlaySpec` PNGs for news Reel), dHash, ffmpeg clip stitching |
| `src/video/ffcompose.py` | `assemble_reel` — single-pass ffmpeg final composite for news Reels (background + overlay PNGs + audio) |
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

**YouTube credentials** (required for YouTube Shorts publishing): `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`. These are OAuth 2.0 credentials linked to the `alper.jasmine@gmail.com` channel. If absent from Secrets Manager, `youtube_worker` exits cleanly with `status: skipped` — no alert sent. To refresh an expired token, create a new OAuth Desktop app credential from Google Cloud Console and re-run the auth flow manually.

**LinkedIn credentials** (optional — cross-posts news Reels to a Company Page): gated by `ENABLE_LINKEDIN` (default `"false"`) **plus** `LINKEDIN_ACCESS_TOKEN` (OAuth token with `w_organization_social` scope) + `LINKEDIN_ORG_ID` (numeric Company Page id → `urn:li:organization:<id>`). With the flag off or creds absent, LinkedIn is simply not registered as a secondary channel (no-op, no alert) — so the code can be merged dark and switched on later by flipping the flag in Secrets Manager. The one-time OAuth helper lives in the gitignored `local_only/get_linkedin_token.py`. Tokens expire ~60 days (refresh tokens ~365 days) — currently a manual re-auth, like YouTube.

For local development, copy `.env.example` to `.env` and run `src/main.py`.

---

## Known Gotchas

- **Failure emails come from Lambda async on-failure destinations** (`failure_alert` in terraform → SNS): every function routes any failed invocation (including timeouts) to the alert email.
- **Lambda import path**: `sys.path` inserts both `.` and `./src` at top of `lambda_handler.py` — necessary because local and Lambda ZIP have different structures.
- **Reels container polling**: Can take 8–10 minutes. Always happens in `reels_worker`, never in the main handler.
- **News Reel narration = hook + content**: `creator.create_news_video` narrates `hook + ". " + content`, then caps length via `tts.cap_narration(text, MAX_NARRATION_WORDS=120)`. The cap truncates **on sentence boundaries only** — never mid-sentence. The earlier raw 100-word cut clipped the final sentence's tail (an 11-word hook + 96-word content = 107 words lost "...in 1928 during the Amsterdam Summer Games."). Content is prompt-capped to 75–95 words, so 120 comfortably fits hook+content; if it ever overflows, whole trailing sentences drop, not half of one.
- **EventBridge cron times are UTC**: All schedules in Terraform are UTC. Amsterdam = UTC+1 (winter) / UTC+2 (summer, CEST). The current crons assume CEST (+2) — may be off by 1 hour in winter.
- **Pexels lazy-loading**: `footage.py` uses pagination with lazy fetching; don't call `len()` on the generator directly.
- **Instagram Insights requires separate permission**: `instagram_manage_insights` must be added to the Meta App for `handler_metrics_collector` to work.
- **S3 bucket name**: hardcoded fallback in `lambda_handler.py` as `news-ai-agent-results-645949963620`. Override via `RESULTS_BUCKET` env var.
- **Lambda ZIP size limit**: unzipped package must stay under 250 MB. Current size ~231 MB. `build_lambda.sh` aborts if >240 MB (safety gate). Key exclusions: `googleapiclient/discovery_cache/documents/` (all except `youtube.v3.json`) and `zstandard/`. If a new heavy dependency is added and the limit is hit, migrate to Lambda Container Image (ECR, no size limit).
- **YouTube OAuth token**: invalidated if user revokes access or GCP OAuth app is in Testing mode (tokens expire in 7 days — must be Production). Fix: re-run OAuth Desktop flow locally, update `YOUTUBE_REFRESH_TOKEN` in Secrets Manager.
- **YouTube vs events**: events Reels (slide-based PIL video) is published to Instagram only. YouTube receives news Reels only. See architectural decision above for rationale.
- **LinkedIn vs events**: like YouTube, LinkedIn receives **news Reels only**. The exclusion is enforced inside `build_crossposter()` — the event pipeline calls `build_crossposter(content_source="event")`, which skips LinkedIn registration. Call sites stay channel-agnostic (they pass a *content source*, never a channel name).
- **LinkedIn needs raw bytes, not a URL**: unlike Meta's `file_url` fetch, LinkedIn's Videos API uploads bytes. `LinkedInPublisher.publish_reel` downloads the presigned S3 URL into memory first — fine because Reels are short (<~50 MB); if a much larger video is ever published this would need streaming/chunking. Required headers on every call: `LinkedIn-Version` (YYYYMM) + `X-Restli-Protocol-Version: 2.0.0`; the created post id comes from the `x-restli-id` response header, not the body.
- **Instagram Stories API limits**: `media_type=STORIES` containers accept **no caption** and **no interactive stickers** (polls/questions/links are app-only). The daily fact's text is baked into the video. Story reach is mostly existing followers, not discovery — Stories are a retention/completion surface, not a growth channel.
- **`ENABLE_INSTAGRAM_STORIES` is checked twice**: first in `_run_daily_fact_pipeline` (so flag-off costs nothing), again in `reels_worker` (defense-in-depth before any Story publish). Both read the same Secrets Manager value.
- **The weekly events post is disabled at two layers** (deprecated for low engagement): (1) the Thursday EventBridge rule `events_thursday_schedule` is `state = "DISABLED"` in Terraform, so the cron never fires; (2) the `ENABLE_EVENT_POSTS` feature flag (Secrets Manager, default off) gates the pipeline itself — checked in the `event_post` dispatch branch (short-circuits before building the AI agent) and again at the top of `_run_event_pipeline` (defense-in-depth for manual/async invokes). Flag-off costs nothing (no scrape/scoring/render). **Re-enable = both: flip the rule to `state = "ENABLED"` in Terraform AND set `ENABLE_EVENT_POSTS=true` in Secrets Manager.** The event-only `[Analytics Optimization]` blocks in `AI_PROMPT_EVENT_SELECTION` are intentionally kept (they only affect event curation, never news, and apply again on re-enable). While events are off, `analytics_engine` skips `event_curation` recommendations so it never appends to the now-dormant event prompt.
- **Facebook Page Stories need `pages_manage_posts`**: the Meta user token (`INSTAGRAM_ACCESS_TOKEN`) must hold the `pages_manage_posts` scope, or `video_stories` returns `(#3) Application does not have the granular permission`. Re-issue the token via `scripts/get_new_instagram_token.py` selecting that scope. The connected Page (name "Dutch Daily", id in `FACEBOOK_PAGE_ID`) has its Page token derived from the user token at runtime — no separate token is stored. The 30-day refresh Lambda preserves the scope.
- **Fact rotation is hot-editable, no redeploy**: to add/change facts, edit `s3://<bucket>/facts/pool.json` directly. New entries (unseen `id`s) jump to the front of the LRU rotation. `DEFAULT_FACTS` only seeds the pool on first run when `pool.json` is absent.
