#!/usr/bin/env python3
"""
Pre-push guard hook for Claude Code.
Reads tool input from stdin, blocks git push if issues are found.

Exit 0 → allow push
Exit 2 → block push (output shown in Claude Code UI + macOS notification)
"""

import json
import re
import subprocess
import sys
from pathlib import Path

# Real secret patterns — NOT placeholder strings like "your_key_here"
SECRET_PATTERNS = [
    (r"sk-ant-api[0-9a-zA-Z\-]{20,}", "Anthropic API key"),
    (r"lsv2_pt_[0-9a-f]{20,}_[0-9a-f]+", "LangSmith API key"),
    (r"sk-lf-[0-9a-f\-]{30,}", "Langfuse secret key"),
    (r"pk-lf-[0-9a-f\-]{30,}", "Langfuse public key"),
    (r"EAA[a-zA-Z0-9]{80,}", "Instagram/Facebook token"),
    (r"sk_[0-9a-f]{38,}", "ElevenLabs API key"),
    (r"(?i)(password|passwd)\s*=\s*[\"'][^\"']{8,}[\"']", "Hardcoded password"),
]

BINARY_EXTENSIONS = {".zip", ".png", ".jpg", ".jpeg", ".mp4", ".mp3", ".ttf", ".pdf", ".woff"}
MAX_FILE_SIZE_MB = 5


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def get_push_diff_files() -> list[str]:
    r = run(["git", "diff", "origin/main...HEAD", "--name-only"])
    if r.returncode != 0 or not r.stdout.strip():
        r = run(["git", "diff", "--cached", "--name-only"])
    return [f for f in r.stdout.strip().splitlines() if f]


def get_push_diff() -> str:
    r = run(["git", "diff", "origin/main...HEAD"])
    if r.returncode != 0 or not r.stdout.strip():
        r = run(["git", "diff", "--cached"])
    return r.stdout


def check_env_tracked() -> list[str]:
    r = run(["git", "ls-files", ".env"])
    return ["`.env` is tracked by git — contains real credentials!"] if r.stdout.strip() else []


def check_secrets() -> list[str]:
    issues = []
    r = run(["git", "ls-files"])
    tracked = [
        f for f in r.stdout.strip().splitlines()
        if f and Path(f).suffix not in BINARY_EXTENSIONS
    ]
    for path in tracked:
        try:
            content = Path(path).read_text(encoding="utf-8", errors="ignore")
            for pattern, label in SECRET_PATTERNS:
                if re.search(pattern, content):
                    issues.append(f"{label} in `{path}`")
        except (FileNotFoundError, IsADirectoryError):
            pass
    return issues


def check_large_files() -> list[str]:
    issues = []
    for path in get_push_diff_files():
        try:
            size_mb = Path(path).stat().st_size / (1024 * 1024)
            if size_mb > MAX_FILE_SIZE_MB:
                issues.append(f"Large file ({size_mb:.1f} MB): `{path}`")
        except FileNotFoundError:
            pass
    return issues


def check_readme_freshness() -> list[str]:
    changed = set(get_push_diff_files())
    source_changed = any(
        f.startswith("src/")
        or f in ("lambda_handler.py", ".env.example")
        or f.startswith("requirements/")
        for f in changed
    )
    if source_changed and "README.md" not in changed:
        return ["Source/config files changed but `README.md` was not updated"]
    return []


def check_debug_code() -> list[str]:
    """Only flag print()/pdb in src/ and lambda_handler.py — not in scripts or hooks."""
    issues = []
    diff = get_push_diff()
    current_file = ""
    for line in diff.splitlines():
        if line.startswith("+++"):
            current_file = line[4:].split("\t")[0].lstrip("b/")
            continue
        in_source = current_file.startswith("src/") or current_file == "lambda_handler.py"
        if not in_source or not line.startswith("+"):
            continue
        stripped = line[1:].strip()
        if re.match(r"print\s*\(", stripped):
            issues.append(f"Debug `print()` in `{current_file}`: {stripped[:70]}")
        elif re.match(r"(import pdb|breakpoint\s*\(|pdb\.set_trace)", stripped):
            issues.append(f"Debugger in `{current_file}`: {stripped[:70]}")
    return issues[:3]


def notify_macos(title: str, message: str) -> None:
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}" sound name "Basso"'],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def format_section(title: str, items: list[str], icon: str) -> str:
    if not items:
        return ""
    lines = [f"\n{title}"]
    lines += [f"  {icon} {item}" for item in items]
    return "\n".join(lines)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read())
        cmd = data.get("tool_input", {}).get("command", "")
    except Exception:
        sys.exit(0)

    if not re.search(r"\bgit\s+push\b", cmd):
        sys.exit(0)

    errors: list[str] = []
    warnings: list[str] = []

    errors += check_env_tracked()
    errors += check_secrets()
    errors += check_large_files()
    warnings += check_debug_code()
    warnings += check_readme_freshness()

    if not errors and not warnings:
        print("✅ Pre-push checks passed — no issues found.")
        sys.exit(0)

    output_parts = ["=" * 60, "🔍 PRE-PUSH GUARD REPORT"]
    output_parts.append(format_section("❌ SECURITY / SIZE (blocking)", errors, "•"))
    output_parts.append(format_section("⚠️  WARNINGS (blocking)", warnings, "•"))
    output_parts.append("=" * 60)

    if errors:
        output_parts.append("🚫 PUSH BLOCKED — fix the issues above before pushing.")
        notify_macos("🚫 Push Blocked", f"{len(errors)} issue(s): {errors[0][:60]}")
    else:
        output_parts.append(
            "⚠️  PUSH BLOCKED — review warnings above.\n"
            "If intentional, run `git push` directly in the terminal to bypass."
        )
        notify_macos("⚠️ Push Warning", warnings[0][:60])

    print("\n".join(p for p in output_parts if p))
    sys.exit(2)


if __name__ == "__main__":
    main()
