"""Tests for the failure-visibility layers.

Covers the drought watchdog (does Instagram actually have a recent post?), the
"published nothing" alerts on the previously silent pipeline exits, the CI health
check, and the top-level except re-raising so AWS sees a failed invocation.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))
sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

from metrics_collector import MetricsCollector


class _Ctx:
    def get_remaining_time_in_millis(self):
        return 900_000


def _media(hours_ago: float) -> list:
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S+0000")
    return [{"id": "m1", "timestamp": ts, "media_type": "VIDEO"}]


def _collector() -> MetricsCollector:
    return MetricsCollector.__new__(MetricsCollector)  # no AWS/Instagram env needed


# ── Layer 1: drought watchdog ─────────────────────────────────────────────────

def test_recent_post_is_healthy():
    with patch("metrics_collector.send_alert") as alert:
        result = _collector().check_publish_drought(_media(hours_ago=7))
    assert result is None
    alert.assert_not_called()


def test_stale_post_alerts():
    with patch("metrics_collector.send_alert") as alert:
        result = _collector().check_publish_drought(_media(hours_ago=40))
    assert result["drought"] is True
    assert result["hours_since_last_post"] == pytest.approx(40, abs=0.2)
    alert.assert_called_once()
    assert "PUBLISH_FAILED" == alert.call_args[0][2]


def test_gap_from_one_viral_skip_is_not_a_drought():
    # Largest legitimate gap: the viral guard skips one of the two daily slots.
    with patch("metrics_collector.send_alert") as alert:
        assert _collector().check_publish_drought(_media(hours_ago=24)) is None
    alert.assert_not_called()


def test_empty_media_alerts_loudly():
    with patch("metrics_collector.send_alert") as alert:
        result = _collector().check_publish_drought([])
    assert result["drought"] is True
    assert result["hours_since_last_post"] is None
    assert "30 days" in alert.call_args[0][0]


def test_threshold_is_read_at_call_time():
    # get_secrets() populates env during the invoke, after this module was imported.
    with patch("metrics_collector.send_alert") as alert, \
         patch.dict(os.environ, {"PUBLISH_DROUGHT_HOURS": "6"}):
        assert _collector().check_publish_drought(_media(hours_ago=7)) is not None
    alert.assert_called_once()


def test_collect_runs_the_watchdog_even_with_nothing_to_collect():
    collector = _collector()
    collector.collect_account_metrics = MagicMock(return_value={"followers_count": 100})
    collector._recent_media = MagicMock(return_value=[])
    with patch("metrics_collector.send_alert") as alert:
        result = collector.collect()
    assert result["drought"]["drought"] is True
    alert.assert_called_once()


# ── Layer 2: every "published nothing" exit alerts ────────────────────────────

def _run_news_pipeline(articles, posts=None, reviewed=None):
    """Drive the news pipeline to one of its early exits. Returns (body, send_alert mock)."""
    scraper = MagicMock()
    scraper.scrape_all_sources.return_value = [MagicMock(to_dict=lambda: a) for a in articles]

    agent = MagicMock()
    agent.process_batch.return_value = posts if posts is not None else []
    agent.quality_check.side_effect = reviewed

    import lambda_handler as lh
    with patch("lambda_handler.get_secrets"), \
         patch("lambda_handler.should_skip_next_post", return_value=(False, {})), \
         patch("lambda_handler.DutchNewsScraper", return_value=scraper), \
         patch("lambda_handler.get_published_urls", return_value=(set(), [], {})), \
         patch("lambda_handler.NewsAIAgent", return_value=agent), \
         patch("lambda_handler.save_to_s3"), \
         patch("lambda_handler.send_alert") as alert:
        resp = lh.lambda_handler({"format": "reels"}, _Ctx())
    return json.loads(resp["body"]), alert


def test_all_duplicates_alerts():
    article = {"title": "t", "url": "https://nos.nl/1", "source": "nos"}
    import lambda_handler as lh
    scraper = MagicMock()
    scraper.scrape_all_sources.return_value = [MagicMock(to_dict=lambda: article)]
    with patch("lambda_handler.get_secrets"), \
         patch("lambda_handler.should_skip_next_post", return_value=(False, {})), \
         patch("lambda_handler.DutchNewsScraper", return_value=scraper), \
         patch("lambda_handler.get_published_urls",
               return_value=({"https://nos.nl/1"}, [], {})), \
         patch("lambda_handler.save_to_s3"), \
         patch("lambda_handler.send_alert") as alert:
        resp = lh.lambda_handler({"format": "reels"}, _Ctx())

    assert json.loads(resp["body"])["status"] == "all_duplicates"
    alert.assert_called_once()  # no longer silent


def test_no_posts_generated_alerts():
    body, alert = _run_news_pipeline(
        articles=[{"title": "t", "url": "https://nos.nl/1", "source": "nos"}],
        posts=[],
    )
    assert body["status"] == "no_posts"
    alert.assert_called_once()


def test_quality_gate_rejecting_everything_alerts():
    post = MagicMock()
    post.to_dict.return_value = {"hook": "h", "content": "c"}
    body, alert = _run_news_pipeline(
        articles=[{"title": "t", "url": "https://nos.nl/1", "source": "nos"}],
        posts=[post],
        reviewed=[None],  # quality gate rejects it
    )
    assert body["status"] == "quality_gate_rejected"
    alert.assert_called_once()


# ── Layer 3: CI health check ──────────────────────────────────────────────────

@patch("lambda_handler.DutchNewsScraper")
@patch("lambda_handler.should_skip_next_post")
@patch("lambda_handler.get_secrets")
def test_health_check_returns_healthy_without_doing_work(mock_secrets, mock_guard, mock_scraper):
    import lambda_handler as lh
    resp = lh.lambda_handler({"health_check": True}, _Ctx())

    assert json.loads(resp["body"])["status"] == "healthy"
    mock_secrets.assert_called_once()   # proves secrets are readable
    mock_scraper.assert_not_called()    # but nothing is scraped, generated or published
    mock_guard.assert_not_called()


# ── Layer 4: failures reach AWS ───────────────────────────────────────────────

@patch("lambda_handler.alert_on_exception")
@patch("lambda_handler.save_to_s3")
@patch("lambda_handler.get_secrets")
def test_runtime_error_is_alerted_and_re_raised(mock_secrets, mock_save, mock_alert):
    # Raising is what gives AWS an Errors datapoint, arms the CloudWatch alarm and
    # fires the on-failure destination — signals that a returned 500 dict suppressed.
    mock_secrets.side_effect = RuntimeError("secret store unreachable")
    import lambda_handler as lh
    with pytest.raises(RuntimeError, match="secret store unreachable"):
        lh.lambda_handler({"format": "reels"}, _Ctx())

    mock_alert.assert_called_once()  # the in-code email still goes out first
    mock_save.assert_called_once()   # and the S3 error trace is still written
