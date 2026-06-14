"""
YouTube Data API v3 publisher for YouTube Shorts.

Uploads 1080×1920 MP4 videos (≤60s) as YouTube Shorts to the channel
linked to the configured OAuth credentials. The refresh token is loaded
from environment variables (which come from Secrets Manager at Lambda start).
"""

import logging
import os
import tempfile
from urllib.parse import urlparse

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

logger = logging.getLogger(__name__)

_SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
_MAX_TITLE_LEN = 100


class YouTubePublisher:
    def __init__(self):
        self._client_id = os.environ['YOUTUBE_CLIENT_ID']
        self._client_secret = os.environ['YOUTUBE_CLIENT_SECRET']
        self._refresh_token = os.environ['YOUTUBE_REFRESH_TOKEN']

    def _get_credentials(self) -> Credentials:
        creds = Credentials(
            token=None,
            refresh_token=self._refresh_token,
            client_id=self._client_id,
            client_secret=self._client_secret,
            token_uri='https://oauth2.googleapis.com/token',
            scopes=_SCOPES,
        )
        creds.refresh(Request())
        return creds

    def upload_short(
        self,
        video_url: str,
        title: str,
        description: str,
        tags: list,
    ) -> dict:
        """Download video from presigned URL and upload it as a YouTube Short.

        Returns dict with ``video_id`` and ``url`` on success.
        Raises on HTTP / API errors so the caller can alert + log.
        """
        if len(title) > _MAX_TITLE_LEN:
            title = title[:_MAX_TITLE_LEN]

        # Validate URL before fetching — prevents SSRF against internal endpoints
        parsed = urlparse(video_url)
        if parsed.scheme != 'https' or not parsed.netloc.endswith('.amazonaws.com'):
            raise ValueError(f"video_url must be an HTTPS S3 presigned URL, got: {parsed.scheme}://{parsed.netloc}")

        logger.info("⬇️  Downloading video from presigned URL...")
        resp = requests.get(video_url, stream=True, timeout=120)
        resp.raise_for_status()

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.mp4', delete=False, dir='/tmp') as tmp_file:
                for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                    tmp_file.write(chunk)
                tmp_path = tmp_file.name
            logger.info(f"✅ Video downloaded: {tmp_path}")

            youtube = build(
                'youtube', 'v3',
                credentials=self._get_credentials(),
                cache_discovery=False,  # Lambda has no writable home dir for discovery cache
            )

            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags,
                    'categoryId': '25',  # News & Politics
                    'defaultLanguage': 'nl',
                    'defaultAudioLanguage': 'nl',
                },
                'status': {
                    'privacyStatus': 'public',
                    'selfDeclaredMadeForKids': False,
                },
            }
            media = MediaFileUpload(tmp_path, mimetype='video/mp4', resumable=True)
            insert_request = youtube.videos().insert(
                part='snippet,status', body=body, media_body=media,
            )

            logger.info("⬆️  Uploading to YouTube Shorts...")
            response = None
            while response is None:
                status, response = insert_request.next_chunk()
                if status:
                    logger.info(f"📊 Upload progress: {int(status.progress() * 100)}%")

            video_id = response['id']
            url = f"https://www.youtube.com/shorts/{video_id}"
            logger.info(f"✅ YouTube Short published: {url}")
            return {'video_id': video_id, 'url': url}

        except HttpError as e:
            logger.error(f"❌ YouTube API error {e.status_code}: {e.error_details}")
            raise
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
