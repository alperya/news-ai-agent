"""Social Media Publisher"""
import json
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
        
        self.client = tweepy.Client(  # type: ignore[attr-defined]
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
            data = getattr(response, "data", {}) or {}  # type: ignore[attr-defined]
            logger.info(f"✅ Posted: {data.get('id')}")
            return data
        except (tweepy.Forbidden if tweepy is not None else Exception) as e:  # type: ignore[attr-defined]
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
    """Instagram Publisher using Meta Graph API."""

    def __init__(self):
        self.access_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
        self.instagram_account_id = os.getenv('INSTAGRAM_ACCOUNT_ID')

        if not self.access_token:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN not found in environment")
        if not self.instagram_account_id:
            raise ValueError("INSTAGRAM_ACCOUNT_ID not found in environment")

        self.graph_api_url = "https://graph.facebook.com/v24.0"

        logger.info(f"📋 Instagram API Configuration:")
        logger.info(f"   API Version: v24.0")
        logger.info(f"   Account ID: {self.instagram_account_id}")
        logger.info(f"   Access Token: ***{self.access_token[-4:]}")

    def _ensure_valid_token(self):
        # Token kept fresh by the token-refresh Lambda (runs every 30 days)
        pass
    
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
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": media_id,
                "post_type": "photo",
            }))

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

    def publish_carousel(self, image_urls: list, caption: str, dry_run: bool = False) -> dict:
        """Publish a carousel post (2–10 images) to Instagram.

        Args:
            image_urls: List of public image URLs (first = overview card, rest = event slides).
            caption:    Caption text for the carousel post.
            dry_run:    If True, skip API calls.

        Returns:
            dict with media id and URL.
        """
        if dry_run:
            logger.info(f"[DRY RUN] Would post carousel ({len(image_urls)} slides) to Instagram")
            return {"id": "dry_run", "slides": len(image_urls)}

        if not (2 <= len(image_urls) <= 10):
            raise ValueError(f"Carousel requires 2–10 images, got {len(image_urls)}")

        try:
            self._ensure_valid_token()

            # Step 1: Create a carousel item container for each image
            child_ids: list = []
            for i, url in enumerate(image_urls):
                logger.info(f"   📷 Creating carousel item {i+1}/{len(image_urls)}...")
                resp = requests.post(
                    f"{self.graph_api_url}/{self.instagram_account_id}/media",
                    params={
                        "image_url": url,
                        "is_carousel_item": "true",
                        "access_token": self.access_token,
                    },
                )
                resp.raise_for_status()
                child_id = resp.json().get("id")
                if not child_id:
                    raise ValueError(f"No id returned for carousel item {i+1}")

                # Wait for child item to be ready before moving on
                if not self._check_container_status(child_id, max_attempts=20, delay=2):
                    raise ValueError(f"Carousel item {i+1} not ready after max attempts")
                child_ids.append(child_id)
                logger.info(f"   ✅ Carousel item {i+1} ready: {child_id}")

            # Step 2: Create the parent carousel container
            logger.info(f"📦 Creating carousel container ({len(child_ids)} children)...")
            resp = requests.post(
                f"{self.graph_api_url}/{self.instagram_account_id}/media",
                params={
                    "media_type": "CAROUSEL",
                    "caption": caption,
                    "children": ",".join(child_ids),
                    "access_token": self.access_token,
                },
            )
            resp.raise_for_status()
            creation_id = resp.json().get("id")
            if not creation_id:
                raise ValueError("No creation_id returned for carousel container")

            logger.info(f"✅ Carousel container created: {creation_id}")

            # Step 3: Wait for carousel container to be ready
            if not self._check_container_status(creation_id, max_attempts=30, delay=2):
                raise ValueError("Carousel container not ready after max attempts")

            # Step 4: Publish
            self._ensure_valid_token()
            logger.info("📤 Publishing carousel...")
            resp = requests.post(
                f"{self.graph_api_url}/{self.instagram_account_id}/media_publish",
                params={"creation_id": creation_id, "access_token": self.access_token},
            )
            resp.raise_for_status()
            media_id = resp.json().get("id")
            logger.info(f"✅ Carousel posted to Instagram: {media_id} ({len(image_urls)} slides)")
            return {"id": media_id, "creation_id": creation_id, "slides": len(image_urls),
                    "url": f"https://www.instagram.com/p/{media_id}/"}

        except requests.exceptions.HTTPError as e:
            error_msg = f"Instagram Carousel API error: {str(e)}"
            if hasattr(e.response, "text"):
                error_msg += f" — {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error publishing carousel: {str(e)}")
            raise

    def publish_reels(self, content: str, video_url: str, dry_run: bool = False):
        """Publish a Reels video to Instagram.

        Args:
            content: Caption text for the Reel.
            video_url: Public URL to the video file (must be accessible by Meta).
            dry_run: If True, skip actual API calls.

        Returns:
            dict with media id and URL.
        """
        if dry_run:
            logger.info(f"[DRY RUN] Would post Reel to Instagram:\n{content}")
            logger.info(f"[DRY RUN] Video URL: {video_url}")
            return {'id': 'dry_run', 'text': content, 'type': 'reels'}

        try:
            # STEP 0: Ensure valid token
            logger.info("🔐 Validating Instagram token...")
            self._ensure_valid_token()

            # Step 1: Create REELS media container
            logger.info("📦 Creating Reels media container...")
            container_url = f"{self.graph_api_url}/{self.instagram_account_id}/media"
            container_params = {
                'media_type': 'REELS',
                'video_url': video_url,
                'caption': content,
                'access_token': self.access_token,
            }

            response = requests.post(container_url, params=container_params)
            response.raise_for_status()
            creation_id = response.json().get('id')

            if not creation_id:
                raise ValueError("No creation_id returned from Instagram API")

            logger.info(f"✅ Reels container created: {creation_id}")

            # Step 2: Wait for video processing (videos take longer than photos).
            # max_attempts=80, delay=8 → polls up to ~10 minutes, matching Lambda 2 timeout.
            logger.info("⏳ Waiting for video to be processed...")
            if not self._check_container_status(creation_id, max_attempts=80, delay=8):
                raise ValueError("Reels container not ready after maximum attempts")

            # Step 3: Ensure token still valid
            logger.info("🔐 Validating token before publishing...")
            self._ensure_valid_token()

            # Step 4: Publish the Reels container
            logger.info("📤 Publishing Reels...")
            publish_url = f"{self.graph_api_url}/{self.instagram_account_id}/media_publish"
            publish_params = {
                'creation_id': creation_id,
                'access_token': self.access_token,
            }

            publish_response = requests.post(publish_url, params=publish_params)
            publish_response.raise_for_status()

            media_id = publish_response.json().get('id')
            logger.info(f"✅ Reels published: {media_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": media_id,
                "post_type": "reel",
            }))

            return {
                'id': media_id,
                'creation_id': creation_id,
                'type': 'reels',
                'url': f"https://www.instagram.com/reel/{media_id}/",
            }

        except requests.exceptions.HTTPError as e:
            error_msg = f"Instagram Reels API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting Reels: {str(e)}")
            raise

    def publish_story(self, video_url: str, dry_run: bool = False):
        """Publish a video to Instagram Stories.

        Stories use the same two-step container/publish flow as Reels but with
        ``media_type='STORIES'``. The Stories endpoint does NOT accept a caption
        — the text lives on the video itself.

        Args:
            video_url: Public URL to the video file (must be accessible by Meta).
            dry_run:   If True, skip actual API calls.

        Returns:
            dict with media id and type.
        """
        if dry_run:
            logger.info("[DRY RUN] Would post story to Instagram")
            logger.info(f"[DRY RUN] Video URL: {video_url}")
            return {'id': 'dry_run', 'type': 'story'}

        try:
            # STEP 0: Ensure valid token
            logger.info("🔐 Validating Instagram token...")
            self._ensure_valid_token()

            # Step 1: Create STORIES media container
            logger.info("📦 Creating Story media container...")
            container_url = f"{self.graph_api_url}/{self.instagram_account_id}/media"
            container_params = {
                'media_type': 'STORIES',
                'video_url': video_url,
                'access_token': self.access_token,
            }

            response = requests.post(container_url, params=container_params)
            response.raise_for_status()
            creation_id = response.json().get('id')

            if not creation_id:
                raise ValueError("No creation_id returned from Instagram API")

            logger.info(f"✅ Story container created: {creation_id}")

            # Step 2: Wait for video processing (videos take longer than photos).
            logger.info("⏳ Waiting for video to be processed...")
            if not self._check_container_status(creation_id, max_attempts=80, delay=8):
                raise ValueError("Story container not ready after maximum attempts")

            # Step 3: Ensure token still valid
            logger.info("🔐 Validating token before publishing...")
            self._ensure_valid_token()

            # Step 4: Publish the Story container
            logger.info("📤 Publishing Story...")
            publish_url = f"{self.graph_api_url}/{self.instagram_account_id}/media_publish"
            publish_params = {
                'creation_id': creation_id,
                'access_token': self.access_token,
            }

            publish_response = requests.post(publish_url, params=publish_params)
            publish_response.raise_for_status()

            media_id = publish_response.json().get('id')
            logger.info(f"✅ Story published: {media_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": media_id,
                "post_type": "story",
            }))

            return {
                'id': media_id,
                'creation_id': creation_id,
                'type': 'story',
            }

        except requests.exceptions.HTTPError as e:
            error_msg = f"Instagram Story API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting Story: {str(e)}")
            raise


