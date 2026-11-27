"""Rebuild or check the engine's embedding projection (`models/starling-embedding.npy`).

The matrix is a fixed Johnson-Lindenstrauss random projection: 256 hashed word and bigram features
are multiplied by a 256x96 matrix of Gaussian values (mean 0, standard deviation 1/sqrt(96)) drawn
from a NumPy generator seeded with 20260714. It contains no trained or third-party weights. The same
matrix was previously shipped as the single MatMul initializer of `models/starling-embedding.onnx`.

    python tools/build_embedding_projection.py           # check the shipped file (exit 1 on mismatch)
    python tools/build_embedding_projection.py --write   # regenerate it
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

SEED = 20260714
INPUT_DIMENSION = 256
OUTPUT_DIMENSION = 96
OUTPUT = Path(__file__).resolve().parents[1] / "models" / "starling-embedding.npy"


def projection() -> np.ndarray:
    rng = np.random.default_rng(SEED)
    return rng.normal(0.0, 1.0 / np.sqrt(OUTPUT_DIMENSION), (INPUT_DIMENSION, OUTPUT_DIMENSION)).astype(np.float32)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--write", action="store_true", help="overwrite the shipped matrix")
    args = parser.parse_args()
    weights = projection()
    if args.write:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        np.save(OUTPUT, weights, allow_pickle=False)
        print(f"wrote {OUTPUT.name} {weights.shape} {weights.dtype}")
        return 0
    shipped = np.load(OUTPUT, allow_pickle=False)
    if shipped.shape == weights.shape and shipped.dtype == weights.dtype and np.array_equal(shipped, weights):
        print(f"OK: {OUTPUT.name} is exactly the seeded projection {weights.shape} {weights.dtype}")
        return 0
    difference = float(np.max(np.abs(shipped.astype(np.float64) - weights))) if shipped.shape == weights.shape else None
    print(f"MISMATCH: {OUTPUT.name} shape {shipped.shape} {shipped.dtype}, largest difference {difference}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
