"""Tests for the code quality metric definitions and the regression gate.

These matter more than most tests here: the metric definitions get replayed
across the whole commit history and compared over time, so a silent change in
what "duplication" or "LCOM4" means invalidates every prior data point. Pinning
the definitions against small synthetic sources is what keeps the series
honest.
"""

import ast
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts" / "code_quality"))
sys.path.insert(0, str(ROOT / "src"))

import metrics as M  # noqa: E402
from code_quality_report import compare, render_section, render_table  # noqa: E402


# ── Cyclomatic complexity ───────────────────────────────────────────────────

def test_complexity_counts_branches():
    src = (
        "def f(a, b):\n"
        "    if a:\n"
        "        return 1\n"
        "    elif b:\n"
        "        return 2\n"
        "    for i in range(3):\n"
        "        if i:\n"
        "            pass\n"
        "    return 0\n"
    )
    results = M.complexity_for_source("m", src)
    assert len(results) == 1
    assert results[0].name == "f"
    assert results[0].complexity > 1


def test_complexity_survives_broken_source():
    """A malformed historical commit must score 0, not abort the backfill."""
    assert M.complexity_for_source("m", "def (:\n") == []
    assert M.maintainability_for_source("def (:\n") is None


# ── Duplication ─────────────────────────────────────────────────────────────

def _block(prefix: str, n: int = 8) -> str:
    return "\n".join(f"{prefix}_line_{i} = {i} + {i} * 2" for i in range(n))


def test_duplication_detects_copied_block_across_files():
    body = _block("shared")
    result = M.duplication({"a": body, "b": body})
    assert result["duplicated_line_pct"] == 100.0
    assert result["blocks"] > 0


def test_duplication_ignores_unique_code():
    result = M.duplication({"a": _block("alpha"), "b": _block("beta")})
    assert result["duplicated_line_pct"] == 0.0


def test_duplication_ignores_comments_and_blank_lines():
    """Comment churn must not move the number, or every doc edit reads as a change."""
    plain = _block("x")
    commented = "\n\n".join(f"# note about {line}\n{line}" for line in plain.splitlines())
    assert M.duplication({"a": plain})["total_lines"] == \
        M.duplication({"a": commented})["total_lines"]


def test_duplication_is_a_ratio_not_a_count():
    """Doubling the codebase at constant duplication must report the same %."""
    body = _block("shared")
    small = M.duplication({"a": body, "b": body})
    large = M.duplication({f"f{i}": body for i in range(8)})
    assert small["duplicated_line_pct"] == large["duplicated_line_pct"]


def test_duplication_needs_a_run_longer_than_the_window():
    """Short repeats (import headers, boilerplate) must not count."""
    short = "\n".join(f"a{i} = {i}" for i in range(M.DUPLICATE_BLOCK_LINES - 1))
    assert M.duplication({"a": short, "b": short})["duplicated_line_pct"] == 0.0


# ── Coupling ────────────────────────────────────────────────────────────────

def test_coupling_counts_first_party_only():
    trees = {
        "app": ast.parse("import os\nimport requests\nfrom helper import thing\n"),
        "helper": ast.parse("import json\n"),
    }
    result = M.coupling(trees, first_party=set(trees))
    assert result["app"].efferent == 1        # helper; os/requests excluded
    assert result["helper"].efferent == 0
    assert result["helper"].afferent == 1     # imported by app


def test_coupling_instability():
    trees = {
        "leaf": ast.parse(""),
        "a": ast.parse("import leaf\n"),
        "b": ast.parse("import leaf\n"),
    }
    result = M.coupling(trees, first_party=set(trees))
    assert result["leaf"].instability == 0.0   # depended on, depends on nothing
    assert result["a"].instability == 1.0      # depends, nothing depends on it


def test_coupling_ignores_self_import():
    trees = {"m": ast.parse("import m\n")}
    assert M.coupling(trees, first_party={"m"})["m"].efferent == 0


# ── Depth of inheritance ────────────────────────────────────────────────────

