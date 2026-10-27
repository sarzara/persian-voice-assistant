import shutil
import subprocess
import wave
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import User, VoiceMessage
from app.db.session import session_scope
from app.stt.audio import AudioConversionError, ogg_to_wav
from app.stt.service import SttOutcome, classify, transcribe_voice_message
from app.stt.vosk_engine import Transcript, combine_segments

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_opus(path: Path, seconds: float = 1.0) -> Path:
    """A real OGG/Opus file, like Telegram sends (48 kHz)."""
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-ar", "48000", "-c:a", "libopus", str(path)],
        check=True,
    )
    return path


class FakeRecognizer:
    def __init__(self, transcript: Transcript):
        self.transcript = transcript
        self.seen_format = None

    def transcribe(self, wav_path: Path) -> Transcript:
        with wave.open(str(wav_path)) as wf:
            self.seen_format = (wf.getnchannels(), wf.getframerate(), wf.getsampwidth())
        return self.transcript


@needs_ffmpeg
async def test_ffmpeg_produces_16k_mono_pcm(tmp_path):
    wav = await ogg_to_wav(make_opus(tmp_path / "v.ogg"), tmp_path / "v.wav")
    with wave.open(str(wav)) as wf:
        assert (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) == (1, 16000, 2)


@needs_ffmpeg
async def test_ffmpeg_rejects_garbage(tmp_path):
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not audio")
    with pytest.raises(AudioConversionError):
        await ogg_to_wav(bad, tmp_path / "bad.wav")


def test_combine_segments_mean_confidence():
    t = combine_segments([
        {"text": "فردا ساعت ده", "result": [{"word": "فردا", "conf": 1.0}, {"word": "ساعت", "conf": 0.8}, {"word": "ده", "conf": 0.6}]},
        {"text": ""},
    ])
    assert t.text == "فردا ساعت ده" and t.confidence == pytest.approx(0.8)


def test_combine_segments_empty():
    assert combine_segments([{"text": ""}]) == Transcript(text="", confidence=0.0, words=[])


@pytest.mark.parametrize(
    ("text", "conf", "expected"),
    [("", 0.0, SttOutcome.EMPTY), ("   ", 0.9, SttOutcome.EMPTY), ("سلام", 0.3, SttOutcome.LOW_CONFIDENCE), ("سلام", 0.9, SttOutcome.OK)],
)
def test_classify(text, conf, expected):
    assert classify(text, conf, 0.6) is expected


async def _voice_row(path: Path) -> int:
    async with session_scope() as s:
        user = User(telegram_user_id=111111111)
        s.add(user)
        await s.flush()
        vm = VoiceMessage(user_id=user.id, telegram_file_id="F", duration_s=1, audio_path=str(path))
        s.add(vm)
        await s.flush()
        return vm.id


@needs_ffmpeg
async def test_transcript_stored_and_audio_deleted(db, tmp_path):
    ogg = make_opus(tmp_path / "v.ogg")
    vm_id = await _voice_row(ogg)
    rec = FakeRecognizer(Transcript("فردا ساعت ده با علی جلسه", 0.91))

    result = await transcribe_voice_message(vm_id, ogg, recognizer=rec)

    assert result.usable and rec.seen_format == (1, 16000, 2)
    assert not ogg.exists() and not ogg.with_suffix(".wav").exists()
    async with session_scope() as s:
        row = (await s.execute(select(VoiceMessage).where(VoiceMessage.id == vm_id))).scalar_one()
    assert row.transcript == "فردا ساعت ده با علی جلسه" and row.stt_confidence == pytest.approx(0.91)
    assert row.audio_path is None


async def test_audio_deleted_even_when_conversion_fails(db, tmp_path):
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not audio")
    vm_id = await _voice_row(bad)
    result = await transcribe_voice_message(vm_id, bad, recognizer=FakeRecognizer(Transcript("x", 1.0)))
    assert result.outcome is SttOutcome.FAILED
    assert not bad.exists()
