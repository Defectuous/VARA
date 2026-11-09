"""Reference outputs from openWakeWord's own Python code, for the C# listener's parity tests.

Run inside the wake-word training environment (WSL), from the repository root:

    ~/vara-wakeword/.venv/bin/python tools/wakeword/make_parity_fixtures.py SPEECH.wav [VARA_CLIP.wav ...]

For each input it writes, to clients/windows/Vara.Listener.Tests/Fixtures/:
  <name>.wav            16 kHz mono 16-bit audio, padded with 1 s of silence at each end
  <name>.features.json  the 96-value feature openWakeWord produces for every 80 ms chunk
  <name>.scores.json    openWakeWord's "vara" score for every chunk (only if models/vara.onnx exists)

A third fixture, noise.*, is seeded random noise, so it is reproducible.
"""

import hashlib
import json
import sys
import wave
from math import gcd
from pathlib import Path

import numpy as np
from openwakeword.model import Model
from openwakeword.utils import AudioFeatures
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "clients" / "windows" / "Vara.Listener.Tests" / "Fixtures"
MODELS = ROOT / "clients" / "windows" / "Vara.Listener" / "models"
VARA_MODEL = ROOT / "models" / "vara.onnx"
CHUNK = 1280


def load_16k(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        rate, channels = w.getframerate(), w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    if rate != 16000:
        g = gcd(rate, 16000)
        data = resample_poly(data, 16000 // g, rate // g)
    return np.clip(data, -32768, 32767).astype(np.int16)


def write_wav(path: Path, audio: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(audio.astype("<i2").tobytes())


def fixture(name: str, audio: np.ndarray) -> None:
    silence = np.zeros(16000, dtype=np.int16)
    audio = np.concatenate([silence, audio, silence])
    audio = audio[: len(audio) - len(audio) % CHUNK]  # whole 80 ms chunks only
    write_wav(OUT / f"{name}.wav", audio)
    chunks = [audio[i : i + CHUNK] for i in range(0, len(audio), CHUNK)]

    features = AudioFeatures(
        melspec_model_path=str(MODELS / "melspectrogram.onnx"),
        embedding_model_path=str(MODELS / "embedding_model.onnx"),
        inference_framework="onnx",
    )
    per_chunk = []
    for chunk in chunks:
        features(chunk)
        per_chunk.append([round(float(v), 6) for v in features.feature_buffer[-1]])
    (OUT / f"{name}.features.json").write_text(json.dumps(per_chunk))
    summary = f"{name}: {len(chunks)} chunks"

    if VARA_MODEL.exists():
        model = Model(
            wakeword_models=[str(VARA_MODEL)],
            melspec_model_path=str(MODELS / "melspectrogram.onnx"),
            embedding_model_path=str(MODELS / "embedding_model.onnx"),
            inference_framework="onnx",
        )
        scores = [round(float(model.predict(chunk)["vara"]), 6) for chunk in chunks]
        # Record which model made these scores, so the tests can tell stale fixtures from real mismatches.
        model_sha256 = hashlib.sha256(VARA_MODEL.read_bytes()).hexdigest()
        (OUT / f"{name}.scores.json").write_text(
            json.dumps({"model_sha256": model_sha256, "scores": scores})
        )
        summary += f", peak score {max(scores):.3f}"
    print(summary)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for path in map(Path, sys.argv[1:]):
        fixture(path.stem, load_16k(path))
    rng = np.random.default_rng(1234)
    fixture("noise", (rng.normal(0, 2000, 16000 * 3)).astype(np.int16))


if __name__ == "__main__":
    main()
