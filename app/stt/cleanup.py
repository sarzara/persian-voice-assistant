"""Periodic deletion of leftover audio files.

Normally audio is deleted right after transcription; this catches files left by a
crash or restart mid-transcription, so no audio survives longer than the retention.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy import update

from app.config import get_settings
from app.db.models import VoiceMessage
from app.db.session import session_scope

log = logging.getLogger(__name__)
AUDIO_SUFFIXES = {".ogg", ".oga", ".opus", ".wav"}


async def cleanup_audio(root: Path, max_age_s: float, *, now: float | None = None) -> list[Path]:
    now = now or time.time()
    removed: list[Path] = []
    if not root.exists():
        return removed
    for path in root.rglob("*"):
        if path.suffix.lower() in AUDIO_SUFFIXES and path.is_file():
            try:
                if now - path.stat().st_mtime > max_age_s:
                    path.unlink()
                    removed.append(path)
            except FileNotFoundError:
                continue  # deleted concurrently by the STT service
    if removed:
        async with session_scope() as s:
            await s.execute(update(VoiceMessage).where(VoiceMessage.audio_path.in_([str(p) for p in removed]))
                            .values(audio_path=None))
        log.warning("Deleted %d leftover audio file(s)", len(removed), extra={"files": len(removed)})
    return removed


async def cleanup_loop() -> None:
    settings = get_settings()
    while True:
        try:
            await cleanup_audio(Path(settings.voice_storage_dir), settings.audio_retention_min * 60)
        except Exception:  # noqa: BLE001 - the job must keep running
            log.exception("Audio cleanup failed")
        await asyncio.sleep(settings.audio_cleanup_interval_min * 60)
