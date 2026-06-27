"""Unit tests for LinkedIn Company Page Reel publishing.

Mock strategy: patch `social_publisher.requests`; no real network calls.
"""
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests as real_requests

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from social_publisher import LinkedInPublisher
from publishing import REEL, PHOTO, STORY


@pytest.fixture(autouse=True)
def _linkedin_env(monkeypatch):
    """Pin deterministic LinkedIn creds (a conftest may load a real .env)."""
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "li-token")
    monkeypatch.setenv("LINKEDIN_ORG_ID", "123")


def _resp(json_data=None, content=b"", headers=None):
    r = MagicMock()
    r.json.return_value = json_data or {}
    r.content = content
    r.headers = headers or {}
    r.raise_for_status.return_value = None
    return r


class TestAdapterInterface:
    def test_supports_reel_only(self):
        li = LinkedInPublisher()
        assert li.name == "linkedin"
        assert li.supports(REEL)
        assert not li.supports(PHOTO)
        assert not li.supports(STORY)

    def test_author_urn_built_from_org_id(self):
        assert LinkedInPublisher().author_urn == "urn:li:organization:123"

    def test_publish_delegates_reel(self):
        li = LinkedInPublisher()
        with patch.object(li, "publish_reel") as m:
            li.publish(REEL, media_url="v.mp4", caption="c")
            m.assert_called_once_with(video_url="v.mp4", caption="c", dry_run=False)

    def test_publish_rejects_unsupported_kind(self):
        with pytest.raises(ValueError, match="does not support"):
            LinkedInPublisher().publish(STORY, media_url="v.mp4")

    def test_missing_token_raises(self):
        with patch.dict(os.environ, {"LINKEDIN_ORG_ID": "123"}, clear=True):
            with pytest.raises(ValueError, match="LINKEDIN_ACCESS_TOKEN"):
                LinkedInPublisher()

    def test_missing_org_id_raises(self):
        with patch.dict(os.environ, {"LINKEDIN_ACCESS_TOKEN": "t"}, clear=True):
            with pytest.raises(ValueError, match="LINKEDIN_ORG_ID"):
                LinkedInPublisher()


class TestLinkedInReel:
    @patch("social_publisher.requests")
    def test_full_upload_and_post_flow(self, mock_req):
        mock_req.get.side_effect = [
            _resp(content=b"video-bytes"),                 # download
            _resp({"status": "AVAILABLE"}),                # processing status
        ]
        mock_req.post.side_effect = [
            _resp({"value": {"video": "urn:li:video:1",    # initializeUpload
                             "uploadInstructions": [
                                 {"uploadUrl": "https://up/1", "firstByte": 0, "lastByte": 10}]}}),
            _resp({}),                                     # finalizeUpload
            _resp(headers={"x-restli-id": "urn:li:share:99"}),  # create post
        ]
        mock_req.put.return_value = _resp(headers={"ETag": "etag-1"})

        result = LinkedInPublisher().publish_reel(
            video_url="https://x/v.mp4", caption="hello #nl")

        assert result["id"] == "urn:li:share:99"
        assert result["video_urn"] == "urn:li:video:1"
        assert result["type"] == "linkedin_reel"

        # initializeUpload owner + size
        init_body = mock_req.post.call_args_list[0].kwargs["json"]["initializeUploadRequest"]
        assert init_body["owner"] == "urn:li:organization:123"
        assert init_body["fileSizeBytes"] == len(b"video-bytes")

        # bytes PUT to the upload URL, ETag collected into finalize
        assert mock_req.put.call_args.args[0] == "https://up/1"
        finalize_body = mock_req.post.call_args_list[1].kwargs["json"]["finalizeUploadRequest"]
        assert finalize_body["uploadedPartIds"] == ["etag-1"]
        assert finalize_body["video"] == "urn:li:video:1"

        # create post references the video + commentary
        post_body = mock_req.post.call_args_list[2].kwargs["json"]
        assert post_body["author"] == "urn:li:organization:123"
        assert post_body["commentary"] == "hello #nl"
        assert post_body["content"]["media"]["id"] == "urn:li:video:1"
        assert post_body["lifecycleState"] == "PUBLISHED"

    @patch("social_publisher.requests")
    def test_processing_failure_raises(self, mock_req):
        # Keep the real exception class so the adapter's except-clause is valid
        mock_req.exceptions.HTTPError = real_requests.exceptions.HTTPError
        mock_req.get.side_effect = [
            _resp(content=b"v"),
            _resp({"status": "PROCESSING_FAILED"}),
        ]
        mock_req.post.side_effect = [
            _resp({"value": {"video": "urn:li:video:1",
                             "uploadInstructions": [
                                 {"uploadUrl": "https://up/1", "firstByte": 0, "lastByte": 0}]}}),
            _resp({}),  # finalize
        ]
        mock_req.put.return_value = _resp(headers={"ETag": "etag-1"})

        with pytest.raises(ValueError, match="processing failed"):
            LinkedInPublisher().publish_reel(video_url="https://x/v.mp4")

    def test_dry_run_skips_api(self):
        with patch("social_publisher.requests") as mock_req:
            result = LinkedInPublisher().publish_reel("https://x/v.mp4", dry_run=True)
            mock_req.get.assert_not_called()
            mock_req.post.assert_not_called()
        assert result["id"] == "dry_run"
        assert result["type"] == "linkedin_reel"
