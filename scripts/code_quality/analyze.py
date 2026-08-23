#!/usr/bin/env python3
"""Compute code quality metrics for a source tree and emit one JSON blob.

    python scripts/code_quality/analyze.py [ROOT] [-o result.json]

ROOT defaults to the repo root. Configuration comes from `[tool.code_quality]`
in ROOT/pyproject.toml, so a replay of an old commit uses THAT commit's config
— which is what makes `backfill_history.py` produce a comparable series rather
than re-scoring history against today's rules.

The output shape is deliberately flat under "metrics": `gate.py`, the weekly
email and the history file all key off those names, and adding a metric means
adding one entry here plus one `[tool.code_quality.metrics.*]` table.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from metrics import (  # noqa: E402  (path shim must run first)
    classes,
    complexity_for_source,
    coupling,
    duplication,
    maintainability_for_source,
)

DEFAULT_INCLUDE = ["src", "lambda_handler.py", "token_refresher.py"]
DEFAULT_EXCLUDE = [".venv", "lambda_build", "local_only", "analytics", "tests"]


def load_config(root: Path) -> dict:
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        # Historical commits predate pyproject.toml. Fall back to the defaults
        # rather than failing, so the backfill can still score early history.
        return {"include": DEFAULT_INCLUDE, "exclude": DEFAULT_EXCLUDE, "metrics": {}}
    with pyproject.open("rb") as fh:
        data = tomllib.load(fh)
    cfg = data.get("tool", {}).get("code_quality", {})
    cfg.setdefault("include", DEFAULT_INCLUDE)
    cfg.setdefault("exclude", DEFAULT_EXCLUDE)
    cfg.setdefault("metrics", {})
    return cfg


def discover(root: Path, include: list[str], exclude: list[str]) -> list[Path]:
    excluded = set(exclude)
    files: list[Path] = []
    for entry in include:
        target = root / entry
        if target.is_file() and target.suffix == ".py":
            files.append(target)
        elif target.is_dir():
            for path in sorted(target.rglob("*.py")):
                if any(part in excluded or part == "__pycache__" for part in path.parts):
                    continue
                files.append(path)

    if files:
        return sorted(set(files))

    # Fallback for early history: this project kept its application code at the
    # repo ROOT (ai_agent.py, news_scraper.py) until the `src/` move in
    # 218accc (2026-02-13). Without this, every commit before that date scores
    # as an empty tree — 44 commits of zeros that look like real measurements
    # and would make any trend line fiction.
    #
    # Only triggers when the configured include list found nothing at all, so a
    # modern commit can never accidentally fall back to a root-wide scan.
    for path in sorted(root.glob("*.py")):
        if path.name.startswith("test_"):
            continue
        files.append(path)
    return sorted(set(files))


def module_name(path: Path, root: Path) -> str:
    """Flat module name, matching how this project actually imports.

    `sys.path` carries both the repo root and `src/`, so `src/video/footage.py`
    is importable as `video.footage` and `src/ai_agent.py` as `ai_agent`. The
    coupling graph has to use the same names the import statements use, or every
    first-party import looks external and coupling reads as zero.
    """
    rel = path.relative_to(root)
    parts = list(rel.with_suffix("").parts)
    if parts and parts[0] == "src":
        parts = parts[1:]
    return ".".join(parts) if parts else rel.stem


def _git(root: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def analyze(root: Path) -> dict:
    cfg = load_config(root)
    files = discover(root, cfg["include"], cfg["exclude"])

    sources: dict[str, str] = {}
    trees: dict[str, ast.AST] = {}
    unparseable: list[str] = []
    for path in files:
        name = module_name(path, root)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        sources[name] = text
        try:
            trees[name] = ast.parse(text)
        except SyntaxError:
            unparseable.append(name)

    # ── complexity + maintainability ──
    all_functions = []
    mi_values: list[float] = []
    for name, text in sources.items():
        all_functions.extend(complexity_for_source(name, text))
        mi = maintainability_for_source(text)
        if mi is not None:
            mi_values.append(mi)

    complexities = [f.complexity for f in all_functions]
    worst = sorted(all_functions, key=lambda f: -f.complexity)[:10]

    # ── duplication ──
    dup = duplication(sources)

    # ── coupling ──
    cpl = coupling(trees, first_party=set(trees))
    worst_coupling = sorted(cpl.values(), key=lambda m: -m.efferent)[:10]

    # ── classes: DIT + LCOM4 ──
    class_infos = classes(trees)

    metrics = {
        "max_cyclomatic_complexity": max(complexities) if complexities else 0,
        "mean_cyclomatic_complexity": (
            round(sum(complexities) / len(complexities), 2) if complexities else 0.0
        ),
        "duplicated_line_pct": dup["duplicated_line_pct"],
        "max_module_coupling": max((m.efferent for m in cpl.values()), default=0),
        "max_depth_of_inheritance": max((c.dit for c in class_infos), default=0),
        "max_lcom4": max((c.lcom4 for c in class_infos), default=0),
        "min_maintainability_index": min(mi_values) if mi_values else 0.0,
    }

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "commit": _git(root, "rev-parse", "HEAD"),
        "commit_short": _git(root, "rev-parse", "--short", "HEAD"),
        "commit_subject": _git(root, "log", "-1", "--pretty=%s"),
        "commit_date": _git(root, "log", "-1", "--pretty=%cI"),
        "metrics": metrics,
        "context": {
            "files": len(sources),
            "python_lines": sum(len(s.splitlines()) for s in sources.values()),
            "functions": len(all_functions),
            "classes": len(class_infos),
            "duplicated_lines": dup["duplicated_lines"],
            "normalised_lines": dup["total_lines"],
            "duplicate_blocks": dup["blocks"],
            "unparseable_modules": unparseable,
        },
        # Detail, for the article and for reading an alert without re-running.
        "detail": {
            "worst_complexity": [
                {"module": f.module, "name": f.name, "line": f.lineno, "cc": f.complexity}
                for f in worst
            ],
            "worst_coupling": [
                {
                    "module": m.module,
                    "ce": m.efferent,
                    "ca": m.afferent,
                    "instability": m.instability,
                }
                for m in worst_coupling
            ],
            "least_cohesive": [
                {"module": c.module, "name": c.name, "lcom4": c.lcom4, "methods": c.methods}
                for c in sorted(class_infos, key=lambda c: -c.lcom4)[:10]
                if c.lcom4 > 1
            ],
            "deepest_hierarchy": [
                {"module": c.module, "name": c.name, "dit": c.dit, "bases": c.bases}
                for c in sorted(class_infos, key=lambda c: -c.dit)[:5]
            ],
        },
    }


def attach_coverage(result: dict, coverage_json: Path) -> dict:
    """Fold `pytest --cov --cov-report=json` output into the result.

    Kept out of `analyze()` on purpose: coverage requires actually running the
    test suite, which would make the 99-commit backfill both slow and unreliable
    (old commits' tests need old dependencies). CI has already run pytest by the
    time this is called, so the number is free there and simply absent in the
    backfill — `compare()` skips metrics that aren't present.
    """
    try:
        data = json.loads(coverage_json.read_text(encoding="utf-8"))
        pct = float(data["totals"]["percent_covered"])
    except (OSError, ValueError, KeyError) as exc:
        print(f"note: no coverage attached ({exc})", file=sys.stderr)
        return result
    result["metrics"]["test_coverage_pct"] = round(pct, 2)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="tree to analyse")
    parser.add_argument("-o", "--output", help="write JSON here instead of stdout")
    parser.add_argument("--coverage-json", help="pytest --cov-report=json output to fold in")
    args = parser.parse_args()

    result = analyze(Path(args.root).resolve())
    if args.coverage_json:
        result = attach_coverage(result, Path(args.coverage_json))
    text = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
