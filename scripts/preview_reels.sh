#!/bin/bash
# Generate a local Reels preview video without publishing.
# Usage: ./scripts/preview_reels.sh
#
# The generated video is saved to output/preview_reels_<timestamp>.mp4
# Review it locally before enabling real publishing.

set -e

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

echo "🎬 Generating Reels preview (no publishing)..."
python -c "
import sys, os
sys.path.insert(0, 'src')
os.environ.setdefault('ANTHROPIC_API_KEY', os.environ.get('ANTHROPIC_API_KEY', ''))

from dotenv import load_dotenv
load_dotenv()

from main import NewsAIPipeline
from video import create_news_video
from datetime import datetime

# Run pipeline in dry-run mode to get a post
pipeline = NewsAIPipeline({
    'output_dir': 'output',
    'max_articles_per_source': 2,
    'max_posts': 1,
    'dry_run': True,
    'platform': 'instagram',
    'use_existing_today': True,
})

print('📰 Running pipeline to get a post...')
results = pipeline.run(dry_run=True)

# Find the latest post file
from pathlib import Path
import json

output_dir = Path('output')
post_files = sorted(output_dir.glob('posts_*.json'), key=lambda p: p.stat().st_mtime, reverse=True)

if not post_files:
    print('❌ No post files found. Run the pipeline first.')
    sys.exit(1)

with open(post_files[0], 'r', encoding='utf-8') as f:
    posts = json.load(f)

if not posts:
    print('❌ No posts in the latest file.')
    sys.exit(1)

post = posts[0]
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
output_path = f'output/preview_reels_{timestamp}.mp4'

print(f'🎬 Creating video from: {post.get(\"original_title\", \"Unknown\")}')
create_news_video(
    title=post.get('original_title', 'Haber'),
    content=post.get('full_post', post.get('content', '')),
    source=post.get('source', 'Unknown'),
    hashtags=post.get('hashtags', []),
    output_path=output_path,
    emoji=post.get('emoji', '📰'),
    image_url=post.get('image_url'),
)

print(f'')
print(f'✅ Preview video saved: {output_path}')
print(f'   Open it to review before enabling real publishing.')
"
