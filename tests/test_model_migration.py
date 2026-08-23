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

_MODEL_VARS = ("CONTENT_MODEL", "REVIEW_MODEL", "FOOTAGE_QUERY_MODEL", "VISION_MODEL")


def _clean_agent(monkeypatch):
    for var in _MODEL_VARS:
        monkeypatch.delenv(var, raising=False)
    return _agent()


def test_quality_critical_roles_stay_on_opus_5(monkeypatch):
    """The two roles that must never be cheapened.

    Batch selection is the editorial brain — it picks the story and writes the
    post, so its model IS the product. The vision footage gate is what stops a
    Reel claiming the wrong place; CLAUDE.md records the measured failures
    (Harlingen for Deventer, Kansai for Schiphol, a fireplace for a wildfire)
    that make model quality here a correctness property, not a nicety.

    If a future cost retune moves either of these off Opus, that should be a
    deliberate decision that fails this test first.
    """
    agent = _clean_agent(monkeypatch)
    assert agent.model == "claude-opus-5"
    assert agent.vision_model == "claude-opus-5"


def test_secondary_roles_default_to_sonnet(monkeypatch):
    """quality_check rewrites one short post; footage queries are structured
    extraction whose safety decision is made in code by
    `footage_geo.derive_place_mode`, not by the model."""
    agent = _clean_agent(monkeypatch)
    assert agent.review_model == "claude-sonnet-5"
    assert agent.footage_model == "claude-sonnet-5"


def test_vision_model_is_a_separate_knob_from_review(monkeypatch):
    """The regression this prevents.

    `REVIEW_MODEL` used to drive quality_check, score_events AND the vision
    footage gate. Lowering it to save money on quality_check would silently
    have downgraded the gate — the change looks like a cost tweak and lands as
    a correctness regression that only shows up in the comments section weeks
    later. Splitting the knob is what makes the cost retune safe at all.
    """
    monkeypatch.setenv("REVIEW_MODEL", "claude-haiku-4-5")
    monkeypatch.delenv("VISION_MODEL", raising=False)
    agent = _agent()
    assert agent.review_model == "claude-haiku-4-5"
    assert agent.vision_model == "claude-opus-5", (
        "VISION_MODEL must not follow REVIEW_MODEL"
    )


def test_model_env_overrides_still_work(monkeypatch):
    monkeypatch.setenv("REVIEW_MODEL", "claude-haiku-4-5")
    assert _agent().review_model == "claude-haiku-4-5"


def test_vision_gate_uses_the_vision_model(monkeypatch):
    """Guards the wiring, not just the attribute: the gate must actually pass
    `self.vision_model` to the API, not `self.review_model`."""
    monkeypatch.setenv("VISION_MODEL", "vision-sentinel")
    monkeypatch.setenv("REVIEW_MODEL", "review-sentinel")
    agent = _agent()
    agent.client.messages.create.return_value = SimpleNamespace(
        content=[_text_block("1: PASS")], stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=1, output_tokens=1),
    )
    agent.validate_footage_thumbnails("headline", [], ["https://img/1.jpg"])
    assert agent.client.messages.create.call_args.kwargs["model"] == "vision-sentinel"


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


def test_model_knobs_are_reachable_from_secrets_manager():
    """A knob absent from get_secrets() cannot be set without a deploy.

    CLAUDE.md records this trap for the viral and content-mix thresholds: they
    are read at call time, but `get_secrets()` has to copy them out of the
    secret first or the value is unreachable. The model knobs had exactly that
    gap — documented as hot-tunable, in practice deploy-only.
    """
    text = (Path(__file__).parent.parent / "lambda_handler.py").read_text(encoding="utf-8")
    for var in ("CONTENT_MODEL", "REVIEW_MODEL", "FOOTAGE_QUERY_MODEL", "VISION_MODEL"):
        assert var in text, f"{var} is not copied out of Secrets Manager by get_secrets()"


def test_readme_documents_the_shipped_model_defaults():
    """README config table must not contradict the code.

    Caught by a pre-push audit: after the per-role retune the README still said
    `REVIEW_MODEL` defaulted to Opus 5 while the code shipped Sonnet 5. Docs
    that disagree with defaults are worse than absent docs — someone tunes the
    wrong knob and cannot work out why nothing changed.
    """
    root = Path(__file__).parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    agent = (SRC / "ai_agent.py").read_text(encoding="utf-8")

    for var in _MODEL_VARS:
        m = re.search(rf"os\.getenv\('{var}',\s*'([\w.-]+)'\)", agent)
        assert m, f"{var} default not found in ai_agent.py"
        default = m.group(1)
        row = re.search(rf"\| `{var}` \|[^|]*\|([^|]*)\|", readme)
        assert row, f"{var} missing from the README configuration table"
        assert default in row.group(1), (
            f"README says {var} = {row.group(1).strip()!r} but code defaults to {default!r}"
        )


# ── Observability must never break publishing ───────────────────────────────

def test_agent_constructs_with_the_real_langsmith_wrapper():
    """The regression that took production down for two slots.

    `anthropic` 1.0.0 removed the legacy `.completions` API while langsmith's
    `wrap_anthropic` still referenced `client.completions.create`, so building
    the agent raised AttributeError before a single article was scraped —
    2026-08-22 19:00 and 2026-08-23 09:00 AMS both published nothing.

    Every other test in this file patches `_ls_wrap_anthropic` with identity,
    which is exactly why none of them caught it. This one uses the REAL
    installed packages, so the pinned dependency combination is verified in CI
    on every push rather than discovered by a missed slot.
    """
    agent = NewsAIAgent(api_key="test-key-not-used-for-network")
    assert agent.client is not None
    assert hasattr(agent.client, "messages")


def test_tracing_failure_degrades_instead_of_raising(monkeypatch):
    """Even if the wrapper breaks again, the run must continue untraced.

    The pin keeps tracing working today; this keeps the account publishing on
    the day some future wrapper/SDK pair breaks again. Same principle the
    publishing path already follows — a Facebook or YouTube failure never
    blocks Instagram.
    """
    def _boom(_client):
        raise AttributeError("'Anthropic' object has no attribute 'completions'")

    monkeypatch.setattr("ai_agent._ls_wrap_anthropic", _boom)
    agent = NewsAIAgent(api_key="test-key-not-used-for-network")
    assert agent.client is not None
    assert hasattr(agent.client, "messages")   # usable, just unwrapped
