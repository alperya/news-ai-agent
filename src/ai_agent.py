"""
AI Agent using Claude API for content processing
Transforms raw news into engaging social media content
"""

import anthropic
import os
import re
import json
import logging
from typing import List, Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

try:
    from langsmith.wrappers import wrap_anthropic as _ls_wrap_anthropic
except ImportError:
    _ls_wrap_anthropic = None  # type: ignore[assignment]

try:
    from langfuse import observe as _lf_observe, get_client as _lf_get_client  # type: ignore[assignment]
    _LANGFUSE_AVAILABLE = True

    class _LangfuseCtx:
        """Thin adapter: maps our internal call to Langfuse v4 get_client() API."""
        def update_current_observation(
            self, *, model: Optional[str] = None, input=None, output=None,
            usage: Optional[dict] = None, metadata: Optional[dict] = None, **_: object,
        ) -> None:
            try:
                _lf_get_client().update_current_generation(
                    model=model,
                    input=input,
                    output=output,
                    usage_details=usage,
                    metadata=metadata,
                )
            except Exception:
                pass

    _lf_ctx = _LangfuseCtx()  # type: ignore[assignment]

except ImportError:
    _LANGFUSE_AVAILABLE = False

    def _lf_observe(fn=None, **kwargs):  # type: ignore[misc]
        if fn is not None:
            return fn
        return lambda f: f

    class _NoopCtx:  # type: ignore[no-redef]
        def update_current_observation(self, **kwargs: object) -> None:
            pass
    _lf_ctx = _NoopCtx()  # type: ignore[assignment]

try:
    import boto3
except ImportError:
    boto3 = None

from footage_geo import build_footage_plan

# Load environment variables

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Project root: parent of src/ locally, same dir in Lambda flat ZIP
_THIS_DIR = Path(__file__).parent
PROJECT_ROOT = _THIS_DIR.parent if (_THIS_DIR / '__init__.py').exists() or _THIS_DIR.name == 'src' else _THIS_DIR
PROMPTS_DIR = PROJECT_ROOT / 'prompts'

# Shipped default for the footage-query prompt. Unlike the other prompts this one
# must never be missing: without it generate_footage_queries returns nothing and
# the pipeline falls back to keyword extraction off the *Dutch* headline — which
# is the wrong-city bug it exists to prevent. Secrets Manager
# (AI_PROMPT_FOOTAGE_QUERIES) still overrides it for hot edits.
_FOOTAGE_QUERIES_PROMPT = """You are selecting stock footage for a Dutch news Instagram Reel.

Dutch article headline: {title}
Article context: {description}

STEP 1 — Locate the story.
Name the single most specific real-world place the story is about, in English.
Use "" when the story has no geographic anchor.
place_type is one of:
  "city"    — a town, city, village, port or municipality
  "region"  — a province or region (Zeeland, Twente, Randstad)
  "country" — the Netherlands as a whole, or a foreign country
  "none"    — no geographic anchor

STEP 2 — Write exactly 5 Pexels queries in English, most specific first.

The hard rule: NEVER put the name of a city, town, village, port, harbour or
province in a query. Not first, not last, not combined with another word.
Pexels never returns nothing — it returns the closest visual match from
anywhere on earth. Asking for a small Dutch town therefore guarantees footage
of a DIFFERENT town, which viewers correctly read as fake.

At least 2 of the 5 queries MUST carry a national Dutch anchor — the word
"dutch" or "netherlands" attached to a generic subject (dutch canal, dutch
farmland, dutch motorway, dutch police car, dutch cycling path). This is a
promise about the COUNTRY, never a claim about a town, so it is always safe,
and it is what stops the Reel from looking like it was shot anywhere on earth.
You MAY name a foreign country only when the story is about that country.

STEP 3 — Make every query non-identifiable and subject-focused.
Describe the THING the story is about, not the place it sits in.
  Prefer: close up, detail, interior, hands, machinery, equipment, texture,
          low water line, empty berth, crane hook, barge deck, control room.
  Forbid: skyline, aerial, drone, panorama, cityscape, establishing shot,
          landmark, monument, city square, town centre, street signage.
A wide shot is fine as long as nobody could name the place from it — a polder,
flat farmland, a canal or a motorway names no town.

Every query must describe the story's actual SUBJECT. Never reach for a
decorative stand-in that merely shares a word with it: a heath fire is not a
fireplace, a match or a campfire. Avoid "cozy", "serene", "rustic" and other
stock set-dressing words — they return staged studio footage.
2-4 words per query. Concrete visual nouns. English only.
Queries 4 and 5 must be the most generic version of the theme so they are safe
fallbacks (e.g. "river barge closeup", "low water river").

STEP 4 — "avoid": 3-5 English visual concepts that must NOT appear.
When place_type is "city" or "region", always include "recognisable skyline"
and "named landmark". Always include "indoor or decorative setting" and
"mountains or rocky terrain" — the Netherlands is flat, and an indoor scene
never depicts an outdoor news event. Add wrong weather, wrong season, wrong
country, unrelated celebration as relevant.

Return ONLY valid JSON:
{{"place": "...", "place_type": "city|region|country|none",
  "queries": ["...", "...", "...", "...", "..."],
  "avoid": ["...", "...", "..."]}}
"""


