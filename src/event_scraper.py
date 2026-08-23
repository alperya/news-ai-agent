"""
Event Scraper for the Netherlands
Sources (7):
  API  — Ticketmaster
  RSS  — amsterdam.nl, uitagenda.nl, denhaag.nl, partyflock.nl, festileaks.nl, doedagen.nl

Note: Eventbrite removed public API access in 2023; replaced by partyflock.nl RSS.
"""

import html
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

NL_CITIES = {
    "amsterdam", "rotterdam", "den haag", "the hague", "utrecht", "eindhoven",
    "groningen", "tilburg", "almere", "breda", "nijmegen", "enschede", "haarlem",
    "arnhem", "zaandam", "amersfoort", "maastricht", "dordrecht", "leiden",
    "zoetermeer", "zwolle", "deventer", "delft", "alkmaar", "leeuwarden",
    "apeldoorn", "venlo", "goes", "middelburg", "heerlen", "netherlands",
    "nederland", "nl",
}

CATEGORY_EMOJI: Dict[str, str] = {
    "festival": "🎉",
    "concert": "🎵",
    "music": "🎵",
    "museum": "🎨",
    "art": "🎨",
    "sport": "🏃",
    "sports": "🏃",
    "theatre": "🎭",
    "theater": "🎭",
    "food": "🍽️",
    "film": "🎬",
    "family": "👨‍👩‍👧",
    "outdoor": "🌿",
    "other": "📍",
}

# RSS feeds that need no API key — (source_name, url, default_location)
# amsterdam.nl: /agenda/?output=rss (old /agenda/rss/ returns 403)
# partyflock.nl: replaces Eventbrite (Eventbrite shut down public API access in 2023)
_RSS_SOURCES = [
    ("denhaag.nl",   "https://www.denhaag.nl/nl/rss/",  "The Hague"),
    ("festileaks.nl", "https://festileaks.com/feed/",   "Netherlands"),
    ("doedagen.nl",   "https://www.doedagen.nl/feed/",  "Netherlands"),
]


@dataclass
class EventItem:
    title: str
    description: str
    url: str
    start_date: str           # ISO format: "2025-06-14T20:00:00+00:00"
    location: str             # "Amsterdam"
    source: str               # e.g. "Eventbrite"
    venue: Optional[str] = None
    category: str = "other"
    price: Optional[str] = None    # "Free" | "€25" | "€15–€45"
    image_url: Optional[str] = None

    def emoji(self) -> str:
        return CATEGORY_EMOJI.get(self.category.lower(), "📍")

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["emoji"] = self.emoji()
        return d


