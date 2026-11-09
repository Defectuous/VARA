"""Text-to-speech, run locally with Kokoro-82M (the same voices as SiteRecorder and SiteReader).

The model and voice files live in data/models/kokoro (see README, Voice). They load on first use.
"""

import io
import re
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np


class SpeechUnavailable(RuntimeError):
    """The speech model isn't installed or can't be loaded."""


class UnknownVoice(ValueError):
    pass


@dataclass
class Speech:
    samples: np.ndarray  # float32, -1..1
    sample_rate: int


class Synthesizer(Protocol):
    def voices(self) -> list[str]: ...

    def synthesize(self, text: str, voice: str, speed: float) -> Speech: ...


# The assistant's name as initials is spelled out letter by letter; said as a word it sounds right.
_NAME = re.compile(r"\bV\.\s?A\.\s?R\.\s?A\.?", re.IGNORECASE)


def speakable(text: str) -> str:
    return _NAME.sub("Vara", text).strip()


def to_wav(speech: Speech) -> bytes:
    pcm = (np.clip(speech.samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(speech.sample_rate)
        w.writeframes(pcm.tobytes())
    return buffer.getvalue()


class KokoroSynthesizer:
    def __init__(self, model_path: Path, voices_path: Path) -> None:
        self.model_path = model_path
        self.voices_path = voices_path
        self._kokoro = None
        self._lock = threading.Lock()

    def _load(self):
        with self._lock:
            if self._kokoro is None:
                missing = [p for p in (self.model_path, self.voices_path) if not p.exists()]
                if missing:
                    raise SpeechUnavailable(
                        "Speech model files are missing: " + ", ".join(str(p) for p in missing)
                    )
                from kokoro_onnx import Kokoro

                self._kokoro = Kokoro(str(self.model_path), str(self.voices_path))
            return self._kokoro

    def voices(self) -> list[str]:
        return sorted(self._load().get_voices())

    def synthesize(self, text: str, voice: str, speed: float) -> Speech:
        kokoro = self._load()
        if voice not in kokoro.get_voices():
            raise UnknownVoice(voice)
        # British voices (bf_/bm_) read best with British pronunciation.
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, rate = kokoro.create(speakable(text), voice=voice, speed=speed, lang=lang)
        return Speech(np.asarray(samples, dtype=np.float32), rate)

    def warm_up(self, voice: str) -> None:
        """Load the model and speak once, so the first real reply doesn't wait (about 3 s otherwise)."""
        self.synthesize("Hello.", voice, 1.0)
