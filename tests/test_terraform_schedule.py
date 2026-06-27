"""Infrastructure-as-code guard: the daily-fact EventBridge schedule must
stay wired to the main Lambda with the right cron + input."""
from pathlib import Path

_TF = (Path(__file__).parent.parent / "infrastructure" / "terraform" / "main.tf").read_text()


def test_daily_fact_rule_exists():
    assert 'aws_cloudwatch_event_rule" "daily_fact_schedule"' in _TF


def test_daily_fact_cron_is_06_utc():
    # 08:00 Amsterdam (CEST) == 06:00 UTC
    assert "cron(0 6 * * ? *)" in _TF


def test_daily_fact_target_sends_daily_fact_format():
    assert 'format = "daily_fact"' in _TF


def test_daily_fact_has_invoke_permission():
    assert 'aws_lambda_permission" "allow_daily_fact_eventbridge"' in _TF


# ── Guaranteed failure alerting (async on-failure destinations) ──────────────

def test_failure_alert_destinations_exist():
    """Every Lambda must route async failures (incl. timeout) to the SNS topic."""
    assert 'aws_lambda_function_event_invoke_config" "failure_alert"' in _TF
    assert "on_failure" in _TF
    assert "destination = aws_sns_topic.alerts[0].arn" in _TF


def test_failure_alert_covers_all_lambdas():
    """All six functions must be in the failure-alert map."""
    for fn in (
        "aws_lambda_function.news_agent.function_name",
        "aws_lambda_function.reels_publish.function_name",
        "aws_lambda_function.youtube_publish.function_name",
        "aws_lambda_function.token_refresh.function_name",
        "aws_lambda_function.metrics_collector.function_name",
        "aws_lambda_function.analytics_engine.function_name",
    ):
        assert fn in _TF


def test_main_render_lambda_does_not_retry_on_failure():
    """Heavy main render: retries=0 so a timeout alerts immediately (no 3× burn)."""
    assert "maximum_retry_attempts = each.value.retries" in _TF
    assert "retries = 0 }" in _TF
