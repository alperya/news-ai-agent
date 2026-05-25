# 🤖 News AI Agent — Dutch News Automation

An intelligent AI-powered pipeline that automatically scrapes Dutch news from NOS and RTL Nieuws, processes them with Claude AI, and publishes engaging content to Instagram — both as photo posts and auto-generated Reels videos with TTS narration.

## 📦 Features

- ✅ Multi-source RSS scraping (NOS, RTL Nieuws)
- ✅ Claude Opus 4.6 AI content generation with Holland-priority news selection
- ✅ Quality gate: structural validation + AI language review (Claude Haiku 4.5) before publishing
- ✅ Instagram Graph API integration with automatic token refresh
- ✅ **Instagram Reels** — auto-generated news videos with TTS narration, stock footage & subtitles
- ✅ Tiered news selection (🇳🇱 Holland → 🇪🇺 Europe → 🌍 Global)
- ✅ Source attribution with article links in posts
- ✅ Duplicate detection (prevents reposting same articles)
- ✅ AWS Lambda deployment with scheduled posting (3× daily)
- ✅ Infrastructure as Code (Terraform)
- ✅ AWS Secrets Manager for credential management
- ✅ Email alerts on errors (OOM, token expiry, publish failures) via AWS SNS
- ✅ Objective, news-agency style content (BBC/Reuters style)
- ✅ **LangSmith + Langfuse** observability — LLM traces, token usage, latency & cost dashboard

## 🎬 Reels Video Pipeline

The afternoon posting slot automatically generates an Instagram Reels video:

1. **TTS Narration** — ElevenLabs Multilingual v2 (natural voice) with edge-tts fallback (free)
2. **Stock Footage** — Pexels API auto-selects relevant HD clips based on article keywords
3. **Subtitles** — Word-timed subtitle overlay with rounded orange background
4. **Background Music** — Ambient news music with 1-second fade-out
5. **4-tier Visual Fallback** — Pexels video → article image → Pexels photo → animated gradient

Video specs: 1080×1920 (9:16), 30 FPS, H.264 High, 4000 kbps, AAC audio, 9 stock clips per video.

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
├── lambda_handler.py              # AWS Lambda entry point
├── src/                           # Application source code
│   ├── main.py                    # Local pipeline runner (CLI)
│   ├── ai_agent.py                # Claude AI content processing
│   ├── news_scraper.py            # RSS scraping (NOS, RTL Nieuws)
│   ├── social_publisher.py        # Instagram publishing (photo + Reels)
│   ├── token_manager.py           # Instagram token refresh management
│   ├── notifier.py                # Error alerts via AWS SNS (email)
│   ├── video/                     # 🎬 Reels video generation package
│   │   ├── __init__.py            # Public API exports
│   │   ├── config.py              # Video constants & env configuration
│   │   ├── tts.py                 # TTS narration (ElevenLabs + edge-tts)
│   │   ├── footage.py             # Pexels stock video/photo fetcher
│   │   ├── effects.py             # Ken Burns, gradients, subtitle clips
│   │   ├── audio.py               # Narration + background music mixing
│   │   └── creator.py             # Main orchestrator
│   ├── fonts/                     # Bundled fonts (Montserrat Bold, OFL)
│   └── music/                     # Background music for Reels
├── prompts/                       # AI prompt templates (editable text files)
│   ├── batch_selection.txt        # News selection & prioritization prompt
│   ├── single_article.txt         # Single article content generation prompt
│   └── quality_check.txt          # Quality gate review prompt
├── scripts/                       # Shell & utility scripts
│   ├── build_lambda.sh            # Build Lambda deployment ZIP
│   ├── deploy.sh                  # Full deployment script
│   ├── run_pipeline.sh            # Local pipeline runner
│   ├── preview_reels.sh           # Generate a preview Reels video locally
│   ├── aws_deploy_wizard.sh       # Interactive AWS setup wizard
│   └── update_secrets.py          # Push .env & prompts to AWS Secrets Manager
├── requirements/                  # Python dependencies
│   ├── base.txt                   # Development dependencies
│   └── lambda.txt                 # Lambda runtime dependencies
├── infrastructure/
│   └── terraform/                 # AWS infrastructure (Lambda, S3, EventBridge)
├── tests/                         # Test suite (35 tests)
├── output/                        # Generated articles & posts (gitignored)
└── errors/                        # Rejected/corrected posts log (gitignored)
```

## 📅 Posting Schedule

AWS Lambda runs 3× daily via EventBridge (UTC → Amsterdam CET/CEST):

| Schedule  | UTC   | Amsterdam | Format         |
|-----------|-------|-----------|----------------|
| Morning   | 07:00 | 08:00     | 📷 Photo post  |
| Afternoon | 11:30 | 12:30     | 🎬 Reels video |
| Evening   | 16:30 | 17:30     | 📷 Photo post  |

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
| **API** | Instagram Graph API v24.0 |

## ⚙️ Configuration

All configuration is managed through environment variables (`.env` locally, AWS Secrets Manager in production). See [.env.example](.env.example) for the full template.

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | ✅ | Anthropic API key for Claude |
| `INSTAGRAM_ACCESS_TOKEN` | ✅ | Instagram Graph API long-lived token |
| `INSTAGRAM_ACCOUNT_ID` | ✅ | Instagram Business Account ID |
| `ELEVENLABS_API_KEY` | ❌ | ElevenLabs TTS API key (falls back to free edge-tts) |
| `ELEVENLABS_VOICE_ID` | ❌ | ElevenLabs voice ID (default: Daniel) |
| `PEXELS_API_KEY` | ❌ | Pexels API key for stock footage (falls back to article image) |
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

# 35 tests covering scraper, AI agent, quality gate, video pipeline, social publisher
```

## 📝 License

MIT License — See [LICENSE](LICENSE) for details.