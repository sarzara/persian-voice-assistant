"""Voice message -> transcript, stored on the voice_messages row. Audio is always deleted."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sqlalchemy import update

from app.config import get_settings
from app.db.models import VoiceMessage
from app.db.repo import audit
from app.db.session import session_scope
from app.stt.audio import AudioConversionError, ogg_to_wav
from app.stt.vosk_engine import Recognizer, SttModelMissing, VoskRecognizer

log = logging.getLogger(__name__)


class SttOutcome(str, Enum):
    OK = "ok"
    EMPTY = "empty"            # nothing recognised
    LOW_CONFIDENCE = "low_confidence"
    FAILED = "failed"          # conversion / model error


@dataclass(frozen=True)
class SttResult:
    outcome: SttOutcome
    text: str = ""
    confidence: float = 0.0
    language: str = "fa"  # which speech model was used: "fa" or "en"

    @property
    def usable(self) -> bool:
        return self.outcome is SttOutcome.OK


_recognizers: dict[str, Recognizer] = {}


def get_recognizer(language: str = "fa") -> Recognizer:
    """One shared recognizer per language; each model is loaded once per process."""
    if language not in _recognizers:
        settings = get_settings()
        path = settings.vosk_model_path_en if language == "en" else settings.vosk_model_path
        _recognizers[language] = VoskRecognizer(path)
    return _recognizers[language]


def classify(text: str, confidence: float, min_confidence: float) -> SttOutcome:
    if not text.strip():
        return SttOutcome.EMPTY
    if confidence < min_confidence:
        return SttOutcome.LOW_CONFIDENCE
    return SttOutcome.OK


async def transcribe_voice_message(
    voice_message_id: int, audio_path: Path, *, language: str = "fa", recognizer: Recognizer | None = None
) -> SttResult:
    """Transcribe with the model for `language` ("fa" or "en"). `recognizer` is for tests."""
    settings = get_settings()
    language = "en" if language == "en" else "fa"
    recognizer = recognizer or get_recognizer(language)
    wav_path = audio_path.with_suffix(".wav")
    try:
        await ogg_to_wav(audio_path, wav_path)
        transcript = await asyncio.to_thread(recognizer.transcribe, wav_path)
        outcome = classify(transcript.text, transcript.confidence, settings.stt_min_confidence)
        result = SttResult(outcome, transcript.text.strip(), transcript.confidence)
    except (AudioConversionError, SttModelMissing, ValueError, OSError) as exc:
        log.error("STT failed for voice_message %s: %s", voice_message_id, exc)
        result = SttResult(SttOutcome.FAILED)
    finally:
        # Spec: audio is deleted after transcription, success or not.
        audio_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)

    async with session_scope() as session:
        await session.execute(
            update(VoiceMessage)
            .where(VoiceMessage.id == voice_message_id)
            .values(
                transcript=result.text or None,
                stt_confidence=result.confidence if result.outcome is not SttOutcome.FAILED else None,
                audio_path=None,
            )
        )
        await audit(
            session, "stt.completed", "system",
            voice_message_id=voice_message_id, outcome=result.outcome.value, confidence=result.confidence,
        )
    log.info("STT voice_message=%s outcome=%s conf=%.2f", voice_message_id, result.outcome.value, result.confidence)
    return result
