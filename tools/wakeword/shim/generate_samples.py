"""Adapter between openWakeWord's trainer and the current piper-sample-generator.

openWakeWord's train.py imports `generate_samples` from a folder and doesn't pass the voice model; the
packaged generator needs it. This adapter also:
  - widens the speaking speeds (openWakeWord always uses 0.75/1.0/1.25),
  - drops negative phrases that contain the wake word (openWakeWord builds negatives by splitting
    multi-word targets, so "hey vara" would yield "vara" as something to ignore),
  - with VARA_PHONEME_VARIANTS set, tops up positives with second-syllable-stress variants (vuh-RAH),
    given as sounds because no spelling makes espeak produce them,
  - converts the generator's 22,050 Hz output to the 16 kHz openWakeWord needs,
  - supplies pronunciations for the target words, which aren't in CMUdict (the DeepPhonemizer model
    openWakeWord would download for them is no longer available).

Copied to ~/vara-wakeword/psg_shim by tools/wakeword/prepare.sh.
"""

import os
import re as _re
import sys

ROOT = os.path.expanduser("~/vara-wakeword")
sys.path.insert(0, os.path.join(ROOT, "piper-sample-generator"))
from piper_sample_generator.__main__ import generate_samples as _generate

MODEL = os.path.join(ROOT, "data", "en_US-libritts_r-medium.pt")

PHONEME_VARIANTS = [
    "vəɹˈɑː",  # vuh-RAH
    "vʌɹˈɑː",
    "hˈeɪ vəɹˈɑː",
    "oʊkˈeɪ vəɹˈɑː",
    "vɑːɹˈɑː",  # VAH-RAH
]

_WAKE_SPELLINGS = {
    "vara",
    "vahra",
    "varah",
    "vera",
    "varra",
    "vora",
    "vaira",
    "vairah",
    "vayra",
    "vehra",
}


def _sounds_like_wake_word(word):
    import pronouncing

    word = word.lower()
    if word in _WAKE_SPELLINGS:
        return True
    return any(
        _re.fullmatch(r"V (AA|AE|EH|EY|ER|AH|AO|IH)\d( R)? (AH|ER|AA)\d", p)
        for p in pronouncing.phones_for_word(word)
    )


def _drop_wake_words(texts):
    kept = [t for t in texts if not any(_sounds_like_wake_word(w) for w in t.split())]
    print(
        f"negative phrases: kept {len(kept)} of {len(texts)} (dropped ones containing the wake word)",
        flush=True,
    )
    return kept


def generate_samples(*args, model=MODEL, **kwargs):
    kwargs["length_scales"] = [0.6, 0.75, 0.9, 1.0, 1.15, 1.3]
    output_dir = str(kwargs.get("output_dir", ""))
    if os.environ.get("VARA_PHONEME_VARIANTS") and "positive" in output_dir:
        kwargs["text"] = PHONEME_VARIANTS
        kwargs["phoneme_input"] = True
    elif "negative" in output_dir and isinstance(kwargs.get("text"), list):
        kwargs["text"] = _drop_wake_words(kwargs["text"])
    result = _generate(*args, model=model, **kwargs)
    from resample16k import resample_dir

    resample_dir(kwargs["output_dir"])
    return result


import pronouncing

pronouncing.init_cmu()
for _word, _phones in {
    "vara": ["V AA1 R AH0", "V AE1 R AH0", "V EH1 R AH0"],
    "vahra": ["V AA1 R AH0"],
    "varah": ["V AA1 R AH0", "V AE1 R AH0"],
    "vaira": ["V EH1 R AH0"],
    "vayra": ["V EY1 R AH0"],
}.items():
    pronouncing.lookup.setdefault(_word, _phones)
