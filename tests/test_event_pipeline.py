"""
Tests for the weekly events pipeline.
Covers: event_scraper, ai_agent event methods (score_events, select_and_format_events),
        and event_card pure utilities (_wrap, _gradient_fallback, generate_carousel_slides).

Mock strategy: all external I/O (Claude API, Pexels, PIL rendering) is mocked.
See local_only/test_architecture.md for the full pattern guide.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


# ═══════════════════════════════════════════════════════════════════════════════
# event_scraper
# ═══════════════════════════════════════════════════════════════════════════════

from event_scraper import (
    EventItem, EventScraper,
    _extract_city, _guess_category, _parse_rss_date, _tm_category,
)

SAMPLE_EVENT_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Amsterdam Jazz Festival 2026</title>
      <link>https://example.com/event/1</link>
      <description>A great jazz festival in Amsterdam city centre.</description>
      <pubDate>Sat, 20 Jun 2026 14:00:00 +0200</pubDate>
    </item>
    <item>
      <title>Rotterdam Art Exhibition</title>
      <link>https://example.com/event/2</link>
      <description>Modern art exhibition at Boijmans Rotterdam.</description>
      <pubDate>Sun, 21 Jun 2026 10:00:00 +0200</pubDate>
    </item>
  </channel>
</rss>"""


@pytest.fixture
def scraper():
    return EventScraper()


def _make_event(title="Cool Festival", days=2, location="Amsterdam"):
    start = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    return EventItem(title=title, description="desc", url="http://x.com/e",
                     start_date=start, location=location, source="test")


# ── EventItem ──────────────────────────────────────────────────────────────────

class TestEventItem:
    def test_emoji_concert(self):
        ev = EventItem("T", "", "", "", "AMS", "t", category="concert")
        assert ev.emoji() == "🎵"

    def test_emoji_festival(self):
        ev = EventItem("T", "", "", "", "AMS", "t", category="festival")
        assert ev.emoji() == "🎉"

    def test_emoji_sport(self):
        ev = EventItem("T", "", "", "", "AMS", "t", category="sports")
        assert ev.emoji() == "🏃"

    def test_emoji_museum(self):
        ev = EventItem("T", "", "", "", "AMS", "t", category="museum")
        assert ev.emoji() == "🎨"

    def test_emoji_unknown_defaults_to_pin(self):
        ev = EventItem("T", "", "", "", "AMS", "t", category="xyz_unknown")
        assert ev.emoji() == "📍"

    def test_to_dict_contains_emoji_key(self):
        ev = EventItem("Festival", "desc", "http://x.com", "2026-06-20", "Amsterdam", "test", category="festival")
        d = ev.to_dict()
        assert d["emoji"] == "🎉"
        assert d["title"] == "Festival"
        assert d["location"] == "Amsterdam"

    def test_to_dict_has_all_dataclass_fields(self):
        ev = EventItem("T", "d", "u", "s", "l", "src")
        for field in ("title", "description", "url", "start_date", "location", "source", "category"):
            assert field in ev.to_dict()


# ── _is_nl_location ────────────────────────────────────────────────────────────

class TestIsNLLocation:
    def test_amsterdam_is_nl(self, scraper):
        assert scraper._is_nl_location("Amsterdam") is True

    def test_city_inside_sentence(self, scraper):
        assert scraper._is_nl_location("Event in Rotterdam") is True

    def test_the_hague_is_nl(self, scraper):
        assert scraper._is_nl_location("The Hague") is True

    def test_case_insensitive(self, scraper):
        assert scraper._is_nl_location("AMSTERDAM") is True

    def test_foreign_city_not_nl(self, scraper):
        assert scraper._is_nl_location("Berlin") is False

    def test_empty_string_not_nl(self, scraper):
        assert scraper._is_nl_location("") is False

    def test_netherlands_keyword_is_nl(self, scraper):
        assert scraper._is_nl_location("Netherlands") is True


# ── _is_valid ──────────────────────────────────────────────────────────────────