class FacebookPublisher:
    """Publish video Stories to a Facebook Page via the Graph API.

    Uses the same Meta user token as Instagram (INSTAGRAM_ACCESS_TOKEN); the
    Page access token is derived from it at runtime, so there is no separate
    token to manage. Requires the `pages_manage_posts` permission.
    """

    def __init__(self):
        self.user_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
        self.page_id = os.getenv('FACEBOOK_PAGE_ID')
        if not self.user_token:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN not found in environment")
        if not self.page_id:
            raise ValueError("FACEBOOK_PAGE_ID not found in environment")
        self.graph_api_url = "https://graph.facebook.com/v24.0"
        self._page_token = None

    def _get_page_token(self) -> str:
        """Derive the Page access token from the user token (cached)."""
        if self._page_token:
            return self._page_token
        resp = requests.get(
            f"{self.graph_api_url}/{self.page_id}",
            params={'fields': 'access_token', 'access_token': self.user_token},
        )
        resp.raise_for_status()
        token = resp.json().get('access_token')
        if not token:
            raise ValueError("Could not derive Facebook Page access token")
        self._page_token = token
        return token

    def _wait_upload_complete(self, video_id: str, page_token: str,
                              max_attempts: int = 60, delay: int = 5) -> None:
        """Poll until Meta finishes fetching/processing the uploaded video."""
        status_url = f"{self.graph_api_url}/{video_id}"
        for attempt in range(max_attempts):
            resp = requests.get(status_url, params={'fields': 'status', 'access_token': page_token})
            resp.raise_for_status()
            status = resp.json().get('status', {})
            up = (status.get('uploading_phase') or {}).get('status')
            proc = (status.get('processing_phase') or {}).get('status')
            if up == 'error' or proc == 'error':
                raise ValueError(f"Facebook video processing error: {status}")
            if up == 'complete' and proc in ('complete', None, 'not_started'):
                # uploaded; processing may still be in_progress but finish can proceed
                if proc != 'in_progress':
                    logger.info(f"✅ FB video ready (attempt {attempt + 1})")
                    return
            logger.info(f"⏳ FB video processing... (attempt {attempt + 1}/{max_attempts}, "
                        f"upload={up}, processing={proc})")
            time.sleep(delay)
        raise ValueError("Facebook video not ready after maximum attempts")

    def publish_story(self, video_url: str, dry_run: bool = False):
        """Publish a video to the Facebook Page's Stories.

        Args:
            video_url: Public URL to the video (must be fetchable by Meta).
            dry_run:   If True, skip actual API calls.
        """
        if dry_run:
            logger.info("[DRY RUN] Would post Facebook Story")
            logger.info(f"[DRY RUN] Video URL: {video_url}")
            return {'id': 'dry_run', 'type': 'facebook_story'}

        try:
            page_token = self._get_page_token()

            # Step 1: Start an upload session
            logger.info("📦 Starting Facebook video-story upload session...")
            start = requests.post(
                f"{self.graph_api_url}/{self.page_id}/video_stories",
                params={'upload_phase': 'start', 'access_token': page_token},
            )
            start.raise_for_status()
            sj = start.json()
            video_id = sj['video_id']
            upload_url = sj['upload_url']
            logger.info(f"✅ Upload session: video_id={video_id}")

            # Step 2: Hand Meta the hosted file URL to fetch
            logger.info("📤 Uploading video to Facebook (hosted file)...")
            up = requests.post(
                upload_url,
                headers={'Authorization': f'OAuth {page_token}', 'file_url': video_url},
            )
            up.raise_for_status()

            # Step 3: Wait until the upload is fetched/processed
            logger.info("⏳ Waiting for Facebook to process the video...")
            self._wait_upload_complete(video_id, page_token)

            # Step 4: Finish → publishes the Story
            logger.info("📤 Publishing Facebook Story...")
            fin = requests.post(
                f"{self.graph_api_url}/{self.page_id}/video_stories",
                params={'upload_phase': 'finish', 'video_id': video_id, 'access_token': page_token},
            )
            fin.raise_for_status()
            fj = fin.json()
            post_id = fj.get('post_id') or fj.get('id')
            logger.info(f"✅ Facebook Story published: {post_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": post_id,
                "post_type": "facebook_story",
            }))
            return {'id': post_id, 'video_id': video_id, 'type': 'facebook_story'}

        except requests.exceptions.HTTPError as e:
            error_msg = f"Facebook Story API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting Facebook Story: {str(e)}")
            raise

    def publish_reel(self, video_url: str, caption: str = "", dry_run: bool = False):
        """Publish a video as a Reel on the Facebook Page.

        Mirrors the Instagram Reels publish so that whatever goes to Instagram
        as a Reel is cross-posted to Facebook as a Reel too.

        Args:
            video_url: Public URL to the video (must be fetchable by Meta).
            caption:   Reel description/caption.
            dry_run:   If True, skip actual API calls.
        """
        if dry_run:
            logger.info("[DRY RUN] Would post Facebook Reel")
            logger.info(f"[DRY RUN] Video URL: {video_url}")
            return {'id': 'dry_run', 'type': 'facebook_reel'}

        try:
            page_token = self._get_page_token()

            # Step 1: Start an upload session
            logger.info("📦 Starting Facebook Reel upload session...")
            start = requests.post(
                f"{self.graph_api_url}/{self.page_id}/video_reels",
                params={'upload_phase': 'start', 'access_token': page_token},
            )
            start.raise_for_status()
            sj = start.json()
            video_id = sj['video_id']
            upload_url = sj['upload_url']
            logger.info(f"✅ Upload session: video_id={video_id}")

            # Step 2: Hand Meta the hosted file URL to fetch
            logger.info("📤 Uploading video to Facebook (hosted file)...")
            up = requests.post(
                upload_url,
                headers={'Authorization': f'OAuth {page_token}', 'file_url': video_url},
            )
            up.raise_for_status()

            # Step 3: Wait until the upload is fetched/processed
            logger.info("⏳ Waiting for Facebook to process the video...")
            self._wait_upload_complete(video_id, page_token)

            # Step 4: Finish → publishes the Reel
            logger.info("📤 Publishing Facebook Reel...")
            fin = requests.post(
                f"{self.graph_api_url}/{self.page_id}/video_reels",
                params={
                    'upload_phase': 'finish',
                    'video_id': video_id,
                    'video_state': 'PUBLISHED',
                    'description': caption,
                    'access_token': page_token,
                },
            )
            fin.raise_for_status()
            fj = fin.json()
            post_id = fj.get('post_id') or video_id
            logger.info(f"✅ Facebook Reel published: {post_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": post_id,
                "post_type": "facebook_reel",
            }))
            return {'id': post_id, 'video_id': video_id, 'type': 'facebook_reel'}

        except requests.exceptions.HTTPError as e:
            error_msg = f"Facebook Reel API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting Facebook Reel: {str(e)}")
            raise

    def publish_photo(self, image_url: str, caption: str = "", dry_run: bool = False):
        """Publish a photo post to the Facebook Page.

        Args:
            image_url: Public image URL.
            caption:   Post text.
            dry_run:   If True, skip actual API calls.
        """
        if dry_run:
            logger.info("[DRY RUN] Would post Facebook photo")
            return {'id': 'dry_run', 'type': 'facebook_photo'}

        try:
            page_token = self._get_page_token()
            logger.info("📤 Publishing Facebook photo...")
            resp = requests.post(
                f"{self.graph_api_url}/{self.page_id}/photos",
                params={'url': image_url, 'caption': caption, 'access_token': page_token},
            )
            resp.raise_for_status()
            rj = resp.json()
            post_id = rj.get('post_id') or rj.get('id')
            logger.info(f"✅ Facebook photo published: {post_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": post_id,
                "post_type": "facebook_photo",
            }))
            return {'id': post_id, 'type': 'facebook_photo'}

        except requests.exceptions.HTTPError as e:
            error_msg = f"Facebook photo API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting Facebook photo: {str(e)}")
            raise
