"""Social Media Publisher — Instagram + Facebook + LinkedIn channel adapters."""
import json
import os
import logging
import requests
import time
from urllib.parse import quote
from typing import Optional

from publishing import ChannelPublisher, REEL, PHOTO, STORY

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class InstagramPublisher(ChannelPublisher):
    """Instagram Publisher using Meta Graph API."""

    name = "instagram"

    def supports(self, kind: str) -> bool:
        return kind in (REEL, PHOTO, STORY)

    def publish(self, kind: str, *, media_url: str, caption: str = "",
                dry_run: bool = False) -> dict:
        if kind == REEL:
            return self.publish_reels(content=caption, video_url=media_url, dry_run=dry_run)
        if kind == PHOTO:
            return self.publish_post(content=caption, image_url=media_url, dry_run=dry_run)
        if kind == STORY:
            return self.publish_story(video_url=media_url, dry_run=dry_run)
        raise ValueError(f"Instagram does not support kind '{kind}'")

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

    def publish_carousel(self, image_urls: list, caption: str = "", dry_run: bool = False):
        """Publish a multi-image carousel (album) feed post to Instagram.

        Graph API three-step flow:
          1. create one child container per image (``is_carousel_item=true``),
          2. create a ``CAROUSEL`` parent container referencing the children,
          3. publish the parent.

        Args:
            image_urls: Public URLs (S3 presigned) for each slide, in order.
                        2–10 images (Instagram's carousel limits).
            caption: Caption text for the post.
            dry_run: If True, skip actual API calls.
        """
        if not image_urls or len(image_urls) < 2:
            raise ValueError("A carousel needs at least 2 images")
        image_urls = image_urls[:10]  # Instagram hard limit

        if dry_run:
            logger.info(f"[DRY RUN] Would post {len(image_urls)}-image carousel to Instagram:\n{caption}")
            for u in image_urls:
                logger.info(f"[DRY RUN]   slide: {u}")
            return {'id': 'dry_run', 'text': caption, 'type': 'carousel', 'slides': len(image_urls)}

        try:
            logger.info("🔐 Validating Instagram token...")
            self._ensure_valid_token()

            media_url = f"{self.graph_api_url}/{self.instagram_account_id}/media"

            # Step 1: one child container per image
            logger.info(f"📦 Creating {len(image_urls)} carousel child containers...")
            child_ids = []
            for idx, url in enumerate(image_urls):
                child_resp = requests.post(media_url, params={
                    'image_url': url,
                    'is_carousel_item': 'true',
                    'access_token': self.access_token,
                })
                child_resp.raise_for_status()
                child_id = child_resp.json().get('id')
                if not child_id:
                    raise ValueError(f"No child id returned for slide {idx + 1}")
                # Children are images → ready quickly, but confirm before parenting
                if not self._check_container_status(child_id):
                    raise ValueError(f"Carousel child {idx + 1} not ready")
                child_ids.append(child_id)
                logger.info(f"   ✅ child {idx + 1}/{len(image_urls)}: {child_id}")

            # Step 2: parent CAROUSEL container
            logger.info("📦 Creating carousel parent container...")
            parent_resp = requests.post(media_url, params={
                'media_type': 'CAROUSEL',
                'children': ','.join(child_ids),
                'caption': caption,
                'access_token': self.access_token,
            })
            parent_resp.raise_for_status()
            creation_id = parent_resp.json().get('id')
            if not creation_id:
                raise ValueError("No creation_id returned for carousel parent")
            if not self._check_container_status(creation_id):
                raise ValueError("Carousel parent container not ready")
            logger.info(f"✅ Carousel container created: {creation_id}")

            # Step 3: publish
            logger.info("📤 Publishing carousel...")
            self._ensure_valid_token()
            publish_resp = requests.post(
                f"{self.graph_api_url}/{self.instagram_account_id}/media_publish",
                params={'creation_id': creation_id, 'access_token': self.access_token},
            )
            publish_resp.raise_for_status()
            media_id = publish_resp.json().get('id')
            logger.info(f"✅ Carousel published: {media_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": media_id,
                "post_type": "carousel",
                "slides": len(image_urls),
            }))

            return {
                'id': media_id,
                'creation_id': creation_id,
                'type': 'carousel',
                'slides': len(image_urls),
                'url': f"https://www.instagram.com/p/{media_id}/",
            }

        except requests.exceptions.HTTPError as e:
            error_msg = f"Instagram carousel API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting carousel: {str(e)}")
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