class TestIsValid:
    def test_valid_future_event_passes(self, scraper):
        assert scraper._is_valid(_make_event(), days_ahead=7) is True

    def test_past_event_rejected(self, scraper):
        ev = _make_event()
        ev.start_date = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        assert scraper._is_valid(ev, days_ahead=7) is False

    def test_event_within_grace_period_passes(self, scraper):
        ev = _make_event()
        ev.start_date = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        assert scraper._is_valid(ev, days_ahead=7) is True

    def test_too_far_future_rejected(self, scraper):
        assert scraper._is_valid(_make_event(days=10), days_ahead=7) is False

    def test_empty_title_rejected(self, scraper):
        assert scraper._is_valid(_make_event(title=""), days_ahead=7) is False

    def test_short_title_rejected(self, scraper):
        assert scraper._is_valid(_make_event(title="Fun"), days_ahead=7) is False

    def test_missing_start_date_rejected(self, scraper):
        ev = _make_event()
        ev.start_date = ""
        assert scraper._is_valid(ev, days_ahead=7) is False

    def test_missing_location_rejected(self, scraper):
        assert scraper._is_valid(_make_event(location=""), days_ahead=7) is False


# ── _deduplicate ───────────────────────────────────────────────────────────────

class TestDeduplicate:
    def _ev(self, title, url):
        return EventItem(title, "", url, "2026-06-20T14:00:00+00:00", "Amsterdam", "test")

    def test_duplicate_url_removed(self, scraper):
        evs = [self._ev("A", "https://ex.com/1"), self._ev("A copy", "https://ex.com/1")]
        assert len(scraper._deduplicate(evs)) == 1

    def test_trailing_slash_deduped(self, scraper):
        evs = [self._ev("A", "https://ex.com/event/"), self._ev("B", "https://ex.com/event")]
        assert len(scraper._deduplicate(evs)) == 1

    def test_duplicate_title_removed(self, scraper):
        evs = [self._ev("Amsterdam Music Festival", "https://ex.com/1"),
               self._ev("Amsterdam Music Festival", "https://ex.com/2")]
        assert len(scraper._deduplicate(evs)) == 1

    def test_unique_events_all_kept(self, scraper):
        evs = [self._ev("A", "https://ex.com/1"), self._ev("B", "https://ex.com/2"),
               self._ev("C", "https://ex.com/3")]
        assert len(scraper._deduplicate(evs)) == 3

    def test_empty_list_returns_empty(self, scraper):
        assert scraper._deduplicate([]) == []


# ── _parse_event_rss ───────────────────────────────────────────────────────────

class TestParseEventRSS:
    def test_parses_two_items(self, scraper):
        events = scraper._parse_event_rss(SAMPLE_EVENT_RSS, "test", "Netherlands")
        assert len(events) == 2

    def test_parses_titles_and_links(self, scraper):
        events = scraper._parse_event_rss(SAMPLE_EVENT_RSS, "test", "Netherlands")
        assert events[0].title == "Amsterdam Jazz Festival 2026"
        assert events[0].url == "https://example.com/event/1"

    def test_city_extracted_from_title(self, scraper):
        events = scraper._parse_event_rss(SAMPLE_EVENT_RSS, "test", "Netherlands")
        assert events[0].location == "Amsterdam"

    def test_falls_back_to_default_location(self, scraper):
        rss = """<?xml version="1.0"?><rss version="2.0"><channel>
          <item><title>Generic Event Somewhere</title><link>https://ex.com/e</link>
          <description>No NL city mentioned here.</description>
          <pubDate>Sat, 20 Jun 2026 14:00:00 +0000</pubDate></item>
        </channel></rss>"""
        events = scraper._parse_event_rss(rss, "test", "Utrecht")
        assert events[0].location == "Utrecht"

    def test_source_set_on_all_events(self, scraper):
        events = scraper._parse_event_rss(SAMPLE_EVENT_RSS, "denhaag.nl", "The Hague")
        assert all(e.source == "denhaag.nl" for e in events)

    def test_invalid_xml_returns_empty(self, scraper):
        assert scraper._parse_event_rss("<not valid xml", "test", "NL") == []

    def test_empty_string_returns_empty(self, scraper):
        assert scraper._parse_event_rss("", "test", "NL") == []


# ── _parse_ticketmaster ────────────────────────────────────────────────────────

