# 🤖 News AI Agent — Dutch News & Events Automation

An intelligent AI-powered pipeline that automatically scrapes Dutch news from NOS and RTL Nieuws, processes them with Claude AI, and publishes engaging content to **Instagram** and **YouTube Shorts** — both as photo posts and auto-generated Reels videos with TTS narration. Every week it also publishes a curated events digest for the Netherlands to Instagram.

## 📦 Features

- ✅ Multi-source RSS scraping (NOS, RTL Nieuws)
- ✅ Claude Opus 4.6 AI content generation with Holland-priority news selection
- ✅ Quality gate: structural validation + AI language review (Claude Haiku 4.5) before publishing
- ✅ Instagram Graph API integration with automatic token refresh
- ✅ **Instagram Reels** — auto-generated news videos with TTS narration, stock footage & subtitles
- ✅ **YouTube Shorts** — same Reels video simultaneously published as a YouTube Short (news only; separate async Lambda)
- ✅ **Daily Dutch-fact Story** — a curated "Did you know?" fact rendered as a short vertical video (Pexels B-roll + on-screen text + music, no TTS) and published as an Instagram Story each morning; feature-flagged, with a hot-editable S3 fact pool and least-recently-used rotation
- ✅ **Facebook Page cross-posting** — whatever is published to Instagram (Reels, photos, the daily Story) is mirrored to the connected Facebook Page (FB Reels / photos / Stories); best-effort, never blocks Instagram
- ✅ **Reels hook** — AI-generated 8–12 word attention hook, displayed as a large overlay for the first 3 seconds
- ✅ Tiered news selection (🇳🇱 Holland → 🇪🇺 Europe → 🌍 Global) with viral potential scoring
- ✅ Source attribution with article links in posts
- ✅ Duplicate detection — URL-based (all-time) + cross-source semantic deduplication via 3-day title window passed to AI selection
- ✅ **Weekly events post** — every Wednesday 18:00, a PIL-generated infographic of 5–12 curated NL events drawn from 8 sources (Eventbrite, Ticketmaster, amsterdam.nl, rotterdam.nl, denhaag.nl, uitagenda.nl, festileaks.nl, doedagen.nl)
- ✅ AWS Lambda deployment with scheduled posting (daily Story + 2 Reels/day + weekly events)
- ✅ Infrastructure as Code (Terraform)
- ✅ AWS Secrets Manager for credential management
- ✅ Email alerts on errors (OOM, token expiry, publish failures) via AWS SNS
- ✅ Objective, news-agency style content (BBC/Reuters style)
- ✅ **LangSmith + Langfuse** observability — LLM traces, token usage, latency & cost dashboard

## 🎬 Reels Video Pipeline

The afternoon posting slot automatically generates an Instagram Reels video:

1. **Hook overlay** — AI-generated attention sentence displayed in large white text for the first 3 seconds
2. **TTS Narration** — ElevenLabs Multilingual v2 (natural voice) with edge-tts fallback (free); 75–95 word target (~30–40 s)
3. **Cover & Footage** — when the article's own photo is *fresh* it becomes the cover (Ken Burns) with Pexels stock clips filling the body (hybrid); otherwise Pexels clips are used throughout. AI-generated queries + Haiku thumbnail validation pick relevant clips
4. **Cover de-duplication** — the same cover (Pexels clip *or* news photo) is never reused within `FOOTAGE_REUSE_WINDOW_DAYS` (default 30); near-duplicate photos are caught by a perceptual hash so a recurring source template card can't repeat
5. **Subtitles** — Word-timed orange subtitle overlay (62px Montserrat Bold, lower-third TikTok placement)
6. **Background Music** — Mood-based: upbeat (`news_music.mp3`) for positive news, calm/alternative (60/40) for neutral
7. **Visual Fallback** — fresh news photo cover → Pexels stock video → Pexels photo → animated gradient

Video specs: 1080×1920 (9:16), 30 FPS, H.264 High, 4000 kbps, AAC audio, 9 stock clips per video, target duration 30–45 s.

## 🚀 Quick Start

### Prerequisites

