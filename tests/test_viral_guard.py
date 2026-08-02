"""Tests for the viral-skip guard: threshold, guardrails and pipeline wiring."""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import viral_guard as vg


class _Ctx:
    def get_remaining_time_in_millis(self):
        return 900_000


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _collector(engagement: int, baseline_median: int = 100, baseline_count: int = 20):
    """A MetricsCollector stub: fixed live insights + a synthetic baseline scan."""
    collector = MagicMock()
    collector.account_id = "acct"
    collector.access_token = "tok"
    collector._post_insights.return_value = {
        "likes": engagement, "comments": 0, "shares": 0, "saved": 0,
    }
    collector._table.scan.return_value = {
        "Items": [
            {"post_id": f"m{i}", "likes": str(baseline_median), "comments": "0",
             "shares": "0", "saves": "0"}
            for i in range(baseline_count)
        ]
    }
    return collector


def _run(engagement, *, age_hours=3.0, media_id="viral-1", state=None,
         baseline_median=100, baseline_count=20, insights=None):
    """Drive should_skip_next_post with everything external stubbed out."""
    collector = _collector(engagement, baseline_median, baseline_count)
    if insights is not None:
        collector._post_insights.return_value = insights
    written = {}

    def _write(bucket, key, data):
        written[key] = data

    with patch.object(vg, "MetricsCollector", return_value=collector), \
         patch.object(vg, "_latest_media", return_value={
             "id": media_id, "timestamp": _iso(age_hours), "media_type": "VIDEO",
             "media_product_type": "REELS"}), \
         patch.object(vg, "_read_json", return_value=state or {}), \
         patch.object(vg, "_write_json", side_effect=_write), \
         patch.dict(os.environ, {"ENABLE_VIRAL_SKIP": "true"}):
        skip, payload = vg.should_skip_next_post("bkt")
    return skip, payload, written


# ── Flag ──────────────────────────────────────────────────────────────────────

def test_flag_off_checks_nothing():
    with patch.object(vg, "MetricsCollector") as mock_collector, \
         patch.dict(os.environ, {"ENABLE_VIRAL_SKIP": "false"}):
        skip, payload = vg.should_skip_next_post("bkt")
    assert skip is False
    assert payload["reason"] == "flag_disabled"
    mock_collector.assert_not_called()  # no Graph API call, no DynamoDB scan


# ── Threshold ─────────────────────────────────────────────────────────────────

def test_viral_post_skips_and_records_state():
    skip, payload, written = _run(engagement=2500, baseline_median=100)
    assert skip is True
    assert payload["reason"] == "previous_post_viral"
    assert payload["multiplier"] == 25.0
    assert written[vg._SKIP_STATE_KEY]["last_skipped_media_id"] == "viral-1"


def test_above_multiplier_but_below_absolute_floor_does_not_skip():
    # 800 is 8× a median of 100 but under the 1000 absolute floor.
    skip, payload, written = _run(engagement=800, baseline_median=100)
    assert skip is False
    assert payload["reason"] == "below_threshold"
    assert written == {}


def test_above_absolute_floor_but_below_multiplier_does_not_skip():
    # 1500 clears the floor but is only 3× a median of 500 (< 5×).
    skip, payload, _ = _run(engagement=1500, baseline_median=500)
    assert skip is False
    assert payload["reason"] == "below_threshold"


# ── Guardrails ────────────────────────────────────────────────────────────────

def test_old_post_is_never_viral():
    skip, payload, _ = _run(engagement=99_999, age_hours=30)
    assert skip is False
    assert payload["reason"] == "post_too_old"


def test_same_post_never_skips_twice():
    skip, payload, written = _run(
        engagement=9999, media_id="viral-1",
        state={"last_skipped_media_id": "viral-1"},
    )
    assert skip is False
    assert payload["reason"] == "already_skipped_once"
    assert written == {}