def _as_int(value, default: int = 0) -> int:
    """Coerce a model-supplied score to int; models return "2" as often as 2."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class SocialMediaPost:
    """Processed social media post"""
    original_title: str
    original_url: str
    source: str
    content: str
    hashtags: List[str]
    emoji: str
    hook: str = ""
    platform: str = "twitter"
    image_url: Optional[str] = None
    # Engagement fields. Deliberately kept OUT of `content`: the quality gate
    # only ever sees `content`, which must stay in neutral news-agency register,
    # and the "no calls to action" ban applies to the news body alone.
    cta_question: str = ""
    save_prompt: str = ""
    series: str = ""
    # Editorial-transparency fields (filled from the batch-selection response).
    # Why the AI chose this article and the strongest candidates it passed over.
    selection_reason: str = ""
    # The two ranking axes. `personal_stake` is the SAVE score (why someone keeps
    # the post); `share_potential` is the SHARE score (why someone sends it on).
    # Both default to 0 so the pipeline still runs against a prompt version that
    # predates either field — the live prompt lives in Secrets Manager and is
    # edited without a deploy, so code must never *require* a prompt field.
    personal_stake: int = 0
    share_potential: int = 0
    runner_ups: List[Dict] = field(default_factory=list)
    _corrected: bool = field(default=False, init=False, repr=False)

    def to_dict(self) -> Dict:
        return {
            'original_title': self.original_title,
            'original_url': self.original_url,
            'source': self.source,
            'content': self.content,
            'hashtags': self.hashtags,
            'emoji': self.emoji,
            'hook': self.hook,
            'platform': self.platform,
            'image_url': self.image_url,
            'full_post': self.format_post(),
            'cta_question': self.cta_question,
            'save_prompt': self.save_prompt,
            'series': self.series,
            'selection_reason': self.selection_reason,
            'personal_stake': self.personal_stake,
            'share_potential': self.share_potential,
            'runner_ups': self.runner_ups,
        }

    # Named recurring series. A fixed list, not free text: open-ended names
    # fragment into one-offs, and a series only converts viewers into followers
    # (and becomes sponsorable) once it is recognisable across weeks.
    SERIES_HASHTAGS = {
        "Wat Verandert Er Vandaag": "#WatVerandertErVandaag",
        "Nederland Droogt Uit": "#NederlandDroogtUit",
    }

    def format_post(self) -> str:
        """Format complete social media post with source"""
        hashtags = list(self.hashtags)
        series_tag = self.SERIES_HASHTAGS.get(self.series)
        if series_tag and series_tag not in hashtags:
            hashtags.append(series_tag)
        hashtags_str = ' '.join(hashtags)

        source_line = f"\n📰 Source: {self.original_url}"
        # Only prepend emoji if content doesn't already start with one
        content = self.content
        if not content or not self._starts_with_emoji(content):
            content = f"{self.emoji} {content}"
        # A named series leads the caption so it is recognisable in the feed.
        if self.series:
            content = f"📌 {self.series}\n\n{content}"

        # The engagement ask lives after the news body, never inside it.
        cta_lines = [line for line in (self.cta_question, self.save_prompt) if line]
        cta_block = ("\n\n" + "\n".join(cta_lines)) if cta_lines else ""

        # Add dot separator before hashtags if content was corrected by quality gate
        separator = "\n.\n" if self._corrected else "\n\n"
        return f"{content}{cta_block}{source_line}{separator}{hashtags_str}"

    @staticmethod
    def _starts_with_emoji(text: str) -> bool:
        """Check if text starts with an emoji character."""
        if not text:
            return False
        cp = ord(text[0])
        # Common emoji ranges: Misc Symbols, Dingbats, Emoticons, Transport, Supplemental
        return cp > 0x2600


# USD per million tokens, (input, output). Used only to annotate the
# `claude_usage` log line so cost per role is readable in CloudWatch without a
# spreadsheet. VERIFY AGAINST CURRENT PRICING BEFORE TRUSTING A TOTAL — these
# are list prices as of 2026-08 and Anthropic changes them. An unknown model
# simply logs `est_cost_usd: null` rather than guessing.
#
# Note claude-sonnet-5 carries introductory pricing of $2/$10 through
# 2026-08-31; $3/$15 below is the standard rate, so estimates are conservative.
_MODEL_PRICES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-fable-5": (10.0, 50.0),
}

# Models that reject `output_config.effort` outright. Effort is supported on the
# Opus 4.5+ and Sonnet 4.6+ families; Haiku 4.5 and Sonnet 4.5 return
# 400 "This model does not support the effort parameter."
#
# Checked BEFORE the call so an incompatible model never burns a request to
# discover it — which matters because the vision A/B deliberately points a
# challenger at exactly such a model.
_NO_EFFORT_MODELS = {
    "claude-haiku-4-5",
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5",
}

# Learned at runtime, PER MODEL. Was a single process-wide flag, which meant one
# incompatible model disabled effort for every other role in the container: the
# Haiku A/B shadow call poisoned effort for Opus 5 batch selection and the
# Sonnet 5 vision gate, silently undoing the cost tuning for the rest of that
# container's life. Keyed by model so a rejection only ever speaks for itself.
_EFFORT_UNSUPPORTED: set = set()


def _effort_for(role: str, default: str) -> str:
    """Per-role effort, read at CALL time so a retune needs no deploy.

    Effort is the cheapest cost lever available — it changes how much the model
    thinks without changing which model runs, and thinking is billed as output
    tokens at the model's output rate. Hard-coding it at each call site made
    every cost adjustment a code change; this makes it a Secrets Manager edit,
    matching how the viral-skip and content-mix thresholds already work.

    Read at call time, NOT at import: `get_secrets()` copies values out of
    Secrets Manager after the container has already imported this module, so a
    module-level `os.environ.get` would freeze the deploy-time default and a
    retune would silently never apply. `get_secrets()` must also list each key.
    """
    return os.environ.get(f"EFFORT_{role.upper()}", default)

# Hard ceiling on images per vision call. The caller batches too
# (`video.footage.VALIDATION_BATCH`); this is the API-side guard so a future
# caller cannot send an unbounded number of images in one request.
#
# The two MUST agree: `footage.py` zips its batch against the returned results,
# so a caller batch larger than this limit would be silently truncated here and
# the surplus clips dropped without ever being judged. `tests/test_footage_geo.py`
# asserts the relationship.
VISION_BATCH_LIMIT = 15


class NewsAIAgent:
    """AI Agent for processing news into social media content"""
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv('ANTHROPIC_API_KEY')
        if not self.api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        base_client = anthropic.Anthropic(api_key=self.api_key)
        # Tracing must NEVER be able to stop the account publishing.
        #
        # This exact line took production down for two slots (2026-08-22 19:00
        # and 2026-08-23 09:00 AMS): `anthropic` 1.0.0 removed the legacy
        # `.completions` API, and langsmith's `wrap_anthropic` still reaches for
        # `client.completions.create`, so it raised AttributeError while merely
        # *constructing* the agent — before a single article was scraped.
        #
        # Same principle the publishing path already follows (a Facebook or
        # YouTube failure never blocks Instagram): an observability wrapper is
        # strictly optional, so a failure here degrades to an unwrapped client
        # and the run continues without traces.
        self.client = base_client
        if _ls_wrap_anthropic:
            try:
                self.client = _ls_wrap_anthropic(base_client)
            except Exception as exc:
                logger.warning(
                    f"⚠️  LangSmith tracing disabled — wrap_anthropic failed "
                    f"({type(exc).__name__}: {exc}). Publishing continues untraced."
                )

        # ── Model per role (2026-08 retune) ──────────────────────────────────
        #
        # Everything ran on claude-opus-5 from the Opus 5 migration until this
        # retune. Two roles stay there because they are the ones that decide
        # whether a post is good and whether it is *wrong*:
        #
        #   CONTENT_MODEL — batch selection is the editorial brain. It picks the
        #     story, scores personal_stake/share_potential, and writes the post.
        #     Cheapening this is cheapening the product.
        #   VISION_MODEL  — the footage gate. CLAUDE.md is explicit that model
        #     quality here directly prevents published mistakes (Harlingen,
        #     Kansai, the cosy-fireplace heatwave). A wrong-place Reel costs
        #     more in credibility than a year of the savings.
        #
        # The rest are structured extraction or short-form rewriting, where
        # Sonnet 5 is at or near Opus quality:
        #
        #   REVIEW_MODEL        — quality_check rewrites one short post.
        #   FOOTAGE_QUERY_MODEL — extracts place + place_type + queries. The
        #     safety *decision* is made in code by footage_geo.derive_place_mode,
        #     not by the model, so this is extraction, not judgement.
        #
        # VISION_MODEL is a NEW knob split out of REVIEW_MODEL. Before the
        # split those three roles shared one variable, so lowering REVIEW_MODEL
        # to save money on quality_check would silently have downgraded the
        # footage gate too — the single most expensive mistake available here.
        self.model = os.getenv('CONTENT_MODEL', 'claude-opus-5')
        self.review_model = os.getenv('REVIEW_MODEL', 'claude-sonnet-5')
        self.footage_model = os.getenv('FOOTAGE_QUERY_MODEL', 'claude-sonnet-5')
        self.vision_model = os.getenv('VISION_MODEL', 'claude-sonnet-5')

    def _create(self, *, role: str, model: str, max_tokens: int,
                effort: Optional[str] = None, **kwargs):
        """Single entry point for every Claude call: effort + usage logging.

        Two things happen here that used to happen nowhere:

        1. `output_config={"effort": ...}` — no call site set this before, so
           all nine ran at the API default of `high` with adaptive thinking
           billed into `max_tokens` at output-token rates. Effort is the
           cheapest lever available: it changes spend without changing model.

        2. A structured `claude_usage` log line per call, so cost per role is
           measurable instead of estimated. There was no token accounting in
           this repo at all, which meant any model/effort decision was
           guesswork. Uses the same `json.dumps({"event": ...})` shape as the
           `viral_skip` log so CloudWatch Logs Insights can query them alike.

        Effort degrades safely: if the SDK is too old to accept `output_config`
        or a model override rejects it, the flag is dropped process-wide and the
        call is retried plain. A systematic 400 here would otherwise push every
        call site into its own fallback path and quietly disable the agent.
        """
        effort = _effort_for(role, effort) if effort else effort
        params = dict(model=model, max_tokens=max_tokens, **kwargs)
        effort_sent = bool(
            effort
            and model not in _NO_EFFORT_MODELS
            and model not in _EFFORT_UNSUPPORTED
        )
        if effort_sent:
            params["output_config"] = {"effort": effort}

        try:
            response = self.client.messages.create(**params)
        except Exception as exc:
            if "output_config" not in params:
                raise
            logger.warning(
                f"⚠️  effort not accepted by {model} ({type(exc).__name__}: {exc}); "
                f"retrying without it and disabling effort for {model} only"
            )
            _EFFORT_UNSUPPORTED.add(model)
            effort_sent = False
            params.pop("output_config")
            response = self.client.messages.create(**params)

        # Log what was actually SENT, not what was asked for: recording an
        # effort the API rejected would make the cost telemetry describe a
        # request that never happened.
        self._log_usage(role, model, effort if effort_sent else None, response)
        return response

    @staticmethod
    def _log_usage(role: str, model: str, effort: Optional[str], response) -> None:
        """Emit one structured usage line. Never raises — it is telemetry."""
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            inp = getattr(usage, "input_tokens", 0) or 0
            out = getattr(usage, "output_tokens", 0) or 0
            cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
            cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
            price = _MODEL_PRICES.get(model)
            cost = None
            if price:
                # Cache reads bill at ~0.1x input, writes at ~1.25x.
                cost = round(
                    (inp * price[0] + cache_read * price[0] * 0.1
                     + cache_write * price[0] * 1.25 + out * price[1]) / 1_000_000,
                    6,
                )
            logger.info(json.dumps({
                "event": "claude_usage",
                "role": role,
                "model": model,
                "effort": effort or "default",
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "est_cost_usd": cost,
                "stop_reason": getattr(response, "stop_reason", None),
            }))
        except Exception:  # telemetry must never break a publish
            pass

    @staticmethod
    def _response_text(response) -> str:
        """First text block of a Messages response.

        On Opus 5 thinking is on by default and thinking blocks precede text in
        ``response.content`` — indexing ``content[0]`` would silently return ""
        (every caller would degrade to its fallback with no error). Refusals
        (HTTP 200 + ``stop_reason == "refusal"``) are logged for the same
        reason: the safe fallback still runs, but the audit trail says why.
        """
        if getattr(response, 'stop_reason', None) == 'refusal':
            details = getattr(response, 'stop_details', None)
            logger.warning(
                f"⚠️  Model refused request (category: {getattr(details, 'category', None)})"
            )
        for block in response.content:
            # Thinking blocks carry `.thinking`, text blocks a str `.text` —
            # match on the payload, not only `.type`, so test doubles work too.
            text = getattr(block, 'text', None)
            if isinstance(text, str) and getattr(block, 'type', 'text') != 'thinking':
                return text
        return ""

    @staticmethod
    def _load_prompt(filename: str, env_var: str) -> str:
        """Load prompt from file, fall back to env var (for Lambda)."""
        prompt_file = PROMPTS_DIR / filename
        if prompt_file.exists():
            return prompt_file.read_text(encoding='utf-8')
        value = os.getenv(env_var)
        if value:
            return value.replace('\\n', '\n')
        raise ValueError(f"Prompt not found: {prompt_file} or env var {env_var}")

    @classmethod
    def _load_prompt_or(cls, filename: str, env_var: str, default: str) -> str:
        """Like :meth:`_load_prompt` but never raises — falls back to *default*.

        Used for prompts introduced after the code that reads them, so a deploy
        landing before the Secrets Manager entry exists degrades to the shipped
        text instead of silently disabling the feature.
        """
        try:
            return cls._load_prompt(filename, env_var)
        except ValueError:
            return default

    @_lf_observe(name="process_article")
    def process_article(self, article: Dict, target_platform: str = "twitter") -> SocialMediaPost:
        """Process single article into social media post"""
        prompt = self._create_prompt(article, target_platform)

        try:
            logger.info(f"Processing article: {article['title'][:50]}...")

            response = self._create(
                role="single_article",
                model=self.model,
                max_tokens=4000,  # thinking counts against max_tokens on Opus 5
                effort="high",
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )
            text = self._response_text(response)
            _lf_ctx.update_current_observation(
                input=prompt,
                output=text,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"platform": target_platform, "article_title": article.get('title', '')},
            )

            result = self._parse_response(text)
            
            return SocialMediaPost(
                original_title=article['title'],
                original_url=article['url'],
                source=article['source'],
                content=result['content'],
                hashtags=result['hashtags'],
                emoji=result['emoji'],
                hook=result.get('hook', ''),
                platform=target_platform,
                image_url=article.get('image_url'),
                # Optional on this fallback path — the single-article prompt may
                # predate these fields, and an empty CTA is better than a crash.
                cta_question=(result.get('cta_question') or '').strip(),
                save_prompt=(result.get('save_prompt') or '').strip(),
            )
            
        except Exception as e:
            logger.error(f"Error processing article: {str(e)}")
            raise
    
    def _create_prompt(self, article: Dict, platform: str) -> str:
        """Create prompt for Claude"""
        if platform == "twitter":
            max_length = 280
        elif platform == "instagram":
            max_length = 2200
        else:
            max_length = 500

        prompt_template = self._load_prompt('single_article.txt', 'AI_PROMPT_SINGLE_ARTICLE')
        hashtag_instruction = 'Include 5-10 relevant hashtags (in English)' if platform == 'instagram' else 'Include 3-5 relevant hashtags (in English)'

        return prompt_template.format(
            title=article['title'],
            description=article['description'],
            source=article['source'].upper(),
            category=article.get('category', 'general'),
            platform=platform,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )
    
    def _parse_response(self, response_text: str) -> Dict:
        """Parse Claude's JSON response"""
        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = json.loads(response_text)
            
            required = ['content', 'emoji', 'hashtags']
            if not all(key in result for key in required):
                raise ValueError("Missing required fields in response")
            
            return result
            
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON response: {response_text}")
            return {
                'content': 'Son dakika haberi',
                'emoji': '📰',
                'hashtags': ['#Netherlands', '#News', '#Europe']
            }

    @_lf_observe(name="quality_check")
    def quality_check(self, post: SocialMediaPost) -> Optional[SocialMediaPost]:
        """Quality gate: structural checks + lightweight AI language review.
        Returns the (possibly corrected) post if it passes, None if rejected.
        """
        errors = []

        # ── Structural checks (no API call needed) ──
        if not post.content or len(post.content.strip()) < 20:
            errors.append("Content is empty or too short (min 20 chars)")
        if not post.emoji:
            errors.append("Emoji is missing")
        if not post.hashtags or len(post.hashtags) == 0:
            errors.append("Hashtags are missing")
        if not post.original_url:
            errors.append("Source URL is missing")
        if not post.source:
            errors.append("Source name is missing")

        if errors:
            logger.error(f"❌ Quality gate REJECTED (structural): {errors}")
            logger.info(json.dumps({
                "event": "quality_gate_rejected",
                "reason": "structural",
                "errors": errors,
                "source": post.source,
                "url": post.original_url,
            }))
            self._save_error(post, errors)
            return None

        # ── AI language review ──
        try:
            prompt_template = self._load_prompt('quality_check.txt', 'AI_PROMPT_QUALITY_CHECK')
            prompt = prompt_template.format(content=post.content)

            response = self._create(
                role="quality_check",
                model=self.review_model,
                max_tokens=4000,  # thinking counts against max_tokens on Opus 5
                effort="medium",
                messages=[{"role": "user", "content": prompt}]
            )
            raw = self._response_text(response)
            _lf_ctx.update_current_observation(
                input=prompt,
                output=raw,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.review_model,
                metadata={"post_source": post.source, "platform": post.platform},
            )

            json_match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(json_match.group()) if json_match else json.loads(raw)

            if not result.get('pass', True):
                reason = result.get('reason', 'AI language review failed')
                logger.error(f"❌ Quality gate REJECTED (language): {reason}")
                logger.info(json.dumps({
                    "event": "quality_gate_rejected",
                    "reason": "language",
                    "detail": reason,
                    "source": post.source,
                }))
                self._save_error(post, [reason])
                return None

            if result.get('corrected_content'):
                issues = result.get('issues', [])
                logger.info(f"✏️  Quality gate corrected content: {issues}")
                original_content = post.content
                post.content = result['corrected_content']
                post._corrected = True
                self._save_correction(post, original_content, result['corrected_content'], issues)
            else:
                logger.info("✅ Quality gate: content is clean")

        except Exception as e:
            # AI review failure is non-blocking — structural checks already passed
            logger.warning(f"⚠️  Quality gate AI review skipped (error): {e}")

        return post

    @_lf_observe(name="generate_footage_queries")
    def generate_footage_queries(self, title: str, description: str) -> tuple:
        """Generate Pexels-optimised search queries + a geographic footage plan.

        Returns (queries, avoid_terms, plan):
            queries     — up to 5 English queries, most-specific first, sanitised
            avoid_terms — 3–5 visual concepts that must NOT appear in footage
            plan        — see :func:`footage.build_footage_plan`; carries the
                          story's place and the derived ``place_mode`` that tells
                          the rest of the pipeline how careful to be.

        Queries and avoid terms fall back to empty lists on failure; the plan is
        always a valid dict so callers never need to guard it.
        """
        prompt = self._load_prompt_or(
            'footage_queries.txt', 'AI_PROMPT_FOOTAGE_QUERIES', _FOOTAGE_QUERIES_PROMPT,
        ).format(title=title, description=description)
        try:
            response = self._create(
                role="footage_queries",
                model=self.footage_model,
                max_tokens=3000,  # thinking counts against max_tokens on Opus 5
                effort="low",
                messages=[{"role": "user", "content": prompt}],
            )
            text = self._response_text(response)
            match = re.search(r'\{.*\}', text, re.DOTALL)
            data = json.loads(match.group() if match else text)
            queries = [q for q in data.get('queries', []) if isinstance(q, str) and q.strip()]
            avoid = [a for a in data.get('avoid', []) if isinstance(a, str) and a.strip()]

            plan = build_footage_plan(
                place=data.get('place') or "",
                place_type=data.get('place_type') or "none",
                queries=queries[:5],
                avoid=avoid[:5],
            )
            _lf_ctx.update_current_observation(
                input=prompt,
                output=text,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.footage_model,
                metadata={
                    "article_title": title,
                    "place": plan["place"],
                    "place_mode": plan["place_mode"],
                },
            )
            if plan["queries"]:
                logger.info(json.dumps({"event": "footage_plan", **plan}, ensure_ascii=False))
                return plan["queries"], plan["avoid"], plan
        except Exception as e:
            logger.warning(f"⚠️  Footage query generation failed, falling back to keyword extraction: {e}")
        return [], [], build_footage_plan()

    # Fixed caption suffix (not a prompt) — kept stable across posts.
    _CAROUSEL_HASHTAGS = (
        "\n\n#Netherlands #DutchNews #Holland #Amsterdam #Dutch "
        "#ExpatsNL #LivingInTheNetherlands #DidYouKnow #DutchFacts #NLtrivia"
    )

    def generate_carousel_caption(self, fact_texts: List[str]) -> str:
        """Caption for the weekly Dutch-fact carousel (hook + save/follow CTA + tags).

        The AI writes only the short caption body (prompt in
        ``prompts/carousel_caption.txt`` → ``AI_PROMPT_CAROUSEL_CAPTION``); a
        curated hashtag block is appended in code so tags stay stable. Falls back
        to a template if the API call fails — the carousel is never blocked by
        caption generation.
        """
        preview = "\n".join(f"- {t}" for t in fact_texts[:7])
        try:
            prompt = self._load_prompt('carousel_caption.txt', 'AI_PROMPT_CAROUSEL_CAPTION').format(facts=preview)
            response = self._create(
                role="carousel_caption",
                model=self.model,
                max_tokens=2000,  # thinking counts against max_tokens on Opus 5
                effort="low",
                messages=[{"role": "user", "content": prompt}],
            )
            text = self._response_text(response).strip()
            if text:
                logger.info("📝 Carousel caption generated")
                return text + self._CAROUSEL_HASHTAGS
        except Exception as e:
            logger.warning(f"⚠️  Carousel caption generation failed, using template: {e}")
        template = (
            "Your weekly dose of Netherlands trivia is here. "
            "Swipe through, save it for later, and follow for a new Dutch fact every day."
        )
        return template + self._CAROUSEL_HASHTAGS

    def validate_footage_thumbnails(
        self,
        headline: str,
        avoid_terms: List[str],
        thumbnail_urls: List[str],
        place_mode: str = "none",
        place: str = "",
        details_out: Optional[List[dict]] = None,
    ) -> List[bool]:
        """Check each thumbnail with Haiku vision. True = usable, False = skip.

        **One** set of criteria for every place mode. The split that used to
        live here — a "can a viewer name this place?" block for ``no_stock`` and
        a "plausibly related" block for everything else — is why a heatwave Reel
        shipped cosy-fireplace clips: the relevance question existed in only one
        branch and the identifiability question in the other, so a decorative
        indoor fire passed both ways. A single block cannot drift like that.

        The four tests, all mandatory:
          1. subject   — does the image show what the story is ABOUT?
          2. place     — does it claim a location we cannot verify? (*place* is
             named when we may show it, so "wrong airport" stays checkable)
          3. terrain   — could this be the Netherlands at all?
          4. avoid     — the story's own *avoid_terms*.

        Generic-but-unmistakably-Dutch **wide** shots pass on purpose: a polder,
        a canal, flat farmland or a motorway names no town, so it fails none of
        the tests above while being the only thing that keeps the account
        looking Dutch.

        Batches up to 15 thumbnails in a single call. Falls back to all-True on
        API error; *details_out* records that so the audit trail shows it.
        """
        if not thumbnail_urls:
            return []

        batch = thumbnail_urls[:VISION_BATCH_LIMIT]
        avoid_str = ", ".join(avoid_terms) if avoid_terms else "none"
        n = len(batch)

        # Naming the place makes the wrong-location check concrete: "schiphol
        # airport" queries happily return Kansai, and a generic "wrong country
        # or landmark" instruction let that through. With no nameable place
        # (no_stock/none) any identifiable location is wrong by definition.
        if place_mode == "stock_ok" and place:
            place_rule = (
                f"This story is in {place}, so footage of {place} is fine. FAIL if the "
                "image is identifiable as a DIFFERENT place: another city's skyline, "
                "another airport or port, a landmark or setting a viewer could name."
            )
        else:
            place_rule = (
                "We do NOT have verified footage of where this story happened, so the "
                "footage must not claim to be anywhere in particular. FAIL if a viewer "
                "could name the place: a recognisable city skyline, harbour panorama or "
                "aerial cityscape; a landmark, monument, bridge or building; readable "
                "signage, shop or street names, or number plates; a flag."
            )

        criteria = (
            "Judge each image on FOUR tests. Any single failure means FAIL.\n\n"
            "1. SUBJECT — does the image plausibly show what this story is ABOUT?\n"
            "   FAIL decorative or staged stand-ins that merely share a word with the "
            "story: a fireplace, candle, match or campfire for a wildfire story; a "
            "studio set-up; cosy or 'serene' stock set dressing. An outdoor news event "
            "needs outdoor footage of that event, not a domestic scene.\n"
            f"2. PLACE — {place_rule}\n"
            "3. TERRAIN — could this be the Netherlands? The country is flat and has no "
            "mountains, rocky river gorges, cliffs, canyons, deserts, tropical "
            "vegetation or alpine forest. FAIL those, and FAIL weather or season that "
            "contradicts the story (blooming spring fields for a drought, snow for a "
            "heatwave).\n"
            f"4. AVOID — FAIL if the image shows: {avoid_str}\n\n"
            "PASS everything else, including wide shots that are generically Dutch "
            "without naming a town: canals, polders, flat farmland, motorways, cycle "
            "paths, rows of terraced houses. Those are what makes the Reel read as "
            "Dutch news — do not fail them for being wide."
        )

        content: List[dict] = [
            {
                "type": "text",
                "text": (
                    f'News headline: "{headline}"\n'
                    f"{criteria}\n\n"
                    f"For each of the {n} images below, reply with exactly one line:\n"
                    '"<number>: PASS" or "<number>: FAIL - <one-word reason>"'
                ),
            }
        ]
        for url in batch:
            content.append({"type": "image", "source": {"type": "url", "url": url}})

        try:
            response = self._create(
                role="vision_footage_gate",
                # 15 PASS/FAIL verdicts measured at 502-606 output tokens, so a
                # 3000 ceiling only ever bought thinking. Right-sized with room
                # to spare; effort low because this is visual classification,
                # not reasoning.
                model=self.vision_model,
                max_tokens=1000,
                effort="low",
                messages=[{"role": "user", "content": content}],
            )
            text = self._response_text(response)
            results: List[bool] = []
            for i in range(1, n + 1):
                match = re.search(
                    rf'^\s*{i}\s*:\s*(PASS|FAIL)[^\n]*', text, re.IGNORECASE | re.MULTILINE,
                )
                ok = match.group(1).upper() == 'PASS' if match else True
                results.append(ok)
                if details_out is not None:
                    details_out.append({
                        "index": i,
                        "ok": ok,
                        "reason": match.group(0).strip()[:60] if match else "unparsed",
                    })
            logger.info(f"🖼️  Thumbnail validation ({place_mode}): {sum(results)}/{n} passed")
            self._shadow_compare(content, results, headline, place_mode)
            return results
        except Exception as e:
            logger.warning(f"⚠️  Thumbnail validation failed, using all clips: {e}")
            if details_out is not None:
                details_out.extend(
                    {"index": i, "ok": True, "reason": "gate_error"} for i in range(1, n + 1)
                )
            return [True] * n

    def _shadow_compare(self, content: list, primary: List[bool],
                        headline: str, place_mode: str) -> None:
        """Run a challenger model on the SAME thumbnails and record the delta.

        Answers "could a cheaper model run this gate?" with production data
        instead of a guess. The gate is 58% of a run's Claude spend and is
        input-dominated, so the only lever that moves it is the input price per
        token: Opus $5, Sonnet $3, Haiku $1 per MTok.

        WHY AGREEMENT RATE ALONE IS THE WRONG METRIC
        --------------------------------------------
        The two directions of disagreement are not equally bad:

          * shadow FAILs what primary PASSed  -> stricter. Wasteful: fewer
            candidate clips survive, so the search escalates more often. Costs
            money, never ships a wrong image.
          * shadow PASSes what primary FAILed -> LOOSER. This is the dangerous
            one. Primary rejected that clip because it claimed the wrong place,
            showed impossible terrain, or wasn't the story's subject — the exact
            failures (Harlingen, Kansai, the cosy fireplace) this gate exists to
            stop. A cheaper model that is merely "95% in agreement" but looser
            on the 5% is not cheaper, it is broken.

        So the recorded verdict is driven by `looser` count, not by agreement.

        Entirely best-effort: flag-gated, wrapped, and never touches `primary`.
        A shadow failure must not cost a post.
        """
        shadow_model = os.environ.get("VISION_SHADOW_MODEL", "").strip()
        if not shadow_model or shadow_model == self.vision_model:
            return
        try:
            response = self._create(
                role="vision_shadow",
                model=shadow_model,
                max_tokens=1000,
                effort="low",
                messages=[{"role": "user", "content": content}],
            )
            text = self._response_text(response)
            n = len(primary)
            shadow: List[bool] = []
            for i in range(1, n + 1):
                m = re.search(rf'^\s*{i}\s*:\s*(PASS|FAIL)[^\n]*',
                              text, re.IGNORECASE | re.MULTILINE)
                shadow.append(m.group(1).upper() == 'PASS' if m else True)

            agree = sum(1 for a, b in zip(primary, shadow, strict=True) if a == b)
            looser = [i for i, (a, b) in enumerate(zip(primary, shadow, strict=True), 1)
                      if b and not a]
            stricter = [i for i, (a, b) in enumerate(zip(primary, shadow, strict=True), 1)
                        if a and not b]

            record = {
                "event": "vision_shadow",
                "headline": headline[:80],
                "place_mode": place_mode,
                "primary_model": self.vision_model,
                "shadow_model": shadow_model,
                "clips": n,
                "agree": agree,
                "agreement_pct": round(agree / n * 100, 1) if n else 0.0,
                "shadow_looser": len(looser),      # DANGEROUS direction
                "shadow_stricter": len(stricter),  # merely wasteful
                "primary_passed": sum(primary),
                "shadow_passed": sum(shadow),
            }
            logger.info(json.dumps(record))
            self._persist_shadow(record)
        except Exception as exc:
            logger.warning(f"⚠️  Vision shadow comparison skipped: {exc}")

    @staticmethod
    def _persist_shadow(record: dict) -> None:
        """One small object per comparison, so the weekly email can aggregate.

        Per-run keys rather than one appended file: two news runs a day plus a
        carousel could otherwise race a read-modify-write and lose samples.
        """
        if boto3 is None:
            return
        try:
            bucket = os.environ.get(
                "RESULTS_BUCKET", "news-ai-agent-results-645949963620")
            key = f"vision_ab/{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
            boto3.client("s3").put_object(
                Bucket=bucket, Key=key,
                Body=json.dumps(record).encode("utf-8"),
                ContentType="application/json",
            )
        except Exception as exc:
            logger.warning(f"⚠️  Could not persist vision A/B sample: {exc}")

    # ── Event methods ──────────────────────────────────────────────────────────

    @_lf_observe(name="score_events")
    def score_events(self, events: List[Dict]) -> List[Dict]:
        """Score each event with Claude Haiku (0–8). Returns events with score ≥ 5.

        Scoring rubric per event (total 8 pts):
          audience_fit   0–3: relevant to English-speaking expats/tourists?
          completeness   0–2: has title + date + location + price?
          public_access  0–2: genuinely public event (not private/corporate)?
          visual_appeal  0–1: photogenic / Instagram-worthy?
        """
        if not events:
            return []

        events_json = json.dumps(
            [{"index": i, "title": e.get("title"), "description": e.get("description"),
              "location": e.get("location"), "start_date": e.get("start_date"),
              "price": e.get("price"), "category": e.get("category"), "source": e.get("source")}
             for i, e in enumerate(events)],
            ensure_ascii=False,
        )

        prompt = (
            "You are evaluating Netherlands events for an English-language Instagram account "
            "targeting expats, international students, and tourists.\n\n"
            "Score each event from 0–8 using this rubric:\n"
            "  audience_fit  (0–3): How relevant to English-speaking non-Dutch audience?\n"
            "  completeness  (0–2): Has usable title + date + location? Price is a bonus.\n"
            "  public_access (0–2): Is it a genuine public event anyone can attend?\n"
            "  visual_appeal (0–1): Is it visually interesting for Instagram?\n\n"
            f"Events (JSON):\n{events_json}\n\n"
            "Return ONLY valid JSON: "
            '{{"scores": [{{"index": 0, "score": 7}}, ...]}}'
        )

        try:
            response = self._create(
                role="score_events",
                model=self.review_model,
                max_tokens=4000,  # thinking counts against max_tokens on Opus 5
                effort="low",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = self._response_text(response)
            _lf_ctx.update_current_observation(
                input=prompt,
                output=raw,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.review_model,
                metadata={"event_count": len(events)},
            )
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group() if match else raw)

            score_map = {s["index"]: s["score"] for s in result.get("scores", [])}
            passed = [
                {**events[i], "_score": score_map.get(i, 0)}
                for i in range(len(events))
                if score_map.get(i, 0) >= 5
            ]
            all_scored = [
                {**events[i], "_score": score_map.get(i, 0)}
                for i in range(len(events))
            ]
            logger.info(f"🔍 Event scoring: {len(events)} in → {len(passed)} passed (score ≥ 5)")
            # Store for pipeline reporting
            self._last_score_prompt = prompt
            self._last_score_response = raw
            self._last_all_scored = all_scored
            return passed

        except Exception as e:
            logger.warning(f"⚠️  Event scoring failed, using all events unfiltered: {e}")
            self._last_score_prompt = prompt
            self._last_score_response = str(e)
            self._last_all_scored = []
            return events

    @_lf_observe(name="select_and_format_events")
    def select_and_format_events(
        self, events: List[Dict], date_range: str, min_events: int = 5, max_events: int = 12
    ) -> Optional[Dict]:
        """Select best events and generate Instagram caption + card data.

        Returns dict with keys:
          selected_events: list of event dicts (title, date_label, location, price, emoji, description)
          caption:         full formatted Instagram caption string
          hashtags:        list of hashtag strings

        Returns None on failure.
        """
        if not events:
            return None

        events_text = ""
        for i, ev in enumerate(events, 1):
            events_text += (
                f"\nEVENT {i}:\n"
                f"  Title: {ev.get('title', '')}\n"
                f"  Description: {ev.get('description', '')}\n"
                f"  Start: {ev.get('start_date', '')}\n"
                f"  Location: {ev.get('location', '')}\n"
                f"  Venue: {ev.get('venue', '') or 'N/A'}\n"
                f"  Category: {ev.get('category', '')}\n"
                f"  Price: {ev.get('price') or 'Unknown'}\n"
                f"  Source: {ev.get('source', '')}\n"
                f"  URL: {ev.get('url', '')}\n"
            )

        prompt_template = self._load_prompt('event_selection.txt', 'AI_PROMPT_EVENT_SELECTION')
        prompt = prompt_template.format(
            event_count=len(events),
            min_events=min_events,
            max_events=max_events,
            date_range=date_range,
            events_text=events_text,
        )

        try:
            response = self._create(
                role="events_selection",
                model=self.model,
                max_tokens=8000,  # thinking counts against max_tokens on Opus 5
                effort="medium",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = self._response_text(response)
            _lf_ctx.update_current_observation(
                input=prompt,
                output=raw,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"event_count": len(events), "max_events": max_events, "date_range": date_range},
            )
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group() if match else raw)

            selected = result.get("selected_events", [])
            caption = result.get("caption", "")
            hashtags = result.get("hashtags", [])

            if not selected or not caption:
                logger.error("❌ Event selection: empty response from AI")
                return None

            logger.info(f"✅ Events selected: {len(selected)} events, caption {len(caption)} chars")
            return {
                "selected_events": selected,
                "caption": caption,
                "hashtags": hashtags,
                "_prompt": prompt,
                "_raw_response": raw,
            }

        except Exception as e:
            logger.error(f"❌ Event selection/formatting failed: {e}")
            return None

    def _save_error(self, post: SocialMediaPost, reasons: List[str]):
        """Save rejected post details to S3 (Lambda) or local errors/ directory."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        error_data = {
            'timestamp': timestamp,
            'rejected_reasons': reasons,
            'original_content': post.content,
            'full_post': post.format_post(),
            'original_title': post.original_title,
            'original_url': post.original_url,
            'source': post.source,
            'emoji': post.emoji,
            'hashtags': post.hashtags,
            'platform': post.platform,
        }

        filename = f'rejected_{timestamp}.json'
        self._persist_json(f'errors/{filename}', error_data)

    def _save_correction(self, post: SocialMediaPost, original: str, corrected: str, issues: List[str]):
        """Save correction details (before/after) to S3 (Lambda) or local errors/ directory."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        correction_data = {
            'timestamp': timestamp,
            'type': 'correction',
            'issues': issues,
            'original_content': original,
            'corrected_content': corrected,
            'original_title': post.original_title,
            'original_url': post.original_url,
            'source': post.source,
            'platform': post.platform,
        }

        filename = f'corrected_{timestamp}.json'
        self._persist_json(f'errors/{filename}', correction_data)

    def _persist_json(self, key: str, data: dict):
        """Write JSON to S3 (when RESULTS_BUCKET is set) or local filesystem."""
        bucket = os.environ.get('RESULTS_BUCKET')

        if bucket and boto3:
            try:
                s3 = boto3.client('s3')
                s3.put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=json.dumps(data, ensure_ascii=False, indent=2),
                    ContentType='application/json',
                )
                logger.info(f"📝 Saved to S3: s3://{bucket}/{key}")
                return
            except Exception as e:
                logger.warning(f"⚠️  S3 write failed, falling back to local: {e}")

        # Local fallback (development)
        local_path = PROJECT_ROOT / key
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with open(local_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"📝 Saved locally: {local_path}")
    
    def process_batch(self, articles: List[Dict], max_posts: int = 10, platform: str = "twitter",
                      recently_published: Optional[List[str]] = None,
                      slot_brief: str = "", content_mix: str = "") -> List[SocialMediaPost]:
        """Process multiple articles - selects which articles to post in a single API call"""
        if not articles:
            logger.warning("No articles to process")
            return []

        try:
            logger.info(f"Selecting and processing articles from {len(articles)} total articles (max {max_posts} posts)")

            # Single API call to select articles and create posts
            posts = self._select_and_process_articles(
                articles, max_posts, platform, recently_published,
                slot_brief=slot_brief, content_mix=content_mix,
            )

            logger.info(f"Successfully selected and processed {len(posts)} posts")
            return posts

        except Exception as e:
            logger.error(f"Error in batch processing: {str(e)}")
            # Fallback: process first article only
            if articles:
                try:
                    logger.info("Falling back to processing first article only")
                    post = self.process_article(articles[0], target_platform=platform)
                    return [post]
                except Exception:
                    pass
            return []

    @_lf_observe(name="select_and_process_articles")
    def _select_and_process_articles(self, articles: List[Dict], max_posts: int, platform: str,
                                     recently_published: Optional[List[str]] = None,
                                     slot_brief: str = "", content_mix: str = "") -> List[SocialMediaPost]:
        """Select which articles to post and create posts for them in a single API call"""
        prompt = self._create_batch_selection_prompt(
            articles, max_posts, platform, recently_published,
            slot_brief=slot_brief, content_mix=content_mix,
        )

        try:
            response = self._create(
                role="batch_selection",
                model=self.model,
                max_tokens=8000,  # thinking counts against max_tokens on Opus 5
                effort="medium",
                messages=[{
                    "role": "user",
                    "content": prompt
                }]
            )
            text = self._response_text(response)
            _lf_ctx.update_current_observation(
                input=prompt,
                output=text,
                usage={"input": response.usage.input_tokens, "output": response.usage.output_tokens},
                model=self.model,
                metadata={"article_count": len(articles), "max_posts": max_posts, "platform": platform},
            )

            result = self._parse_batch_response(text, articles, platform)
            return result
            
        except Exception as e:
            logger.error(f"Error in article selection: {str(e)}")
            raise
    
    def _create_batch_selection_prompt(self, articles: List[Dict], max_posts: int, platform: str,
                                       recently_published: Optional[List[str]] = None,
                                       slot_brief: str = "", content_mix: str = "") -> str:
        """Create prompt for selecting articles and creating posts"""
        if platform == "twitter":
            max_length = 280
        elif platform == "instagram":
            max_length = 2200
        else:
            max_length = 500

        articles_text = ""
        for i, article in enumerate(articles, 1):
            articles_text += f"""
ARTICLE {i}:
- Title: {article['title']}
- Description: {article['description']}
- Source: {article['source'].upper()}
- Category: {article.get('category', 'general')}
- URL: {article['url']}
"""

        # Topic-ARC dedup, not title matching. The old rule blocked anything
        # that resembled a published title, which suppressed the escalation
        # beats that are the most engaging part of a running story. What is
        # actually banned is the same event told from the same angle.
        if recently_published:
            titles_list = "\n".join(f"- {t}" for t in recently_published)
            recent_publications = (
                "PUBLISHED IN THE LAST 72 HOURS:\n"
                f"{titles_list}\n\n"
                "DEDUP RULE — SAME EVENT + SAME ANGLE ONLY.\n"
                "Block a candidate only when it retells one of the above from the SAME angle "
                "with nothing materially new (the same match result, the same accident, the "
                "same announcement re-reported by another outlet).\n"
                "EXPLICITLY ALLOWED — a continuation of a running story when there is a NEW "
                "HARD NUMBER or a clear ESCALATION: a new record ('laagste ooit gemeten'), a "
                "next stage ('vierde hittegolf'), a new consequence ('haven dicht'), new "
                "damage, casualties, or a distinct phenomenon. A covered heatwave does NOT "
                "block the storms that end it.\n"
                "WHEN YOU SELECT A CONTINUATION, the 'hook' MUST open with the escalation "
                "frame — state the previous stage, then the new one "
                "(e.g. 'Last week the river hit a record low — today the port closed').\n\n"
            )
        else:
            recent_publications = ""

        prompt_template = self._load_prompt('batch_selection.txt', 'AI_PROMPT_BATCH_SELECTION')
        hashtag_instruction = 'Include 5-10 relevant hashtags (in English)' if platform == 'instagram' else 'Include 3-5 relevant hashtags (in English)'

        body = prompt_template.format(
            recent_publications=recent_publications,
            article_count=len(articles),
            max_posts=max_posts,
            platform=platform,
            articles_text=articles_text,
            max_length=max_length,
            hashtag_instruction=hashtag_instruction
        )

        # The slot brief and content mix are PREPENDED, not substituted through
        # a {placeholder}. The prompt template lives in Secrets Manager and is
        # edited independently of a deploy, so a new placeholder would make the
        # two a matched pair: publishing the prompt first raises KeyError in the
        # running Lambda, which process_batch swallows into the single-article
        # fallback — a silent collapse to "publish articles[0], no tiers".
        # Prepending keeps prompt and code independently deployable in either order.
        preamble = ''.join(part + "\n\n" for part in (slot_brief, content_mix) if part)
        return preamble + body
    
    def _parse_batch_response(self, response_text: str, articles: List[Dict], platform: str) -> List[SocialMediaPost]:
        """Parse batch response and create SocialMediaPost objects"""
        try:
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
            if json_match:
                result = json.loads(json_match.group())
            else:
                result = json.loads(response_text)
            
            if 'selected_articles' not in result:
                raise ValueError("Missing 'selected_articles' field in response")

            # Editorial-transparency: top runner-up candidates the AI passed over.
            # Optional — older prompts / responses omit it, so default to [].
            runner_ups = result.get('runner_ups') or []
            if not isinstance(runner_ups, list):
                runner_ups = []

            posts = []
            for selected in result['selected_articles']:
                article_index = selected.get('article_index')
                if article_index is None:
                    logger.warning("Missing article_index in selected article, skipping")
                    continue

                # Convert to 0-based index
                idx = article_index - 1
                if idx < 0 or idx >= len(articles):
                    logger.warning(f"Invalid article_index {article_index}, skipping")
                    continue

                article = articles[idx]

                # A series name outside the known set is dropped rather than
                # honoured — an invented one-off series is worse than none.
                series = (selected.get('series') or '').strip()
                if series and series not in SocialMediaPost.SERIES_HASHTAGS:
                    logger.warning(f"Unknown series '{series}' — ignoring")
                    series = ''

                post = SocialMediaPost(
                    original_title=article['title'],
                    original_url=article['url'],
                    source=article['source'],
                    content=selected.get('content', ''),
                    hashtags=selected.get('hashtags', []),
                    emoji=selected.get('emoji', '📰'),
                    hook=selected.get('hook', ''),
                    platform=platform,
                    image_url=article.get('image_url'),
                    cta_question=(selected.get('cta_question') or '').strip(),
                    save_prompt=(selected.get('save_prompt') or '').strip(),
                    series=series,
                    selection_reason=selected.get('selection_reason', ''),
                    personal_stake=_as_int(selected.get('personal_stake'), 0),
                    share_potential=_as_int(selected.get('share_potential'), 0),
                    runner_ups=runner_ups,
                )
                posts.append(post)

            if not posts:
                logger.warning("No valid posts created from batch response")

            # Structured, machine-parseable record of the editorial decision so it
            # survives in CloudWatch even if posts_*.json is later pruned. Consumed
            # by the weekly selection-review email.
            try:
                logger.info(json.dumps({
                    "event": "selection_decision",
                    "pool_size": len(articles),
                    "chosen": [
                        {
                            "title": p.original_title,
                            "selection_reason": p.selection_reason,
                            "personal_stake": p.personal_stake,
                            "share_potential": p.share_potential,
                            "series": p.series,
                        }
                        for p in posts
                    ],
                    "runner_ups": runner_ups,
                }, ensure_ascii=False))
            except Exception:
                pass

            return posts
            
        except json.JSONDecodeError:
            logger.error(f"Failed to parse JSON response: {response_text[:500]}")
            # Fallback: return empty list
            return []
        except Exception as e:
            logger.error(f"Error parsing batch response: {str(e)}")
            return []


def save_posts_json(posts: List[SocialMediaPost], filename: str = 'social_posts.json'):
    """Save processed posts to JSON"""
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump([post.to_dict() for post in posts], f, ensure_ascii=False, indent=2)
    logger.info(f"Saved {len(posts)} posts to {filename}")


if __name__ == "__main__":
    with open('articles.json', 'r', encoding='utf-8') as f:
        articles = json.load(f)
    
    agent = NewsAIAgent()
    posts = agent.process_batch(articles, max_posts=5)
    
    for post in posts:
        print("\n" + "="*60)
        print(post.format_post())
    
    save_posts_json(posts)
