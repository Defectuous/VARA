"""Speech-to-text for voice requests, run locally with faster-whisper.

Clients send WAV audio (16-bit PCM, any sample rate, mono or stereo). It is decoded here with
the standard library rather than through faster-whisper's PyAV decoder, so the audio format is
under our control and does not depend on PyAV's API.
"""

import contextlib
import ctypes
import glob
import io
import logging
import os
import re
import sys
import threading
import time
import wave
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Protocol

import numpy as np

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

log = logging.getLogger("vara.voice")

SAMPLE_RATE = 16_000
WAKE_WORD = "vara"
# Words people put before the wake word ("hey VARA", "ok VARA").
_LEAD_INS = {"hey", "hi", "ok", "okay", "oh", "yo", "hello"}


class AudioError(ValueError):
    """The uploaded audio could not be used."""


def decode_wav(data: bytes) -> np.ndarray:
    """16-bit PCM WAV bytes -> float32 mono samples at 16 kHz."""
    try:
        with wave.open(io.BytesIO(data)) as w:
            if w.getsampwidth() != 2:
                raise AudioError("Audio must be 16-bit PCM WAV")
            rate, channels = w.getframerate(), w.getnchannels()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise AudioError(f"Not a readable WAV file: {exc}") from exc
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    if channels > 1:
        samples = samples[: len(samples) - len(samples) % channels]
        samples = samples.reshape(-1, channels).mean(axis=1)
    if samples.size == 0:
        raise AudioError("The audio is empty")
    if rate != SAMPLE_RATE:
        count = round(samples.size * SAMPLE_RATE / rate)
        samples = np.interp(
            np.linspace(0, samples.size - 1, count), np.arange(samples.size), samples
        ).astype(np.float32)
    return samples


def _is_wake_word(word: str) -> bool:
    # Whisper writes the name many ways: VARA, Vara, V.A.R.A., Varah, Vera, Vara's...
    word = re.sub(r"'s$", "", word)
    return word == WAKE_WORD or SequenceMatcher(None, word, WAKE_WORD).ratio() >= 0.75


def split_wake_word(text: str) -> tuple[bool, str]:
    """Whether the transcript starts with the wake word, and the request that follows it.

    Clients keep a moment of audio from before the on-device wake-word detector fired, so a real
    activation's transcript starts with "VARA" (optionally after "hey"/"ok"). A transcript that
    doesn't is treated as a false wake-up.
    """
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z.']*", text))
    for match in tokens[:3]:
        word = match.group().replace(".", "").lower()
        if _is_wake_word(word):
            return True, text[match.end() :].lstrip(" ,.!?:;-").strip()
        if word not in _LEAD_INS:
            break
    return False, text.strip()


@dataclass
class Transcript:
    text: str
    language: str | None
    audio_seconds: float
    processing_seconds: float


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray) -> Transcript: ...


def _register_cuda_libraries() -> None:
    """Make the pip-installed NVIDIA libraries (cuBLAS, cuDNN) findable by CTranslate2.

    They keep their DLLs/shared objects inside site-packages/nvidia/*/, which isn't on the loader's path.
    """
    try:
        import nvidia
    except ImportError:
        return
    for root in nvidia.__path__:
        if sys.platform == "win32":
            for folder in glob.glob(os.path.join(root, "*", "bin")):
                os.add_dll_directory(folder)
                os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
        else:
            # Linux: load them up front so CTranslate2's dlopen finds them already in the process.
            for library in sorted(glob.glob(os.path.join(root, "*", "lib", "lib*.so*"))):
                with contextlib.suppress(OSError):
                    ctypes.CDLL(library, mode=ctypes.RTLD_GLOBAL)


class WhisperTranscriber:
    """Loads the model on first use (or at `warm_up`), so the server starts quickly.

    `device="auto"` uses the NVIDIA GPU when it can (about 0.16 s for a short request on the RTX 5060,
    against about 2 s on the 5800X) and falls back to the CPU otherwise. `compute_type="auto"` picks
    float16 on the GPU and int8 on the CPU.
    """

    def __init__(self, model: str, device: str, compute_type: str) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self._model = None
        self._lock = threading.Lock()

    def _create(self, device: str, compute_type: str):
        from faster_whisper import WhisperModel

        if compute_type == "auto":
            compute_type = "float16" if device == "cuda" else "int8"
        return WhisperModel(self.model_name, device=device, compute_type=compute_type)

    def _load(self):
        with self._lock:
            if self._model is None:
                if self.device in ("auto", "cuda"):
                    _register_cuda_libraries()
                if self.device != "auto":
                    self._model = self._create(self.device, self.compute_type)
                else:
                    try:
                        self._model = self._create("cuda", self.compute_type)
                        # Loading succeeds without the CUDA libraries; running is the real test.
                        self._run(self._model, np.zeros(SAMPLE_RATE // 2, dtype=np.float32))
                        self.device = "cuda"
                    except (RuntimeError, ValueError, OSError) as exc:
                        log.info("Speech-to-text on the CPU (GPU unavailable: %s)", exc)
                        self._model = self._create("cpu", self.compute_type)
                        self.device = "cpu"
                where = "GPU" if self.device == "cuda" else "CPU"
                log.info("Speech-to-text: Whisper %s on the %s", self.model_name, where)
            return self._model

    @staticmethod
    def _run(model, audio: np.ndarray, vad: bool = False) -> str:
        segments, _ = model.transcribe(
            audio, language="en", beam_size=1, initial_prompt="VARA", vad_filter=vad
        )
        return "".join(segment.text for segment in segments).strip()

    def warm_up(self) -> None:
        """Load the model and run it once. On a new GPU the first run compiles CUDA code (5-25 s on the
        RTX 5060), which should happen at startup rather than on the first spoken request."""
        model = self._load()
        noise = np.random.default_rng(0).normal(0, 0.01, SAMPLE_RATE).astype(np.float32)
        start = time.perf_counter()
        self._run(model, noise)
        log.info("Speech-to-text warmed up in %.1f s", time.perf_counter() - start)

    def transcribe(self, audio: np.ndarray) -> Transcript:
        model = self._load()
        start = time.perf_counter()
        # The prompt teaches Whisper how the assistant's name is spelled.
        segments, info = model.transcribe(
            audio, language="en", beam_size=1, initial_prompt="VARA", vad_filter=True
        )
        text = "".join(segment.text for segment in segments).strip()
        return Transcript(
            text=text,
            language=info.language,
            audio_seconds=round(audio.size / SAMPLE_RATE, 2),
            processing_seconds=round(time.perf_counter() - start, 2),
        )
