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
