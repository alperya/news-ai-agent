"""Tests for the channel-agnostic CrossPoster dispatch."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from publishing import CrossPoster, ChannelPublisher, build_crossposter, REEL, PHOTO, STORY


class Fake(ChannelPublisher):
    def __init__(self, name, kinds=(REEL, PHOTO, STORY), fail=False):
        self.name = name
        self._kinds = kinds
        self._fail = fail
        self.calls = []

    def supports(self, kind):
        return kind in self._kinds

    def publish(self, kind, *, media_url, caption="", dry_run=False):
        self.calls.append((kind, media_url, caption, dry_run))
        if self._fail:
            raise RuntimeError(f"{self.name} boom")
        return {"id": f"{self.name}-{kind}"}


def test_primary_and_secondary_both_publish():
    p, s = Fake("instagram"), Fake("facebook")
    out = CrossPoster(p, [s]).publish(REEL, media_url="u", caption="c")
    assert out["primary_error"] is None
    assert out["results"]["instagram"]["id"] == "instagram-reel"
    assert out["results"]["facebook"]["id"] == "facebook-reel"


@patch("publishing.alert_on_exception")
def test_secondary_failure_is_best_effort(mock_alert):
    p, s = Fake("instagram"), Fake("facebook", fail=True)
    out = CrossPoster(p, [s]).publish(STORY, media_url="u")
    assert out["primary_error"] is None  # run not failed
    assert "error" in out["results"]["facebook"]
    mock_alert.assert_called_once()


@patch("publishing.alert_on_exception")
def test_primary_failure_is_surfaced_but_secondary_still_runs(mock_alert):
    p, s = Fake("instagram", fail=True), Fake("facebook")
    out = CrossPoster(p, [s]).publish(REEL, media_url="u")
    assert isinstance(out["primary_error"], Exception)
    assert out["results"]["facebook"]["id"] == "facebook-reel"  # secondary still attempted
    mock_alert.assert_called()


def test_unsupported_kind_is_skipped():
    p = Fake("instagram")
    s = Fake("facebook", kinds=(REEL,))  # no STORY support
    out = CrossPoster(p, [s]).publish(STORY, media_url="u")
    assert "facebook" not in out["results"]
    assert out["results"]["instagram"]["id"] == "instagram-story"


def test_dry_run_propagates_to_all_channels():
    p, s = Fake("instagram"), Fake("facebook")
    CrossPoster(p, [s]).publish(PHOTO, media_url="u", dry_run=True)
    assert p.calls[0][3] is True
    assert s.calls[0][3] is True


def test_build_crossposter_adds_facebook_when_page_id_set(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "a")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "p")
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINKEDIN_ORG_ID", raising=False)
    cp = build_crossposter()
    assert cp.primary.name == "instagram"
    assert [s.name for s in cp.secondaries] == ["facebook"]


def test_build_crossposter_instagram_only_without_page_id(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "a")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("LINKEDIN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("LINKEDIN_ORG_ID", raising=False)
    cp = build_crossposter()
    assert cp.secondaries == []


def test_build_crossposter_adds_linkedin_for_news_when_enabled(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "a")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.setenv("ENABLE_LINKEDIN", "true")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "li-token")
    monkeypatch.setenv("LINKEDIN_ORG_ID", "123")
    cp = build_crossposter()  # default content_source="news"
    assert [s.name for s in cp.secondaries] == ["linkedin"]


def test_build_crossposter_omits_linkedin_when_flag_off(monkeypatch):
    # Creds present but feature flag off (default) → LinkedIn stays dark.
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "a")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("ENABLE_LINKEDIN", raising=False)
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "li-token")
    monkeypatch.setenv("LINKEDIN_ORG_ID", "123")
    cp = build_crossposter()
    assert cp.secondaries == []


def test_build_crossposter_excludes_linkedin_for_events(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_ACCESS_TOKEN", "t")
    monkeypatch.setenv("INSTAGRAM_ACCOUNT_ID", "a")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.setenv("ENABLE_LINKEDIN", "true")
    monkeypatch.setenv("LINKEDIN_ACCESS_TOKEN", "li-token")
    monkeypatch.setenv("LINKEDIN_ORG_ID", "123")
    cp = build_crossposter(content_source="event")
    assert cp.secondaries == []
