"""
Video package — single-pass ffmpeg final assembly.

Burns all (static) overlays onto the background and muxes the mixed audio in
ONE ffmpeg `filter_complex` pass. This replaces moviepy's `write_videofile`
for the news Reel, which composited every frame in pure Python (single-thread)
and dominated runtime (~9 min → Lambda 15-min timeout risk). ffmpeg does one
decode + C-speed compositing + one encode (~1-2 min) with pixel-equivalent
output (same PIL-rendered overlays, same x264 settings).
"""

import logging
import os
import subprocess
import time
from typing import List

from .config import FPS, VIDEO_HEIGHT, VIDEO_WIDTH
from .effects import OverlaySpec

logger = logging.getLogger(__name__)


def assemble_reel(
    bg_path: str,
    audio_path: str,
    overlays: List[OverlaySpec],
    total_duration: float,
    output_path: str,
) -> str:
    """Compose final Reel = background + timed overlay PNGs + audio, via ffmpeg.

    Args:
        bg_path:        Background video file (looped/cut to total_duration).
        audio_path:     Mixed narration+music audio file.
        overlays:       Static OverlaySpec list (gradient, subtitles, hook).
                        Each is a full-width PNG (x=0) shown over [start, end].
        total_duration: Final video length in seconds.
        output_path:    Destination .mp4.

    Returns:
        Absolute path to the rendered video.
    """
    from imageio_ffmpeg import get_ffmpeg_exe
    ffmpeg_bin = get_ffmpeg_exe()

    # Normalize the background, then chain one overlay per PNG with a time gate.
    chain = [
        f"[0:v]scale={VIDEO_WIDTH}:{VIDEO_HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_WIDTH}:{VIDEO_HEIGHT},fps={FPS},setsar=1[v0]"
    ]
    prev = "v0"
    png_input_idx = 2  # inputs 0=bg, 1=audio, PNGs start at 2
    for i, ov in enumerate(overlays):
        out = f"v{i + 1}"
        chain.append(
            f"[{prev}][{png_input_idx}]"
            f"overlay=0:{ov.y}:enable='between(t,{ov.start:.3f},{ov.end:.3f})'[{out}]"
        )
        prev = out
        png_input_idx += 1
    filter_complex = ";".join(chain)

    cmd = [
        ffmpeg_bin, "-y",
        "-stream_loop", "-1", "-i", bg_path,  # loop bg if shorter than audio
        "-i", audio_path,
    ]
    for ov in overlays:
        cmd += ["-i", ov.png_path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{prev}]", "-map", "1:a",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264", "-preset", "ultrafast",
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-b:v", "4000k",
        "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k",
        output_path,
    ]

    logger.info(f"🎬 ffmpeg final assembly: {len(overlays)} overlays → {output_path}")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, timeout=600)
    if result.returncode != 0 or not os.path.exists(output_path):
        stderr = result.stderr.decode(errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg final assembly failed: {stderr}")
    logger.info(f"✅ ffmpeg assembly done in {time.time() - t0:.1f}s")
    return os.path.abspath(output_path)
