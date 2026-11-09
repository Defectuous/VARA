"""Convert WAV clips to 16 kHz mono 16-bit in place. Skips clips that are already 16 kHz."""

import os
import sys
import wave
from math import gcd
from multiprocessing import Pool

import numpy as np
from scipy.signal import resample_poly


def _convert(path):
    with wave.open(path) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        if rate == 16000 and ch == 1 and width == 2:
            return 0
        data = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32)
    if ch > 1:
        data = data.reshape(-1, ch).mean(axis=1)
    g = gcd(rate, 16000)
    out = np.clip(resample_poly(data, 16000 // g, rate // g), -32768, 32767).astype("<i2")
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(out.tobytes())
    os.replace(tmp, path)
    return 1


def resample_dir(directory):
    files = [os.path.join(directory, f) for f in os.listdir(directory) if f.endswith(".wav")]
    with Pool() as pool:
        converted = sum(pool.imap_unordered(_convert, files, chunksize=64))
    print(f"resampled {converted} of {len(files)} clips in {directory}", flush=True)


if __name__ == "__main__":
    for d in sys.argv[1:]:
        resample_dir(d)
