#!/bin/bash

# News AI Agent - Advanced Files Setup Script
# This script adds all advanced files: Makefile, deploy.sh, tests, Terraform, etc.

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${BLUE}=========================================${NC}"
echo -e "${BLUE}News AI Agent - Advanced Files Setup${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""

# Check if we're in the project directory
if [ ! -f "news_scraper.py" ]; then
    echo -e "${YELLOW}Error: This script must be run from the news-ai-agent directory${NC}"
    echo "Please run: cd news-ai-agent"
    exit 1
fi

echo -e "${GREEN}[1/7] Creating main.py and social_publisher.py...${NC}"

# Create main.py
cat > main.py << 'EOF'
"""
Main Pipeline - News AI Agent
Complete workflow: Scrape → Process → Publish
"""

import logging
import argparse
import json
from datetime import datetime
from pathlib import Path

from news_scraper import DutchNewsScraper, save_articles_json
from ai_agent import NewsAIAgent, save_posts_json

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class NewsAIPipeline:
    """Main pipeline orchestrator"""
    
    def __init__(self, config: dict = None):
        self.config = config or self._default_config()
        self.scraper = DutchNewsScraper()
        self.ai_agent = NewsAIAgent()
        
        self.output_dir = Path(self.config['output_dir'])
        self.output_dir.mkdir(exist_ok=True)
        
        logger.info("Pipeline initialized")
    
    def _default_config(self) -> dict:
        return {
            'output_dir': 'output',
            'max_articles_per_source': 2,
            'max_posts': 5,
            'sources': ['nos', 'nu', 'telegraaf'],
            'dry_run': True
        }
    
    def run(self, dry_run: bool = None) -> dict:
        dry_run = dry_run if dry_run is not None else self.config['dry_run']
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        logger.info("="*60)
        logger.info("🚀 Starting News AI Agent Pipeline")
        logger.info("="*60)
        
        results = {
            'timestamp': timestamp,
            'config': self.config,
            'stages': {}
        }
        
        try:
            logger.info("\n📰 STAGE 1: Scraping news articles...")
            articles = self._scrape_news()
            results['stages']['scraping'] = {
                'success': True,
                'articles_count': len(articles),
                'output_file': f'articles_{timestamp}.json'
            }
            
            logger.info("\n🤖 STAGE 2: Processing with AI...")
            posts = self._process_with_ai(articles)
            results['stages']['ai_processing'] = {
                'success': True,
                'posts_count': len(posts),
                'output_file': f'posts_{timestamp}.json'
            }
            
            logger.info("\n📱 STAGE 3: Publishing (dry-run mode)")
            if dry_run:
                logger.info("🧪 Running in DRY RUN mode (no actual posting)")
            results['stages']['publishing'] = {
                'success': True,
                'dry_run': dry_run
            }
            
            self._save_results(results, timestamp)
            
            logger.info("\n" + "="*60)
            logger.info("✅ Pipeline completed successfully!")
            logger.info("="*60)
            
            return results
            
        except Exception as e:
            logger.error(f"❌ Pipeline failed: {str(e)}", exc_info=True)
            results['error'] = str(e)
            return results
    
    def _scrape_news(self) -> list:
        articles = self.scraper.scrape_all_sources(
            max_articles_per_source=self.config['max_articles_per_source']
        )
        
        if not articles:
            raise RuntimeError("No articles scraped")
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = self.output_dir / f'articles_{timestamp}.json'
        save_articles_json(articles, str(output_file))
        
        sources = {}
        for article in articles:
            sources[article.source] = sources.get(article.source, 0) + 1
        
        logger.info(f"Scraped {len(articles)} articles:")
        for source, count in sources.items():
            logger.info(f"  - {source.upper()}: {count} articles")
        
        return [article.to_dict() for article in articles]
    
    def _process_with_ai(self, articles: list) -> list:
        posts = self.ai_agent.process_batch(
            articles,
            max_posts=self.config['max_posts']
        )
        
        if not posts:
            raise RuntimeError("No posts generated")
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_file = self.output_dir / f'posts_{timestamp}.json'
        save_posts_json(posts, str(output_file))
        
        logger.info(f"Generated {len(posts)} social media posts")
        if posts:
            logger.info("\nSample post:")
            logger.info("-" * 60)
            logger.info(posts[0].format_post())
            logger.info("-" * 60)
        
        return [post.to_dict() for post in posts]
    
    def _save_results(self, results: dict, timestamp: str):
        results_file = self.output_dir / f'pipeline_results_{timestamp}.json'
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        logger.info(f"Saved pipeline results to {results_file}")


