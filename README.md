# 🤖 News AI Agent - Dutch News Automation

An intelligent AI-powered pipeline that automatically scrapes Dutch news from NOS and NU.nl, processes them with Claude AI, and publishes engaging content to Instagram. Prioritizes Holland-related news with a tiered selection system.

## 📦 Features

- ✅ Multi-source RSS scraping (NOS, NU.nl)
- ✅ Claude Opus 4.6 AI content generation with Holland-priority news selection
- ✅ Quality gate: structural validation + AI language review (Claude Haiku 4.5) before publishing
- ✅ Instagram Graph API integration with automatic token refresh
- ✅ Tiered news selection (🇳🇱 Holland → 🇪🇺 Europe → 🌍 Global)
- ✅ Source attribution with article links in posts
- ✅ Duplicate detection (prevents reposting same articles)
- ✅ AWS Lambda deployment with scheduled posting (3x daily)
- ✅ Infrastructure as Code (Terraform)
- ✅ AWS Secrets Manager for credential management
- ✅ Objective, news-agency style content (BBC/Reuters style)

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

# Configure API keys
# Create a .env file with the following variables:
#   ANTHROPIC_API_KEY=your_key
#   INSTAGRAM_ACCESS_TOKEN=your_token
#   INSTAGRAM_ACCOUNT_ID=your_account_id
# AI prompts are stored in prompts/ folder (see prompts/*.txt.example)

# Run pipeline (dry-run mode - no actual posting)
python main.py --dry-run

# Run with actual posting
python main.py --no-dry-run --platform instagram --max-posts 1
```

### AWS Lambda Deployment

```bash
# One-step deployment (build + Terraform)
./scripts/deploy.sh

# Or step by step:
./scripts/build_lambda.sh                # Build Lambda deployment package
cd infrastructure/terraform
terraform init && terraform apply     # Deploy infrastructure
```

## 📁 Project Structure

```
├── ai_agent.py                  # Claude AI content processing
├── news_scraper.py              # RSS scraping (NOS, NU.nl)
├── social_publisher.py          # Instagram & Twitter publishing
├── token_manager.py             # Instagram token refresh management
├── main.py                      # Local pipeline runner (CLI)
├── lambda_handler.py            # AWS Lambda entry point
├── Makefile                     # Build shortcuts
├── Dockerfile                   # Container build
├── scripts/                     # Shell & utility scripts
│   ├── build_lambda.sh          # Build Lambda deployment ZIP
│   ├── deploy.sh                # Full deployment script
│   ├── run_pipeline.sh          # Local pipeline runner
│   ├── aws_deploy_wizard.sh     # Interactive AWS setup wizard
│   └── update_secrets.py        # Push .env & prompts to AWS Secrets Manager
├── requirements/                # Python dependencies
│   ├── base.txt                 # Development dependencies
│   └── lambda.txt               # Lambda runtime dependencies
├── prompts/                     # AI prompt templates (editable text files)
│   ├── batch_selection.txt      # News selection & prioritization prompt
│   ├── single_article.txt       # Single article content generation prompt
│   └── quality_check.txt        # Quality gate review prompt
├── infrastructure/
│   └── terraform/               # AWS infrastructure (Lambda, S3, EventBridge)
├── output/                      # Generated articles & posts (gitignored)
├── errors/                      # Rejected/corrected posts log (gitignored)
└── tests/                       # Test suite
```

## 📅 Posting Schedule

When deployed to AWS Lambda (EventBridge cron, UTC → Amsterdam):
| Schedule | UTC | Amsterdam (CET) |
|----------|-----|-----------------|
| Morning  | 07:00 | 08:00 |
| Afternoon | 11:30 | 12:30 |
| Evening  | 16:30 | 17:30 |

## 🛠️ Tech Stack

- **AI**: Claude Opus 4.6 (content generation) + Claude Haiku 4.5 (quality review)
- **Cloud**: AWS Lambda, S3, EventBridge, Secrets Manager
- **IaC**: Terraform
- **Language**: Python 3.12
- **API**: Instagram Graph API v24.0

## ⚙️ Configuration

All configuration is managed through environment variables (`.env` locally, AWS Secrets Manager in production):

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude |
| `INSTAGRAM_ACCESS_TOKEN` | Instagram Graph API long-lived token |
| `INSTAGRAM_ACCOUNT_ID` | Instagram Business Account ID |
| `REVIEW_MODEL` | Quality gate model (default: `claude-haiku-4-5-20251001`) |
| `AI_PROMPT_BATCH_SELECTION` | Prompt for news selection (Lambda fallback, loaded from `prompts/` locally) |
| `AI_PROMPT_SINGLE_ARTICLE` | Prompt for article processing (Lambda fallback) |
| `AI_PROMPT_QUALITY_CHECK` | Prompt for quality gate review (Lambda fallback) |

## 📝 License

MIT License - See [LICENSE](LICENSE) for details.