"""
Guards for the Opus 5 migration (2026-08).

Three API changes bite this codebase specifically; these tests keep them from
creeping back in:
  1. `temperature` returns 400 on Opus 5 — no call may pass it.
  2. Thinking is on by default and thinking blocks precede text in
     `response.content` — text must never be read via `content[0]`.
  3. `max_tokens` caps thinking + response together — tiny budgets truncate.
"""

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from ai_agent import NewsAIAgent

SRC = Path(__file__).parent.parent / 'src'


def _agent():
    with patch("ai_agent.anthropic.Anthropic") as mock_anthropic, \
         patch("ai_agent._ls_wrap_anthropic", new=lambda c: c):
        mock_anthropic.return_value = MagicMock()
        return NewsAIAgent(api_key="test-key")


# ── Model defaults ───────────────────────────────────────────────────────────

def test_all_roles_default_to_opus_5(monkeypatch):
    for var in ("CONTENT_MODEL", "REVIEW_MODEL", "FOOTAGE_QUERY_MODEL"):
        monkeypatch.delenv(var, raising=False)
    agent = _agent()
    assert agent.model == "claude-opus-5"
    assert agent.review_model == "claude-opus-5"
    assert agent.footage_model == "claude-opus-5"


def test_model_env_overrides_still_work(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "claude-haiku-4-5")
    assert _agent().review_model == "claude-haiku-4-5"


# ── No sampling params (400 on Opus 5) ───────────────────────────────────────

def test_no_temperature_in_any_api_call():
    """`temperature`/`top_p`/`top_k` are removed on Opus 5 — a single leftover
    kills that pipeline stage with a 400 on every run."""
    offenders = []
    for py in list(SRC.rglob("*.py")) + [Path(__file__).parent.parent / "lambda_handler.py"]:
        text = py.read_text(encoding="utf-8")
        if re.search(r"^\s*(temperature|top_p|top_k)\s*=", text, re.MULTILINE):
            offenders.append(py.name)
    assert not offenders, f"sampling params found in: {offenders}"


# ── Text extraction (thinking blocks precede text) ───────────────────────────

def _thinking_block():
    return SimpleNamespace(type="thinking", thinking="internal reasoning...")


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def test_response_text_skips_leading_thinking_block():
    response = SimpleNamespace(
        content=[_thinking_block(), _text_block('{"ok": true}')],
        stop_reason="end_turn",
    )
    assert NewsAIAgent._response_text(response) == '{"ok": true}'


def test_response_text_empty_on_refusal_without_text():
    """A refusal is HTTP 200 with empty content — callers get '' and their
    existing fallbacks run instead of an unhandled IndexError."""
    response = SimpleNamespace(content=[], stop_reason="refusal", stop_details=None)
    assert NewsAIAgent._response_text(response) == ""


def test_no_content_zero_indexing_in_api_modules():
    """Reading `response.content[0]` silently returns a thinking block on
    Opus 5; all text extraction must scan for the text block."""
    offenders = []
    for name in ("ai_agent.py", "selection_reviewer.py", "analytics_engine.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if re.search(r"(response|msg)\.content\[0\]", line) and not line.strip().startswith("#"):
                offenders.append(f"{name}:{i}")
    assert not offenders, f"content[0] indexing found: {offenders}"
