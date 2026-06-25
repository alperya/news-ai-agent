"""Unit tests for Instagram Story + Facebook Page Story publishing.

Mock strategy: patch `social_publisher.requests`; no real network calls.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

os.environ.setdefault("INSTAGRAM_ACCESS_TOKEN", "user-token")
os.environ.setdefault("INSTAGRAM_ACCOUNT_ID", "ig-acct")

from social_publisher import InstagramPublisher, FacebookPublisher


def _resp(json_data):
    r = MagicMock()
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


class TestInstagramStory:
    @patch("social_publisher.requests")
    def test_uses_stories_media_type_without_caption(self, mock_req):
        mock_req.post.side_effect = [_resp({"id": "container1"}), _resp({"id": "media1"})]
        mock_req.get.return_value = _resp({"status_code": "FINISHED"})

        result = InstagramPublisher().publish_story(video_url="https://x/v.mp4")

        assert result == {"id": "media1", "creation_id": "container1", "type": "story"}
        params = mock_req.post.call_args_list[0].kwargs["params"]
        assert params["media_type"] == "STORIES"
        assert params["video_url"] == "https://x/v.mp4"
        assert "caption" not in params  # Stories reject captions

    def test_dry_run_skips_api(self):
        with patch("social_publisher.requests") as mock_req:
            result = InstagramPublisher().publish_story(video_url="https://x/v.mp4", dry_run=True)
            mock_req.post.assert_not_called()
        assert result["id"] == "dry_run"
        assert result["type"] == "story"


class TestFacebookStory:
    @patch("social_publisher.requests")
    def test_full_video_stories_flow(self, mock_req):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        mock_req.get.side_effect = [
            _resp({"access_token": "page-token"}),  # derive page token
            _resp({"status": {"uploading_phase": {"status": "complete"},
                              "processing_phase": {"status": "complete"}}}),  # ready
        ]
        mock_req.post.side_effect = [
            _resp({"video_id": "v1", "upload_url": "https://rupload/v1"}),  # start
            _resp({"success": True}),                                       # upload
            _resp({"post_id": "p1", "success": True}),                      # finish
        ]

        result = FacebookPublisher().publish_story(video_url="https://x/v.mp4")

        assert result["id"] == "p1"
        assert result["type"] == "facebook_story"
        # start phase
        assert mock_req.post.call_args_list[0].kwargs["params"]["upload_phase"] == "start"
        # upload handed Meta the hosted file URL
        assert mock_req.post.call_args_list[1].kwargs["headers"]["file_url"] == "https://x/v.mp4"
        # finish phase references the video_id
        assert mock_req.post.call_args_list[2].kwargs["params"]["upload_phase"] == "finish"

    def test_missing_page_id_raises(self):
        env = {k: v for k, v in os.environ.items() if k != "FACEBOOK_PAGE_ID"}
        with patch.dict(os.environ, env, clear=True):
            os.environ["INSTAGRAM_ACCESS_TOKEN"] = "user-token"
            with pytest.raises(ValueError, match="FACEBOOK_PAGE_ID"):
                FacebookPublisher()

    def test_dry_run_skips_api(self):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        with patch("social_publisher.requests") as mock_req:
            result = FacebookPublisher().publish_story("https://x/v.mp4", dry_run=True)
            mock_req.post.assert_not_called()
        assert result["id"] == "dry_run"
        assert result["type"] == "facebook_story"


class TestFacebookReel:
    @patch("social_publisher.requests")
    def test_full_video_reels_flow(self, mock_req):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        mock_req.get.side_effect = [
            _resp({"access_token": "page-token"}),
            _resp({"status": {"uploading_phase": {"status": "complete"},
                              "processing_phase": {"status": "complete"}}}),
        ]
        mock_req.post.side_effect = [
            _resp({"video_id": "v1", "upload_url": "https://rupload/v1"}),  # start
            _resp({"success": True}),                                       # upload
            _resp({"post_id": "p1"}),                                       # finish
        ]

        result = FacebookPublisher().publish_reel(video_url="https://x/v.mp4", caption="hi #nl")

        assert result["id"] == "p1"
        assert result["type"] == "facebook_reel"
        # start uses the video_reels endpoint
        assert "video_reels" in mock_req.post.call_args_list[0].args[0]
        assert mock_req.post.call_args_list[0].kwargs["params"]["upload_phase"] == "start"
        assert mock_req.post.call_args_list[1].kwargs["headers"]["file_url"] == "https://x/v.mp4"
        # finish publishes with the caption
        fin = mock_req.post.call_args_list[2].kwargs["params"]
        assert fin["upload_phase"] == "finish"
        assert fin["video_state"] == "PUBLISHED"
        assert fin["description"] == "hi #nl"

    def test_dry_run_skips_api(self):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        with patch("social_publisher.requests") as mock_req:
            result = FacebookPublisher().publish_reel("https://x/v.mp4", dry_run=True)
            mock_req.post.assert_not_called()
        assert result["type"] == "facebook_reel"


class TestFacebookPhoto:
    @patch("social_publisher.requests")
    def test_publishes_photo_with_caption(self, mock_req):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        mock_req.get.return_value = _resp({"access_token": "page-token"})
        mock_req.post.return_value = _resp({"id": "ph1", "post_id": "pp1"})

        result = FacebookPublisher().publish_photo(image_url="https://x/i.jpg", caption="cap")

        assert result["id"] == "pp1"
        assert result["type"] == "facebook_photo"
        params = mock_req.post.call_args.kwargs["params"]
        assert params["url"] == "https://x/i.jpg"
        assert params["caption"] == "cap"
        assert "photos" in mock_req.post.call_args.args[0]

    def test_dry_run_skips_api(self):
        os.environ["FACEBOOK_PAGE_ID"] = "page1"
        with patch("social_publisher.requests") as mock_req:
            result = FacebookPublisher().publish_photo("https://x/i.jpg", dry_run=True)
            mock_req.post.assert_not_called()
        assert result["type"] == "facebook_photo"