class TestParseTicketmaster:
    def _tm(self, city="Amsterdam", segment="Music", genre="Rock", lo=25.0, hi=75.0):
        return {
            "name": "Test Concert", "url": "https://ticketmaster.com/event/1",
            "_embedded": {"venues": [{"city": {"name": city}, "name": "Ziggo Dome"}]},
            "dates": {"start": {"dateTime": "2026-06-20T20:00:00Z"}},
            "classifications": [{"segment": {"name": segment}, "genre": {"name": genre}}],
            "priceRanges": [{"min": lo, "max": hi}],
            "images": [{"url": "https://ex.com/img.jpg", "ratio": "16_9"}],
        }

    def test_parses_dutch_city(self, scraper):
        ev = scraper._parse_ticketmaster(self._tm())
        assert ev is not None and ev.location == "Amsterdam"

    def test_rejects_foreign_city(self, scraper):
        assert scraper._parse_ticketmaster(self._tm(city="London")) is None

    def test_price_range_formatted(self, scraper):
        assert scraper._parse_ticketmaster(self._tm()).price == "€25–€75"

    def test_single_price_no_dash(self, scraper):
        ev = scraper._parse_ticketmaster(self._tm(lo=30.0, hi=30.0))
        assert ev.price == "€30"

    def test_no_price_ranges_is_none(self, scraper):
        data = self._tm()
        data["priceRanges"] = []
        ev = scraper._parse_ticketmaster(data)
        assert ev.price is None

    def test_music_segment_maps_to_concert(self, scraper):
        assert scraper._parse_ticketmaster(self._tm(segment="Music")).category == "concert"

    def test_venue_name_set(self, scraper):
        assert scraper._parse_ticketmaster(self._tm()).venue == "Ziggo Dome"

    def test_malformed_event_returns_none(self, scraper):
        assert scraper._parse_ticketmaster({}) is None

    def test_no_api_key_skips_ticketmaster(self, scraper):
        scraper._ticketmaster_key = ""
        assert scraper._fetch_ticketmaster(days_ahead=7) == []


# ── helper functions ───────────────────────────────────────────────────────────

class TestGuessCategory:
    @pytest.mark.parametrize("text,expected", [
        ("Amsterdam Summer Festival", "festival"),
        ("Jazz concert at Paradiso", "concert"),
        ("Dutch music event Rotterdam", "concert"),
        ("New exhibition at Stedelijk Museum", "museum"),
        ("Rotterdam Marathon 2026", "sport"),
        ("Opera at the national theatre", "theatre"),
        ("Film premiere at cinema Amsterdam", "film"),
        ("Children's activities for families in Utrecht", "family"),
        ("Random miscellaneous happening", "other"),
    ])
    def test_detection(self, text, expected):
        assert _guess_category(text) == expected


class TestExtractCity:
    def test_extracts_amsterdam(self):
        assert _extract_city("Summer festival in Amsterdam") == "Amsterdam"

    def test_extracts_rotterdam(self):
        assert _extract_city("Event in Rotterdam") == "Rotterdam"

    def test_den_haag_not_partial_match(self):
        assert _extract_city("Den Haag music event") == "Den Haag"

    def test_returns_none_for_foreign(self):
        assert _extract_city("Event in Berlin Germany") is None


class TestTmCategory:
    @pytest.mark.parametrize("segment,genre,expected", [
        ("Music", "Rock", "concert"),
        ("Sports", "Football", "sport"),
        ("Arts & Theatre", "Ballet", "theatre"),
        ("Film", "Action", "film"),
        ("Miscellaneous", "General", "other"),
    ])
    def test_mapping(self, segment, genre, expected):
        assert _tm_category(segment, genre) == expected


class TestParseRSSDate:
    def test_rfc822_format(self):
        assert "2026" in _parse_rss_date("Thu, 20 Jun 2026 14:00:00 +0200")

    def test_iso_z_format(self):
        assert "2026-06-20" in _parse_rss_date("2026-06-20T14:00:00Z")

    def test_date_only_format(self):
        assert "2026" in _parse_rss_date("2026-06-20")

    def test_empty_returns_current_year(self):
        assert str(datetime.now(timezone.utc).year) in _parse_rss_date("")

    def test_unparseable_returns_original(self):
        assert _parse_rss_date("not a date") == "not a date"


# ═══════════════════════════════════════════════════════════════════════════════
# ai_agent — event methods
# ═══════════════════════════════════════════════════════════════════════════════

