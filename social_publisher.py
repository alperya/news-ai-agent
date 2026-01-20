"""Social Media Publisher"""
import tweepy
import os
import logging
import requests
import time
from datetime import datetime
from typing import Optional

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


class InstagramPublisher:
    """Instagram Publisher using Meta Graph API"""
    
    def __init__(self):
        self.access_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
        self.instagram_account_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
        
        if not self.access_token:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN not found in environment")
        if not self.instagram_account_id:
            raise ValueError("INSTAGRAM_ACCOUNT_ID not found in environment")
        
        self.graph_api_url = "https://graph.facebook.com/v18.0"
    
    def _download_image(self, image_url: str) -> Optional[bytes]:
        """Download image from URL"""
        try:
            response = requests.get(image_url, timeout=30)
            response.raise_for_status()
            return response.content
        except Exception as e:
            logger.error(f"Error downloading image from {image_url}: {str(e)}")
            return None
    
    def _upload_image(self, image_data: bytes, caption: str) -> Optional[str]:
        """Upload image to Instagram using Graph API"""
        try:
            # Step 1: Create media container
            container_url = f"{self.graph_api_url}/{self.instagram_account_id}/media"
            container_params = {
                'image_url': '',  # We'll upload directly
                'caption': caption,
                'access_token': self.access_token
            }
            
            # For direct upload, we need to upload the image first
            # Instagram Graph API requires image to be publicly accessible
            # So we'll use a different approach: upload to a temporary location
            # or use image_url if available
            
            # Alternative: Use image_url directly if it's publicly accessible
            # For now, we'll use a simplified approach
            logger.warning("Instagram image upload requires publicly accessible URL")
            logger.info("Using caption-only post (image upload needs public URL)")
            
            return None
            
        except Exception as e:
            logger.error(f"Error uploading image: {str(e)}")
            return None
    
    def _check_container_status(self, creation_id: str, max_attempts: int = 30, delay: int = 2) -> bool:
        """Check if media container is ready for publishing"""
        status_url = f"{self.graph_api_url}/{creation_id}"
        
        for attempt in range(max_attempts):
            try:
                response = requests.get(status_url, params={'fields': 'status_code', 'access_token': self.access_token})
                response.raise_for_status()
                data = response.json()
                
                status = data.get('status_code')
                
                if status == 'FINISHED':
                    logger.info(f"✅ Media container ready (attempt {attempt + 1})")
                    return True
                elif status == 'ERROR':
                    error_msg = data.get('status', 'Unknown error')
                    raise ValueError(f"Media container error: {error_msg}")
                else:
                    # Status: IN_PROGRESS or other
                    logger.info(f"⏳ Media container processing... (attempt {attempt + 1}/{max_attempts}, status: {status})")
                    time.sleep(delay)
                    
            except requests.exceptions.HTTPError as e:
                logger.warning(f"Status check failed (attempt {attempt + 1}): {str(e)}")
                time.sleep(delay)
        
        return False
    
    def publish_post(self, content: str, image_url: Optional[str] = None, dry_run: bool = False):
        """Publish post to Instagram"""
        if dry_run:
            logger.info(f"[DRY RUN] Would post to Instagram:\n{content}")
            if image_url:
                logger.info(f"[DRY RUN] With image: {image_url}")
            return {'id': 'dry_run', 'text': content}
        
        try:
            # Instagram Graph API requires image for photo posts
            if not image_url:
                logger.warning("⚠️  Instagram posts typically require an image")
                logger.info("Creating caption-only post (may fail if image required)")
            
            # Step 1: Create media container
            logger.info("📦 Creating media container...")
            container_url = f"{self.graph_api_url}/{self.instagram_account_id}/media"
            container_params = {
                'caption': content,
                'access_token': self.access_token
            }
            
            if image_url:
                container_params['image_url'] = image_url
                logger.info(f"   Image URL: {image_url}")
            
            response = requests.post(container_url, params=container_params)
            response.raise_for_status()
            creation_id = response.json().get('id')
            
            if not creation_id:
                raise ValueError("No creation_id returned from Instagram API")
            
            logger.info(f"✅ Media container created: {creation_id}")
            
            # Step 2: Wait for container to be ready
            logger.info("⏳ Waiting for media to be processed...")
            if not self._check_container_status(creation_id):
                raise ValueError("Media container not ready after maximum attempts")
            
            # Step 3: Publish the container
            logger.info("📤 Publishing media...")
            publish_url = f"{self.graph_api_url}/{self.instagram_account_id}/media_publish"
            publish_params = {
                'creation_id': creation_id,
                'access_token': self.access_token
            }
            
            publish_response = requests.post(publish_url, params=publish_params)
            publish_response.raise_for_status()
            
            media_id = publish_response.json().get('id')
            logger.info(f"✅ Posted to Instagram: {media_id}")
            
            return {
                'id': media_id,
                'creation_id': creation_id,
                'url': f"https://www.instagram.com/p/{media_id}/"
            }
            
        except requests.exceptions.HTTPError as e:
            error_msg = f"Instagram API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"Error posting to Instagram: {str(e)}")
            raise
