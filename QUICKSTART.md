# 🚀 Quick Start Guide

## Installation

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
nano .env  # Add your API keys

# 3. Test scraper
python -c "from news_scraper import DutchNewsScraper; scraper = DutchNewsScraper(); print(scraper.scrape_source('nos', 'general')[:2])"

# 4. Run full pipeline
python main.py --dry-run --max-posts 3
```

## API Keys Needed

- Anthropic API Key: https://console.anthropic.com/
- Twitter Developer Account: https://developer.twitter.com/

## Common Commands

```bash
# Test scraper only
python news_scraper.py

# Test AI agent only  
python ai_agent.py

# Full pipeline (safe mode)
python main.py --dry-run

# Deploy to AWS
./deploy.sh
```
