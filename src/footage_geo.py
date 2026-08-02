"""
Geographic safety for stock footage selection.

The problem this solves: Pexels **never returns nothing**. Search it for
"deventer harbour" and it hands back the closest visual match from anywhere on
earth — Hamburg, Singapore, Rotterdam. Viewers who know Deventer read that as
"the AI generated a fake image".

Nothing downstream can repair it either: no stock library records where a clip
was shot, and no vision model can tell one small river harbour from another.
So the fix is upstream — never *ask* for a place whose footage does not exist,
and reject candidates that visibly claim to be somewhere.

The rule: prefer showing **no** place over showing the **wrong** place.

Kept dependency-light on purpose — imported by both ``ai_agent`` and
``video.footage``, neither of which should pull in the other.
"""

import re
from typing import Dict, List, Optional, Set

from event_scraper import NL_CITIES

# ── Places we are allowed to name ────────────────────────────────────────────
# Dutch places with enough genuinely identifiable stock footage that asking for
# them by name returns *that* place. Everything else is a coin flip, so we say
# nothing rather than guess. This list is a judgement call, deliberately in code
# where it shows up in a diff — never something the model decides per-article.
STOCK_SAFE_PLACES: Set[str] = {
    "amsterdam", "rotterdam", "the hague", "den haag", "utrecht",
    "maastricht", "schiphol", "keukenhof", "zaanse schans", "giethoorn",
    "netherlands", "nederland", "holland", "dutch", "nl",
}

# Country-level terms, safe in *any* query: they promise a country, not a city,
# and the story is in the Netherlands either way. Note this is narrower than
# STOCK_SAFE_PLACES — for a Deventer story "rotterdam port" is still the bug,
# even though Rotterdam is a place we'd happily name in a Rotterdam story.
_NATIONAL_TERMS: Set[str] = {"netherlands", "nederland", "holland", "dutch", "nl"}

# Dutch places beyond the NL_CITIES top-30 that show up in stock-clip slugs and
# queries. Ports and postcard towns dominate on purpose: that is what Pexels has
# footage OF, so that is what a wrong-place match looks like. Proven in the
# wild: a Deventer harbour story shipped with a clip of Harlingen — a viewer
# named the port from the video ("147Km from Deventer") and called it AI slop.
_NL_PLACE_TOKENS: Set[str] = {
    # ports & coast
    "harlingen", "ijmuiden", "vlissingen", "terneuzen", "delfzijl",
    "den helder", "urk", "volendam", "marken", "scheveningen", "katwijk",
    "zandvoort", "hoorn", "enkhuizen", "medemblik", "stavoren", "makkum",
    "lauwersoog", "hellevoetsluis", "brielle", "yerseke", "bruinisse",
    # wadden islands
    "texel", "terschelling", "vlieland", "ameland", "schiermonnikoog",
    # postcard towns
    "giethoorn", "kinderdijk", "gouda", "schoonhoven", "elburg", "kampen",
    "harderwijk", "woerden", "culemborg",
    # river & inland towns that recur in news
    "moerdijk", "gorinchem", "zutphen", "tiel", "wageningen", "ede",
    "sneek", "lelystad", "roermond", "sittard", "weert", "oss", "uden",
    "veghel",
}

# Place names that are also ordinary words. Banning these would mangle innocent
# queries ("river goes dry") and mis-flag innocent slugs ("man-goes-fishing").
_AMBIGUOUS_PLACE_TOKENS: Set[str] = {"goes", "best", "bergen", "nl"}

# Framing words that make Pexels return "somewhere identifiable" rather than
# "the subject". A close-up of a crane could be any port; an aerial never is.
_IDENTIFYING_TERMS: Set[str] = {
    "skyline", "aerial", "panorama", "panoramic", "cityscape", "landmark",
    "monument", "downtown", "city center", "city centre", "town centre",
    "town center", "overview", "drone", "establishing shot", "city square",
}

