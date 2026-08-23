"""Unit tests for youtube_worker Lambda handler."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch


sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('RESULTS_BUCKET', 'news-ai-agent-results-test')

_VALID_EVENT = {
    's3_video_key': 'reels/reels_20260614_090000.mp4',
    'post_content': 'Test nieuws content voor vandaag.',
    'hook': 'Dit is wat er vandaag in Nederland gebeurde',
    'hashtags': ['#Nederland', '#Nieuws'],
    'timestamp': '20260614_090000',
}


def _make_secrets_with_yt_creds():
    """Return a fake secrets dict that includes YouTube credentials."""
    return {
        'INSTAGRAM_ACCESS_TOKEN': 'ig-token',
        'INSTAGRAM_ACCOUNT_ID': '12345',
        'YOUTUBE_CLIENT_ID': 'yt-client-id',
        'YOUTUBE_CLIENT_SECRET': 'yt-client-secret',
        'YOUTUBE_REFRESH_TOKEN': 'yt-refresh-token',
    }


def _make_secrets_without_yt_creds():
    """Return a fake secrets dict that does NOT include YouTube credentials."""
    return {
        'INSTAGRAM_ACCESS_TOKEN': 'ig-token',
        'INSTAGRAM_ACCOUNT_ID': '12345',
    }


class TestYouTubeWorkerHandler:
    @patch('youtube_worker.boto3.session.Session')
    @patch('youtube_worker.boto3.client')
    @patch('youtube_worker.YouTubePublisher')
    def test_skips_when_no_credentials(self, mock_yt_cls, mock_boto_client, mock_session):
        """Handler returns status=skipped when YouTube creds are absent from Secrets Manager."""
        import youtube_worker

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            'SecretString': json.dumps(_make_secrets_without_yt_creds())
        }
        mock_session.return_value.client.return_value = mock_sm

        result = youtube_worker.lambda_handler(dict(_VALID_EVENT), None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'skipped'
        assert body['reason'] == 'credentials_not_configured'
        # Publisher should never have been instantiated
        mock_yt_cls.assert_not_called()

    @patch('youtube_worker.boto3.session.Session')
    @patch('youtube_worker.boto3.client')
    @patch('youtube_worker.YouTubePublisher')
    def test_bad_payload_returns_400(self, mock_yt_cls, mock_boto_client, mock_session):
        """Handler returns 400 when required payload fields are missing."""
        import youtube_worker

        event = {'post_content': 'some content', 'timestamp': '20260614'}
        # s3_video_key and bucket_name are missing

        # _load_secrets shouldn't even be called for this path, but mock it anyway
        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            'SecretString': json.dumps(_make_secrets_with_yt_creds())
        }
        mock_session.return_value.client.return_value = mock_sm

        with patch('youtube_worker.send_alert') as mock_alert:
            result = youtube_worker.lambda_handler(event, None)

        assert result['statusCode'] == 400
        body = json.loads(result['body'])
        assert body['status'] == 'error'
        mock_alert.assert_called_once()

    @patch('youtube_worker.boto3.session.Session')
    @patch('youtube_worker.boto3.client')
    @patch('youtube_worker.YouTubePublisher')
    def test_success_path(self, mock_yt_cls, mock_boto_client, mock_session):
        """Handler calls upload_short and writes result to S3 on success."""
        import youtube_worker

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            'SecretString': json.dumps(_make_secrets_with_yt_creds())
        }
        mock_session.return_value.client.return_value = mock_sm

        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = 'https://s3.example.com/video.mp4?signed=1'
        mock_boto_client.return_value = mock_s3

        mock_publisher_instance = MagicMock()
        mock_publisher_instance.upload_short.return_value = {
            'video_id': 'yt_vid_abc',
            'url': 'https://www.youtube.com/shorts/yt_vid_abc',
        }
        mock_yt_cls.return_value = mock_publisher_instance

        result = youtube_worker.lambda_handler(dict(_VALID_EVENT), None)

        assert result['statusCode'] == 200
        body = json.loads(result['body'])
        assert body['status'] == 'success'
        assert body['result']['video_id'] == 'yt_vid_abc'

        # Verify upload_short was called with sensible args
        upload_call = mock_publisher_instance.upload_short.call_args
        assert '#Shorts' in upload_call.kwargs['title']
        assert len(upload_call.kwargs['title']) <= 100

        # Verify result was persisted to S3
        mock_s3.put_object.assert_called_once()
        put_kwargs = mock_s3.put_object.call_args.kwargs
        assert put_kwargs['Key'].startswith('youtube_results/')

    @patch('youtube_worker.boto3.session.Session')
    @patch('youtube_worker.boto3.client')
    @patch('youtube_worker.YouTubePublisher')
    @patch('youtube_worker.alert_on_exception')
    def test_youtube_failure_sends_alert(self, mock_alert_exc, mock_yt_cls, mock_boto_client, mock_session):
        """Handler sends alert and returns 500 when upload_short raises."""
        import youtube_worker

        mock_sm = MagicMock()
        mock_sm.get_secret_value.return_value = {
            'SecretString': json.dumps(_make_secrets_with_yt_creds())
        }
        mock_session.return_value.client.return_value = mock_sm

        mock_s3 = MagicMock()
        mock_s3.generate_presigned_url.return_value = 'https://s3.example.com/video.mp4?signed=1'
        mock_boto_client.return_value = mock_s3

        mock_publisher_instance = MagicMock()
        mock_publisher_instance.upload_short.side_effect = RuntimeError("Upload failed")
        mock_yt_cls.return_value = mock_publisher_instance

        result = youtube_worker.lambda_handler(dict(_VALID_EVENT), None)

        assert result['statusCode'] == 500
        body = json.loads(result['body'])
        assert body['status'] == 'error'
        mock_alert_exc.assert_called_once()