class TestScoreEvents:
    def _mock_response(self, scores):
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps({"scores": scores}))]
        resp.usage = MagicMock(input_tokens=100, output_tokens=50)
        return resp

    def test_returns_events_above_threshold(self, ai_agent, sample_events_dicts):
        scores = [{"index": 0, "score": 7}, {"index": 1, "score": 6}, {"index": 2, "score": 3}]
        ai_agent.client.messages.create.return_value = self._mock_response(scores)
        result = ai_agent.score_events(sample_events_dicts)
        assert len(result) == 2
        assert all(e["_score"] >= 5 for e in result)

    def test_score_attached_to_event(self, ai_agent, sample_events_dicts):
        scores = [{"index": 0, "score": 8}, {"index": 1, "score": 2}, {"index": 2, "score": 1}]
        ai_agent.client.messages.create.return_value = self._mock_response(scores)
        result = ai_agent.score_events(sample_events_dicts)
        assert result[0]["_score"] == 8
        assert result[0]["title"] == "Amsterdam Jazz Festival"

    def test_empty_input_skips_api_call(self, ai_agent):
        result = ai_agent.score_events([])
        assert result == []
        ai_agent.client.messages.create.assert_not_called()

    def test_fallback_to_unfiltered_on_api_error(self, ai_agent, sample_events_dicts):
        ai_agent.client.messages.create.side_effect = Exception("timeout")
        result = ai_agent.score_events(sample_events_dicts)
        assert result == sample_events_dicts

    def test_stores_raw_response(self, ai_agent, sample_events_dicts):
        raw = '{"scores": [{"index": 0, "score": 7}]}'
        resp = MagicMock()
        resp.content = [MagicMock(text=raw)]
        resp.usage = MagicMock(input_tokens=100, output_tokens=20)
        ai_agent.client.messages.create.return_value = resp
        ai_agent.score_events(sample_events_dicts[:1])
        assert ai_agent._last_score_response == raw

    def test_all_scored_includes_low_scores(self, ai_agent, sample_events_dicts):
        scores = [{"index": 0, "score": 2}, {"index": 1, "score": 1}, {"index": 2, "score": 0}]
        ai_agent.client.messages.create.return_value = self._mock_response(scores)
        passed = ai_agent.score_events(sample_events_dicts)
        assert passed == []
        assert len(ai_agent._last_all_scored) == 3

    def test_all_scored_not_affected_by_threshold(self, ai_agent, sample_events_dicts):
        scores = [{"index": i, "score": 3} for i in range(len(sample_events_dicts))]
        ai_agent.client.messages.create.return_value = self._mock_response(scores)
        ai_agent.score_events(sample_events_dicts)
        assert len(ai_agent._last_all_scored) == len(sample_events_dicts)


class TestSelectAndFormatEvents:
    def _mock_selection(self, selected=None, caption="📅 THIS WEEK IN THE NETHERLANDS", hashtags=None):
        selected = selected or [
            {"event_index": 1, "title": "Amsterdam Jazz Festival", "date_label": "Sat 20 Jun",
             "location": "Amsterdam", "venue": "Paradiso", "price": "€15", "emoji": "🎵",
             "description": "Jazz festival at Paradiso Amsterdam."}
        ]
        resp = MagicMock()
        resp.content = [MagicMock(text=json.dumps({
            "selected_events": selected,
            "caption": caption,
            "hashtags": hashtags or ["#Netherlands"],
        }))]
        resp.usage = MagicMock(input_tokens=200, output_tokens=100)
        return resp

    def test_returns_selected_events_and_caption(self, ai_agent, sample_events_dicts):
        ai_agent.client.messages.create.return_value = self._mock_selection()
        result = ai_agent.select_and_format_events(sample_events_dicts, date_range="14–21 Jun 2026")
        assert result is not None
        assert "selected_events" in result and "caption" in result
        assert len(result["selected_events"]) == 1

    def test_empty_events_returns_none_without_api_call(self, ai_agent):
        result = ai_agent.select_and_format_events([], date_range="14–21 Jun 2026")
        assert result is None
        ai_agent.client.messages.create.assert_not_called()

    def test_empty_ai_response_returns_none(self, ai_agent, sample_events_dicts):
        resp = MagicMock()
        resp.content = [MagicMock(text='{"selected_events": [], "caption": ""}')]
        resp.usage = MagicMock(input_tokens=10, output_tokens=5)
        ai_agent.client.messages.create.return_value = resp
        assert ai_agent.select_and_format_events(sample_events_dicts, date_range="14–21 Jun") is None

    def test_min_and_max_events_injected_in_prompt(self, ai_agent, sample_events_dicts):
        ai_agent.client.messages.create.return_value = self._mock_selection()
        ai_agent.select_and_format_events(sample_events_dicts, date_range="14–21 Jun", min_events=5, max_events=12)
        prompt = ai_agent.client.messages.create.call_args[1]["messages"][0]["content"]
        assert "5" in prompt and "12" in prompt

    def test_returns_none_on_api_error(self, ai_agent, sample_events_dicts):
        ai_agent.client.messages.create.side_effect = Exception("connection error")
        assert ai_agent.select_and_format_events(sample_events_dicts, date_range="14–21 Jun") is None


