"""Infrastructure-as-code guard: the daily-fact EventBridge schedule must
stay wired to the main Lambda with the right cron + input."""

import re
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


# ── Architecture docs must not drift from Terraform ─────────────────────────

_ARCH = Path(__file__).parent.parent / "architecture"


def test_architecture_runtime_matches_terraform_crons():
    """The diagrams quote cron expressions. Prose drifts; Terraform does not.

    This is what keeps architecture/ honest: if someone retunes a schedule and
    forgets the doc, CI says so instead of the doc quietly becoming fiction.
    """
    runtime = (_ARCH / "runtime.md").read_text(encoding="utf-8")
    tf = "".join(
        (Path(__file__).parent.parent / "infrastructure" / "terraform" / name).read_text(
            encoding="utf-8"
        )
        for name in ("main.tf", "analytics.tf")
    )
    for cron in re.findall(r'schedule_expression = "([^"]+)"', tf):
        assert cron in runtime, f"{cron} is in Terraform but not in architecture/runtime.md"


def test_architecture_records_disabled_rules():
    """Both disabled rules must be shown as disabled — a diagram that implies
    the events post still fires is worse than no diagram."""
    runtime = (_ARCH / "runtime.md").read_text(encoding="utf-8")
    for rule in ("evening_schedule", "events_thursday_schedule"):
        idx = runtime.find(rule)
        assert idx != -1, f"{rule} missing from runtime.md"
        assert "DISABLED" in runtime[idx:idx + 200], f"{rule} not marked DISABLED"


def test_architecture_docs_cross_link():
    for name in ("sdlc.md", "runtime.md", "data.md", "README.md"):
        assert (_ARCH / name).exists(), f"architecture/{name} missing"