def test_dit_counts_project_internal_bases_only():
    trees = {
        "m": ast.parse(
            "class Base: pass\n"
            "class Middle(Base): pass\n"
            "class Leaf(Middle): pass\n"
            "class External(Exception): pass\n"
        )
    }
    by_name = {c.name: c for c in M.classes(trees)}
    assert by_name["Base"].dit == 1
    assert by_name["Middle"].dit == 2
    assert by_name["Leaf"].dit == 3
    # Exception is not defined in the project, so this is a root by our definition
    assert by_name["External"].dit == 1


# ── LCOM4 ───────────────────────────────────────────────────────────────────

def test_lcom4_cohesive_class_scores_one():
    trees = {"m": ast.parse(
        "class C:\n"
        "    def __init__(self):\n"
        "        self.value = 1\n"
        "    def get(self):\n"
        "        return self.value\n"
        "    def bump(self):\n"
        "        self.value += 1\n"
    )}
    assert M.classes(trees)[0].lcom4 == 1


def test_lcom4_splits_unrelated_responsibilities():
    """Two method groups touching disjoint state = two classes wearing one name."""
    trees = {"m": ast.parse(
        "class C:\n"
        "    def a1(self):\n"
        "        return self.alpha\n"
        "    def a2(self):\n"
        "        self.alpha = 2\n"
        "    def b1(self):\n"
        "        return self.beta\n"
        "    def b2(self):\n"
        "        self.beta = 3\n"
    )}
    assert M.classes(trees)[0].lcom4 == 2


def test_lcom4_connects_methods_that_call_each_other():
    trees = {"m": ast.parse(
        "class C:\n"
        "    def a(self):\n"
        "        return self.alpha\n"
        "    def b(self):\n"
        "        return self.a()\n"
    )}
    assert M.classes(trees)[0].lcom4 == 1


def test_lcom4_single_method_class_is_cohesive_by_convention():
    trees = {"m": ast.parse("class C:\n    def only(self):\n        return 1\n")}
    assert M.classes(trees)[0].lcom4 == 1


# ── The gate: ratchet vs industry vs hard gate ──────────────────────────────

CFG = {
    "max_cyclomatic_complexity": {
        "label": "CC", "direction": "lower_is_better", "industry": 10, "gate": 60,
    },
    "min_maintainability_index": {
        "label": "MI", "direction": "higher_is_better", "industry": 20, "gate": 12,
    },
}


def _result(cc, mi):
    return {"metrics": {"max_cyclomatic_complexity": cc, "min_maintainability_index": mi}}


def test_over_industry_is_not_a_regression():
    """The whole design rests on this: 4 of 7 metrics sit permanently over
    industry, so if 'over industry' alerted, every commit would mail forever
    and the mail would be ignored."""
    rows = compare(_result(54, 16.0), _result(54, 16.0), CFG)
    assert all(r["over_industry"] for r in rows)
    assert not any(r["regressed"] for r in rows)


def test_regression_detected_in_both_directions():
    rows = {r["key"]: r for r in compare(_result(60, 10.0), _result(50, 16.0), CFG)}
    assert rows["max_cyclomatic_complexity"]["regressed"]   # lower_is_better, went up
    assert rows["min_maintainability_index"]["regressed"]   # higher_is_better, went down


def test_improvement_is_not_a_regression():
    rows = compare(_result(40, 25.0), _result(50, 16.0), CFG)
    assert not any(r["regressed"] for r in rows)


def test_hard_gate_breach_is_flagged_separately():
    rows = {r["key"]: r for r in compare(_result(90, 16.0), _result(88, 16.0), CFG)}
    assert rows["max_cyclomatic_complexity"]["breaches_gate"]


def test_missing_previous_yields_no_regression():
    """First run has no baseline; it must report cleanly rather than alerting."""
    rows = compare(_result(54, 16.0), None, CFG)
    assert not any(r["regressed"] for r in rows)
    assert all(r["previous"] is None for r in rows)


def test_metric_absent_from_result_is_skipped():
    """Coverage is injected by CI only when pytest --cov ran; its absence must
    not crash the renderer."""
    cfg = dict(CFG, test_coverage_pct={
        "label": "Cov", "direction": "higher_is_better", "industry": 80, "gate": 0,
    })
    rows = compare(_result(54, 16.0), None, cfg)
    assert "test_coverage_pct" not in {r["key"] for r in rows}


