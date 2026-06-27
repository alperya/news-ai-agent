"""Multi-channel publishing — provider-agnostic dispatch.

A :class:`CrossPoster` fans one rendered artifact (a video or image already in
S3, exposed as a public URL) out to many social channels. Instagram is the
**primary** — its failure is a real error surfaced to the caller. Every other
channel (Facebook today; LinkedIn/TikTok/Twitter next) is a **best-effort
secondary**: a failure logs + alerts but never blocks the run.

Adding a channel = write a :class:`ChannelPublisher` adapter and register it in
:func:`build_crossposter`. The call sites (reels_worker, event pipeline, photo
path) never change.
"""
import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

from notifier import alert_on_exception, detect_error_type

logger = logging.getLogger(__name__)

# Content kinds a channel may publish
REEL = "reel"
PHOTO = "photo"
STORY = "story"


class ChannelPublisher(ABC):
    """Common interface every social channel adapter implements."""

    name: str = "channel"

    @abstractmethod
    def supports(self, kind: str) -> bool:
        """Whether this channel can publish the given content *kind*."""

    @abstractmethod
    def publish(self, kind: str, *, media_url: str, caption: str = "",
                dry_run: bool = False) -> dict:
        """Publish *media_url* (with *caption*) as the given *kind*."""


class CrossPoster:
    """Fan a single artifact out to a primary channel + best-effort secondaries."""

    def __init__(self, primary: ChannelPublisher, secondaries: list):
        self.primary = primary
        self.secondaries = secondaries

    def publish(self, kind: str, *, media_url: str, caption: str = "",
                dry_run: bool = False) -> dict:
        """Publish to every channel that supports *kind*.

        Returns ``{"results": {channel_name: result|{'error': ...}},
        "primary_error": Exception|None}``. The caller decides what a primary
        failure means (e.g. a non-200 response); secondaries never fail the run.
        """
        results: dict = {}
        primary_error: Optional[Exception] = None

        # Primary — strict (failure surfaced to the caller)
        try:
            results[self.primary.name] = self.primary.publish(
                kind, media_url=media_url, caption=caption, dry_run=dry_run,
            )
            logger.info(f"✅ {self.primary.name} {kind} published")
        except Exception as e:
            logger.error(f"❌ {self.primary.name} {kind} publish failed: {e}", exc_info=True)
            alert_on_exception(f"{self.primary.name} {kind} publish failed", e, detect_error_type(e))
            primary_error = e

        # Secondaries — best-effort (logged + alerted, never block)
        for pub in self.secondaries:
            if not pub.supports(kind):
                logger.info(f"⏭️  {pub.name} does not support {kind} — skipping")
                continue
            try:
                results[pub.name] = pub.publish(
                    kind, media_url=media_url, caption=caption, dry_run=dry_run,
                )
                logger.info(f"✅ {pub.name} {kind} published")
            except Exception as e:
                logger.error(f"❌ {pub.name} {kind} publish failed (non-critical): {e}", exc_info=True)
                alert_on_exception(f"{pub.name} {kind} publish failed", e, detect_error_type(e))
                results[pub.name] = {"error": str(e)}

        return {"results": results, "primary_error": primary_error}


def build_crossposter(*, content_source: str = "news") -> CrossPoster:
    """Construct the CrossPoster for the currently-configured channels.

    Instagram is always the primary. Each secondary is added only when its
    credentials are present, so enabling a channel is purely configuration.

    *content_source* lets a channel opt out of a content type without the call
    sites knowing channel names: LinkedIn takes **news** Reels only (event
    slideshows underperform there — same rationale as the YouTube exclusion), so
    the event pipeline passes ``content_source="event"``.
    """
    # Lazy import avoids a circular import (social_publisher imports this module)
    from social_publisher import InstagramPublisher, FacebookPublisher, LinkedInPublisher

    primary = InstagramPublisher()
    secondaries: list = []
    if os.environ.get("FACEBOOK_PAGE_ID"):
        secondaries.append(FacebookPublisher())
    # LinkedIn: news Reels only, gated by an explicit feature flag (default off)
    # *and* its own credentials — so the code can ship dark; nothing publishes
    # until ENABLE_LINKEDIN=true is set in Secrets Manager.
    linkedin_enabled = os.environ.get("ENABLE_LINKEDIN", "false").lower() == "true"
    if content_source != "event" and linkedin_enabled \
            and os.environ.get("LINKEDIN_ACCESS_TOKEN") \
            and os.environ.get("LINKEDIN_ORG_ID"):
        secondaries.append(LinkedInPublisher())
    # Future channels register here, each gated by its own credentials — one line each.
    return CrossPoster(primary, secondaries)
