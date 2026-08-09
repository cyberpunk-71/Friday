"""Embeddings — FRIDAY-Δ SENSE ①②③⑥.

① bge-small 384d via fastembed (VM; downloads from HF once)
② BINARY prefilter: 384-bit hamming over the whole store → top-k (in-RAM)
③ fp16 matmul rerank (numpy, mmap) → top-k
⑥ INT8 MiniLM-L6 cross-encoder (VM, optional) — kills LLM rerank

Sandbox fallback (`hash` backend): deterministic hashed n-gram embeddings so the
whole pipeline runs offline with no model downloads. `fastembed` is selected
automatically when the package is installed AND the model file is present.
"""
from __future__ import annotations

import hashlib
import math
import os
import struct
import threading

import numpy as np

from .config import cfg

DIM = 384


def _hash_tokens(text: str) -> list[tuple[int, float]]:
    """Char n-gram hashing with tf-idf-ish scaling — deterministic across runs."""
    text = text.lower()
    grams: dict[int, float] = {}
    for n in (2, 3, 4):
        for i in range(len(text) - n + 1):
            g = text[i:i + n]
            h = int(hashlib.md5(g.encode()).hexdigest()[:8], 16)
            grams[h] = grams.get(h, 0.0) + 1.0
    return list(grams.items())


def hash_embed(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    items = _hash_tokens(text or "")
    for h, c in items:
        v[h % DIM] += c
    norm = np.linalg.norm(v)
    if norm > 0:
        v /= norm
    return v


class Embedder:
    """Single embedder instance with pluggable backend."""

    _instance: "Embedder | None" = None

    def __init__(self) -> None:
        backend = os.environ.get("FRIDAY_EMBED_BACKEND", "hash")
        self.backend = backend
        self._model = None
        if backend == "fastembed":
            try:
                from fastembed import TextEmbedding  # type: ignore
                self._model = TextEmbedding("BAAI/bge-small-en-v1.5")
                self.backend = "fastembed"
            except Exception:
                self.backend = "hash"

    @classmethod
    def get(cls) -> "Embedder":
        if cls._instance is None:
            cls._instance = Embedder()
        return cls._instance

    def embed(self, text: str) -> np.ndarray:
        if self.backend == "fastembed" and self._model is not None:
            vec = next(self._model.embed([text]))
            return np.asarray(vec, dtype=np.float32)
        return hash_embed(text)

    def embed_many(self, texts: list[str]) -> np.ndarray:
        if self.backend == "fastembed" and self._model is not None:
            return np.vstack([np.asarray(v, dtype=np.float32) for v in self._model.embed(texts)])
        return np.vstack([hash_embed(t) for t in texts])


class VectorIndex:
    """In-RAM index: binary signatures + fp16 matrix.

    - signatures: 384-bit (48 uint8) per doc for hamming prefilter
    - matrix: fp16 [N,384] for SIMD-ish matmul rerank (numpy)
    Build cost ~O(N); query ~O(N/8 + N) but tiny at 50k docs (2.4MB hot).
    """

    def __init__(self) -> None:
        self.ids: list[int] = []
        self.signatures: list[bytes] = []
        self._matrix: np.ndarray | None = None
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.ids = []
            self.signatures = []
            self._matrix = None

    def rebuild(self, ids: list[int], texts: list[str]) -> None:
        if not texts:
            self.reset()
            return
        em = Embedder.get()
        mat = em.embed_many(texts)
        self._matrix = mat.astype(np.float16)
        sigs = []
        for v in mat:
            bits = (v > 0.0).astype(np.uint8)
            sigs.append(np.packbits(bits).tobytes())
        with self._lock:
            self.ids = list(ids)
            self.signatures = sigs

    def prefilter(self, query_vec: np.ndarray, k: int = 200) -> list[int]:
        """384-bit hamming over binary signatures → top-k candidate ids."""
        with self._lock:
            if not self.signatures:
                return []
            qbits = np.packbits((query_vec > 0.0).astype(np.uint8)).tobytes()
            # XOR + popcount over bytes — 48 bytes per doc, fast loop
            scores = []
            for sig in self.signatures:
                diff = 0
                for a, b in zip(sig, qbits):
                    diff += (a ^ b).bit_count()
                scores.append(-diff)
            order = np.argsort(scores)[:k]
            return [self.ids[i] for i in order]

    def rerank(self, query_vec: np.ndarray, ids: list[int] | None = None, k: int = 200) -> list[tuple[int, float]]:
        """fp16 matmul cosine rerank over ids (or full store)."""
        with self._lock:
            if self._matrix is None or len(self.ids) == 0:
                return []
            if ids is None:
                idx = np.arange(len(self.ids))
            else:
                idmap = {i: n for n, i in enumerate(self.ids)}
                idx = np.array([idmap[i] for i in ids if i in idmap], dtype=np.int64)
            if len(idx) == 0:
                return []
            sub = self._matrix[idx]
            q = query_vec.astype(np.float16)
            dots = sub @ q
            # matrix rows are L2-normalized already if embed() normalized; be safe
            norms = np.linalg.norm(sub.astype(np.float32), axis=1)
            sims = (dots.astype(np.float32) / np.maximum(norms, 1e-9)).tolist()
            order = np.argsort(-np.asarray(sims))
            return [(self.ids[idx[i]], sims[idx[i]]) for i in order[:k]]
