"""The bundled embedder: one fixed random projection, computed with numpy.

`models/starling-embedding.onnx` is a single MatMul with a seeded 256x96 matrix. The
engine runs that same matrix from `models/starling-embedding.npy`, so no model
runtime is loaded in any engine process. `fixtures_embedding_pins.json` holds vectors
the ONNX path produced before the switch; the numpy path must reproduce them.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from app.embeddings import (INPUT_DIMENSION, OUTPUT_DIMENSION, WEIGHTS_PATH, LocalEmbedder,
                            LocalOnnxEmbedder)

ENGINE_DIR = Path(__file__).resolve().parents[1]
PINS = json.loads((Path(__file__).resolve().parent / "fixtures_embedding_pins.json")
                  .read_text(encoding="utf-8"))


def test_bundled_embedder_is_local_and_deterministic():
    embedder = LocalEmbedder()
    assert embedder.available
    texts = ["AI agents need good evals", "AI agents need careful evals", "gardening in spring"]
    vectors = embedder.encode(texts)
    assert vectors.shape == (3, 96)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)
    assert float(vectors[0] @ vectors[1]) > float(vectors[0] @ vectors[2])
    assert np.array_equal(vectors, embedder.encode(texts))


def test_the_numpy_projection_reproduces_the_onnx_vectors():
    pinned = np.asarray(PINS["vectors"], dtype=np.float32)
    vectors = LocalEmbedder().encode(PINS["texts"])
    assert vectors.shape == pinned.shape == (len(PINS["texts"]), OUTPUT_DIMENSION)
    assert np.allclose(vectors, pinned, rtol=0.0, atol=1e-6), float(np.max(np.abs(vectors - pinned)))


def test_the_shipped_matrix_is_the_pinned_projection():
    weights = np.load(WEIGHTS_PATH, allow_pickle=False)
    assert weights.dtype == np.float32
    assert weights.shape == (INPUT_DIMENSION, OUTPUT_DIMENSION)
    assert hashlib.sha256(weights.astype("<f4").tobytes()).hexdigest() == PINS["weights_sha256"]


def test_missing_or_malformed_weights_leave_the_embedder_unavailable(tmp_path):
    assert not LocalEmbedder(tmp_path / "absent.npy").available
    wrong_shape = tmp_path / "wrong-shape.npy"
    np.save(wrong_shape, np.zeros((4, 4), dtype=np.float32))
    assert not LocalEmbedder(wrong_shape).available


def test_no_model_runtime_is_loaded():
    assert LocalOnnxEmbedder is LocalEmbedder
    probe = ("import sys, app.embeddings as e; e.LocalEmbedder().encode(['a b c']); "
             "print('onnxruntime' in sys.modules)")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ENGINE_DIR,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "False"
