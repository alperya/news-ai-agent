"""Tests for reels_worker orchestration over the CrossPoster.

The worker no longer knows about Instagram/Facebook directly — it dispatches a
REEL or STORY to `build_crossposter()` and maps a primary failure to a non-200
response. Channel fan-out / best-effort behaviour is covered in
test_publishing.py.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

import reels_worker
from publishing import REEL, STORY


def _event(**kw):
    e = {"s3_video_key": "facts/f.mp4", "bucket_name": "bkt", "timestamp": "ts"}
    e.update(kw)
    return e


def _crossposter(primary_error=None, results=None):
    cp = MagicMock()
    cp.publish.return_value = {
        "results": results or {"instagram": {"id": "ig1"}, "facebook": {"id": "fb1"}},
        "primary_error": primary_error,
    }
    return cp


@patch("reels_worker.build_crossposter")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_daily_fact_dispatches_story(mock_secrets, mock_boto, mock_build):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    cp = _crossposter()
    mock_build.return_value = cp

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "true", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(publish_reel=False, publish_story=True), None)

    assert resp["statusCode"] == 200
    cp.publish.assert_called_once()
    assert cp.publish.call_args.args[0] == STORY


@patch("reels_worker.build_crossposter")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_flag_off_skips_story(mock_secrets, mock_boto, mock_build):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    cp = _crossposter()
    mock_build.return_value = cp

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false", "FACEBOOK_PAGE_ID": "p1"}):
        resp = reels_worker.lambda_handler(_event(publish_reel=False, publish_story=True), None)

    assert resp["statusCode"] == 200
    cp.publish.assert_not_called()


@patch("reels_worker.build_crossposter")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_reel_is_default_and_dispatches_reel(mock_secrets, mock_boto, mock_build):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    cp = _crossposter()
    mock_build.return_value = cp

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 200
    cp.publish.assert_called_once()
    assert cp.publish.call_args.args[0] == REEL
    assert cp.publish.call_args.kwargs["caption"] == "caption"


@patch("reels_worker.build_crossposter")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_facebook_only_failure_still_200(mock_secrets, mock_boto, mock_build):
    # primary (Instagram) ok, a secondary error is recorded but primary_error is None
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    cp = _crossposter(results={"instagram": {"id": "ig1"}, "facebook": {"error": "boom"}})
    mock_build.return_value = cp

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 200  # Instagram succeeded; Facebook is best-effort


@patch("reels_worker.build_crossposter")
@patch("reels_worker.boto3")
@patch("reels_worker._load_secrets")
def test_primary_failure_returns_500(mock_secrets, mock_boto, mock_build):
    mock_boto.client.return_value.generate_presigned_url.return_value = "https://signed/v.mp4"
    cp = _crossposter(primary_error=Exception("instagram boom"))
    mock_build.return_value = cp

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false"}):
        resp = reels_worker.lambda_handler(_event(post_content="caption"), None)

    assert resp["statusCode"] == 500


@patch("reels_worker.send_alert")
def test_missing_caption_for_reel_returns_400(mock_alert):
    resp = reels_worker.lambda_handler(_event(), None)  # publish_reel defaults True, no post_content
    assert resp["statusCode"] == 400
