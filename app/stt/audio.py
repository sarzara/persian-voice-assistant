"""ffmpeg conversion: Telegram OGG/Opus -> 16 kHz mono 16-bit PCM WAV (what Vosk expects)."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
FFMPEG_TIMEOUT_S = 60


class AudioConversionError(Exception):
    pass


async def ogg_to_wav(src: Path, dst: Path, *, timeout_s: float = FFMPEG_TIMEOUT_S) -> Path:
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-acodec", "pcm_s16le", "-f", "wav",
        str(dst),
    ]
    # Argument list (no shell), so file names can never be interpreted as commands.
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise AudioConversionError(f"ffmpeg timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise AudioConversionError(f"ffmpeg exited {proc.returncode}: {stderr.decode(errors='replace')[:300]}")
    return dst