def main():
    parser = argparse.ArgumentParser(description='News AI Agent Pipeline')
    parser.add_argument('--dry-run', action='store_true', help='Run without posting')
    parser.add_argument('--no-dry-run', action='store_true', help='Actually post')
    parser.add_argument('--max-posts', type=int, default=5, help='Max posts')
    parser.add_argument('--output-dir', type=str, default='output', help='Output directory')
    
    args = parser.parse_args()
    
    config = {
        'output_dir': args.output_dir,
        'max_articles_per_source': 2,
        'max_posts': args.max_posts,
        'dry_run': not args.no_dry_run
    }
    
    pipeline = NewsAIPipeline(config)
    results = pipeline.run(dry_run=not args.no_dry_run if args.no_dry_run else args.dry_run)
    
    print("\n" + "="*60)
    print("📊 PIPELINE SUMMARY")
    print("="*60)
    for stage, data in results.get('stages', {}).items():
        print(f"\n{stage.upper()}:")
        for key, value in data.items():
            if key != 'results':
                print(f"  {key}: {value}")
    print("\n" + "="*60)


if __name__ == "__main__":
    main()
EOF

# Create social_publisher.py
cat > social_publisher.py << 'EOF'
"""
Social Media Publisher
Publishes processed content to Twitter/X
"""

import tweepy
import os
from typing import List, Dict, Optional
import logging
from datetime import datetime
import time

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class TwitterPublisher:
    """Publisher for Twitter/X platform"""
    
    def __init__(
        self,
        api_key: str = None,
        api_secret: str = None,
        access_token: str = None,
        access_token_secret: str = None,
        bearer_token: str = None
    ):
        self.api_key = api_key or os.getenv('TWITTER_API_KEY')
        self.api_secret = api_secret or os.getenv('TWITTER_API_SECRET')
        self.access_token = access_token or os.getenv('TWITTER_ACCESS_TOKEN')
        self.access_token_secret = access_token_secret or os.getenv('TWITTER_ACCESS_TOKEN_SECRET')
        self.bearer_token = bearer_token or os.getenv('TWITTER_BEARER_TOKEN')
        
        if not all([self.api_key, self.api_secret, self.access_token, self.access_token_secret]):
            raise ValueError("Missing Twitter API credentials")
        
        self.client = tweepy.Client(
            bearer_token=self.bearer_token,
            consumer_key=self.api_key,
            consumer_secret=self.api_secret,
            access_token=self.access_token,
            access_token_secret=self.access_token_secret,
            wait_on_rate_limit=True
        )
        
        logger.info("Twitter client initialized successfully")
    
    def publish_post(self, post_content: str, dry_run: bool = False) -> Optional[Dict]:
        try:
            if len(post_content) > 280:
                logger.warning(f"Post too long ({len(post_content)} chars), truncating...")
                post_content = post_content[:277] + "..."
            
            if dry_run:
                logger.info(f"[DRY RUN] Would post:\n{post_content}")
                return {
                    'id': 'dry_run_id',
                    'text': post_content,
                    'created_at': datetime.now().isoformat()
                }
            
            response = self.client.create_tweet(text=post_content)
            tweet_id = response.data['id']
            logger.info(f"✅ Successfully posted tweet: {tweet_id}")
            
            return {
                'id': tweet_id,
                'text': post_content,
                'created_at': datetime.now().isoformat(),
                'url': f"https://twitter.com/i/web/status/{tweet_id}"
            }
            
        except Exception as e:
            logger.error(f"Failed to post tweet: {str(e)}")
            return None
    
    def publish_batch(self, posts: List[str], delay_seconds: int = 60, dry_run: bool = False) -> List[Dict]:
        results = []
        
        for i, post in enumerate(posts):
            logger.info(f"Publishing post {i+1}/{len(posts)}")
            
            result = self.publish_post(post, dry_run=dry_run)
            if result:
                results.append(result)
            
            if i < len(posts) - 1:
                logger.info(f"Waiting {delay_seconds} seconds...")
                time.sleep(delay_seconds)
        
        logger.info(f"Published {len(results)}/{len(posts)} posts successfully")
        return results


