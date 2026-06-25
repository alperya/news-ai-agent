"""Tests for the daily Dutch-fact pipeline routing + feature-flag gate."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import lambda_handler as lh


class _Ctx:
    def get_remaining_time_in_millis(self):
        return 900_000


def test_flag_off_generates_nothing():
    with patch("dutch_facts.get_fact_for_today") as mock_get, \
         patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "false"}):
        resp = lh._run_daily_fact_pipeline("ts", "bkt")
    assert json.loads(resp["body"])["status"] == "skipped"
    mock_get.assert_not_called()  # no fact pick, no Pexels, no render


@patch("lambda_handler.boto3")
@patch("video.create_fact_video")
@patch("dutch_facts.get_fact_for_today")
def test_flag_on_renders_and_delegates_story(mock_get_fact, mock_create, mock_boto):
    mock_get_fact.return_value = {
        "id": "bikes-outnumber-people", "text": "t", "footage_queries": ["q"],
    }
    mock_s3, mock_lambda = MagicMock(), MagicMock()
    mock_boto.client.side_effect = lambda svc, *a, **k: {"s3": mock_s3, "lambda": mock_lambda}.get(svc, MagicMock())

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "true"}), \
         patch("builtins.open", mock_open(read_data=b"x")):
        resp = lh._run_daily_fact_pipeline("ts", "bkt", dry_run=False)

    assert resp["statusCode"] == 200
    mock_get_fact.assert_called_once()
    mock_create.assert_called_once()
    payload = json.loads(mock_lambda.invoke.call_args.kwargs["Payload"].decode())
    assert payload["publish_story"] is True
    assert payload["publish_reel"] is False


@patch("lambda_handler.boto3")
@patch("video.create_fact_video")
@patch("dutch_facts.get_fact_for_today")
def test_dry_run_does_not_delegate(mock_get_fact, mock_create, mock_boto):
    mock_get_fact.return_value = {"id": "bikes-outnumber-people", "text": "t", "footage_queries": ["q"]}
    mock_lambda = MagicMock()
    mock_boto.client.side_effect = lambda svc, *a, **k: {"lambda": mock_lambda}.get(svc, MagicMock())

    with patch.dict(os.environ, {"ENABLE_INSTAGRAM_STORIES": "true"}), \
         patch("builtins.open", mock_open(read_data=b"x")):
        resp = lh._run_daily_fact_pipeline("ts", "bkt", dry_run=True)

    assert json.loads(resp["body"])["status"] == "dry_run"
    mock_lambda.invoke.assert_not_called()


@patch("lambda_handler.get_secrets")
@patch("lambda_handler._run_daily_fact_pipeline")
def test_daily_fact_format_routes_to_pipeline(mock_pipeline, mock_secrets):
    mock_pipeline.return_value = {"statusCode": 200, "body": "{}"}
    lh.lambda_handler({"format": "daily_fact"}, _Ctx())
    mock_pipeline.assert_called_once()
