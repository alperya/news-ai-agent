"""
Tests for _stitch_clips_to_file's lead-clip duration.

Before this parameter existed the stitcher split *total_duration* evenly across
every clip, so a 4 s cover rendered by _build_background was silently trimmed to
total/len(clips) — the cover-duration constant had no effect at all.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


def _durations_for(clip_paths, total, lead=None, clip_len=30.0):
    """Run the stitcher with ffmpeg stubbed; return the -t value per clip."""
    import video.effects as E

    durations = []

    class _Clip:
        duration = clip_len
        h, w = 1920, 1080
        def close(self): pass

    def _fake_run(cmd, **kwargs):
        if "-t" in cmd:
            durations.append(float(cmd[cmd.index("-t") + 1]))
        return MagicMock(returncode=0)

    with patch.object(E, "VideoFileClip", lambda p: _Clip()), \
         patch.object(E.subprocess, "run", _fake_run), \
         patch("imageio_ffmpeg.get_ffmpeg_exe", lambda: "ffmpeg"), \
         patch("os.path.exists", return_value=True), \
         patch("os.path.getsize", return_value=1024):
        E._stitch_clips_to_file(clip_paths, total, lead_duration=lead)
    return durations


def test_stitch_lead_duration_shrinks_body_segments():
    """A 7 s lead leaves the remainder split across the other clips."""
    clips = [f"/tmp/c{i}.mp4" for i in range(10)]  # 1 lead + 9 body
    durations = _durations_for(clips, total=35.0, lead=7.0)
    assert durations[0] == 7.0
    assert all(abs(d - 28.0 / 9) < 1e-6 for d in durations[1:]), durations


def test_stitch_without_lead_duration_keeps_even_split():
    """Default None reproduces the original behaviour exactly."""
    clips = [f"/tmp/c{i}.mp4" for i in range(10)]
    durations = _durations_for(clips, total=35.0, lead=None)
    assert all(abs(d - 3.5) < 1e-6 for d in durations), durations


def test_stitch_body_never_below_minimum():
    """MIN_CLIP_DURATION still floors the body segments."""
    from video.effects import MIN_CLIP_DURATION
    clips = [f"/tmp/c{i}.mp4" for i in range(10)]
    durations = _durations_for(clips, total=12.0, lead=7.0)
    assert all(d >= MIN_CLIP_DURATION for d in durations[1:]), durations


def test_source_label_overlay_is_timed_and_positioned(tmp_path):
    """The disclosure label sits clear of the hook scrim and subtitle band."""
    from video.effects import build_source_label_overlay
    from video.config import VIDEO_HEIGHT

    specs = build_source_label_overlay(str(tmp_path), start=7.0, duration=3.5)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.start == 7.0 and spec.end == 10.5
    assert 0.05 * VIDEO_HEIGHT < spec.y < 0.30 * VIDEO_HEIGHT
    assert Path(spec.png_path).exists()