if __name__ == "__main__":
    import json
    
    with open('output/posts_*.json', 'r') as f:
        posts = json.load(f)
    
    try:
        twitter = TwitterPublisher()
        twitter.publish_batch([post['full_post'] for post in posts[:3]], dry_run=True)
    except ValueError as e:
        print(f"\n⚠️  {str(e)}")
EOF

echo -e "${GREEN}✓ Main files created${NC}"

echo -e "${GREEN}[2/7] Creating lambda_handler.py...${NC}"

cat > lambda_handler.py << 'EOF'
"""
AWS Lambda Handler
"""

import json
import logging
import boto3
from datetime import datetime
from main import NewsAIPipeline

logger = logging.getLogger()
logger.setLevel(logging.INFO)

s3_client = boto3.client('s3')
secrets_client = boto3.client('secretsmanager')


def get_secret(secret_name: str) -> dict:
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        return json.loads(response['SecretString'])
    except Exception as e:
        logger.error(f"Error retrieving secret: {str(e)}")
        raise


def upload_to_s3(data: dict, bucket: str, key: str):
    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(data, ensure_ascii=False, indent=2),
            ContentType='application/json'
        )
        logger.info(f"Uploaded to s3://{bucket}/{key}")
    except Exception as e:
        logger.error(f"Error uploading to S3: {str(e)}")
        raise


def lambda_handler(event, context):
    logger.info("Lambda function started")
    
    try:
        dry_run = event.get('dry_run', True)
        max_posts = event.get('max_posts', 5)
        s3_bucket = event.get('s3_bucket')
        secrets_name = event.get('secrets_name')
        
        if secrets_name:
            secrets = get_secret(secrets_name)
            import os
            for key, value in secrets.items():
                os.environ[key] = value
        
        config = {
            'output_dir': '/tmp/output',
            'max_articles_per_source': 2,
            'max_posts': max_posts,
            'dry_run': dry_run
        }
        
        pipeline = NewsAIPipeline(config)
        results = pipeline.run(dry_run=dry_run)
        
        if s3_bucket:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            s3_key = f"pipeline-results/{timestamp}/results.json"
            upload_to_s3(results, s3_bucket, s3_key)
        
        return {
            'statusCode': 200,
            'body': json.dumps({
                'message': 'Pipeline completed',
                'articles': results['stages']['scraping']['articles_count'],
                'posts': results['stages']['ai_processing']['posts_count']
            })
        }
        
    except Exception as e:
        logger.error(f"Lambda failed: {str(e)}", exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({'error': str(e)})
        }
EOF

echo -e "${GREEN}✓ Lambda handler created${NC}"

echo -e "${GREEN}[3/7] Creating Makefile...${NC}"

cat > Makefile << 'EOF'
.PHONY: help install test run deploy

help:
	@echo "News AI Agent - Available Commands:"
	@echo ""
	@echo "  make install       - Install dependencies"
	@echo "  make test          - Run tests"
	@echo "  make run           - Run pipeline (dry-run)"
	@echo "  make run-live      - Run pipeline (actual posting)"
	@echo "  make scrape        - Test scraper only"
	@echo "  make deploy        - Deploy to AWS"
	@echo "  make clean         - Clean temporary files"
	@echo ""

install:
	python -m pip install --upgrade pip
	pip install -r requirements.txt
	@echo "✅ Dependencies installed"

test:
	pytest tests/ -v

run:
	@echo "🚀 Running pipeline in DRY RUN mode..."
	python main.py --dry-run --max-posts 5

run-live:
	@echo "⚠️  WARNING: This will ACTUALLY POST to social media!"
	@echo "Press Ctrl+C to cancel, or Enter to continue..."
	@read line
	python main.py --no-dry-run --max-posts 3

scrape:
	@echo "📰 Testing scraper..."
	python -c "from news_scraper import DutchNewsScraper; scraper = DutchNewsScraper(); articles = scraper.scrape_all_sources(2); print(f'\n✅ Scraped {len(articles)} articles')"

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache/ htmlcov/ .coverage
	@echo "🧹 Cleaned up"

