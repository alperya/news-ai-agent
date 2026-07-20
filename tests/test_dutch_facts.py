"""Unit tests for the daily Dutch-fact pool + S3-backed LRU rotation."""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import dutch_facts


@pytest.fixture
def s3_store(monkeypatch):
    """In-memory replacement for the S3 read/write helpers; silence SNS alerts."""
    store: dict = {}
    monkeypatch.setattr(dutch_facts, "_read_json", lambda bucket, key, default: store.get(key, default))
    monkeypatch.setattr(dutch_facts, "_write_json", lambda bucket, key, data: store.__setitem__(key, data))
    monkeypatch.setattr(dutch_facts, "send_alert", lambda *a, **k: True)
    return store


class TestPoolIntegrity:
    def test_unique_ids(self):
        ids = [f["id"] for f in dutch_facts.DEFAULT_FACTS]
        assert len(ids) == len(set(ids))

    def test_every_fact_has_required_fields(self):
        for f in dutch_facts.DEFAULT_FACTS:
            assert f["id"] and f["text"] and f["footage_queries"]

    def test_no_em_dash_ai_tells(self):
        # Recent requirement: avoid AI-tell punctuation (em/en dashes) in copy.
        for f in dutch_facts.DEFAULT_FACTS:
            assert "—" not in f["text"] and "–" not in f["text"], f["id"]

    def test_bikes_opens_the_calendar(self):
        assert dutch_facts.DEFAULT_FACTS[0]["id"] == "bikes-outnumber-people"


class TestRotation:
    def test_seeds_pool_to_s3_when_absent(self, s3_store):
        dutch_facts.get_fact_for_today("bkt")
        assert "facts/pool.json" in s3_store
        assert len(s3_store["facts/pool.json"]) == len(dutch_facts.DEFAULT_FACTS)

    def test_first_pick_is_bikes(self, s3_store):
        assert dutch_facts.get_fact_for_today("bkt")["id"] == "bikes-outnumber-people"

    def test_deterministic_curated_order(self, s3_store):
        picks = [dutch_facts.get_fact_for_today("bkt")["id"] for _ in range(3)]
        assert picks == [f["id"] for f in dutch_facts.DEFAULT_FACTS[:3]]

    def test_no_repeat_until_pool_exhausted(self, s3_store):
        n = len(dutch_facts.DEFAULT_FACTS)
        picks = [dutch_facts.get_fact_for_today("bkt")["id"] for _ in range(n)]
        assert len(set(picks)) == n  # every fact aired once before any repeat

    def test_rotation_state_persisted(self, s3_store):
        dutch_facts.get_fact_for_today("bkt")
        rot = s3_store["facts/_rotation.json"]
        assert "bikes-outnumber-people" in rot["used"]

    def test_lru_repeat_after_exhaustion_does_not_crash(self, s3_store):
        n = len(dutch_facts.DEFAULT_FACTS)
        for _ in range(n):
            dutch_facts.get_fact_for_today("bkt")
        nxt = dutch_facts.get_fact_for_today("bkt")  # all used → LRU repeat
        assert nxt["id"] in {f["id"] for f in dutch_facts.DEFAULT_FACTS}

    def test_refill_alert_fires_once_per_cycle(self, s3_store, monkeypatch):
        calls = []
        monkeypatch.setattr(dutch_facts, "send_alert", lambda *a, **k: calls.append(a))
        for _ in range(len(dutch_facts.DEFAULT_FACTS)):
            dutch_facts.get_fact_for_today("bkt")
        assert len(calls) == 1  # one reminder as the pool neared exhaustion

    def test_stale_used_ids_are_dropped(self, s3_store):
        # A used id no longer in the pool must not break selection.
        s3_store["facts/_rotation.json"] = {"used": {"removed-fact": "2020-01-01"}, "cycle_alerted": False}
        fact = dutch_facts.get_fact_for_today("bkt")
        assert fact["id"] == "bikes-outnumber-people"
        assert "removed-fact" not in s3_store["facts/_rotation.json"]["used"]


class TestWeeklyFacts:
    def _ids(self, facts):
        return [f["id"] for f in facts]

    def test_empty_when_no_rotation(self, s3_store):
        assert dutch_facts.get_weekly_facts("bkt") == []

    def test_returns_recent_window_only(self, s3_store):
        today = date.today()
        pool = dutch_facts.DEFAULT_FACTS
        s3_store["facts/_rotation.json"] = {"used": {
            pool[0]["id"]: (today - timedelta(days=1)).isoformat(),   # in window
            pool[1]["id"]: (today - timedelta(days=6)).isoformat(),   # in window
            pool[2]["id"]: (today - timedelta(days=20)).isoformat(),  # too old
        }}
        got = self._ids(dutch_facts.get_weekly_facts("bkt", days=7))
        assert pool[0]["id"] in got and pool[1]["id"] in got
        assert pool[2]["id"] not in got

    def test_ordered_oldest_to_newest(self, s3_store):
        today = date.today()
        pool = dutch_facts.DEFAULT_FACTS
        s3_store["facts/_rotation.json"] = {"used": {
            pool[0]["id"]: (today - timedelta(days=2)).isoformat(),
            pool[1]["id"]: (today - timedelta(days=5)).isoformat(),
            pool[2]["id"]: today.isoformat(),
        }}
        got = self._ids(dutch_facts.get_weekly_facts("bkt", days=7))
        assert got == [pool[1]["id"], pool[0]["id"], pool[2]["id"]]

    def test_skips_ids_not_in_pool(self, s3_store):
        today = date.today()
        pool = dutch_facts.DEFAULT_FACTS
        s3_store["facts/_rotation.json"] = {"used": {
            "removed-fact": today.isoformat(),
            pool[0]["id"]: today.isoformat(),
        }}
        got = self._ids(dutch_facts.get_weekly_facts("bkt"))
        assert got == [pool[0]["id"]]

    def test_ignores_malformed_dates(self, s3_store):
        pool = dutch_facts.DEFAULT_FACTS
        s3_store["facts/_rotation.json"] = {"used": {
            pool[0]["id"]: "not-a-date",
            pool[1]["id"]: date.today().isoformat(),
        }}
        got = self._ids(dutch_facts.get_weekly_facts("bkt"))
        assert got == [pool[1]["id"]]

    def test_min_threshold_constant_sane(self):
        assert dutch_facts.MIN_CAROUSEL_FACTS >= 2
