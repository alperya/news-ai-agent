"""
Video creation package for Instagram Reels.

Usage::

    from video import create_news_video

    create_news_video(
        title="...",
        content="...",
        source="...",
        hashtags=[...],
        output_path="output/reel.mp4",
        image_url="https://...",
    )
"""

from .creator import create_news_video
from .tts import SubtitleSegment, clean_for_narration, group_subtitle_segments

__all__ = [
    "create_news_video",
    "SubtitleSegment",
    "clean_for_narration",
    "group_subtitle_segments",
]