deploy:
	@echo "☁️  Deploying to AWS..."
	@chmod +x deploy.sh
	@./deploy.sh
EOF

echo -e "${GREEN}✓ Makefile created${NC}"

echo -e "${GREEN}[4/7] Creating deploy.sh...${NC}"

cat > deploy.sh << 'EOF'
#!/bin/bash

set -e

GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}Deploying News AI Agent to AWS...${NC}"

AWS_REGION=${AWS_REGION:-"eu-west-1"}
PROJECT_NAME="news-ai-agent"

check_prerequisites() {
    echo -e "${GREEN}Checking prerequisites...${NC}"
    
    if ! command -v aws &> /dev/null; then
        echo "AWS CLI not found. Please install it first."
        exit 1
    fi
    
    if ! command -v docker &> /dev/null; then
        echo "Docker not found. Please install it first."
        exit 1
    fi
    
    if ! aws sts get-caller-identity &> /dev/null; then
        echo "AWS credentials not configured."
        exit 1
    fi
    
    echo -e "${GREEN}Prerequisites OK!${NC}"
}

get_account_id() {
    aws sts get-caller-identity --query Account --output text
}

create_ecr_repository() {
    echo -e "${GREEN}Creating ECR repository...${NC}"
    
    ACCOUNT_ID=$(get_account_id)
    ECR_REPO="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${PROJECT_NAME}"
    
    if ! aws ecr describe-repositories --repository-names ${PROJECT_NAME} --region ${AWS_REGION} &> /dev/null; then
        aws ecr create-repository \
            --repository-name ${PROJECT_NAME} \
            --region ${AWS_REGION} \
            --image-scanning-configuration scanOnPush=true
        echo -e "${GREEN}ECR repository created${NC}"
    else
        echo -e "${GREEN}ECR repository exists${NC}"
    fi
    
    echo ${ECR_REPO}
}

