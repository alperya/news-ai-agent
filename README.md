# 🤖 News AI Agent - Dutch News Automation

An intelligent AI-powered pipeline that automatically scrapes Dutch news from NOS, NU.nl, and Telegraaf, processes them with Claude AI, and publishes to social media.

## 🚀 Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env with your keys

# Run pipeline (dry-run mode)
python main.py --dry-run

# Run with actual posting
python main.py --no-dry-run --max-posts 3
```

## 📦 Features

- ✅ Multi-source RSS scraping (NOS, NU.nl, Telegraaf)
- ✅ Claude AI content generation
- ✅ Twitter/X integration
- ✅ AWS Lambda deployment ready
- ✅ Docker support
- ✅ Infrastructure as Code (Terraform)

## 📖 Documentation

- See `QUICKSTART.md` for 10-minute setup guide
- See `LEARNING_GUIDE.md` for educational content

## 🛠️ Tech Stack

- **AI**: Claude Sonnet 4 (Anthropic)
- **Cloud**: AWS Lambda, S3, EventBridge, Secrets Manager
- **IaC**: Terraform
- **Container**: Docker
- **Language**: Python 3.11+

## 📝 License

MIT License - See LICENSE file for details

## 🎉 Status: Fully Working!

Successfully scraping Dutch news from NOS, NU.nl, and Telegraaf, 
processing with Claude AI, and generating social media content.