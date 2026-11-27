"""In-process text embeddings for private archive retrieval, computed with numpy.

A deterministic word and bigram sketch (256 hashed features) is projected to 96
dimensions by one fixed matrix and L2-normalised. The matrix is the seeded random
projection an earlier ONNX file held as its only operation (a single MatMul). It
ships as `models/starling-embedding.npy`, so no model runtime
is loaded in any engine process. `tests/test_embeddings.py` pins the output to
vectors produced by the ONNX path.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

import numpy as np

INPUT_DIMENSION = 256
OUTPUT_DIMENSION = 96
WEIGHTS_PATH = Path(__file__).resolve().parents[1] / "models" / "starling-embedding.npy"


def _hashed_features(text: str) -> np.ndarray:
    """Create a private, deterministic word/bigram sketch for the projection."""
    tokens = re.findall(r"[\w']+", text.lower(), flags=re.UNICODE)[:600]
    features = np.zeros(INPUT_DIMENSION, dtype=np.float32)
    terms = [*tokens, *(f"{left}::{right}" for left, right in zip(tokens, tokens[1:]))]
    for term in terms:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=4).digest()
        value = int.from_bytes(digest, "big")
        features[value % INPUT_DIMENSION] += -1.0 if value & 1 else 1.0
    norm = float(np.linalg.norm(features))
    return features / norm if norm > 0 else features


@lru_cache(maxsize=2)
def _load_weights(path: str) -> np.ndarray | None:
    """The projection matrix, loaded once per path; None when missing or malformed."""
    try:
        weights = np.load(path, allow_pickle=False)
    except (OSError, ValueError):
        return None
    if weights.shape != (INPUT_DIMENSION, OUTPUT_DIMENSION):
        return None
    weights = np.ascontiguousarray(weights, dtype=np.float32)
    weights.setflags(write=False)
    return weights


class LocalEmbedder:
    """The bundled random-projection embedder: no network and no model runtime."""

    def __init__(self, weights_path: Path | None = None) -> None:
        self.path = weights_path or WEIGHTS_PATH
        self.weights = _load_weights(str(self.path))

    @property
    def available(self) -> bool:
        return self.weights is not None

    def encode(self, texts: list[str]) -> np.ndarray:
        if self.weights is None:
            raise RuntimeError("The bundled embedding weights are unavailable.")
        inputs = np.stack([_hashed_features(text) for text in texts]).astype(np.float32)
        output = inputs @ self.weights
        norms = np.linalg.norm(output, axis=1, keepdims=True)
        return output / np.maximum(norms, 1e-8)


# The name the retrieval callers have always imported.
LocalOnnxEmbedder = LocalEmbedder
