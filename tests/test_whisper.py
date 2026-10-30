"""Whisper API speech to text (Groq / OpenAI-compatible): parsing, language, fallback to Vosk."""

import math
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from app.stt.service import SttOutcome, transcribe_voice_message
from app.stt.vosk_engine import Transcript
from app.stt.whisper_api import (
    MIN_CONFIDENCE,
    WhisperClient,
    WhisperError,
    parse_response,
)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def seg(text, logprob=-0.2, no_speech=0.01):
    return {"text": text, "avg_logprob": logprob, "no_speech_prob": no_speech}


def test_parse_english():
    t = parse_response({"language": "english", "text": " Meeting with Ali tomorrow at ten.",
                        "segments": [seg(" Meeting with Ali tomorrow at ten.", -0.1)]})
    assert t.text == "Meeting with Ali tomorrow at ten." and t.language == "en"
    assert t.confidence == pytest.approx(math.exp(-0.1))


def test_parse_persian_and_unknown_language():
    assert parse_response({"language": "persian", "segments": [seg("فردا ساعت ده")]}).language == "fa"
    assert parse_response({"language": "arabic", "segments": [seg("غدا")]}).language is None


def test_silence_segments_are_dropped():
    t = parse_response({"language": "english", "text": "Thank you.", "segments": [seg("Thank you.", no_speech=0.9)]})
    assert t.text == "" and t.confidence == 0.0


def test_threshold_matches_whisper_failure_logprob():
    assert MIN_CONFIDENCE == pytest.approx(math.exp(-1.0))


def _client(responses, seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)
    return WhisperClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def whisper_env(monkeypatch):
    monkeypatch.setenv("WHISPER_API_KEY", "test-key")
    monkeypatch.setenv("WHISPER_BASE_URL", "https://stt.example/v1")


async def test_request_shape_and_detected_language(whisper_env, tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF....WAVE")
    seen = []
    client = _client([httpx.Response(200, json={"language": "english", "segments": [seg("hello")]})], seen)
    t = await client.transcribe(wav)
    req = seen[0]
    assert str(req.url) == "https://stt.example/v1/audio/transcriptions"
    assert req.headers["authorization"] == "Bearer test-key"
    body = req.content.decode("latin-1")
    assert "verbose_json" in body and "whisper-large-v3-turbo" in body
    assert 'name="language"' not in body  # auto: Whisper detects
    assert t.text == "hello" and t.language == "en"


async def test_other_language_is_retried_as_persian(whisper_env, tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF....WAVE")
    seen = []
    client = _client([
        httpx.Response(200, json={"language": "arabic", "segments": [seg("غدا الساعة")]}),
        httpx.Response(200, json={"language": "persian", "segments": [seg("فردا ساعت ده")]}),
    ], seen)
    t = await client.transcribe(wav)
    assert len(seen) == 2 and 'name="language"' in seen[1].content.decode("latin-1")
    assert t.text == "فردا ساعت ده" and t.language == "fa"


async def test_http_error_and_missing_key(monkeypatch, whisper_env, tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")
    with pytest.raises(WhisperError, match="HTTP 429"):
        await _client([httpx.Response(429, text="rate limited")], []).transcribe(wav)
    monkeypatch.setenv("WHISPER_API_KEY", "")
    from app.config import get_settings
    get_settings.cache_clear()  # settings are cached per process
    with pytest.raises(WhisperError, match="WHISPER_API_KEY"):
        await WhisperClient().transcribe(wav)


# ---- through transcribe_voice_message (database) ----

def make_opus(path: Path) -> Path:
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-ar", "48000", "-c:a", "libopus", str(path)], check=True)
    return path


async def _voice_row(path):
    from app.db.models import User, VoiceMessage
    from app.db.session import session_scope
    async with session_scope() as s:
        user = User(telegram_user_id=4242)
        s.add(user)
        await s.flush()
        vm = VoiceMessage(user_id=user.id, telegram_file_id="f", duration_s=1, audio_path=str(path))
        s.add(vm)
        await s.flush()
        return vm.id


class FakeWhisper:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.hint = result, error, "unset"

    async def transcribe(self, wav_path, language=None):
        self.hint = language
        if self.error:
            raise self.error
        return self.result


class FakeVosk:
    def __init__(self, transcript):
        self.transcript, self.called = transcript, False

    def transcribe(self, wav_path):
        self.called = True
        return self.transcript


@needs_ffmpeg
async def test_whisper_english_without_any_setting(db, tmp_path):
    ogg = make_opus(tmp_path / "v.ogg")
    vm_id = await _voice_row(ogg)
    whisper = FakeWhisper(Transcript("Book a meeting with Sara on Friday", 0.8, language="en"))
    vosk = FakeVosk(Transcript("x", 1.0))
    result = await transcribe_voice_message(vm_id, ogg, language="auto", whisper=whisper, recognizer=vosk)
    assert result.usable and result.language == "en" and whisper.hint is None and not vosk.called


@needs_ffmpeg
async def test_whisper_low_confidence_uses_whisper_threshold(db, tmp_path):
    ogg = make_opus(tmp_path / "v.ogg")
    vm_id = await _voice_row(ogg)
    whisper = FakeWhisper(Transcript("فردا", 0.30, language="fa"))
    result = await transcribe_voice_message(vm_id, ogg, whisper=whisper, recognizer=FakeVosk(Transcript("", 0)))
    assert result.outcome is SttOutcome.LOW_CONFIDENCE


@needs_ffmpeg
async def test_whisper_failure_falls_back_to_vosk(db, tmp_path):
    ogg = make_opus(tmp_path / "v.ogg")
    vm_id = await _voice_row(ogg)
    vosk = FakeVosk(Transcript("فردا ساعت ده جلسه", 0.9))
    whisper = FakeWhisper(error=WhisperError("HTTP 429"))
    result = await transcribe_voice_message(vm_id, ogg, language="fa", whisper=whisper, recognizer=vosk)
    assert vosk.called and whisper.hint == "fa"
    assert result.usable and result.language == "fa" and result.text == "فردا ساعت ده جلسه"