def test_insufficient_baseline_does_not_skip():
    skip, payload, _ = _run(engagement=9999, baseline_count=4)
    assert skip is False
    assert payload["reason"] == "insufficient_baseline"


def test_missing_insights_does_not_skip():
    skip, payload, _ = _run(engagement=9999, insights={})
    assert skip is False
    assert payload["reason"] == "no_insights"


def test_no_media_does_not_skip():
    with patch.object(vg, "MetricsCollector", return_value=MagicMock()), \
         patch.object(vg, "_latest_media", return_value={}), \
         patch.dict(os.environ, {"ENABLE_VIRAL_SKIP": "true"}):
        skip, payload = vg.should_skip_next_post("bkt")
    assert skip is False
    assert payload["reason"] == "no_media"


def test_graph_api_error_fails_open():
    with patch.object(vg, "MetricsCollector", return_value=MagicMock()), \
         patch.object(vg, "_latest_media", side_effect=RuntimeError("429 rate limited")), \
         patch.dict(os.environ, {"ENABLE_VIRAL_SKIP": "true"}):
        skip, payload = vg.should_skip_next_post("bkt")
    assert skip is False
    assert payload["reason"] == "guard_error"


def test_thresholds_are_read_at_call_time():
    # get_secrets() populates env during the invoke, i.e. after this module was
    # imported at container init — so a threshold set then must still take effect.
    with patch.dict(os.environ, {"VIRAL_MIN_ENGAGEMENT": "5000"}):
        skip, payload, _ = _run(engagement=2500, baseline_median=100)
    assert skip is False  # 25× the median, but now under the raised floor
    assert payload["reason"] == "below_threshold"


def test_malformed_threshold_falls_back_to_default():
    with patch.dict(os.environ, {"VIRAL_MIN_ENGAGEMENT": "not-a-number"}):
        skip, _, _ = _run(engagement=2500, baseline_median=100)
    assert skip is True  # default floor of 1000 still applies


def test_baseline_excludes_the_post_being_judged():
    collector = _collector(engagement=0, baseline_median=100, baseline_count=3)
    collector._table.scan.return_value["Items"].append(
        {"post_id": "viral-1", "likes": "50000", "comments": "0", "shares": "0", "saves": "0"}
    )
    values = vg._baseline_engagements(collector, exclude_id="viral-1")
    assert values == [100, 100, 100]  # the viral post itself never inflates its own baseline


# ── Pipeline wiring ───────────────────────────────────────────────────────────

@patch("lambda_handler.send_alert")
@patch("lambda_handler.DutchNewsScraper")
@patch("lambda_handler.should_skip_next_post")
@patch("lambda_handler.get_secrets")
def test_news_run_skips_before_scraping(mock_secrets, mock_guard, mock_scraper, mock_alert):
    mock_guard.return_value = (True, {
        "reason": "previous_post_viral", "media_id": "m1", "engagement": 3000,
        "median_engagement": 100, "multiplier": 30.0, "age_hours": 4.0, "baseline_posts": 20,
    })
    import lambda_handler as lh
    resp = lh.lambda_handler({"format": "reels"}, _Ctx())

    body = json.loads(resp["body"])
    assert body["status"] == "skipped"
    assert body["reason"] == "previous_post_viral"
    mock_scraper.assert_not_called()  # no scrape, no AI, no render
    mock_alert.assert_called_once()


@patch("lambda_handler._run_daily_fact_pipeline")
@patch("lambda_handler.should_skip_next_post")
@patch("lambda_handler.get_secrets")
def test_daily_fact_run_ignores_the_guard(mock_secrets, mock_guard, mock_pipeline):
    mock_pipeline.return_value = {"statusCode": 200, "body": "{}"}
    import lambda_handler as lh
    lh.lambda_handler({"format": "daily_fact"}, _Ctx())

    mock_guard.assert_not_called()  # Stories are a separate surface — never gated
    mock_pipeline.assert_called_once()