# ═══════════════════════════════════════════════════════════════════════════════
# video/event_card — pure utility functions
# ═══════════════════════════════════════════════════════════════════════════════

class TestGradientFallback:
    def test_correct_dimensions(self):
        from video.event_card import _gradient_fallback
        img = _gradient_fallback(1080, 1920)
        assert img.size == (1080, 1920)

    def test_rgb_mode(self):
        from video.event_card import _gradient_fallback
        assert _gradient_fallback(100, 200).mode == "RGB"

    def test_arbitrary_dimensions(self):
        from video.event_card import _gradient_fallback
        img = _gradient_fallback(300, 400)
        assert img.size == (300, 400)


class TestWrap:
    def _font(self, char_w=10):
        f = MagicMock()
        f.getbbox = lambda text: (0, 0, len(text) * char_w, 20)
        return f

    def test_short_text_single_line(self):
        from video.event_card import _wrap
        result = _wrap("Short title", self._font(), max_width=500)
        assert result == ["Short title"]

    def test_long_text_respects_max_lines(self):
        from video.event_card import _wrap
        long = " ".join([f"Word{i}" for i in range(20)])
        result = _wrap(long, self._font(char_w=20), max_width=100, max_lines=2)
        assert len(result) <= 2

    def test_single_word_always_returned(self):
        from video.event_card import _wrap
        result = _wrap("Superlongword", self._font(), max_width=10)
        assert len(result) >= 1

    def test_empty_text_does_not_crash(self):
        from video.event_card import _wrap
        result = _wrap("", self._font(), max_width=500)
        assert isinstance(result, list)


class TestFetchPexelsPhotoNoKey:
    def test_returns_none_without_api_key(self):
        from video.event_card import _fetch_pexels_photo
        with patch("video.event_card.PEXELS_API_KEY", ""), \
             patch.dict("os.environ", {}, clear=True):
            result = _fetch_pexels_photo("amsterdam canal")
        assert result is None


class TestGenerateCarouselSlides:
    def _events(self, n):
        return [{"title": f"Event {i}", "date_label": "Sat 20 Jun", "location": "Amsterdam",
                 "venue": "Venue", "price": "Free", "emoji": "🎉"} for i in range(n)]

    def test_4_events_produces_2_slides(self):
        from video.event_card import generate_carousel_slides
        with patch("video.event_card.create_cover_slide"), \
             patch("video.event_card.create_event_list_slide"):
            paths = generate_carousel_slides(self._events(4), "14–21 Jun", "/tmp/test")
        assert len(paths) == 2  # 1 cover + 1 list (4 / EVENTS_PER_SLIDE = 1 chunk)

    def test_5_events_produces_3_slides(self):
        from video.event_card import generate_carousel_slides
        with patch("video.event_card.create_cover_slide"), \
             patch("video.event_card.create_event_list_slide"):
            paths = generate_carousel_slides(self._events(5), "14–21 Jun", "/tmp/test")
        assert len(paths) == 3  # 1 cover + 2 list: [0:4] + [4:5]

    def test_8_events_produces_3_slides(self):
        from video.event_card import generate_carousel_slides
        with patch("video.event_card.create_cover_slide"), \
             patch("video.event_card.create_event_list_slide"):
            paths = generate_carousel_slides(self._events(8), "14–21 Jun", "/tmp/test")
        assert len(paths) == 3  # 1 cover + 2 list: [0:4] + [4:8]

    def test_12_events_produces_4_slides(self):
        from video.event_card import generate_carousel_slides
        with patch("video.event_card.create_cover_slide"), \
             patch("video.event_card.create_event_list_slide"):
            paths = generate_carousel_slides(self._events(12), "14–21 Jun", "/tmp/test")
        assert len(paths) == 4  # 1 cover + 3 list: [0:4] + [4:8] + [8:12]

    def test_paths_have_expected_prefixes(self):
        from video.event_card import generate_carousel_slides
        with patch("video.event_card.create_cover_slide"), \
             patch("video.event_card.create_event_list_slide"):
            paths = generate_carousel_slides(self._events(4), "14–21 Jun", "/tmp/myslide")
        assert paths[0] == "/tmp/myslide_cover.jpg"
        assert "/tmp/myslide_list" in paths[1]
