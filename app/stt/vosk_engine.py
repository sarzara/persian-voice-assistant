"""Vosk recognizer wrapper.

The model (~50 MB on disk, ~150 MB in RAM) is loaded once per process and
shared; KaldiRecognizer objects are cheap and created per transcription.
Recognition is CPU-bound and synchronous, so callers run it in a thread.
"""

from __future__ import annotations

import json
import logging
import threading
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

FRAMES_PER_CHUNK = 4000


@dataclass(frozen=True)
class Transcript:
    text: str
    # Mean per-word confidence in [0, 1]; 0.0 when nothing was recognised.
    confidence: float
    words: list[dict] = field(default_factory=list)


class Recognizer(Protocol):
    def transcribe(self, wav_path: Path) -> Transcript: ...


class SttModelMissing(RuntimeError):
    pass


class VoskRecognizer:
    _lock = threading.Lock()
    _models: dict[str, object] = {}

    def __init__(self, model_path: Path) -> None:
        self.model_path = Path(model_path)

    def _model(self):
        key = str(self.model_path)
        with self._lock:
            if key not in self._models:
                if not (self.model_path / "am").exists() and not (self.model_path / "conf").exists():
                    raise SttModelMissing(f"Vosk model not found at {self.model_path}")
                import vosk

                vosk.SetLogLevel(-1)
                log.info("Loading Vosk model from %s", self.model_path)
                self._models[key] = vosk.Model(str(self.model_path))
            return self._models[key]

    def transcribe(self, wav_path: Path) -> Transcript:
        import vosk

        model = self._model()
        with wave.open(str(wav_path), "rb") as wf:
            if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
                raise ValueError("expected 16-bit mono WAV")
            rec = vosk.KaldiRecognizer(model, wf.getframerate())
            rec.SetWords(True)
            segments: list[dict] = []
            while True:
                data = wf.readframes(FRAMES_PER_CHUNK)
                if not data:
                    break
                if rec.AcceptWaveform(data):
                    segments.append(json.loads(rec.Result()))
            segments.append(json.loads(rec.FinalResult()))
        return combine_segments(segments)


def combine_segments(segments: list[dict]) -> Transcript:
    words = [w for seg in segments for w in seg.get("result", [])]
    text = " ".join(seg.get("text", "").strip() for seg in segments if seg.get("text", "").strip())
    confidence = sum(float(w.get("conf", 0.0)) for w in words) / len(words) if words else 0.0
    return Transcript(text=text, confidence=round(confidence, 4), words=words)
