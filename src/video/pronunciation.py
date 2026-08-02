"""
Dutch place-name pronunciation for English TTS narration.

The narration voice reads English text with English rules, so Dutch proper
nouns come out wrong — most famously "Twente" as "Twenty", which a viewer
called out in the comments ("At least try and program the AI voice to
pronounce it correctly"). The audience lives in the Netherlands and hears the
real pronunciations daily, so misreads register instantly as "AI slop".

Approach: a curated respelling lexicon applied to the text sent to TTS, then
mapped back so subtitles still show the real spelling. Subtitles are derived
from the *spoken* text's character alignment (see ``tts.py``), which is why
the display text must be restored afterwards rather than kept separate.

Design rules for respellings:
- **single token** (no spaces/hyphens) so word counts never drift between the
  spoken text and the alignment both engines return;
- **globally unique nonsense words** ("tventuh") so restoration is a safe
  reverse lookup — no index bookkeeping, robust even if an engine drops or
  merges tokens;
- conservative: only names that are clearly misread AND recur in Dutch news.
  A bad respelling is worse than an English accent, so entries are added after
  listening, not speculatively.

The lexicon is extendable without a redeploy via the ``TTS_PRONUNCIATIONS``
env var (JSON object, lowercase original → lowercase respelling), mirroring
the Secrets Manager pattern used for prompts.
"""

import json
import logging
import os
import re
from typing import Dict, List

logger = logging.getLogger(__name__)

# Lowercase original → lowercase single-token respelling an English voice
# reads approximately like the Dutch. Verified by ear before adding.
NL_TTS_RESPELL: Dict[str, str] = {
    # the comment-reported incident
    "twente": "tventuh",
    # provinces & regions
    "friesland": "freesland",
    "drenthe": "drentuh",
    "overijssel": "overeyssel",
    "flevoland": "flayvoland",
    "zeeland": "zaylant",
    # cities & towns that recur in news
    "enschede": "enskhuhday",
    "nijmegen": "nymayghen",
    "groningen": "khroningen",
    "utrecht": "ootrekt",
    "eindhoven": "eynthoven",
    "maastricht": "mahstrikt",
    "arnhem": "arnem",
    "leiden": "lyden",
    "breda": "bredah",
    "zwolle": "zvolluh",
    "deventer": "dayventer",
    "zutphen": "zutfen",
    "gorinchem": "khorinkem",
    "dordrecht": "dordrekt",
    "amersfoort": "ahmersfoart",
    "gouda": "howda",
    "haag": "hahg",
    # coast, water & islands
    "schiphol": "skippol",
    "scheveningen": "skayveningen",
    "ijssel": "eyssel",
    "ijsselmeer": "eysselmayr",
    "texel": "tessel",
    "terschelling": "terskelling",
    # recurring news words & institutions
    "vierdaagse": "feerdaakhsuh",
    "keukenhof": "kurkenhof",
    "giethoorn": "kheethoorn",
    "rijkswaterstaat": "rikeswaterstaat",
    "rijksmuseum": "rikesmuseum",
    "oranje": "oranyuh",
    "ajax": "ahyaks",
    "feyenoord": "fyenoart",
}


def _lexicon() -> Dict[str, str]:
    """Built-in lexicon merged with the optional TTS_PRONUNCIATIONS env JSON."""
    merged = dict(NL_TTS_RESPELL)
    raw = os.environ.get("TTS_PRONUNCIATIONS", "")
    if raw:
        try:
            extra = json.loads(raw)
            merged.update({
                str(k).lower(): str(v).lower()
                for k, v in extra.items() if str(k).strip() and str(v).strip()
            })
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning(f"⚠️  TTS_PRONUNCIATIONS is not valid JSON — ignoring: {e}")
    return merged


def _shape_case(template: str, word: str) -> str:
    """Give *word* the case shape of *template* (UPPER / Capitalized / lower)."""
    if template.isupper():
        return word.upper()
    if template[:1].isupper():
        return word.capitalize()
    return word


def _alternation(words) -> re.Pattern:
    """Word-boundary alternation, longest first so 'ijsselmeer' beats 'ijssel'."""
    ordered = sorted(words, key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(re.escape(w) for w in ordered) + r")\b",
                      re.IGNORECASE)


def respell_for_tts(text: str) -> str:
    """Replace known Dutch names with phonetic respellings for the TTS engine.

    Case shape and punctuation survive ("Twente." → "Tventuh."). Send the
    result to TTS; run :func:`restore_display_words` on whatever comes back
    before showing it to a viewer.
    """
    if not text:
        return text
    lex = _lexicon()
    if not lex:
        return text

    def repl(m: re.Match) -> str:
        return _shape_case(m.group(0), lex[m.group(0).lower()])

    return _alternation(lex.keys()).sub(repl, text)


def restore_display_words(segments: List) -> List:
    """Swap respelled tokens in subtitle segments back to the real spelling.

    Mutates and returns *segments* (``SubtitleSegment`` items — anything with a
    ``.text``). Safe to call on text that was never respelled: the respellings
    are unique nonsense tokens, so nothing else can match.
    """
    lex = _lexicon()
    if not segments or not lex:
        return segments
    reverse = {v: k for k, v in lex.items()}
    pattern = _alternation(reverse.keys())

    def repl(m: re.Match) -> str:
        return _shape_case(m.group(0), reverse[m.group(0).lower()])

    for seg in segments:
        seg.text = pattern.sub(repl, seg.text)
    return segments