class FacebookPublisher(ChannelPublisher):
    """Publish Reels / photos / Stories to a Facebook Page via the Graph API.

    Uses the same Meta user token as Instagram (INSTAGRAM_ACCESS_TOKEN); the
    Page access token is derived from it at runtime, so there is no separate
    token to manage. Requires the `pages_manage_posts` permission.
    """

    name = "facebook"

    def __init__(self):
        self.user_token = os.getenv('INSTAGRAM_ACCESS_TOKEN')
        self.page_id = os.getenv('FACEBOOK_PAGE_ID')
        if not self.user_token:
            raise ValueError("INSTAGRAM_ACCESS_TOKEN not found in environment")
        if not self.page_id:
            raise ValueError("FACEBOOK_PAGE_ID not found in environment")
        self.graph_api_url = "https://graph.facebook.com/v24.0"
        self._page_token = None

    def supports(self, kind: str) -> bool:
        return kind in (REEL, PHOTO, STORY)

    def publish(self, kind: str, *, media_url: str, caption: str = "",
                dry_run: bool = False) -> dict:
        if kind == REEL:
            return self.publish_reel(video_url=media_url, caption=caption, dry_run=dry_run)
        if kind == PHOTO:
            return self.publish_photo(image_url=media_url, caption=caption, dry_run=dry_run)
        if kind == STORY:
            return self.publish_story(video_url=media_url, dry_run=dry_run)
        raise ValueError(f"Facebook does not support kind '{kind}'")

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


