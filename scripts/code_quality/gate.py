#!/usr/bin/env python3
"""Compare a code-quality analysis against the previous commit and alert.

    python scripts/code_quality/gate.py result.json \
        --history code_quality/history.json \
        --bucket news-ai-agent-results-... \
        --publish

DELIBERATELY NON-BLOCKING. This exits 0 even on a regression unless `--strict`
is passed. Every push to `main` auto-deploys with no approval and no rollback,
and the Lambda is on a cron that publishes twice a day; failing the build on a
*ratchet* metric would mean one legitimate refactor blocks a production fix.
The requested behaviour is an email, and an email is what this sends.

(The linters in the `lint` job DO block. That asymmetry is intentional: a lint
failure just leaves the previous working version running on its cron, whereas a
metric ratchet is a judgement call that should inform, not gate.)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from code_quality_report import (  # noqa: E402  (path shim must run first)
    DEFAULT_RATCHET_TOLERANCE,
    compare,
    render_detail,
    render_table,
)

HISTORY_KEY = "code_quality/history.json"
MAX_HISTORY = 500


def load_metric_config(root: Path) -> tuple[dict, float]:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return {}, DEFAULT_RATCHET_TOLERANCE
    with pyproject.open("rb") as fh:
        cfg = tomllib.load(fh).get("tool", {}).get("code_quality", {})
    return cfg.get("metrics", {}), cfg.get("ratchet_tolerance", DEFAULT_RATCHET_TOLERANCE)


def _s3():
    import boto3
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-central-1"))


def read_history(bucket: str | None, local: Path | None) -> dict:
    if local and local.exists():
        return json.loads(local.read_text(encoding="utf-8"))
    if bucket:
        try:
            body = _s3().get_object(Bucket=bucket, Key=HISTORY_KEY)["Body"].read()
            return json.loads(body)
        except Exception as exc:  # first run has no history object yet
            print(f"note: no history read from s3://{bucket}/{HISTORY_KEY} ({exc})")
    return {"config": {}, "entries": []}


def write_history(history: dict, bucket: str | None, local: Path | None) -> None:
    """Persist history, degrading to a warning if S3 is not writable.

    The CI deploy role is created out-of-band (only its ARN is in
    `secrets.AWS_ROLE_ARN`; it is not defined in `infrastructure/terraform/`),
    so its exact permissions are not knowable from this repo. If it lacks
    `s3:PutObject` on `code_quality/*`, that should surface as a warning to fix,
    not as a red job on a metrics step that is non-blocking by design.
    """
    payload = json.dumps(history, indent=2)
    if local:
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(payload, encoding="utf-8")
    if bucket:
        try:
            _s3().put_object(
                Bucket=bucket, Key=HISTORY_KEY,
                Body=payload.encode("utf-8"), ContentType="application/json",
            )
            print(f"wrote s3://{bucket}/{HISTORY_KEY} ({len(history['entries'])} entries)")
        except Exception as exc:
            print(
                f"::warning::could not write s3://{bucket}/{HISTORY_KEY} ({exc}). "
                "The deploy role likely needs s3:PutObject on code_quality/* — "
                "see local_only/ci_role_policy.md."
            )


def resolve_topic_arn(topic_name: str) -> str | None:
    """Find the alerts topic ARN without needing a GitHub secret.

    `notifier.send_alert` reads `SNS_ALERT_TOPIC_ARN` from the environment. In
    Lambda, Terraform injects it. In CI there is no such variable, and adding a
    repo secret would mean the ARN is configured in two places that can drift.
    Since the job already holds OIDC credentials, derive it instead: account id
    from STS + the region + the Terraform-defined topic name
    (`${var.project_name}-alerts`).
    """
    if os.environ.get("SNS_ALERT_TOPIC_ARN"):
        return os.environ["SNS_ALERT_TOPIC_ARN"]
    try:
        import boto3
        region = os.environ.get("AWS_REGION", "eu-central-1")
        account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
        return f"arn:aws:sns:{region}:{account}:{topic_name}"
    except Exception as exc:
        print(f"::warning::could not resolve SNS topic '{topic_name}': {exc}")
        return None


def publish_alert(subject: str, body: str, topic_name: str) -> bool:
    """Send via the existing SNS alert topic.

    Uses `src/notifier.py::send_alert` rather than a fresh boto3 publish —
    `selection_reviewer.py` and `analytics_engine.py` already each hand-rolled
    their own copy of that logic, and a fourth would be one more place to fix
    when the topic or the subject format changes.
    """
    arn = resolve_topic_arn(topic_name)
    if not arn:
        return False
    os.environ["SNS_ALERT_TOPIC_ARN"] = arn  # what send_alert reads
    try:
        from notifier import send_alert
        return send_alert(subject, body, error_type="CODE_QUALITY")
    except Exception as exc:
        print(f"::warning::could not send code-quality alert: {exc}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("result", help="JSON produced by analyze.py")
    ap.add_argument("--history", help="local history.json path (also written)")
    ap.add_argument("--bucket", help="S3 bucket holding code_quality/history.json")
    ap.add_argument("--publish", action="store_true", help="send SNS mail on regression")
    ap.add_argument(
        "--topic-name", default="news-ai-agent-alerts",
        help="SNS topic name, resolved to an ARN via STS when the env var is unset",
    )
    ap.add_argument("--append", action="store_true", help="append this result to history")
    ap.add_argument(
        "--strict", action="store_true",
        help="exit non-zero on regression (NOT used in CI — see module docstring)",
    )
    args = ap.parse_args()

    current = json.loads(Path(args.result).read_text(encoding="utf-8"))
    metric_cfg, tolerance = load_metric_config(ROOT)

    local_history = Path(args.history) if args.history else None
    history = read_history(args.bucket, local_history)
    entries: list[dict] = history.get("entries", [])

    # Previous = the most recent entry that is not this same commit, so a re-run
    # of CI on an unchanged commit compares against real history rather than
    # against itself (which would always report "no movement").
    commit = current.get("commit")
    previous = next((e for e in reversed(entries) if e.get("commit") != commit), None)

    rows = compare(current, previous, metric_cfg or None, tolerance)
    regressions = [r for r in rows if r["regressed"]]
    breaches = [r for r in rows if r["breaches_gate"]]

    table = render_table(rows)
    print(table)
    print()

    if args.append:
        entries = [e for e in entries if e.get("commit") != commit]
        entries.append(current)
        entries.sort(key=lambda e: e.get("commit_date") or "")
        history["entries"] = entries[-MAX_HISTORY:]
        history["config"] = metric_cfg
        write_history(history, args.bucket, local_history)

    if not regressions and not breaches:
        print("No code-quality regression against "
              f"{previous.get('commit_short') if previous else 'baseline'}.")
        return 0

    headline = []
    if breaches:
        headline.append(
            f"{len(breaches)} metric(s) breached the hard gate: "
            + ", ".join(r["label"] for r in breaches)
        )
    if regressions:
        headline.append(
            f"{len(regressions)} metric(s) worse than "
            f"{previous.get('commit_short') if previous else 'baseline'}: "
            + ", ".join(r["label"] for r in regressions)
        )

    body = "\n".join([
        "Code quality moved in the wrong direction on this commit.",
        "",
        *headline,
        "",
        f"Commit:  {current.get('commit_short')}  {current.get('commit_subject', '')}",
        f"Date:    {current.get('commit_date', '')}",
        "",
        table,
        "",
        render_detail(current),
        "",
        "This did NOT block the deploy — the code shipped. The gate is",
        "informational by design; see scripts/code_quality/gate.py.",
    ])
    print(body)

    if args.publish:
        subject = f"📉 Code Quality Regression — {current.get('commit_short', '?')}"
        publish_alert(subject, body, args.topic_name)

    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
