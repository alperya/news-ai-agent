"""Rendering and comparison for code quality metrics.

Lives in `src/` rather than `scripts/` because it has two callers on opposite
sides of the deploy boundary:

  * `scripts/code_quality/gate.py` — runs in CI, compares a fresh analysis
    against the previous commit and emails on regression.
  * `src/analytics_engine.py` — runs in Lambda, appends the weekly section to
    the Sunday analytics email.

`scripts/` is not copied into the Lambda ZIP (see `scripts/build_lambda.sh`),
so anything the Lambda needs has to be here. That also means **the Lambda
cannot read `pyproject.toml`** — it isn't in the ZIP either. Thresholds
therefore travel *with the data*: `gate.py` embeds the metric config into
`code_quality/history.json`, and this module renders from that, falling back to
`DEFAULT_METRIC_CONFIG` for history written before the field existed.

Output is plain text and monospace-aligned on purpose: these emails go out over
SNS, which cannot carry HTML.
"""

from __future__ import annotations

from typing import Any, Optional

# Mirror of the [tool.code_quality.metrics.*] tables in pyproject.toml. Used
# only when a history entry carries no embedded config; pyproject stays the
# source of truth for CI.
DEFAULT_METRIC_CONFIG: dict[str, dict[str, Any]] = {
    "max_cyclomatic_complexity": {
        "label": "Max cyclomatic complexity", "direction": "lower_is_better",
        "industry": 10, "gate": 60, "source": "McCabe (1976)",
    },
    "mean_cyclomatic_complexity": {
        "label": "Mean cyclomatic complexity", "direction": "lower_is_better",
        "industry": 5, "gate": 7, "source": "radon A/B grade boundary",
    },
    "duplicated_line_pct": {
        "label": "Duplicated lines", "direction": "lower_is_better",
        "industry": 3.0, "gate": 5.0, "unit": "%", "source": "SonarQube default gate",
    },
    "max_module_coupling": {
        "label": "Max module coupling (Ce)", "direction": "lower_is_better",
        "industry": 20, "gate": 25, "source": "Martin (1994)",
    },
    "max_depth_of_inheritance": {
        "label": "Max depth of inheritance", "direction": "lower_is_better",
        "industry": 5, "gate": 5, "source": "Chidamber-Kemerer DIT",
    },
    "max_lcom4": {
        "label": "Max LCOM4", "direction": "lower_is_better",
        "industry": 1, "gate": 3, "source": "Hitz-Montazeri LCOM4",
    },
    "min_maintainability_index": {
        "label": "Maintainability index (min)", "direction": "higher_is_better",
        "industry": 20, "gate": 12, "source": "radon A grade",
    },
    "test_coverage_pct": {
        "label": "Test coverage", "direction": "higher_is_better",
        "industry": 80.0, "gate": 50.0, "unit": "%", "source": "common industry target",
    },
}

DEFAULT_RATCHET_TOLERANCE = 0.01

_WIDTH = 78


def _fmt(value: Any, unit: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.2f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text}{unit}"


def _is_worse(current: float, reference: float, direction: str, tolerance: float) -> bool:
    if direction == "higher_is_better":
        return current < reference - tolerance
    return current > reference + tolerance


def _breaches_gate(current: float, gate: float, direction: str) -> bool:
    if direction == "higher_is_better":
        return current < gate
    return current > gate


def compare(
    current: dict,
    previous: Optional[dict],
    config: Optional[dict] = None,
    tolerance: float = DEFAULT_RATCHET_TOLERANCE,
) -> list[dict]:
    """Per-metric comparison rows.

    `current` / `previous` are analyze.py result blobs. Each row carries the
    industry figure, the hard gate, the current and previous values, the delta,
    and two independent booleans:

      regressed     — worse than the previous commit (the ratchet; this alerts)
      over_industry — the wrong side of best practice (informational)

    They are separate because most metrics here are permanently over industry;
    folding the two together would make every commit look like a regression.
    """
    cfg = config or DEFAULT_METRIC_CONFIG
    cur_metrics = current.get("metrics", {})
    prev_metrics = (previous or {}).get("metrics", {})

    rows: list[dict] = []
    for key, spec in cfg.items():
        if key not in cur_metrics:
            continue  # e.g. coverage, injected by CI only when pytest --cov ran
        value = cur_metrics[key]
        prior = prev_metrics.get(key)
        direction = spec.get("direction", "lower_is_better")
        delta = None
        regressed = False
        if isinstance(prior, (int, float)) and isinstance(value, (int, float)):
            delta = round(value - prior, 2)
            regressed = _is_worse(value, prior, direction, tolerance)
        rows.append({
            "key": key,
            "label": spec.get("label", key),
            "unit": spec.get("unit", ""),
            "direction": direction,
            "industry": spec.get("industry"),
            "gate": spec.get("gate"),
            "value": value,
            "previous": prior,
            "delta": delta,
            "regressed": regressed,
            "over_industry": (
                _is_worse(value, spec["industry"], direction, 0)
                if isinstance(spec.get("industry"), (int, float))
                and isinstance(value, (int, float))
                else False
            ),
            "breaches_gate": (
                _breaches_gate(value, spec["gate"], direction)
                if isinstance(spec.get("gate"), (int, float))
                and isinstance(value, (int, float))
                else False
            ),
            "source": spec.get("source", ""),
        })
    return rows


