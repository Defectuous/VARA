"""Prepare wake-word training data in ~/vara-wakeword/data (safe to re-run; skips finished steps).

- openWakeWord's shared feature models
- MIT room impulse responses (room echo), as 16 kHz WAV
- AudioSet background clips from the downloaded parquet shards, as 16 kHz WAV
"""

import glob
import io
import os
from math import gcd

import numpy as np
import soundfile as sf
from scipy.io import wavfile
from scipy.signal import resample_poly

D = os.path.expanduser("~/vara-wakeword/data")


def to16k(audio, rate):
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != 16000:
        g = gcd(rate, 16000)
        audio = resample_poly(audio, 16000 // g, rate // g)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def main():
    import openwakeword.utils

    openwakeword.utils.download_models(model_names=["__none__"])

    out = os.path.join(D, "mit_rirs")
    os.makedirs(out, exist_ok=True)
    if len(os.listdir(out)) < 200:
        import datasets

        for row in datasets.load_dataset(
            "davidscripka/MIT_environmental_impulse_responses", split="train", streaming=True
        ):
            a = row["audio"]
            name = a["path"].split("/")[-1]
            wavfile.write(
                os.path.join(out, name), 16000, to16k(np.asarray(a["array"]), a["sampling_rate"])
            )
    print("room impulse responses:", len(os.listdir(out)))

    out = os.path.join(D, "audioset_16k")
    os.makedirs(out, exist_ok=True)
    if len(os.listdir(out)) < 1000:
        import pyarrow.parquet as pq

        for shard in sorted(glob.glob(os.path.join(D, "audioset", "*.parquet"))):
            table = pq.read_table(shard)
            ids = table.column("video_id").to_pylist()
            skipped = 0
            for vid, item in zip(ids, table.column("audio").to_pylist(), strict=False):
                try:
                    audio, rate = sf.read(io.BytesIO(item["bytes"]), dtype="float32")
                    wavfile.write(os.path.join(out, f"{vid}.wav"), 16000, to16k(audio, rate))
                except (RuntimeError, ValueError, TypeError):  # the odd undecodable clip
                    skipped += 1
            print(f"{shard}: skipped {skipped} undecodable clips")
    print("background clips:", len(os.listdir(out)))


if __name__ == "__main__":
    main()
