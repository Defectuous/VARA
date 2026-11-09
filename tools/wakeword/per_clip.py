"""Per-clip peak scores for the positive evaluation clips (to see which phrasings or voices fail).

~/vara-wakeword/.venv/bin/python tools/wakeword/per_clip.py EVAL_DIR MODEL.onnx
"""

import sys
from pathlib import Path

from evaluate import FEATURES, load, scores
from openwakeword.model import Model

eval_dir, model_path = Path(sys.argv[1]), sys.argv[2]
model = Model(
    wakeword_models=[model_path],
    melspec_model_path=str(FEATURES / "melspectrogram.onnx"),
    embedding_model_path=str(FEATURES / "embedding_model.onnx"),
    inference_framework="onnx",
)
phrases = dict(
    line.split("\t") for line in (eval_dir / "positive" / "index.tsv").read_text().splitlines()
)
for name, phrase in sorted(phrases.items()):
    peak = scores(model, load(eval_dir / "positive" / name)).max()
    print(f"{peak:5.2f}  {name:22} {phrase}")
