"""Tests for reels_worker publish toggles + Facebook cross-post.

Covers the daily-fact path (publish_reel=False, publish_story=True), the
feature-flag gate, and the best-effort Facebook leg.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

import reels_worker


def _event(**kw):
    e = {"s3_video_key": "facts/f.mp4", "bucket_name": "bkt", "timestamp": "ts"}
    e.update(kw)
    return e


@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_daily_fact_publishes_story_and_facebook(mock_secrets, mock_boto, mock_ig, mock_fb):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    ig = mock_ig.return_value
    ig.publish_story.return_value = {"id": "ig1", "type": "story"}
    fb = mock_fb.return_value
    fb.publish_story.return_value = {"id": "fb1", "type": "facebook_story"}

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "true", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(publish_reel=False, publish_story=True), None)

    assert resp["statusCode"] == 200
    ig.publish_story.assert_called_once()
    ig.publish_reels.assert_not_called()
    fb.publish_story.assert_called_once()


@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_flag_off_skips_story_and_facebook(mock_secrets, mock_boto, mock_ig, mock_fb):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(publish_reel=False, publish_story=True), None)

    assert resp["statusCode"] == 200
    mock_ig.return_value.publish_story.assert_not_called()
    mock_fb.return_value.publish_story.assert_not_called()


@patch("reels_worker.alert_on_exception")
@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_facebook_failure_does_not_fail_run(mock_secrets, mock_boto, mock_ig, mock_fb, mock_alert):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    mock_ig.return_value.publish_story.return_value = {"id": "ig1"}
    mock_fb.return_value.publish_story.side_effect = Exception("facebook boom")

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "true", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(publish_reel=False, publish_story=True), None)

    assert resp["statusCode"] == 200  # Instagram succeeded; FB is best-effort
    mock_ig.return_value.publish_story.assert_called_once()


@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_reel_only_is_default(mock_secrets, mock_boto, mock_ig, mock_fb):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    mock_ig.return_value.publish_reels.return_value = {"id": "reel1", "url": "u"}
    # FB cross-post may or may not run depending on FACEBOOK_PAGE_ID; give it a
    # serializable return so the response always encodes cleanly either way.
    mock_fb.return_value.publish_reel.return_value = {"id": "fbr", "type": "facebook_reel"}

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 200
    mock_ig.return_value.publish_reels.assert_called_once()
    mock_ig.return_value.publish_story.assert_not_called()


@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_reel_crossposts_to_facebook(mock_secrets, mock_boto, mock_ig, mock_fb):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    mock_ig.return_value.publish_reels.return_value = {"id": "reel1", "url": "u"}
    fb = mock_fb.return_value
    fb.publish_reel.return_value = {"id": "fbr1", "type": "facebook_reel"}

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 200
    mock_ig.return_value.publish_reels.assert_called_once()
    fb.publish_reel.assert_called_once()
    assert fb.publish_reel.call_args.kwargs["caption"] == "caption"


@patch("reels_worker.alert_on_exception")
@patch("reels_worker.FacebookPublisher")
@patch("reels_worker.InstagramPublisher")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_facebook_reel_failure_does_not_fail_run(mock_secrets, mock_boto, mock_ig, mock_fb, mock_alert):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    mock_ig.return_value.publish_reels.return_value = {"id": "reel1", "url": "u"}
    mock_fb.return_value.publish_reel.side_effect = Exception("fb reel boom")

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 200  # IG reel succeeded; FB is best-effort
    mock_ig.return_value.publish_reels.assert_called_once()


@patch("reels_worker.send_alert")
def test_missing_caption_for_reel_returns_400(mock_alert):
    resp = reels_worker.lambda_handler(_event(), None)  # publish_reel defaults True, no post_content
    assert resp["statusCode"] == 400
