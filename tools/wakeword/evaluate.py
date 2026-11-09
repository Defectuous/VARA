"""Evaluate wake-word models on audio they were not trained on.

Run in the wake-word training environment (WSL):

    ~/vara-wakeword/.venv/bin/python tools/wakeword/evaluate.py EVAL_DIR MODEL.onnx [MODEL2.onnx ...]

EVAL_DIR holds positive/ and negative/ clips from make_eval_clips.ps1 (Windows voices: a different speech
engine from the training voices). Also uses, from ~/vara-wakeword:
  - 300 held-out synthetic positives (output/vara/positive_test)
  - ~2.8 h of real-world background audio (data/audioset_16k) to count false wake-ups per hour.
Positives are scored clean and with background noise mixed in (5-15 dB SNR).
"""

import sys
import wave
from pathlib import Path

import numpy as np
from openwakeword.model import Model

HOME = Path.home() / "vara-wakeword"
FEATURES = Path(__file__).resolve().parents[2] / "clients" / "windows" / "Vara.Listener" / "models"
THRESHOLDS = [0.05, 0.1, 0.2, 0.3, 0.5, 0.7]
CHUNK = 1280
rng = np.random.default_rng(7)


def load(path: Path) -> np.ndarray:
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1, path
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").copy()


def scores(model: Model, audio: np.ndarray) -> np.ndarray:
    """Per-chunk scores, streaming like the listener. 2 s of silence first flushes the start-up buffers."""
    model.reset()
    audio = np.concatenate([np.zeros(32000, np.int16), audio, np.zeros(16000, np.int16)])
    # openWakeWord names each score after the model file, so take the (only) one.
    out = [
        next(iter(model.predict(audio[i : i + CHUNK]).values()))
        for i in range(0, len(audio) - CHUNK + 1, CHUNK)
    ]
    return np.array(out[25:])  # drop the flush period


def with_noise(audio: np.ndarray, noise_pool: list[np.ndarray]) -> np.ndarray:
    noise = noise_pool[rng.integers(len(noise_pool))]
    if len(noise) < len(audio):
        noise = np.tile(noise, len(audio) // len(noise) + 1)
    start = rng.integers(0, len(noise) - len(audio) + 1)
    noise = noise[start : start + len(audio)].astype(np.float32)
    signal = audio.astype(np.float32)
    snr_db = rng.uniform(5, 15)
    p_signal = np.mean(signal**2) + 1e-9
    p_noise = np.mean(noise**2) + 1e-9
    noise *= np.sqrt(p_signal / (p_noise * 10 ** (snr_db / 10)))
    return np.clip(signal + noise, -32768, 32767).astype(np.int16)


def rate(peaks: list[float], t: float) -> str:
    return f"{100 * np.mean(np.array(peaks) >= t):5.1f}%"


def episodes(chunk_scores: np.ndarray, t: float) -> int:
    """Separate wake-ups: rises above the threshold at least 1 s apart."""
    count, last = 0, -100
    for i, s in enumerate(chunk_scores):
        if s >= t and i - last > 12:
            count += 1
            last = i
    return count


def evaluate(
    model_path: str, eval_dir: Path, background: list[Path], noise_pool: list[np.ndarray]
) -> None:
    model = Model(
        wakeword_models=[model_path],
        melspec_model_path=str(FEATURES / "melspectrogram.onnx"),
        embedding_model_path=str(FEATURES / "embedding_model.onnx"),
        inference_framework="onnx",
    )
    pos = [load(p) for p in sorted((eval_dir / "positive").glob("*.wav"))]
    neg_files = sorted((eval_dir / "negative").glob("*.wav"))
    neg = [load(p) for p in neg_files]
    synth_files = sorted((HOME / "output" / "vara" / "positive_test").glob("*.wav"))
    synth = [
        load(p) for p in rng.choice(synth_files, size=min(300, len(synth_files)), replace=False)
    ]

    pos_clean = [scores(model, a).max() for a in pos]
    pos_noisy = [scores(model, with_noise(a, noise_pool)).max() for a in pos]
    neg_peaks = [scores(model, a).max() for a in neg]
    synth_peaks = [scores(model, a).max() for a in synth]

    bg_hours, bg_counts = 0.0, {t: 0 for t in THRESHOLDS}
    for path in background:
        audio = load(path)
        bg_hours += len(audio) / 16000 / 3600
        s = scores(model, audio)
        for t in THRESHOLDS:
            bg_counts[t] += episodes(s, t)

    print(f"\n=== {Path(model_path).name} ===")
    print(
        f"{'threshold':>10} | {'Windows voices':>14} | {'+ noise':>8} | {'synthetic':>9} | "
        f"{'look-alikes':>11} | false wake-ups/hour ({bg_hours:.1f} h)"
    )
    for t in THRESHOLDS:
        print(
            f"{t:>10} | {rate(pos_clean, t):>14} | {rate(pos_noisy, t):>8} | {rate(synth_peaks, t):>9} | "
            f"{rate(neg_peaks, t):>11} | {bg_counts[t] / bg_hours:.2f}"
        )
    worst = sorted(zip(neg_peaks, neg_files), reverse=True)[:3]
    print("highest look-alike scores:", ", ".join(f"{p.name}={s:.2f}" for s, p in worst))
    missed = sorted(zip(pos_clean, sorted((eval_dir / "positive").glob("*.wav"))))[:3]
    print("lowest Windows-voice scores:", ", ".join(f"{p.name}={s:.2f}" for s, p in missed))


def main() -> None:
    eval_dir = Path(sys.argv[1])
    background = sorted((HOME / "data" / "audioset_16k").glob("*.wav"))
    noise_pool = [load(p) for p in rng.choice(background, size=50, replace=False)]
    for model_path in sys.argv[2:]:
        evaluate(model_path, eval_dir, background, noise_pool)


if __name__ == "__main__":
    main()