class EventScraper:
    """Multi-source event scraper covering 8 Netherlands event sources."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (compatible; NewsAIAgent/1.0)",
            "Accept": "application/json, application/xml, text/xml, */*",
        })
        self._ticketmaster_key = os.getenv("TICKETMASTER_API_KEY", "")

    # ── Public ─────────────────────────────────────────────────────────────────

    def scrape_all_sources(self, days_ahead: int = 7) -> List[EventItem]:
        """Fetch from all 8 sources, deduplicate, and return valid events.

        Per-source results are stored in ``self.source_results`` for reporting.
        """
        all_events: List[EventItem] = []
        self.source_results: List[dict] = []

        # API sources — Ticketmaster only (Eventbrite shut down public API in 2023)
        api_sources = [
            ("Ticketmaster", lambda: self._fetch_ticketmaster(days_ahead)),
        ]
        for name, fetcher in api_sources:
            try:
                events = fetcher()
                self.source_results.append({"source": name, "count": len(events), "ok": True})
                logger.info(f"   {name}: {len(events)} events")
                all_events.extend(events)
            except Exception as e:
                self.source_results.append({"source": name, "count": 0, "ok": False, "error": str(e)})
                logger.warning(f"⚠️  {name} failed: {e}")

        # 3–8 — RSS sources
        for source_name, feed_url, default_city in _RSS_SOURCES:
            try:
                resp = self.session.get(feed_url, timeout=12)
                resp.raise_for_status()
                events = self._parse_event_rss(resp.text, source=source_name, default_location=default_city)
                self.source_results.append({"source": source_name, "count": len(events), "ok": True})
                logger.info(f"   {source_name}: {len(events)} events")
                all_events.extend(events)
            except Exception as e:
                self.source_results.append({"source": source_name, "count": 0, "ok": False, "error": str(e)})
                logger.warning(f"⚠️  {source_name} RSS failed: {e}")

        unique = self._deduplicate(all_events)
        valid = [e for e in unique if self._is_valid(e, days_ahead)]
        logger.info(
            f"Events total: {len(all_events)} raw → {len(unique)} unique → {len(valid)} valid"
        )
        return valid

    # ── Source 1: Ticketmaster API ─────────────────────────────────────────────

    def _fetch_ticketmaster(self, days_ahead: int) -> List[EventItem]:
        if not self._ticketmaster_key:
            logger.info("TICKETMASTER_API_KEY not set — skipping Ticketmaster")
            return []

        now = datetime.now(timezone.utc)
        end = now + timedelta(days=days_ahead)
        resp = self.session.get(
            "https://app.ticketmaster.com/discovery/v2/events.json",
            params={
                "apikey": self._ticketmaster_key,
                "countryCode": "NL",
                "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endDateTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "size": 50,
                "sort": "date,asc",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return [
            item for e in (resp.json().get("_embedded") or {}).get("events", [])
            if (item := self._parse_ticketmaster(e)) is not None
        ]

    def _parse_ticketmaster(self, e: dict) -> Optional[EventItem]:
        try:
            venues = (e.get("_embedded") or {}).get("venues", [{}])
            venue_obj = venues[0] if venues else {}
            city = (venue_obj.get("city") or {}).get("name", "")
            if not self._is_nl_location(city):
                return None

            dates = e.get("dates") or {}
            start = (dates.get("start") or {}).get("dateTime") or \
                    (dates.get("start") or {}).get("localDate", "")

            cls = (e.get("classifications") or [{}])[0]
            segment = (cls.get("segment") or {}).get("name", "")
            genre = (cls.get("genre") or {}).get("name", "")
            category = _tm_category(segment, genre)

            price_ranges = e.get("priceRanges") or []
            if price_ranges:
                lo = price_ranges[0].get("min", 0)
                hi = price_ranges[0].get("max", 0)
                price_str: Optional[str] = f"€{lo:.0f}" if lo == hi else f"€{lo:.0f}–€{hi:.0f}"
            else:
                price_str = None

            images = e.get("images") or []
            image_url = next(
                (img["url"] for img in images if img.get("ratio") == "16_9"),
                images[0]["url"] if images else None,
            )

            return EventItem(
                title=e.get("name", ""),
                description=f"{genre} event in {city}".strip(),
                url=e.get("url", ""),
                start_date=start, location=city,
                venue=venue_obj.get("name"), category=category,
                price=price_str, image_url=image_url, source="Ticketmaster",
            )
        except Exception as ex:
            logger.debug(f"Ticketmaster parse error: {ex}")
            return None

    # ── Sources 3–8: RSS ───────────────────────────────────────────────────────

    def _parse_event_rss(
        self, xml_text: str, source: str, default_location: str
    ) -> List[EventItem]:
        try:
            # Strip BOM and leading whitespace — some feeds (e.g. festileaks) prepend \r\n
            xml_text = xml_text.lstrip('﻿\r\n\t ')
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning(f"RSS parse error ({source}): {e}")
            return []

        ns = {
            "content": "http://purl.org/rss/1.0/modules/content/",
            "media":   "http://search.yahoo.com/mrss/",
            "atom":    "http://www.w3.org/2005/Atom",
            "dc":      "http://purl.org/dc/elements/1.1/",
        }
        channel = root.find("channel")
        items = channel.findall("item") if channel is not None else root.findall("atom:entry", ns)

        events: List[EventItem] = []
        for item in items[:30]:
            try:
                # `item` is bound as a default arg, not captured: the closure is
                # redefined per iteration today so late binding never bites, but
                # binding it explicitly keeps that true if the call ever moves.
                # `is None` rather than `or`: an Element with no children is
                # falsy, so `find(tag) or find(tag, ns)` ran the namespaced
                # lookup for every plain <title>/<link> — and Element.__bool__
                # is deprecated besides.
                def txt(tag: str, item=item) -> str:
                    el = item.find(tag)
                    if el is None:
                        el = item.find(tag, ns)
                    return html.unescape(el.text.strip()) if el is not None and el.text else ""

                title = txt("title")
                link = txt("link") or txt("atom:link")
                raw_desc = txt("description") or txt("summary") or txt("atom:summary")
                description = re.sub(r"<[^>]+>", "", raw_desc).strip()[:300]
                pub = txt("pubDate") or txt("dc:date") or txt("atom:published")
                start_date = _parse_rss_date(pub)

                image_url: Optional[str] = None
                media = item.find("media:content", ns)
                if media is not None:
                    image_url = media.attrib.get("url")
                enc = item.find("enclosure")
                if enc is not None and enc.attrib.get("type", "").startswith("image/"):
                    image_url = enc.attrib.get("url") or enc.attrib.get("href")

                location = _extract_city(title + " " + description) or default_location
                category = _guess_category(title + " " + description)

                events.append(EventItem(
                    title=title, description=description, url=link,
                    start_date=start_date, location=location,
                    category=category, image_url=image_url, source=source,
                ))
            except Exception as ex:
                logger.debug(f"RSS item parse error ({source}): {ex}")

        return events

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _deduplicate(self, events: List[EventItem]) -> List[EventItem]:
        seen_urls: set = set()
        seen_titles: set = set()
        unique: List[EventItem] = []
        for e in events:
            url_key = e.url.rstrip("/").lower()
            title_key = re.sub(r"\W+", " ", e.title.lower()).strip()
            if url_key and url_key in seen_urls:
                continue
            if title_key in seen_titles:
                continue
            seen_urls.add(url_key)
            seen_titles.add(title_key)
            unique.append(e)
        return unique

    def _is_nl_location(self, location: str) -> bool:
        loc = location.lower().strip()
        return any(city in loc for city in NL_CITIES)

    def _is_valid(self, event: EventItem, days_ahead: int) -> bool:
        if not event.title or len(event.title) < 5:
            return False
        if not event.start_date:
            return False
        if not event.location:
            return False
        try:
            start = datetime.fromisoformat(event.start_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            if start < now - timedelta(hours=2):
                return False
            if start > now + timedelta(days=days_ahead):
                return False
        except Exception:
            pass
        return True


# ── Module-level helpers ───────────────────────────────────────────────────────

def _parse_rss_date(raw: str) -> str:
    if not raw:
        return datetime.now(timezone.utc).isoformat()
    for fmt in (
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(raw.strip(), fmt).isoformat()
        except ValueError:
            pass
    return raw


def _tm_category(segment: str, genre: str) -> str:
    seg = segment.lower()
    if "music" in seg:
        return "concert"
    if "sport" in seg:
        return "sport"
    if "art" in seg or "theatr" in seg:
        return "theatre"
    if "film" in seg:
        return "film"
    return "other"


def _guess_category(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("festival", "festijn")):
        return "festival"
    if any(w in t for w in ("concert", "muziek", "music", "band", "gig", "optreden")):
        return "concert"
    if any(w in t for w in ("museum", "expositie", "exhibition", "kunst", "gallery")):
        return "museum"
    if any(w in t for w in ("sport", "marathon", "voetbal", "tennis", "wielren", "hardlopen")):
        return "sport"
    if any(w in t for w in ("theater", "theatre", "toneel", "opera", "ballet", "cabaret")):
        return "theatre"
    if any(w in t for w in ("film", "cinema", "bioscoop", "movie")):
        return "film"
    if any(w in t for w in ("familie", "family", "kinderen", "children", "kids")):
        return "family"
    return "other"


def _extract_city(text: str) -> Optional[str]:
    t = text.lower()
    # Check longest names first to avoid "den" matching before "den haag"
    for city in sorted(NL_CITIES, key=len, reverse=True):
        if city in ("nl", "netherlands", "nederland"):
            continue
        if city in t:
            return city.title()
    return None