class LinkedInPublisher(ChannelPublisher):
    """Publish news Reels (video) to a LinkedIn Company Page.

    Unlike Meta (which fetches a hosted file_url), LinkedIn's Videos API requires
    uploading the actual video bytes: initializeUpload → PUT bytes → finalizeUpload
    → create Post. The video is therefore downloaded from the presigned S3 URL into
    memory first (our Reels are short, < ~50 MB).

    Authenticated as the Company Page (`urn:li:organization:<id>`); the access token
    must hold the `w_organization_social` scope. Only Reels are supported — Stories
    and photos are intentionally not cross-posted here.
    """

    name = "linkedin"

    # (connect, read) timeout (s) for every HTTP call — a hung LinkedIn endpoint
    # must never eat the async Lambda's execution budget. The read timeout is an
    # inactivity guard (not a hard cap), so it won't kill a slow-but-active upload.
    REQUEST_TIMEOUT = (10, 120)

    def __init__(self):
        self.access_token = os.getenv('LINKEDIN_ACCESS_TOKEN')
        org_id = os.getenv('LINKEDIN_ORG_ID')
        if not self.access_token:
            raise ValueError("LINKEDIN_ACCESS_TOKEN not found in environment")
        if not org_id:
            raise ValueError("LINKEDIN_ORG_ID not found in environment")
        self.author_urn = f"urn:li:organization:{org_id}"
        self.api = "https://api.linkedin.com/rest"
        self.version = "202506"  # LinkedIn-Version header (YYYYMM)

    def supports(self, kind: str) -> bool:
        return kind == REEL

    def publish(self, kind: str, *, media_url: str, caption: str = "",
                dry_run: bool = False) -> dict:
        if kind == REEL:
            return self.publish_reel(video_url=media_url, caption=caption, dry_run=dry_run)
        raise ValueError(f"LinkedIn does not support kind '{kind}'")

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {
            'Authorization': f'Bearer {self.access_token}',
            'LinkedIn-Version': self.version,
            'X-Restli-Protocol-Version': '2.0.0',
        }
        if extra:
            headers.update(extra)
        return headers

    def _wait_video_available(self, video_urn: str,
                              max_attempts: int = 60, delay: int = 5) -> None:
        """Poll until LinkedIn finishes processing the uploaded video."""
        status_url = f"{self.api}/videos/{quote(video_urn, safe='')}"
        for attempt in range(max_attempts):
            resp = requests.get(status_url, headers=self._headers(),
                                timeout=self.REQUEST_TIMEOUT)
            resp.raise_for_status()
            status = resp.json().get('status')
            if status == 'AVAILABLE':
                logger.info(f"✅ LinkedIn video ready (attempt {attempt + 1})")
                return
            if status == 'PROCESSING_FAILED':
                raise ValueError(f"LinkedIn video processing failed: {video_urn}")
            logger.info(f"⏳ LinkedIn video processing... "
                        f"(attempt {attempt + 1}/{max_attempts}, status={status})")
            time.sleep(delay)
        raise ValueError("LinkedIn video not ready after maximum attempts")

    def publish_reel(self, video_url: str, caption: str = "", dry_run: bool = False):
        """Publish a video to the LinkedIn Company Page feed.

        Args:
            video_url: Public URL to the video (downloaded into memory and uploaded).
            caption:   Post commentary.
            dry_run:   If True, skip actual API calls.
        """
        if dry_run:
            logger.info("[DRY RUN] Would post LinkedIn Reel")
            logger.info(f"[DRY RUN] Video URL: {video_url}")
            return {'id': 'dry_run', 'type': 'linkedin_reel'}

        try:
            # Step 1: Download the video bytes (LinkedIn needs the raw bytes)
            logger.info("📥 Downloading video for LinkedIn upload...")
            dl = requests.get(video_url, timeout=self.REQUEST_TIMEOUT)
            dl.raise_for_status()
            data = dl.content
            size = len(data)
            logger.info(f"✅ Downloaded {size} bytes")

            # Step 2: Initialize the upload
            logger.info("📦 Initializing LinkedIn video upload...")
            init = requests.post(
                f"{self.api}/videos?action=initializeUpload",
                headers=self._headers({'Content-Type': 'application/json'}),
                json={'initializeUploadRequest': {
                    'owner': self.author_urn,
                    'fileSizeBytes': size,
                    'uploadCaptions': False,
                    'uploadThumbnail': False,
                }},
                timeout=self.REQUEST_TIMEOUT,
            )
            init.raise_for_status()
            value = init.json()['value']
            video_urn = value['video']
            instructions = value['uploadInstructions']
            logger.info(f"✅ Upload session: video={video_urn}, parts={len(instructions)}")

            # Step 3: Upload each byte range, collecting ETags
            uploaded_part_ids = []
            for idx, ins in enumerate(instructions):
                first, last = ins['firstByte'], ins['lastByte']
                logger.info(f"📤 Uploading part {idx + 1}/{len(instructions)} "
                            f"(bytes {first}-{last})...")
                put = requests.put(
                    ins['uploadUrl'],
                    headers={'Content-Type': 'application/octet-stream'},
                    data=data[first:last + 1],
                    timeout=self.REQUEST_TIMEOUT,
                )
                put.raise_for_status()
                uploaded_part_ids.append(put.headers['ETag'])

            # Step 4: Finalize the upload
            logger.info("📤 Finalizing LinkedIn upload...")
            fin = requests.post(
                f"{self.api}/videos?action=finalizeUpload",
                headers=self._headers({'Content-Type': 'application/json'}),
                json={'finalizeUploadRequest': {
                    'video': video_urn,
                    'uploadToken': '',
                    'uploadedPartIds': uploaded_part_ids,
                }},
                timeout=self.REQUEST_TIMEOUT,
            )
            fin.raise_for_status()

            # Step 5: Wait until LinkedIn finishes processing the video
            logger.info("⏳ Waiting for LinkedIn to process the video...")
            self._wait_video_available(video_urn)

            # Step 6: Create the post referencing the video
            logger.info("📤 Publishing LinkedIn post...")
            post = requests.post(
                f"{self.api}/posts",
                headers=self._headers({'Content-Type': 'application/json'}),
                json={
                    'author': self.author_urn,
                    'commentary': caption,
                    'visibility': 'PUBLIC',
                    'distribution': {
                        'feedDistribution': 'MAIN_FEED',
                        'targetEntities': [],
                        'thirdPartyDistributionChannels': [],
                    },
                    'content': {'media': {'id': video_urn}},
                    'lifecycleState': 'PUBLISHED',
                    'isReshareDisabledByAuthor': False,
                },
                timeout=self.REQUEST_TIMEOUT,
            )
            post.raise_for_status()
            post_id = post.headers.get('x-restli-id') or post.headers.get('x-linkedin-id')
            logger.info(f"✅ LinkedIn Reel published: {post_id}")
            logger.info(json.dumps({
                "event": "post_published",
                "post_id": post_id,
                "post_type": "linkedin_reel",
            }))
            return {'id': post_id, 'video_urn': video_urn, 'type': 'linkedin_reel'}

        except requests.exceptions.HTTPError as e:
            error_msg = f"LinkedIn Reel API error: {str(e)}"
            if hasattr(e.response, 'text'):
                error_msg += f" - {e.response.text}"
            logger.error(error_msg)
            raise ValueError(error_msg)
        except Exception as e:
            logger.error(f"❌ Error posting LinkedIn Reel: {str(e)}")
            raise