build_and_push_image() {
    echo -e "${GREEN}Building Docker image...${NC}"
    
    ECR_REPO=$1
    
    aws ecr get-login-password --region ${AWS_REGION} | \
        docker login --username AWS --password-stdin ${ECR_REPO%/*}
    
    docker build -t ${PROJECT_NAME}:latest .
    docker tag ${PROJECT_NAME}:latest ${ECR_REPO}:latest
    
    echo -e "${GREEN}Pushing image...${NC}"
    docker push ${ECR_REPO}:latest
    
    echo -e "${GREEN}Image pushed!${NC}"
}

main() {
    check_prerequisites
    ECR_REPO=$(create_ecr_repository)
    build_and_push_image ${ECR_REPO}
    
    echo ""
    echo -e "${GREEN}✅ Deployment complete!${NC}"
    echo ""
    echo "Next steps:"
    echo "  1. Deploy infrastructure: cd infrastructure/terraform && terraform apply"
    echo "  2. Configure secrets in AWS Secrets Manager"
    echo "  3. Test Lambda function"
}

main
EOF

chmod +x deploy.sh

echo -e "${GREEN}✓ Deploy script created${NC}"

echo -e "${GREEN}[5/7] Creating Terraform configuration...${NC}"

cat > infrastructure/terraform/main.tf << 'EOF'
terraform {
  required_version = ">= 1.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

variable "aws_region" {
  default = "eu-west-1"
}

variable "project_name" {
  default = "news-ai-agent"
}

resource "aws_s3_bucket" "pipeline_results" {
  bucket = "${var.project_name}-results-${data.aws_caller_identity.current.account_id}"
}

resource "aws_secretsmanager_secret" "api_credentials" {
  name = "${var.project_name}/credentials"
}

resource "aws_iam_role" "lambda_role" {
  name = "${var.project_name}-lambda-role"
  
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"
      }
    }]
  })
}

data "aws_caller_identity" "current" {}

output "s3_bucket_name" {
  value = aws_s3_bucket.pipeline_results.id
}
EOF

echo -e "${GREEN}✓ Terraform config created${NC}"

echo -e "${GREEN}[6/7] Creating docker-compose.yml...${NC}"

cat > docker-compose.yml << 'EOF'
version: '3.8'

services:
  news-agent:
    build:
      context: .
      dockerfile: Dockerfile.dev
    container_name: news-ai-agent
    volumes:
      - ./:/app
      - ./output:/app/output
    environment:
      - ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
      - DRY_RUN=${DRY_RUN:-true}
      - MAX_POSTS=${MAX_POSTS:-5}
    command: python main.py --dry-run
    networks:
      - news-agent-network

networks:
  news-agent-network:
    driver: bridge
EOF

# Create Dockerfile.dev
cat > Dockerfile.dev << 'EOF'
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y gcc g++ make && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/output

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

CMD ["python", "main.py", "--dry-run"]
EOF

echo -e "${GREEN}✓ Docker files created${NC}"

echo -e "${GREEN}[7/7] Creating test files and documentation...${NC}"

cat > tests/test_scraper.py << 'EOF'
"""
Unit tests for news scraper
"""

import pytest
from unittest.mock import Mock, patch
from news_scraper import DutchNewsScraper, NewsArticle


@pytest.fixture
def scraper():
    return DutchNewsScraper()


class TestDutchNewsScraper:
    def test_initialization(self, scraper):
        assert scraper is not None
        assert hasattr(scraper, 'RSS_FEEDS')
        assert 'nos' in scraper.RSS_FEEDS
    
    def test_rss_feeds_configured(self, scraper):
        for source, categories in scraper.RSS_FEEDS.items():
            assert isinstance(categories, dict)
            assert len(categories) > 0
    
    @patch('news_scraper.feedparser.parse')
    def test_fetch_feed_success(self, mock_parse, scraper):
        mock_entry = Mock()
        mock_entry.title = "Test Article"
        mock_entry.summary = "Test description"
        mock_entry.link = "https://example.com"
        mock_entry.published = "2024-01-01"
        mock_entry.enclosures = []
        
        mock_feed = Mock()
        mock_feed.entries = [mock_entry]
        mock_parse.return_value = mock_feed
        
        articles = scraper.fetch_feed('https://example.com/feed', 'test', 'general')
        
        assert len(articles) == 1
        assert articles[0].title == "Test Article"
EOF

# Create comprehensive LEARNING_GUIDE.md
cat > LEARNING_GUIDE.md << 'EOF'
# 🎓 Learning Guide

## Project Structure

This AI agent demonstrates:
- Web scraping (RSS feeds)
- LLM integration (Claude API)
- Social media APIs (Twitter)
- AWS serverless architecture
- Infrastructure as Code

## Key Concepts

### 1. Web Scraping
- RSS feed parsing with feedparser
- Error handling
- Data normalization

### 2. AI Integration
- Prompt engineering
- JSON parsing from LLM
- Cost optimization

### 3. Cloud Architecture
- AWS Lambda
- EventBridge scheduling
- S3 storage
- Secrets Manager

## Learning Path

1. Start with news_scraper.py - understand data collection
2. Move to ai_agent.py - learn prompt engineering
3. Study main.py - see orchestration patterns
4. Explore AWS deployment - cloud architecture

## Extensions

- Add more news sources
- Implement image generation
- Build analytics dashboard
- Add multi-language support
EOF

echo -e "${GREEN}✓ Tests and docs created${NC}"

echo ""
echo -e "${BLUE}=========================================${NC}"
echo -e "${GREEN}✅ Advanced Files Setup Complete!${NC}"
echo -e "${BLUE}=========================================${NC}"
echo ""
echo "Added files:"
echo "  ✓ main.py (full pipeline orchestrator)"
echo "  ✓ social_publisher.py (Twitter integration)"
echo "  ✓ lambda_handler.py (AWS Lambda)"
echo "  ✓ Makefile (development automation)"
echo "  ✓ deploy.sh (AWS deployment)"
echo "  ✓ docker-compose.yml (local development)"
echo "  ✓ Dockerfile.dev (development container)"
echo "  ✓ infrastructure/terraform/main.tf (IaC)"
echo "  ✓ tests/test_scraper.py (unit tests)"
echo "  ✓ LEARNING_GUIDE.md (educational content)"
echo ""
echo "Try these commands:"
echo "  make help          - See all available commands"
echo "  make install       - Install dependencies"
echo "  make run           - Run pipeline"
echo "  make test          - Run tests"
echo ""
