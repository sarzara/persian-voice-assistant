"""Speech to text with Whisper through an OpenAI-compatible API (default: Groq).

Whisper detects the spoken language itself, so Persian and English voice messages
both work without a language setting. The audio is sent to the provider: this is
opt-in (STT_PROVIDER=whisper); the default stays the offline Vosk models.
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import httpx

from app.config import get_settings
from app.stt.vosk_engine import Transcript

log = logging.getLogger(__name__)

# Whisper itself treats a segment with mean log-probability below -1.0 as a failed decode,
# so exp(-1.0) ~= 0.37 is the "please repeat" threshold for Whisper transcripts.
MIN_CONFIDENCE = math.exp(-1.0)
# Segments Whisper thinks are silence; dropping them avoids hallucinated text such as "Thank you."
NO_SPEECH_PROB = 0.6

_LANGUAGES = {"english": "en", "en": "en", "persian": "fa", "farsi": "fa", "fa": "fa"}


class WhisperError(RuntimeError):
    """The API could not be used (no key, network, HTTP error, bad response)."""


def parse_response(data: dict) -> Transcript:
    """verbose_json -> Transcript with text, confidence in [0, 1] and language ('fa', 'en' or None)."""
    segments = [s for s in data.get("segments") or [] if s.get("no_speech_prob", 0.0) < NO_SPEECH_PROB]
    if data.get("segments") is not None:
        text = " ".join(s.get("text", "").strip() for s in segments).strip()
    else:
        text = (data.get("text") or "").strip()
    if segments:
        mean_logprob = sum(s.get("avg_logprob", -5.0) for s in segments) / len(segments)
        confidence = min(1.0, math.exp(mean_logprob))
    else:
        confidence = 0.0 if not text else 1.0
    language = _LANGUAGES.get(str(data.get("language", "")).strip().lower())
    return Transcript(text=text if confidence else "", confidence=confidence, language=language)


class WhisperClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport  # tests inject httpx.MockTransport

    async def _request(self, wav_path: Path, language: str | None) -> dict:
        s = get_settings()
        key = s.whisper_api_key.get_secret_value()
        if not key:
            raise WhisperError("WHISPER_API_KEY is not set")
        form = {"model": s.whisper_model, "response_format": "verbose_json", "temperature": "0"}
        if language:
            form["language"] = language
        try:
            async with httpx.AsyncClient(
                base_url=s.whisper_base_url, timeout=s.llm_timeout_s, transport=self._transport
            ) as client:
                with wav_path.open("rb") as audio:
                    resp = await client.post(
                        "/audio/transcriptions",
                        headers={"Authorization": f"Bearer {key}"},
                        data=form,
                        files={"file": (wav_path.name, audio, "audio/wav")},
                    )
        except httpx.HTTPError as exc:
            raise WhisperError(f"{exc.__class__.__name__}: {exc}") from exc
        if resp.status_code != 200:
            raise WhisperError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise WhisperError("response is not JSON") from exc

    async def transcribe(self, wav_path: Path, language: str | None = None) -> Transcript:
        """`language` ('fa'/'en') is a hint; None lets Whisper detect it.

        If Whisper detects some other language (Persian is sometimes heard as Arabic or Urdu),
        the audio is transcribed again as Persian, the bot's main language.
        """
        transcript = parse_response(await self._request(wav_path, language))
        if transcript.language is None and transcript.text:
            log.info("Whisper detected an unsupported language; retrying as Persian")
            transcript = parse_response(await self._request(wav_path, "fa"))
        if transcript.language is None:
            transcript = Transcript(transcript.text, transcript.confidence, transcript.words, language or "fa")
        return transcript
