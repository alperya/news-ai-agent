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
