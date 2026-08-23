#!/usr/bin/env python3
"""Replay the whole commit history through analyze.py to build history.json.

    python scripts/code_quality/backfill_history.py -o analytics/cq_history.json
    python scripts/code_quality/backfill_history.py --since "1 year ago" --markdown table.md

WHY THIS IS TRACKED AND NOT A ONE-OFF IN local_only/
-----------------------------------------------------
It looks single-use — replay, get the table, done. But it imports `analyze.py`,
so the moment a metric definition changes (a new metric, a corrected threshold,
a radon upgrade) the whole series stops being comparable with what CI is now
emitting and has to be regenerated. It is not "run once", it is "run whenever
the analyzer changes" — and if it lived outside version control while the
analyzer was inside it, the two would drift and the history would silently mix
metric definitions.

Each commit is materialised with `git archive` into a temp dir rather than
`git worktree` or `git checkout`: it is faster, needs no worktree bookkeeping,
and — importantly — it never touches the working tree, so an interrupted run
cannot leave the repo on a detached HEAD.

Commits are scored with the analyzer's CURRENT definitions (the config comes
from today's pyproject.toml), which is what makes the series comparable. That
is a deliberate departure from how CI scores a single commit, where the commit's
own config is used.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analyze import analyze  # noqa: E402  (path shim must run first)

ROOT = Path(__file__).resolve().parents[2]


def git(*args: str, binary: bool = False):
    result = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, check=True,
        text=not binary,
    )
    return result.stdout


def commit_list(since: str | None) -> list[dict]:
    fmt = "%H%x1f%h%x1f%cI%x1f%s"
    args = ["log", "--reverse", f"--pretty=format:{fmt}"]
    if since:
        args.append(f"--since={since}")
    out = git(*args)
    commits = []
    for line in out.splitlines():
        if not line.strip():
            continue
        sha, short, date, subject = line.split("\x1f", 3)
        commits.append(
            {"commit": sha, "commit_short": short, "commit_date": date,
             "commit_subject": subject}
        )
    return commits


def materialise(sha: str, dest: Path) -> None:
    """Extract a commit's tree into `dest` without touching the working copy."""
    archive = git("archive", "--format=tar", sha, binary=True)
    with tempfile.NamedTemporaryFile(suffix=".tar") as fh:
        fh.write(archive)
        fh.flush()
        with tarfile.open(fh.name) as tar:
            # filter="data" refuses absolute paths and traversal entries. The
            # archive is our own history, but the flag is free and keeps this
            # safe if it is ever pointed at a fork.
            tar.extractall(dest, filter="data")


def score(commits: list[dict], config_root: Path) -> list[dict]:
    entries: list[dict] = []
    total = len(commits)
    for i, meta in enumerate(commits, start=1):
        with tempfile.TemporaryDirectory(prefix="cq-") as tmp:
            tree = Path(tmp)
            try:
                materialise(meta["commit"], tree)
                # Score every commit with TODAY's config so the series is
                # internally comparable; without this, early commits (which
                # predate pyproject.toml) would silently use different paths.
                cfg_src = config_root / "pyproject.toml"
                if cfg_src.exists():
                    (tree / "pyproject.toml").write_bytes(cfg_src.read_bytes())
                result = analyze(tree)
            except Exception as exc:  # a broken historical commit must not stop the run
                print(f"  [{i}/{total}] {meta['commit_short']} SKIPPED: {exc}", file=sys.stderr)
                continue
        result.update(meta)  # real metadata; the temp tree has no .git
        entries.append(result)
        m = result["metrics"]
        print(
            f"  [{i}/{total}] {meta['commit_short']}  "
            f"cc_max={m['max_cyclomatic_complexity']:<4} "
            f"cc_mean={m['mean_cyclomatic_complexity']:<6} "
            f"dup={m['duplicated_line_pct']:<6} "
            f"ce={m['max_module_coupling']:<3} "
            f"mi={m['min_maintainability_index']:<7} "
            f"{meta['commit_subject'][:44]}"
        )
    return entries


def to_markdown(entries: list[dict]) -> str:
    # Markers are SEMANTIC, not directional: `⚠` always means "this commit made
    # the metric worse". Directional arrows are ambiguous here because
    # maintainability index is higher-is-better while every other metric is
    # lower-is-better, so a falling number is bad in one column and good in the
    # rest.
    head = (
        "Legend: `⚠` = this commit made the metric worse · `✓` = improved · "
        "blank = unchanged.\n"
        "MI (maintainability index) is higher-is-better; every other column is "
        "lower-is-better.\n\n"
        "| # | Commit | Date | Subject | Files | Lines | CC max | CC mean | "
        "Dup % | Ce max | DIT | LCOM4 | MI min |\n"
        "|---|--------|------|---------|-------|-------|--------|---------|"
        "-------|--------|-----|-------|--------|"
    )
    rows = [head]
    prev = None
    for i, e in enumerate(entries, start=1):
        m, c = e["metrics"], e["context"]

        def mark(key: str, value, lower_better: bool = True, prev=prev) -> str:
            if prev is None:
                return str(value)
            before = prev["metrics"].get(key)
            if before is None or before == value:
                return str(value)
            worse = value > before if lower_better else value < before
            return f"{value} {'⚠' if worse else '✓'}"

        subject = e["commit_subject"].replace("|", "\\|")[:60]
        rows.append(
            f"| {i} | `{e['commit_short']}` | {e['commit_date'][:10]} | {subject} | "
            f"{c['files']} | {c['python_lines']} | "
            f"{mark('max_cyclomatic_complexity', m['max_cyclomatic_complexity'])} | "
            f"{mark('mean_cyclomatic_complexity', m['mean_cyclomatic_complexity'])} | "
            f"{mark('duplicated_line_pct', m['duplicated_line_pct'])} | "
            f"{mark('max_module_coupling', m['max_module_coupling'])} | "
            f"{m['max_depth_of_inheritance']} | {m['max_lcom4']} | "
            f"{mark('min_maintainability_index', m['min_maintainability_index'], False)} |"
        )
        prev = e
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", help='e.g. "1 year ago"; default is all history')
    ap.add_argument("-o", "--output", default="analytics/cq_history.json")
    ap.add_argument("--markdown", help="also write the commit table as Markdown")
    args = ap.parse_args()

    commits = commit_list(args.since)
    print(f"Scoring {len(commits)} commit(s)...")
    entries = score(commits, ROOT)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps({"config": {}, "entries": entries}, indent=2), encoding="utf-8"
    )
    print(f"\nwrote {out}  ({len(entries)} entries)")

    if args.markdown:
        md = Path(args.markdown)
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text(to_markdown(entries), encoding="utf-8")
        print(f"wrote {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