def render_table(rows: list[dict]) -> str:
    """Monospace comparison table: industry vs gate vs now vs previous."""
    header = (
        f"{'METRIC':<30}{'INDUSTRY':>10}{'GATE':>8}{'NOW':>10}{'PREV':>10}{'Δ':>8}  STATUS"
    )
    lines = [header, "-" * _WIDTH]
    for r in rows:
        unit = r["unit"]
        if r["breaches_gate"]:
            status = "!! GATE BREACH"
        elif r["regressed"]:
            status = "WORSE"
        elif r["over_industry"]:
            status = "over industry"
        else:
            status = "ok"
        delta = "-" if r["delta"] is None else f"{r['delta']:+g}"
        lines.append(
            f"{r['label']:<30}"
            f"{_fmt(r['industry'], unit):>10}"
            f"{_fmt(r['gate'], unit):>8}"
            f"{_fmt(r['value'], unit):>10}"
            f"{_fmt(r['previous'], unit):>10}"
            f"{delta:>8}  {status}"
        )
    lines.append("-" * _WIDTH)
    lines.append(
        "INDUSTRY = published best practice (the target).  "
        "GATE = hard ceiling above today's baseline."
    )
    lines.append(
        "A metric can sit permanently 'over industry' without being a "
        "regression — only Δ triggers mail."
    )
    sourced = [(r["label"], r["source"]) for r in rows if r["source"]]
    if sourced:
        lines.append("")
        lines.append("Industry figures:")
        for label, source in sourced:
            lines.append(f"  {label:<30} {source}")
    return "\n".join(lines)


def render_detail(current: dict, limit: int = 5) -> str:
    """The worst offenders, so an alert is actionable without re-running."""
    detail = current.get("detail", {})
    out: list[str] = []

    worst_cc = detail.get("worst_complexity", [])[:limit]
    if worst_cc:
        out.append("Highest cyclomatic complexity:")
        out.extend(
            f"  {e['cc']:>4}  {e['module']}:{e['line']}  {e['name']}" for e in worst_cc
        )

    worst_cpl = detail.get("worst_coupling", [])[:limit]
    if worst_cpl:
        out.append("")
        out.append("Most coupled modules (Ce = first-party imports):")
        out.extend(
            f"  Ce={e['ce']:<3} Ca={e['ca']:<3} I={e['instability']:<5}  {e['module']}"
            for e in worst_cpl
        )

    least_cohesive = detail.get("least_cohesive", [])[:limit]
    if least_cohesive:
        out.append("")
        out.append("Least cohesive classes (LCOM4 > 1 = more than one responsibility):")
        out.extend(
            f"  LCOM4={e['lcom4']} ({e['methods']} methods)  {e['module']}.{e['name']}"
            for e in least_cohesive
        )
    return "\n".join(out)


def render_week(entries: list[dict]) -> str:
    """Per-commit deltas for the weekly email, oldest first."""
    if not entries:
        return "No commits analysed this week."
    lines = [f"{len(entries)} commit(s) analysed this week:"]
    for i, entry in enumerate(entries):
        prev = entries[i - 1] if i else None
        rows = compare(entry, prev, entry.get("config"))
        moved = [r for r in rows if r["regressed"] or (r["delta"] not in (None, 0))]
        subject = (entry.get("commit_subject") or "")[:52]
        head = f"  {entry.get('commit_short', '?'):<9} {subject}"
        if not moved:
            lines.append(f"{head}  (no metric movement)")
            continue
        lines.append(head)
        for r in moved:
            arrow = "WORSE" if r["regressed"] else "better"
            lines.append(
                f"      {r['label']}: {_fmt(r['previous'], r['unit'])} -> "
                f"{_fmt(r['value'], r['unit'])} ({r['delta']:+g}) {arrow}"
            )
    return "\n".join(lines)


def render_section(entries: list[dict], config: Optional[dict] = None) -> str:
    """The full block appended to the weekly analytics email."""
    if not entries:
        return (
            "CODE QUALITY\n"
            + "-" * _WIDTH
            + "\nNo commits analysed in the last 7 days.\n"
        )
    latest = entries[-1]
    previous = entries[-2] if len(entries) > 1 else None
    rows = compare(latest, previous, config or latest.get("config"))
    ctx = latest.get("context", {})
    parts = [
        "CODE QUALITY",
        "-" * _WIDTH,
        f"Latest analysed commit: {latest.get('commit_short', '?')} "
        f"{(latest.get('commit_subject') or '')[:48]}",
        f"Scope: {ctx.get('files', '?')} files / {ctx.get('python_lines', '?')} lines "
        f"(tests excluded)",
        "",
        render_table(rows),
        "",
        render_week(entries),
    ]
    return "\n".join(parts)
