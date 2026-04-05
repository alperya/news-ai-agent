#!/usr/bin/env python3
"""Download latest post from S3 and publish to Instagram."""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from dotenv import load_dotenv
# override=False so .env placeholders don't clobber real ~/.aws/credentials
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'), override=False)

# Remove placeholder AWS keys that would override ~/.aws/credentials
for _k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY'):
    val = os.environ.get(_k, '')
    if val.startswith('your_'):
        del os.environ[_k]

import boto3
from social_publisher import InstagramPublisher

BUCKET = 'news-ai-agent-results-645949963620'
REGION = 'eu-central-1'


def get_latest_post():
    """Find and download the latest posts_*.json from S3."""
    s3 = boto3.client('s3', region_name=REGION)

    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix='posts_')
    if 'Contents' not in resp:
        print("❌ No posts found in S3!")
        sys.exit(1)

    # Sort by LastModified, pick newest
    objects = sorted(resp['Contents'], key=lambda o: o['LastModified'], reverse=True)
    latest_key = objects[0]['Key']
    print(f"📂 Latest post file: {latest_key}")
    print(f"   Last modified: {objects[0]['LastModified']}")

    obj = s3.get_object(Bucket=BUCKET, Key=latest_key)
    posts = json.loads(obj['Body'].read().decode('utf-8'))
    return posts, latest_key


def main():
    posts, key = get_latest_post()

    if not posts:
        print("❌ Post file is empty!")
        sys.exit(1)

    post = posts[0]  # First (and usually only) post
    full_post = post.get('full_post', '')
    image_url = post.get('image_url')

    print(f"\n📋 Post Preview:")
    print(f"   Title: {post.get('original_title', 'N/A')}")
    print(f"   Image: {image_url}")
    print(f"   Caption length: {len(full_post)} chars")
    print(f"   Platform: {post.get('platform', 'N/A')}")
    print()

    print("📤 Publishing to Instagram...")
    publisher = InstagramPublisher()
    result = publisher.publish_post(
        content=full_post,
        image_url=image_url,
        dry_run=False,
    )

    print()
    print("🎉 SUCCESS!")
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
