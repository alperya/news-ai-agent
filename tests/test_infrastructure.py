"""
Tests for infrastructure modules: notifier, lambda_handler, token_refresher,
and social_publisher dry-run paths.

Mock strategy: boto3 (SNS, S3, Secrets Manager), urllib (token refresh HTTP).
See local_only/test_architecture.md for the full pattern guide.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════════════════════
# notifier
# ═══════════════════════════════════════════════════════════════════════════════

from notifier import alert_on_exception, detect_error_type, send_alert, send_event_summary

_TOPIC = "arn:aws:sns:eu-central-1:123456789:test-topic"


class TestDetectErrorType:
    @pytest.mark.parametrize("msg,expected", [
        ("Cannot allocate memory", "OOM"),
        ("OOM error during processing", "OOM"),
        ("Token has expired, please refresh", "TOKEN_EXPIRED"),
        ("OAuthException code 190 session has expired", "TOKEN_EXPIRED"),
        ("Failed to publish Instagram post", "PUBLISH_FAILED"),
        ("Reels container upload failed", "PUBLISH_FAILED"),
        ("ffmpeg render failed with non-zero exit code", "VIDEO_ERROR"),
        ("video encoding error", "VIDEO_ERROR"),
        ("Something completely unexpected happened", "GENERAL"),
    ])
    def test_classification(self, msg, expected):
        assert detect_error_type(Exception(msg)) == expected


class TestSendAlert:
    def test_returns_false_without_topic_arn(self):
        env = {k: v for k, v in os.environ.items() if k != "SNS_ALERT_TOPIC_ARN"}
        with patch.dict(os.environ, env, clear=True):
            assert send_alert("Test", "Body") is False

    def test_returns_true_on_success(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            result = send_alert("Test subject", "Test message", error_type="OOM")
        assert result is True

    def test_emoji_in_subject(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_alert("Test", "Body", error_type="OOM")
        subject = mock_sns.publish.call_args[1]["Subject"]
        assert "💥" in subject

    def test_subject_truncated_to_100(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_alert("x" * 200, "Body")
        subject = mock_sns.publish.call_args[1]["Subject"]
        assert len(subject) <= 100

    def test_returns_false_on_client_error(self):
        from botocore.exceptions import ClientError
        mock_sns = MagicMock()
        mock_sns.publish.side_effect = ClientError(
            {"Error": {"Code": "InvalidParameter", "Message": "bad"}}, "Publish"
        )
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            assert send_alert("Test", "Body") is False


class TestAlertOnException:
    def test_formats_exception_type_in_body(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            alert_on_exception("Test failure", ValueError("something wrong"))
        body = mock_sns.publish.call_args[1]["Message"]
        assert "ValueError" in body
        assert "something wrong" in body


class TestSendEventSummary:
    def _summary(self, **overrides):
        base = {
            "status": "success",
            "timestamp": "2026-06-14 10:00:00 UTC",
            "source_results": [{"source": "Ticketmaster", "count": 5, "ok": True}],
            "raw_count": 5, "unique_count": 4, "valid_count": 3,
            "scored_count": 2,
            "all_scored": [
                {"title": "Amsterdam Festival", "location": "Amsterdam",
                 "start_date": "2026-06-20", "_score": 7},
            ],
            "score_prompt": "Score these events",
            "score_response": '{"scores": [{"index": 0, "score": 7}]}',
            "selected_events": [
                {"title": "Amsterdam Festival", "location": "Amsterdam",
                 "date_label": "Sat 20 Jun", "price": "Free", "emoji": "🎉"}
            ],
            "caption": "📅 THIS WEEK IN THE NETHERLANDS",
            "selection_prompt": "Select events",
            "selection_response": "{}",
            "publish_result": None,
        }
        base.update(overrides)
        return base

    def test_returns_false_without_topic_arn(self):
        env = {k: v for k, v in os.environ.items() if k != "SNS_ALERT_TOPIC_ARN"}
        with patch.dict(os.environ, env, clear=True):
            assert send_event_summary({"status": "success"}) is False

    def test_returns_true_on_success(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            assert send_event_summary(self._summary()) is True

    def test_body_contains_all_pipeline_stages(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_event_summary(self._summary())
        body = mock_sns.publish.call_args[1]["Message"]
        assert "STAGE 1" in body
        assert "STAGE 2" in body
        assert "STAGE 3" in body

    def test_source_names_appear_in_body(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_event_summary(self._summary())
        assert "Ticketmaster" in mock_sns.publish.call_args[1]["Message"]

    def test_full_score_response_not_truncated(self):
        """Regression: score_response must NOT be cut off at 500 chars (fixed in notifier.py)."""
        scores = [{"index": i, "score": 6} for i in range(20)]
        long_response = json.dumps({"scores": scores})
        assert len(long_response) > 500  # ensure the response is actually long

        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_event_summary(self._summary(
                score_response=long_response,
                all_scored=[{"title": f"Event {i}", "location": "Amsterdam",
                             "start_date": "2026-06-20", "_score": 6} for i in range(20)],
                scored_count=20,
            ))
        body = mock_sns.publish.call_args[1]["Message"]
        assert '"index": 19' in body  # last entry must be present

    def test_failed_status_shows_error(self):
        mock_sns = MagicMock()
        with patch.dict(os.environ, {"SNS_ALERT_TOPIC_ARN": _TOPIC}), \
             patch("notifier.boto3.client", return_value=mock_sns):
            send_event_summary(self._summary(status="ai_failed", error="AI returned empty result"))
        body = mock_sns.publish.call_args[1]["Message"]
        assert "ai_failed" in body.lower() or "AI_FAILED" in body


# ═══════════════════════════════════════════════════════════════════════════════
# token_refresher
# ═══════════════════════════════════════════════════════════════════════════════

import token_refresher


def _urlopen_mock(data: dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestRefreshToken:
    def test_returns_new_token_and_expiry(self):
        mock_resp = _urlopen_mock({"access_token": "new_token_abc", "expires_in": 5183944})
        with patch("token_refresher.urllib.request.urlopen", return_value=mock_resp):
            token, expires = token_refresher._refresh_token("old", "app_id", "app_secret")
        assert token == "new_token_abc"
        assert expires == 5183944

    def test_raises_on_api_error_response(self):
        mock_resp = _urlopen_mock({"error": {"message": "Invalid OAuth token.", "code": 190}})
        with patch("token_refresher.urllib.request.urlopen", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Meta API error"):
                token_refresher._refresh_token("bad_token", "app_id", "app_secret")

    def test_default_expiry_when_missing_from_response(self):
        mock_resp = _urlopen_mock({"access_token": "tok"})  # no expires_in
        with patch("token_refresher.urllib.request.urlopen", return_value=mock_resp):
            _, expires = token_refresher._refresh_token("old", "app", "secret")
        assert expires == 5183944  # ~60 days default


class TestGetPutSecret:
    def test_get_secret_parses_json(self):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"INSTAGRAM_ACCESS_TOKEN": "tok123"})
        }
        with patch("token_refresher.boto3.client", return_value=mock_client):
            result = token_refresher._get_secret()
        assert result["INSTAGRAM_ACCESS_TOKEN"] == "tok123"

    def test_put_secret_writes_json(self):
        mock_client = MagicMock()
        with patch("token_refresher.boto3.client", return_value=mock_client):
            token_refresher._put_secret({"INSTAGRAM_ACCESS_TOKEN": "new_tok"})
        call_kwargs = mock_client.put_secret_value.call_args[1]
        payload = json.loads(call_kwargs["SecretString"])
        assert payload["INSTAGRAM_ACCESS_TOKEN"] == "new_tok"


class TestTokenRefresherLambdaHandler:
    def test_successful_refresh_returns_200(self):
        secret = {"INSTAGRAM_ACCESS_TOKEN": "old", "META_APP_ID": "app", "META_APP_SECRET": "sec"}
        with patch("token_refresher._get_secret", return_value=secret), \
             patch("token_refresher._refresh_token", return_value=("new_tok", 5183944)), \
             patch("token_refresher._put_secret"):
            result = token_refresher.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert "refreshed" in json.loads(result["body"])["message"].lower()

    def test_missing_secrets_raises_value_error(self):
        incomplete = {"INSTAGRAM_ACCESS_TOKEN": "", "META_APP_ID": "", "META_APP_SECRET": ""}
        with patch("token_refresher._get_secret", return_value=incomplete), \
             patch("token_refresher._alert"):
            with pytest.raises(ValueError, match="Missing required secrets"):
                token_refresher.lambda_handler({}, None)

    def test_api_failure_calls_alert_then_re_raises(self):
        secret = {"INSTAGRAM_ACCESS_TOKEN": "old", "META_APP_ID": "app", "META_APP_SECRET": "sec"}
        with patch("token_refresher._get_secret", return_value=secret), \
             patch("token_refresher._refresh_token", side_effect=RuntimeError("API down")), \
             patch("token_refresher._alert") as mock_alert:
            with pytest.raises(RuntimeError):
                token_refresher.lambda_handler({}, None)
        mock_alert.assert_called_once()

    def test_new_token_written_to_secrets(self):
        secret = {"INSTAGRAM_ACCESS_TOKEN": "old", "META_APP_ID": "app", "META_APP_SECRET": "sec"}
        with patch("token_refresher._get_secret", return_value=secret), \
             patch("token_refresher._refresh_token", return_value=("fresh_token", 5183944)), \
             patch("token_refresher._put_secret") as mock_put:
            token_refresher.lambda_handler({}, None)
        written = mock_put.call_args[0][0]
        assert written["INSTAGRAM_ACCESS_TOKEN"] == "fresh_token"


class TestAlertSkipsWhenNoTopic:
    def test_alert_noop_without_topic_arn(self):
        env = {k: v for k, v in os.environ.items() if k != "SNS_ALERT_TOPIC_ARN"}
        with patch.dict(os.environ, env, clear=True), \
             patch("token_refresher.boto3.client") as mock_boto:
            token_refresher._alert("something failed")
        mock_boto.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# lambda_handler utility functions
# ═══════════════════════════════════════════════════════════════════════════════

import lambda_handler as lh


class TestSaveToS3:
    def test_calls_put_object_with_correct_bucket_and_key(self):
        mock_s3 = MagicMock()
        with patch("lambda_handler.boto3.client", return_value=mock_s3):
            lh.save_to_s3({"key": "value"}, "test.json", "my-bucket")
        call_kw = mock_s3.put_object.call_args[1]
        assert call_kw["Bucket"] == "my-bucket"
        assert call_kw["Key"] == "test.json"

    def test_content_type_is_json(self):
        mock_s3 = MagicMock()
        with patch("lambda_handler.boto3.client", return_value=mock_s3):
            lh.save_to_s3({}, "f.json", "bucket")
        assert mock_s3.put_object.call_args[1]["ContentType"] == "application/json"

    def test_body_is_json_serialised(self):
        mock_s3 = MagicMock()
        with patch("lambda_handler.boto3.client", return_value=mock_s3):
            lh.save_to_s3({"score": 7, "title": "Test"}, "x.json", "bucket")
        body = json.loads(mock_s3.put_object.call_args[1]["Body"])
        assert body["score"] == 7

    def test_serialises_list(self):
        mock_s3 = MagicMock()
        with patch("lambda_handler.boto3.client", return_value=mock_s3):
            lh.save_to_s3([{"id": 1}, {"id": 2}], "list.json", "bucket")
        body = json.loads(mock_s3.put_object.call_args[1]["Body"])
        assert len(body) == 2


class TestGetSecrets:
    def _setup_mock(self, secret_dict):
        mock_client = MagicMock()
        mock_client.get_secret_value.return_value = {"SecretString": json.dumps(secret_dict)}
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        return mock_session

    def test_required_env_vars_set(self):
        secret = {
            "ANTHROPIC_API_KEY": "ant-key-123",
            "INSTAGRAM_ACCESS_TOKEN": "ig-token-456",
            "INSTAGRAM_ACCOUNT_ID": "ig-acct-789",
        }
        with patch("lambda_handler.boto3.Session", return_value=self._setup_mock(secret)):
            lh.get_secrets()
        assert os.environ["ANTHROPIC_API_KEY"] == "ant-key-123"
        assert os.environ["INSTAGRAM_ACCESS_TOKEN"] == "ig-token-456"

    def test_optional_pexels_key_set_when_present(self):
        secret = {
            "ANTHROPIC_API_KEY": "k", "INSTAGRAM_ACCESS_TOKEN": "k", "INSTAGRAM_ACCOUNT_ID": "k",
            "PEXELS_API_KEY": "pexels-key-xyz",
        }
        with patch("lambda_handler.boto3.Session", return_value=self._setup_mock(secret)):
            lh.get_secrets()
        assert os.environ.get("PEXELS_API_KEY") == "pexels-key-xyz"

    def test_optional_elevenlabs_key_set_when_present(self):
        secret = {
            "ANTHROPIC_API_KEY": "k", "INSTAGRAM_ACCESS_TOKEN": "k", "INSTAGRAM_ACCOUNT_ID": "k",
            "ELEVENLABS_API_KEY": "el-key-abc",
        }
        with patch("lambda_handler.boto3.Session", return_value=self._setup_mock(secret)):
            lh.get_secrets()
        assert os.environ.get("ELEVENLABS_API_KEY") == "el-key-abc"

    def test_raises_client_error_on_missing_secret(self):
        from botocore.exceptions import ClientError
        mock_client = MagicMock()
        mock_client.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Not found"}},
            "GetSecretValue",
        )
        mock_session = MagicMock()
        mock_session.client.return_value = mock_client
        with patch("lambda_handler.boto3.Session", return_value=mock_session):
            with pytest.raises(ClientError):
                lh.get_secrets()

    def test_returns_secret_dict(self):
        secret = {
            "ANTHROPIC_API_KEY": "k", "INSTAGRAM_ACCESS_TOKEN": "k", "INSTAGRAM_ACCOUNT_ID": "k",
        }
        with patch("lambda_handler.boto3.Session", return_value=self._setup_mock(secret)):
            result = lh.get_secrets()
        assert result["ANTHROPIC_API_KEY"] == "k"


# ── Retention (2 years) ──────────────────────────────────────────────────────

_TF = Path(__file__).parent.parent / "infrastructure" / "terraform"


def test_post_metrics_table_has_ttl():
    """Without a TTL the table grows forever while every reader asks for 7-30
    days — and both readers use scan + FilterExpression, whose cost tracks
    total table size rather than the window."""
    tf = (_TF / "analytics.tf").read_text(encoding="utf-8")
    table = tf.split('resource "aws_dynamodb_table" "post_metrics"')[1].split("\nresource ")[0]
    assert "ttl {" in table
    assert 'attribute_name = "expires_at"' in table
    assert "enabled        = true" in table


def test_prompt_versions_has_no_ttl_on_purpose():
    """This table is the rollback surface for auto-applied prompt changes.
    Expiring rows quietly shortens how far back a recovery can reach, so the
    2-year decision (which was about posts and engagement) does not apply."""
    tf = (_TF / "analytics.tf").read_text(encoding="utf-8")
    table = tf.split('resource "aws_dynamodb_table" "prompt_versions"')[1].split("\nresource ")[0]
    assert "ttl {" not in table


def test_unruled_root_prefixes_now_have_lifecycle_rules():
    """posts_/articles_/pipeline_results_ matched no rule at all before this:
    never transitioned, never expired, kept forever."""
    tf = (_TF / "main.tf").read_text(encoding="utf-8")
    lifecycle = tf.split('resource "aws_s3_bucket_lifecycle_configuration"')[1].split("\nresource ")[0]
    for prefix in ('prefix = "articles_"', 'prefix = "pipeline_results_"', 'prefix = "posts_"'):
        assert prefix in lifecycle, f"no lifecycle rule for {prefix}"
    assert "noncurrent_version_expiration" in lifecycle


def test_posts_prefix_is_never_archived_or_expired():
    """The load-bearing exemption.

    get_published_urls reads the BODY of every posts_ object on every run for
    all-time URL dedup. A GLACIER transition would make those unreadable
    without a restore, breaking URL dedup, the 3-day title window, the 7-day
    content mix, the violence cap and footage dedup at once; an expiration
    would let a two-year-old article be republished as new.
    """
    tf = (_TF / "main.tf").read_text(encoding="utf-8")
    lifecycle = tf.split('resource "aws_s3_bucket_lifecycle_configuration"')[1].split("\nresource ")[0]
    posts_rule = None
    for block in lifecycle.split("  rule {"):
        if 'prefix = "posts_"' in block:
            posts_rule = block
    assert posts_rule, "no posts_ rule found"
    assert "GLACIER" not in posts_rule, "posts_ must stay directly readable"
    assert "expiration {" not in posts_rule.replace("noncurrent_version_expiration", "")
    assert "STANDARD_IA" in posts_rule


def test_metrics_collector_stamps_two_year_expiry():
    """DynamoDB only expires items that CARRY the attribute."""
    import metrics_collector as MC

    published = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    expires = MC._expires_at(published)
    assert isinstance(expires, int)  # TTL reads a Number, not a String
    delta_days = (expires - published.timestamp()) / 86400
    assert 729 <= delta_days <= 731


def test_expiry_anchored_to_publication_not_collection(monkeypatch):
    """collect() re-writes the trailing 30 days daily. Anchoring to `now`
    would slide the expiry forward on every pass, so rows would never age out."""
    import metrics_collector as MC

    old_post = datetime(2025, 1, 1, tzinfo=timezone.utc)
    first = MC._expires_at(old_post)
    second = MC._expires_at(old_post)  # a later collection run, same post
    assert first == second


def test_retention_days_is_tunable(monkeypatch):
    import metrics_collector as MC

    monkeypatch.setenv("METRICS_RETENTION_DAYS", "365")
    published = datetime(2026, 8, 1, tzinfo=timezone.utc)
    delta_days = (MC._expires_at(published) - published.timestamp()) / 86400
    assert 364 <= delta_days <= 366
