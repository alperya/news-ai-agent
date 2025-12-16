#!/bin/bash

# News AI Agent - Advanced Files Setup (pip3 version)

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}News AI Agent - Advanced Files (pip3)${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""

if [ ! -f "news_scraper.py" ]; then
    echo -e "${YELLOW}Error: Run from news-ai-agent directory${NC}"
    exit 1
fi

echo -e "${GREEN}[1/7] Creating main.py and social_publisher.py...${NC}"

cat > main.py << 'EOF'
"""Main Pipeline"""
import logging
import argparse
import json
from datetime import datetime
from pathlib import Path
from news_scraper import DutchNewsScraper, save_articles_json
from ai_agent import NewsAIAgent, save_posts_json

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class NewsAIPipeline:
    def __init__(self, config: dict = None):
        self.config = config or {'output_dir': 'output', 'max_articles_per_source': 2, 'max_posts': 5, 'dry_run': True}
        self.scraper = DutchNewsScraper()
        self.ai_agent = NewsAIAgent()
        self.output_dir = Path(self.config['output_dir'])
        self.output_dir.mkdir(exist_ok=True)
    
    def run(self, dry_run: bool = None):
        dry_run = dry_run if dry_run is not None else self.config['dry_run']
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        logger.info("="*60)
        logger.info("🚀 Starting Pipeline")
        logger.info("="*60)
        
        results = {'timestamp': timestamp, 'stages': {}}
        
        try:
            logger.info("\n📰 STAGE 1: Scraping...")
            articles = self.scraper.scrape_all_sources(self.config['max_articles_per_source'])
            save_articles_json(articles, str(self.output_dir / f'articles_{timestamp}.json'))
            results['stages']['scraping'] = {'success': True, 'count': len(articles)}
            logger.info(f"Scraped {len(articles)} articles")
            
            logger.info("\n🤖 STAGE 2: AI Processing...")
            posts = self.ai_agent.process_batch([a.to_dict() for a in articles], self.config['max_posts'])
            save_posts_json(posts, str(self.output_dir / f'posts_{timestamp}.json'))
            results['stages']['ai_processing'] = {'success': True, 'count': len(posts)}
            logger.info(f"Generated {len(posts)} posts")
            
            if posts:
                logger.info("\nSample post:")
                logger.info("-" * 60)
                logger.info(posts[0].format_post())
                logger.info("-" * 60)
            
            logger.info("\n✅ Pipeline Complete!")
            return results
        except Exception as e:
            logger.error(f"❌ Failed: {str(e)}")
            return results

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--no-dry-run', action='store_true')
    parser.add_argument('--max-posts', type=int, default=5)
    args = parser.parse_args()
    
    config = {'output_dir': 'output', 'max_articles_per_source': 2, 'max_posts': args.max_posts, 'dry_run': not args.no_dry_run}
    pipeline = NewsAIPipeline(config)
    pipeline.run()

if __name__ == "__main__":
    main()
EOF

cat > social_publisher.py << 'EOF'
"""Social Media Publisher"""
import tweepy
import os
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class TwitterPublisher:
    def __init__(self):
        self.api_key = os.getenv('TWITTER_API_KEY')
        self.api_secret = os.getenv('TWITTER_API_SECRET')
        self.access_token = os.getenv('TWITTER_ACCESS_TOKEN')
        self.access_token_secret = os.getenv('TWITTER_ACCESS_TOKEN_SECRET')
        
        if not all([self.api_key, self.api_secret, self.access_token, self.access_token_secret]):
            raise ValueError("Missing Twitter credentials")
        
        self.client = tweepy.Client(
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            access_token=self.access_token,
            access_token_secret=self.access_token_secret
        )
    
    def publish_post(self, content: str, dry_run: bool = False):
        if dry_run:
            logger.info(f"[DRY RUN] Would post:\n{content}")
            return {'id': 'dry_run', 'text': content}
        
        response = self.client.create_tweet(text=content)
        logger.info(f"✅ Posted: {response.data['id']}")
        return response.data
EOF

echo -e "${GREEN}✓ Main files created${NC}"

echo -e "${GREEN}[2/7] Creating lambda_handler.py...${NC}"

cat > lambda_handler.py << 'EOF'
"""AWS Lambda Handler"""
import json
import logging
import boto3
from main import NewsAIPipeline

logger = logging.getLogger()
logger.setLevel(logging.INFO)

def lambda_handler(event, context):
    try:
        config = {
            'output_dir': '/tmp/output',
            'max_articles_per_source': 2,
            'max_posts': event.get('max_posts', 5),
            'dry_run': event.get('dry_run', True)
        }
        
        pipeline = NewsAIPipeline(config)
        results = pipeline.run()
        
        return {
            'statusCode': 200,
            'body': json.dumps({'message': 'Success', 'results': results})
        }
    except Exception as e:
        logger.error(f"Failed: {str(e)}")
        return {'statusCode': 500, 'body': json.dumps({'error': str(e)})}
EOF

echo -e "${GREEN}[3/7] Creating Makefile...${NC}"

cat > Makefile << 'EOF'
.PHONY: help install test run deploy clean

help:
	@echo "Available commands:"
	@echo "  make install    - Install with pip3"
	@echo "  make test       - Run tests"
	@echo "  make run        - Run pipeline (dry-run)"
	@echo "  make run-live   - Run with posting"
	@echo "  make clean      - Clean files"

install:
	python3 -m pip install --upgrade pip
	pip3 install -r requirements.txt
	@echo "✅ Installed"

test:
	pytest tests/ -v

run:
	@echo "🚀 Running (DRY RUN)..."
	python3 main.py --dry-run --max-posts 5

run-live:
	@echo "⚠️  WARNING: Will post to social media!"
	@read line
	python3 main.py --no-dry-run --max-posts 3

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	@echo "🧹 Cleaned"

deploy:
	@chmod +x deploy.sh
	@./deploy.sh
EOF

echo -e "${GREEN}[4/7] Creating deploy.sh...${NC}"

cat > deploy.sh << 'EOF'
#!/bin/bash
set -e

GREEN='\033[0;32m'
NC='\033[0m'

echo -e "${GREEN}Deploying to AWS...${NC}"

AWS_REGION=${AWS_REGION:-"eu-west-1"}
PROJECT_NAME="news-ai-agent"

check_deps() {
    command -v aws >/dev/null || { echo "AWS CLI required"; exit 1; }
    command -v docker >/dev/null || { echo "Docker required"; exit 1; }
}

get_account() {
    aws sts get-caller-identity --query Account --output text
}

create_ecr() {
    ACCOUNT_ID=$(get_account)
    ECR="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT_NAME}"
    
    aws ecr describe-repositories --repository-names ${PROJECT_NAME} --region ${AWS_REGION} 2>/dev/null || \
        aws ecr create-repository --repository-name ${PROJECT_NAME} --region ${AWS_REGION}
    
    echo ${ECR}
}

build_push() {
    ECR=$1
    aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${ECR%/*}
    docker build -t ${PROJECT_NAME}:latest .
    docker tag ${PROJECT_NAME}:latest ${ECR}:latest
    docker push ${ECR}:latest
}

check_deps
ECR=$(create_ecr)
build_push ${ECR}

echo -e "${GREEN}✅ Deployed!${NC}"
EOF

chmod +x deploy.sh

echo -e "${GREEN}[5/7] Creating Terraform...${NC}"

cat > infrastructure/terraform/main.tf << 'EOF'
terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

provider "aws" {
  region = "eu-west-1"
}

resource "aws_s3_bucket" "results" {
  bucket = "news-ai-agent-results-${data.aws_caller_identity.current.account_id}"
}

resource "aws_secretsmanager_secret" "creds" {
  name = "news-ai-agent/credentials"
}

data "aws_caller_identity" "current" {}

output "bucket" { value = aws_s3_bucket.results.id }
EOF

echo -e "${GREEN}[6/7] Creating docker-compose.yml...${NC}"

cat > docker-compose.yml << 'EOF'
version: '3.8'
services:
  news-agent:
    build:
      context: .
      dockerfile: Dockerfile.dev
    volumes:
      - ./:/app
      - ./output:/app/output
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
    command: python3 main.py --dry-run
EOF

cat > Dockerfile.dev << 'EOF'
FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y gcc && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python3", "main.py", "--dry-run"]
EOF

echo -e "${GREEN}[7/7] Creating tests...${NC}"

cat > tests/test_scraper.py << 'EOF'
"""Tests"""
import pytest
from news_scraper import DutchNewsScraper

@pytest.fixture
def scraper():
    return DutchNewsScraper()

def test_init(scraper):
    assert scraper is not None
    assert 'nos' in scraper.RSS_FEEDS

def test_feeds(scraper):
    for source, cats in scraper.RSS_FEEDS.items():
        assert len(cats) > 0
EOF

echo ""
echo -e "${GREEN}✅ Advanced Setup Complete!${NC}"
echo ""
echo "Try: make help"