# Famous places stock libraries name their clips after — the loudest offenders,
# caught for free from the Pexels URL slug before any vision call.
_WORLD_PLACE_TOKENS: Set[str] = {
    "new york", "manhattan", "brooklyn", "los angeles", "san francisco",
    "chicago", "miami", "las vegas", "seattle", "boston", "washington",
    "toronto", "vancouver", "mexico city", "rio de janeiro", "sao paulo",
    "buenos aires", "london", "paris", "eiffel", "berlin", "hamburg", "munich",
    "frankfurt", "cologne", "madrid", "barcelona", "lisbon", "porto", "rome",
    "milan", "venice", "florence", "naples", "athens", "santorini", "vienna",
    "prague", "budapest", "warsaw", "krakow", "zurich", "geneva", "brussels",
    "antwerp", "bruges", "copenhagen", "stockholm", "oslo", "helsinki",
    "reykjavik", "dublin", "edinburgh", "istanbul", "moscow", "kyiv", "dubai",
    "abu dhabi", "doha", "riyadh", "cairo", "marrakech", "cape town",
    "nairobi", "lagos", "mumbai", "delhi", "bangkok", "singapore",
    "hong kong", "shanghai", "beijing", "tokyo", "kyoto", "osaka", "seoul",
    "sydney", "melbourne", "auckland", "bali", "maldives", "hawaii",
    "manhattan bridge", "golden gate", "big ben", "colosseum", "acropolis",
    "sagrada familia", "brandenburg", "times square", "central park",
    # airports — a Schiphol story shipped with Kansai (Osaka) footage and a
    # viewer named the airport from the video
    "kansai", "incheon", "changi", "heathrow", "gatwick", "stansted",
    "charles de gaulle", "orly", "jfk", "laguardia", "gardermoen",
    "tegel", "schoenefeld", "haneda", "narita", "suvarnabhumi", "ataturk",
    "sabiha", "zaventem", "vantaa",
}


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace."""
    return " ".join((text or "").lower().split())


def _tokenise(text: str) -> str:
    """Reduce any string (incl. URL slugs) to space-separated lowercase words."""
    return " ".join(t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t)


def _contains_phrase(haystack: str, phrase: str) -> bool:
    """Word-boundary containment for space-separated token strings."""
    return f" {phrase} " in f" {haystack} "


def derive_place_mode(place: str, place_type: str) -> str:
    """Decide how careful the pipeline must be, in code — never by the model.

    Asking Claude "does Pexels have footage of Deventer?" is exactly the
    unreliable judgement this module exists to design out. The model extracts
    the place (a task it does well); the tier is a lookup.

    Returns ``"none"`` (no geographic anchor), ``"stock_ok"`` (we may name the
    place) or ``"no_stock"`` (footage must not claim any place).
    """
    place_type = _norm(place_type) or "none"
    p = _norm(place)
    if place_type == "none" or not p:
        return "none"
    if place_type == "country" or p in STOCK_SAFE_PLACES:
        return "stock_ok"
    return "no_stock"


def banned_places_for(place: str, place_mode: str) -> Set[str]:
    """Place names that must not appear in a Pexels *query*."""
    if place_mode != "no_stock":
        return set()
    banned = {p for p in NL_CITIES | _NL_PLACE_TOKENS if p not in _NATIONAL_TERMS}
    p = _norm(place)
    if p and p not in _NATIONAL_TERMS:
        banned.add(p)
        banned.update(t for t in p.split() if len(t) > 2)
    return banned - _AMBIGUOUS_PLACE_TOKENS - _NATIONAL_TERMS


def slug_banned_places_for(place: str, place_mode: str) -> Set[str]:
    """Place names that must not appear in a candidate clip's URL slug.

    Broader than the query set, and — unlike queries — active for ``stock_ok``
    stories too: a Schiphol story may *ask* for "schiphol airport", but a clip
    whose slug says Kansai/Osaka is still the wrong airport (a viewer called
    exactly this out). ``no_stock`` bans Dutch + world places; ``stock_ok``
    bans world places only, minus the story's own place. Erring wide for
    foreign-country stories (a Germany story loses Berlin clips) is accepted —
    a generic clip always exists, a retraction doesn't.
    """
    if place_mode == "no_stock":
        return banned_places_for(place, place_mode) | _WORLD_PLACE_TOKENS
    if place_mode == "stock_ok":
        own = {_norm(place)} | set(_norm(place).split())
        return {w for w in _WORLD_PLACE_TOKENS if w not in own}
    return set()


def sanitize_query(query: str, banned_places: Set[str]) -> str:
    """Strip place names and identifying framing from a Pexels query.

    Returns ``""`` when nothing usable remains — callers drop it rather than
    searching for an empty string.
    """
    text = _norm(query)
    if not text:
        return ""
    # Multi-word phrases first, so "den haag" and "city centre" go as a unit.
    for phrase in sorted(
        (p for p in banned_places | _IDENTIFYING_TERMS if " " in p), key=len, reverse=True
    ):
        text = text.replace(phrase, " ")
    kept = [t for t in text.split() if t not in banned_places and t not in _IDENTIFYING_TERMS]
    return " ".join(kept)


def sanitize_queries(queries: List[str], place: str, place_mode: str) -> List[str]:
    """Clean a query list, preserving order and dropping duplicates/empties.

    A no-op for ``stock_ok``/``none`` stories: "amsterdam skyline" is a fine
    query when the story really is about Amsterdam.
    """
    if place_mode != "no_stock":
        return [q.strip() for q in (queries or []) if q and q.strip()]
    banned = banned_places_for(place, place_mode)
    cleaned: List[str] = []
    for q in queries or []:
        s = sanitize_query(q, banned)
        if s and s not in cleaned:
            cleaned.append(s)
    return cleaned


def claims_a_place(slug: str, banned_places: Set[str]) -> bool:
    """True when a Pexels URL slug names a place in *banned_places*.

    Pexels slugs are descriptive (``.../video/aerial-view-of-hamburg-harbor-123/``)
    and the API already returns them, so this filter costs nothing and runs
    before the vision gate. Stock libraries name clips after the famous place —
    which is precisely the failure mode. Build the set with
    :func:`slug_banned_places_for` so it matches the story's place mode.
    """
    tokens = _tokenise(slug)
    if not tokens:
        return False
    for phrase in banned_places:
        if _contains_phrase(tokens, phrase):
            return True
    return False


def build_footage_plan(
    place: str = "",
    place_type: str = "none",
    queries: Optional[List[str]] = None,
    avoid: Optional[List[str]] = None,
) -> Dict:
    """Assemble the plan that travels with a Reel through the whole pipeline.

    Always returns a valid dict — callers never need to guard it.
    """
    place = _norm(place)
    place_type = _norm(place_type) or "none"
    place_mode = derive_place_mode(place, place_type)
    return {
        "place": place,
        "place_type": place_type,
        "place_mode": place_mode,
        "queries": sanitize_queries(queries or [], place, place_mode),
        "avoid": [a.strip() for a in (avoid or []) if a and a.strip()],
    }
