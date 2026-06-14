"""
Isolation test: YouTube Lambda invoke failure must not affect Instagram publish count.

If youtube_worker Lambda doesn't exist yet (before terraform apply) or fails to invoke
for any reason, the main handler must still count the Instagram publish as successful.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault('ANTHROPIC_API_KEY', 'test-key')


class _FakeContext:
    """Minimal Lambda context with plenty of remaining time."""
    def get_remaining_time_in_millis(self):
        return 900_000


def _make_mock_post(hook='Test hook'):
    post = MagicMock()
    post.to_dict.return_value = {
        'original_title': 'Test nieuws',
        'original_url': 'https://nos.nl/artikel/1',
        'source': 'nos',
        'content': 'Test content',
        'hashtags': ['#Nederland'],
        'emoji': '📰',
        'hook': hook,
        'image_url': None,
        'full_post': '📰 Test content\n\n#Nederland',
    }
    post.get = lambda key, default=None: post.to_dict().get(key, default)
    return post


def _make_mock_article():
    article = MagicMock()
    article.to_dict.return_value = {
        'title': 'Test nieuws',
        'url': 'https://nos.nl/artikel/1',
        'source': 'nos',
        'description': 'Test beschrijving',
        'image_url': None,
        'category': 'binnenland',
    }
    return article


@patch('lambda_handler.get_secrets')
@patch('lambda_handler.DutchNewsScraper')
@patch('lambda_handler.get_published_urls')
@patch('lambda_handler.NewsAIAgent')
@patch('lambda_handler.save_to_s3')
@patch('lambda_handler.create_news_video')
@patch('lambda_handler.send_alert')
@patch('lambda_handler.boto3')
def test_youtube_invoke_failure_does_not_affect_published_count(
    mock_boto3, mock_send_alert, mock_create_video, mock_save_s3,
    mock_ai_agent_cls, mock_get_published_urls, mock_scraper_cls, mock_get_secrets,
):
    """
    Scenario: reels_worker invoke succeeds, youtube_worker invoke raises
    ResourceNotFoundException (Lambda not yet deployed).

    Expected: response statusCode=200, posts_published=1.
    Instagram publish is NOT blocked by YouTube failure.
    """
    from botocore.exceptions import ClientError
    import lambda_handler

    # Scraper returns one article
    mock_scraper_cls.return_value.scrape_all_sources.return_value = [_make_mock_article()]
    mock_get_published_urls.return_value = set()

    # AI returns one post, quality gate passes it through
    mock_post = _make_mock_post()
    mock_ai = MagicMock()
    mock_ai.process_batch.return_value = [mock_post]
    mock_ai.quality_check.return_value = mock_post
    mock_ai.generate_footage_queries.return_value = ['netherlands news']
    mock_ai_agent_cls.return_value = mock_ai

    # create_news_video writes a tiny placeholder file
    def _fake_create_video(title, content, source, hashtags, output_path, **kwargs):
        with open(output_path, 'wb') as f:
            f.write(b'\x00\x01\x02')  # dummy bytes

    mock_create_video.side_effect = _fake_create_video

    # S3 client: accepts put_object, returns dummy presigned URL
    mock_s3 = MagicMock()
    mock_s3.put_object.return_value = {}

    # Lambda client: reels invoke succeeds, youtube invoke raises
    mock_lambda_client = MagicMock()

    def _invoke_side_effect(**kwargs):
        fn = kwargs.get('FunctionName', '')
        if 'youtube' in fn:
            raise ClientError(
                {'Error': {'Code': 'ResourceNotFoundException', 'Message': 'Function not found'}},
                'Invoke',
            )
        return {'StatusCode': 202}

    mock_lambda_client.invoke.side_effect = _invoke_side_effect

    def _boto3_client(service, **kwargs):
        return mock_lambda_client if service == 'lambda' else mock_s3

    mock_boto3.client.side_effect = _boto3_client

    event = {'format': 'reels', 'schedule': 'morning', 'time': '11:00'}
    response = lambda_handler.lambda_handler(event, _FakeContext())

    assert response['statusCode'] == 200, (
        f"Expected 200 but got {response['statusCode']}. Body: {response['body']}"
    )
    body = json.loads(response['body'])
    assert body['posts_published'] == 1, (
        "Instagram post should be counted as published even when YouTube Lambda invoke fails. "
        f"Got posts_published={body.get('posts_published')}"
    )

    # Verify reels_fn was invoked (Instagram path worked)
    reels_invoke_calls = [
        c for c in mock_lambda_client.invoke.call_args_list
        if 'reels' in c.kwargs.get('FunctionName', '')
    ]
    assert len(reels_invoke_calls) == 1, "reels_worker should have been invoked exactly once"

    # Verify youtube_fn invoke was attempted (the failure happened)
    youtube_invoke_calls = [
        c for c in mock_lambda_client.invoke.call_args_list
        if 'youtube' in c.kwargs.get('FunctionName', '')
    ]
    assert len(youtube_invoke_calls) == 1, "youtube_worker invoke should have been attempted"
