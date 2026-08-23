"""Unit tests for YouTubePublisher."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

os.environ.setdefault('YOUTUBE_CLIENT_ID', 'test-client-id')
os.environ.setdefault('YOUTUBE_CLIENT_SECRET', 'test-client-secret')
os.environ.setdefault('YOUTUBE_REFRESH_TOKEN', 'test-refresh-token')

from youtube_publisher import YouTubePublisher, _MAX_TITLE_LEN


@pytest.fixture
def publisher():
    return YouTubePublisher()


@pytest.fixture
def mock_video_response():
    """Fake chunked HTTP response for video download."""
    resp = MagicMock()
    resp.iter_content.return_value = [b'fake_mp4_data']
    resp.raise_for_status.return_value = None
    return resp


def _make_resumable_upload_mock(video_id='abc123'):
    """Build a mock that simulates the resumable upload protocol."""
    insert_request = MagicMock()
    insert_request.next_chunk.side_effect = [
        (MagicMock(progress=lambda: 0.5), None),  # 50% progress
        (None, {'id': video_id}),                 # completed
    ]
    return insert_request


class TestUploadShort:
    @patch('youtube_publisher.requests.get')
    @patch('youtube_publisher.build')
    @patch('youtube_publisher.Credentials')
    def test_success_path(self, mock_creds_cls, mock_build, mock_get, publisher, mock_video_response):
        mock_get.return_value = mock_video_response

        mock_creds = MagicMock()
        mock_creds_cls.return_value = mock_creds

        insert_request = _make_resumable_upload_mock('vid_001')
        mock_youtube = MagicMock()
        mock_youtube.videos.return_value.insert.return_value = insert_request
        mock_build.return_value = mock_youtube

        result = publisher.upload_short(
            video_url='https://news-ai-agent-results-test.s3.amazonaws.com/video.mp4?X-Amz-Signature=abc',
            title='Test nieuws vandaag #Shorts',
            description='Test beschrijving\n\n#Nederland',
            tags=['nieuws', 'nederland'],
        )

        assert result['video_id'] == 'vid_001'
        assert result['url'] == 'https://www.youtube.com/shorts/vid_001'

        # Verify API was called with correct body structure
        insert_call_kwargs = mock_youtube.videos.return_value.insert.call_args.kwargs
        body = insert_call_kwargs['body']
        assert body['snippet']['categoryId'] == '25'
        assert body['snippet']['defaultLanguage'] == 'nl'
        assert body['status']['privacyStatus'] == 'public'
        assert body['status']['selfDeclaredMadeForKids'] is False

    @patch('youtube_publisher.requests.get')
    @patch('youtube_publisher.build')
    @patch('youtube_publisher.Credentials')
    def test_title_truncated_to_100_chars(self, mock_creds_cls, mock_build, mock_get, publisher, mock_video_response):
        mock_get.return_value = mock_video_response
        mock_creds_cls.return_value = MagicMock()
        insert_request = _make_resumable_upload_mock()
        mock_youtube = MagicMock()
        mock_youtube.videos.return_value.insert.return_value = insert_request
        mock_build.return_value = mock_youtube

        long_title = 'A' * 105  # 105 chars — over the 100-char limit

        publisher.upload_short(
            video_url='https://news-ai-agent-results-test.s3.amazonaws.com/video.mp4?X-Amz-Signature=abc',
            title=long_title,
            description='desc',
            tags=[],
        )

        insert_call_kwargs = mock_youtube.videos.return_value.insert.call_args.kwargs
        actual_title = insert_call_kwargs['body']['snippet']['title']
        assert len(actual_title) <= _MAX_TITLE_LEN

    @patch('youtube_publisher.requests.get')
    @patch('youtube_publisher.build')
    @patch('youtube_publisher.Credentials')
    def test_http_error_propagates(self, mock_creds_cls, mock_build, mock_get, publisher, mock_video_response):
        from googleapiclient.errors import HttpError
        mock_get.return_value = mock_video_response
        mock_creds_cls.return_value = MagicMock()

        mock_youtube = MagicMock()
        insert_request = MagicMock()
        insert_request.next_chunk.side_effect = HttpError(
            resp=MagicMock(status=403), content=b'quota exceeded'
        )
        mock_youtube.videos.return_value.insert.return_value = insert_request
        mock_build.return_value = mock_youtube

        with pytest.raises(HttpError):
            publisher.upload_short(
                video_url='https://news-ai-agent-results-test.s3.amazonaws.com/video.mp4?X-Amz-Signature=abc',
                title='Test #Shorts',
                description='desc',
                tags=[],
            )