def test_ratchet_tolerance_absorbs_float_noise():
    rows = compare(_result(54, 16.0), _result(54, 16.005), CFG, tolerance=0.01)
    assert not any(r["regressed"] for r in rows)


# ── Rendering ───────────────────────────────────────────────────────────────

def test_table_shows_industry_gate_and_current():
    text = render_table(compare(_result(54, 16.0), _result(50, 16.0), CFG))
    assert "INDUSTRY" in text and "GATE" in text and "NOW" in text and "PREV" in text
    assert "WORSE" in text
    assert "over industry" in text


def test_weekly_section_handles_empty_history():
    assert "No commits analysed" in render_section([])


def test_weekly_section_renders_per_commit_deltas():
    entries = [
        {**_result(50, 16.0), "commit_short": "aaa1111", "commit_subject": "first",
         "context": {"files": 30, "python_lines": 11000}},
        {**_result(54, 16.0), "commit_short": "bbb2222", "commit_subject": "second",
         "context": {"files": 30, "python_lines": 11200}, "config": CFG},
    ]
    text = render_section(entries, CFG)
    assert "bbb2222" in text and "second" in text
    assert "WORSE" in text


# ── End-to-end against the real repo ────────────────────────────────────────

def test_analyze_produces_every_configured_metric():
    """Guards the contract between pyproject's metric tables and analyze.py:
    a metric configured but never emitted would silently vanish from the email."""
    sys.path.insert(0, str(ROOT / "scripts" / "code_quality"))
    import tomllib

    from analyze import analyze

    result = analyze(ROOT)
    with (ROOT / "pyproject.toml").open("rb") as fh:
        configured = tomllib.load(fh)["tool"]["code_quality"]["metrics"]

    emitted = set(result["metrics"])
    # test_coverage_pct is injected by CI after pytest --cov, not by analyze.py
    expected = set(configured) - {"test_coverage_pct"}
    assert expected <= emitted, f"configured but not emitted: {expected - emitted}"
    assert result["context"]["files"] > 0
    assert json.dumps(result)  # must be JSON-serialisable for S3


def test_analyze_excludes_tests_from_scope():
    """Test code has a different complexity profile; including it would mask
    movement in production code."""
    from analyze import analyze
    result = analyze(ROOT)
    modules = {e["module"] for e in result["detail"]["worst_complexity"]}
    assert not any(m.startswith("test_") or m.startswith("tests.") for m in modules)


@pytest.mark.parametrize("key", [
    "max_cyclomatic_complexity", "mean_cyclomatic_complexity", "duplicated_line_pct",
    "max_module_coupling", "max_depth_of_inheritance", "max_lcom4",
    "min_maintainability_index",
])
def test_every_metric_has_an_industry_source(key):
    """An industry figure with no citation is just a number someone made up."""
    import tomllib
    with (ROOT / "pyproject.toml").open("rb") as fh:
        spec = tomllib.load(fh)["tool"]["code_quality"]["metrics"][key]
    assert spec.get("source"), f"{key} has no industry source"
    assert "industry" in spec and "gate" in spec


def test_fallback_config_matches_pyproject():
    """`src/code_quality_report.DEFAULT_METRIC_CONFIG` must mirror pyproject.

    The Lambda cannot read pyproject.toml — `build_lambda.sh` does not copy it
    into the ZIP — so the weekly email falls back to the hard-coded table when a
    history entry carries no embedded config. If the two drift, the email quietly
    reports different thresholds than CI enforced. This caught a real drift when
    the coverage gate was retuned in pyproject but not in the fallback.
    """
    import tomllib

    from code_quality_report import DEFAULT_METRIC_CONFIG

    with (ROOT / "pyproject.toml").open("rb") as fh:
        configured = tomllib.load(fh)["tool"]["code_quality"]["metrics"]

    assert set(DEFAULT_METRIC_CONFIG) == set(configured), (
        "metric keys differ between pyproject.toml and DEFAULT_METRIC_CONFIG"
    )
    for key, spec in configured.items():
        fallback = DEFAULT_METRIC_CONFIG[key]
        for field in ("industry", "gate", "direction"):
            assert fallback[field] == spec[field], (
                f"{key}.{field}: pyproject={spec[field]} fallback={fallback[field]}"
            )
