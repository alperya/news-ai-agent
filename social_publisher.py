"""Social Media Publisher"""
import os
import logging
import requests
import time
from typing import Optional

try:
    import tweepy
    HAS_TWEEPY = True
except ImportError:
    tweepy = None
    HAS_TWEEPY = False

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Import token manager for Instagram token refresh
try:
    from token_manager import InstagramTokenManager
    HAS_TOKEN_MANAGER = True
except ImportError:
    HAS_TOKEN_MANAGER = False
    logger.warning("⚠️  token_manager not available - using static tokens")

class TwitterPublisher:
    def __init__(self):
        if not HAS_TWEEPY:
            raise ImportError("tweepy is not installed. Install it to enable Twitter publishing.")

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
        
        try:
            response = self.client.create_tweet(text=content)
            logger.info(f"✅ Posted: {response.data['id']}")
            return response.data
        except tweepy.Forbidden as e:
            # Check if it's a rate limit error
            error_str = str(e)
            if "You are not permitted to perform this action" in error_str:
                logger.error("❌ Twitter API Rate Limit Exceeded!")
                logger.error("   Daily limit of 17 tweets reached (Essential tier)")
                logger.error("   Limit resets in ~24 hours")
                logger.info("💡 Solutions:")
                logger.info("   1. Wait 24 hours for limit reset")
                logger.info("   2. Use Instagram instead (unlimited)")
                logger.info("   3. Upgrade to Premium tier")
                logger.info("   📚 See: TWITTER_403_EXPLAINED.md")
            raise
        except Exception as e:
            logger.error(f"❌ Error posting to Twitter: {str(e)}")
            raise


class InstagramPublisher:
    """Instagram Publisher using Meta Graph API with automatic token refresh"""
    
    def __init__(self):
        # Try to use token manager for automatic refresh
        self.token_manager = None
        self.access_token = None
        
        if HAS_TOKEN_MANAGER:
            try:
                self.token_manager = InstagramTokenManager()
                self.access_token = self.token_manager.get_valid_token()
                logger.info("✅ Token manager initialized - automatic token refresh enabled")
            except Exception as e:
                logger.debug(f"Token manager failed: {str(e)} - falling back to static token")
                self.token_manager = None
        
        # Fallback to static token from environment
        if not self.access_token:
            self.access_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
            if not self.token_manager and self.access_token:
                logger.info("Using static token from .env (auto-refresh disabled)")
        
        self.instagram_account_id = os.getenv('INSTAGRAM_ACCOUNT_ID')
        
        if not self.access_token:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN not found in environment")
        if not self.instagram_account_id:
            raise ValueError("INSTAGRAM_ACCOUNT_ID not found in environment")
        
        # Use API v24.0
        self.graph_api_url = "https://graph.facebook.com/v24.0"
        
        # Log current configuration for verification
        logger.info(f"📋 Instagram API Configuration:")
        logger.info(f"   API Version: v24.0")
        logger.info(f"   Account ID: {self.instagram_account_id}")
        logger.info(f"   Access Token: {'***' + self.access_token[-4:] if self.access_token else 'NOT SET'}")
        logger.info(f"   Token Refresh: {'Enabled' if self.token_manager else 'Disabled'}")
    
    def _ensure_valid_token(self):
        """Ensure we have a valid token before making API calls"""
        if not self.token_manager:
            # No token manager - can't refresh, just warn
            logger.debug("No token manager - using static token")
            return
        
        try:
            # Get fresh token from manager (will refresh if needed)
            new_token = self.token_manager.get_valid_token()
            
            # Update if token changed
            if new_token != self.access_token:
                logger.info("🔄 Token refreshed and updated")
                self.access_token = new_token
            
        except Exception as e:
            logger.error(f"❌ Token validation failed: {str(e)}")
            raise ValueError(f"Cannot get valid Instagram token: {str(e)}")
    
    def _check_container_status(self, creation_id: str, max_attempts: int = 30, delay: int = 2) -> bool:
        """Check if media container is ready for publishing"""
        status_url = f"{self.graph_api_url}/{creation_id}"
        
        for attempt in range(max_attempts):
            try:
                # Ensure valid token before each status check
                self._ensure_valid_token()
                
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
            # ⚡ STEP 0: Ensure valid token before any API calls
            logger.info("🔐 Validating Instagram token...")
            self._ensure_valid_token()
            logger.info("✅ Token is valid and ready")
            
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
            
            # ⚡ STEP 3: Ensure token is still valid before publishing
            logger.info("🔐 Validating token before publishing...")
            self._ensure_valid_token()
            logger.info("✅ Token valid, proceeding with publish...")
            
            # Step 4: Publish the container
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
            logger.error(f"❌ Error posting to Instagram: {str(e)}")
            raise