- Python 3.12+
- [Anthropic API Key](https://console.anthropic.com/)
- Instagram Business Account + Graph API Token
- AWS CLI & Terraform (for deployment)

### Local Usage

```bash
# Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements/base.txt

# Configure environment variables
cp .env.example .env
# Edit .env with your API keys

# AI prompts are stored in prompts/ folder (see prompts/*.txt.example)

# Run pipeline (dry-run mode — no actual posting)
PYTHONPATH=src python3 src/main.py --dry-run

# Run with actual posting
PYTHONPATH=src python3 src/main.py --no-dry-run --platform instagram --max-posts 1

# Or use Makefile shortcuts
make run          # dry-run
make run-live     # post to Instagram
make test         # run tests
```

### AWS Lambda Deployment

```bash
# One-step deployment (build + Terraform)
./scripts/deploy.sh

# Or step by step:
./scripts/build_lambda.sh              # Build Lambda deployment package
cd infrastructure/terraform
terraform init && terraform apply      # Deploy infrastructure

# Push secrets & prompts to AWS Secrets Manager
python3 scripts/update_secrets.py
```

## 📁 Project Structure

```
├── lambda_handler.py              # AWS Lambda entry point (news + events pipeline)
├── token_refresher.py             # AWS Lambda — refreshes Instagram token every 30 days
├── src/                           # Application source code
│   ├── main.py                    # Local pipeline runner (CLI)
│   ├── ai_agent.py                # Claude AI content processing + event curation
│   ├── news_scraper.py            # RSS scraping (NOS, RTL Nieuws)
│   ├── event_scraper.py           # 📅 8-source NL event scraper
│   ├── publishing.py              # Channel-agnostic CrossPoster (fan-out to all channels)
│   ├── social_publisher.py        # Channel adapters: Instagram + Facebook (photo/Reels/Stories)
│   ├── dutch_facts.py             # 💡 Daily fact pool + S3 LRU rotation
│   ├── youtube_publisher.py       # YouTube Data API v3 — upload as Short
│   ├── youtube_worker.py          # Async Lambda handler for YouTube Shorts
│   ├── notifier.py                # Error alerts via AWS SNS (email)
│   ├── video/                     # 🎬 Reels video generation package
│   │   ├── __init__.py            # Public API exports
│   │   ├── config.py              # Video constants & env configuration
│   │   ├── tts.py                 # TTS narration (ElevenLabs + edge-tts)
│   │   ├── footage.py             # Pexels stock video/photo fetcher
│   │   ├── effects.py             # Ken Burns, gradients, subtitle clips
│   │   ├── audio.py               # Narration + background music mixing
│   │   ├── creator.py             # Reels orchestrator
│   │   └── event_card.py          # 📅 PIL 1080×1080 events infographic generator
│   ├── fonts/                     # Bundled fonts (Montserrat Bold, OFL)
│   └── music/                     # Background music for Reels & fact Stories
├── prompts/                       # AI prompt templates (editable text files)
│   ├── batch_selection.txt        # News selection & prioritization prompt
│   ├── single_article.txt         # Single article content generation prompt
│   ├── quality_check.txt          # Quality gate review prompt
│   └── event_selection.txt        # 📅 Weekly events selection & caption prompt
├── scripts/                       # Shell & utility scripts
│   ├── build_lambda.sh            # Build Lambda deployment ZIP
│   ├── deploy.sh                  # Full deployment script
│   ├── run_pipeline.sh            # Local pipeline runner
│   ├── preview_reels.sh           # Generate a preview Reels video locally
│   ├── aws_deploy_wizard.sh       # Interactive AWS setup wizard
│   ├── update_secrets.py          # Push .env & prompts to AWS Secrets Manager
│   └── get_new_instagram_token.py # Emergency: exchange short-lived token for 60-day token
├── requirements/                  # Python dependencies
│   ├── base.txt                   # Development dependencies
│   └── lambda.txt                 # Lambda runtime dependencies
├── infrastructure/
│   └── terraform/                 # AWS infrastructure (Lambda, S3, EventBridge)
├── tests/                         # Test suite (256 tests)
├── output/                        # Generated articles & posts (gitignored)
└── errors/                        # Rejected/corrected posts log (gitignored)
```

## 📅 Posting Schedule

AWS Lambda runs via EventBridge (UTC → Amsterdam CEST = UTC+2):

| Schedule                | UTC   | Amsterdam | Instagram              | YouTube Shorts         |
|-------------------------|-------|-----------|------------------------|------------------------|
| Daily fact (daily)      | 06:00 | 08:00     | 💡 Story (Dutch fact)  | ✗ (Story only)         |
| Morning (daily)         | 09:00 | 11:00     | 🎬 Reels (news)        | ▶️ Short (same video)  |
| Afternoon (daily)       | 17:00 | 19:00     | 🎬 Reels (news)        | ▶️ Short (same video)  |
| Events (Thursday)       | 16:00 | 18:00     | 📅 Events Reels (IG + FB) | ✗ (no YouTube)         |

> The daily fact Story is gated by `ENABLE_INSTAGRAM_STORIES` (Secrets Manager). When off, nothing is generated or published.

> Note: cron times assume CEST (UTC+2). In winter (CET, UTC+1) posts run 1 hour later Amsterdam time.

### 📅 Weekly Events Post

Weekly (Thursday 18:00 Amsterdam time) the pipeline runs a separate events mode (`format: event_post`) that:
1. Scrapes upcoming NL events from **8 sources**: Eventbrite API, Ticketmaster API, amsterdam.nl, rotterdam.nl, denhaag.nl, uitagenda.nl, festileaks.nl, doedagen.nl
2. Scores each event with Claude Haiku (0–8 rubric: audience fit, completeness, public access, visual appeal)
3. Selects the best 5–12 events with Claude Opus and generates the caption
4. Generates a branded 1080×1080 PIL infographic with event listings
5. Publishes to Instagram as a feed photo post

All posts include an AI-assistance disclaimer at the bottom.

## 🛠️ Tech Stack

| Category | Technology |
|----------|-----------|
| **AI** | Claude Opus 4.6 (content) + Claude Haiku 4.5 (quality gate) |
| **TTS** | ElevenLabs Multilingual v2 (primary) + edge-tts (free fallback) |
| **Video** | moviepy, Pillow, FFmpeg (H.264 / AAC) |
| **Stock Media** | Pexels API (free — video & photo) |
| **Observability** | LangSmith + Langfuse (LLM traces, token usage, cost, latency) |
| **Cloud** | AWS Lambda, S3, EventBridge, Secrets Manager |
| **IaC** | Terraform |
| **Language** | Python 3.12 |
| **Instagram API** | Instagram Graph API v24.0 |
| **YouTube API** | YouTube Data API v3 (OAuth 2.0, resumable upload) |

## ⚙️ Configuration

All configuration is managed through environment variables (`.env` locally, AWS Secrets Manager in production). See [.env.example](.env.example) for the full template.

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key for Claude |
| `INSTAGRAM_ACCESS_TOKEN` | ✅ | Instagram Graph API long-lived token (60-day TTL, auto-refreshed) |
| `INSTAGRAM_ACCOUNT_ID` | ✅ | Instagram Business Account ID |
| `META_APP_ID` | ✅ | Meta App ID — required for automatic token refresh |
| `META_APP_SECRET` | ✅ | Meta App Secret — required for automatic token refresh |
| `ELEVENLABS_API_KEY` | ❌ | ElevenLabs TTS API key (falls back to free edge-tts) |
| `ELEVENLABS_VOICE_ID` | ❌ | ElevenLabs voice ID (default: Daniel) |
| `PEXELS_API_KEY` | ❌ | Pexels API key for stock footage (falls back to article image) |
| `EVENTBRITE_API_KEY` | ❌ | Eventbrite private token — enables Eventbrite event scraping |
| `TICKETMASTER_API_KEY` | ❌ | Ticketmaster Consumer Key — enables Ticketmaster event scraping |
| `YOUTUBE_CLIENT_ID` | ❌ | Google OAuth 2.0 client ID — enables YouTube Shorts publishing |
| `YOUTUBE_CLIENT_SECRET` | ❌ | Google OAuth 2.0 client secret |
| `YOUTUBE_REFRESH_TOKEN` | ❌ | OAuth refresh token for the YouTube channel (long-lived) |
| `ALERT_EMAIL` | ❌ | Email for Lambda error alerts — OOM, token expiry, publish failures |
| `REVIEW_MODEL` | ❌ | Quality gate model (default: `claude-haiku-4-5-20251001`) |
| `LANGCHAIN_API_KEY` | ❌ | LangSmith API key — enables LLM trace dashboard |
| `LANGCHAIN_PROJECT` | ❌ | LangSmith project name (default suggestion: `news-ai-agent`) |
| `LANGCHAIN_TRACING_V2` | ❌ | Set to `true` to activate LangSmith tracing |
| `LANGFUSE_PUBLIC_KEY` | ❌ | Langfuse public key — enables cost & quality dashboard |
| `LANGFUSE_SECRET_KEY` | ❌ | Langfuse secret key |
| `LANGFUSE_BASE_URL` | ❌ | Langfuse host (default: `https://cloud.langfuse.com`) |

## 📊 Observability

When configured, every Claude API call is traced in both dashboards:

| What's tracked | LangSmith | Langfuse |
|----------------|-----------|---------|
| Prompt input / completion output | ✅ | ✅ |
| Token usage (input + output) | ✅ | ✅ |
| Request latency | ✅ | ✅ |
| Model name | ✅ | ✅ |
| Errors | ✅ | ✅ |
| Cost estimation | — | ✅ |
| Per-call metadata (platform, source) | ✅ | ✅ |

**Setup:** Add the keys to `.env` and run `python scripts/update_secrets.py` to push them to AWS Secrets Manager. No code changes needed — both integrations activate automatically when the keys are present.

- [LangSmith dashboard](https://smith.langchain.com) — sign up → Settings → API Keys
- [Langfuse dashboard](https://cloud.langfuse.com) — sign up → Settings → API Keys

## 🧪 Testing

```bash
# Run all tests
make test
# or
pytest tests/ -v

# 256 tests covering scraper, AI agent, quality gate, video pipeline,
# social publisher, YouTube publisher/worker, event scraper, event card,
# notifier, token refresher, lambda handler (incl. YouTube isolation test)
```

## 📝 License

MIT License — See [LICENSE](LICENSE) for details.