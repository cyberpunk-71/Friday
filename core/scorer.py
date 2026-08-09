"""LEARNED SCORER — 24-feature logistic regression with online SGD.

FRIDAY-Δ: the hand-tuned S(m,q,t) constants become the PRIOR; the model is
refit nightly on "was this memory cited and not followed by a correction"
labels. Weights are printable in chat ("I remembered that because:
entity-exact +1.4, correction-flag +2.1, recency −0.3").

Weights live in genome/scorer.json (a genome file → git-tracked, Gym-able).
"""
from __future__ import annotations

import json
import math
import os
import time

import numpy as np

from .config import cfg

FEATURES = [
    "cos", "recency", "strength", "emotion", "freq", "centrality",
    "kind_fact", "kind_preference", "kind_pattern", "kind_goal", "kind_episodic",
    "kind_emotional", "kind_procedure", "kind_correction", "kind_observation",
    "entity_exact", "prefix4", "token_overlap", "correction_flag", "pinned",
    "query_len", "atom_len", "user_edited", "scope_global",
]


class Scorer:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or str(cfg.genome_path("scorer.json"))
        self.w = np.zeros(len(FEATURES), dtype=np.float64)
        self.b = 0.0
        self.n = 0
        self._load_prior()

    def _load_prior(self) -> None:
        # 1. genome weights (learned) if present
        if os.path.exists(self.path):
            try:
                with open(self.path) as f:
                    d = json.load(f)
                self.w = np.asarray(d["w"], dtype=np.float64)
                self.b = float(d.get("b", 0.0))
                self.n = int(d.get("n", 0))
                return
            except Exception:
                pass
        # 2. prior = hand-tuned salience constants (S(m,q,t))
        s = cfg.get("salience", {})
        w = np.zeros(len(FEATURES))
        w[FEATURES.index("cos")] = s.get("w_cos", 0.35)
        w[FEATURES.index("recency")] = s.get("w_rec", 0.15)
        w[FEATURES.index("strength")] = s.get("w_str", 0.15)
        w[FEATURES.index("emotion")] = s.get("w_emo", 0.15)
        w[FEATURES.index("freq")] = s.get("w_freq", 0.10)
        w[FEATURES.index("centrality")] = s.get("w_cen", 0.10)
        self.w = w
        self.b = 0.0

    def save(self) -> None:
        with open(self.path, "w") as f:
            json.dump({"w": self.w.tolist(), "b": self.b, "n": self.n,
                       "features": FEATURES, "saved_ts": time.time()}, f)

    # ---- feature vector ----
    def features(self, atom: dict, qvec: np.ndarray | None, qtext: str,
                 cos_sim: float, recency_days: float, freq_norm: float,
                 centrality: float, prefix_hit: bool, entity_hit: bool) -> np.ndarray:
        f = np.zeros(len(FEATURES))
        kind = atom["kind"]
        f[FEATURES.index("cos")] = cos_sim
        f[FEATURES.index("recency")] = math.exp(-0.05 * recency_days)
        f[FEATURES.index("strength")] = atom.get("strength", 0.5)
        f[FEATURES.index("emotion")] = min(1.0, math.hypot(atom.get("valence", 0), atom.get("arousal", 0)))
        f[FEATURES.index("freq")] = freq_norm
        f[FEATURES.index("centrality")] = centrality
        kf = f"kind_{kind}" if f"kind_{kind}" in FEATURES else None
        if kf:
            f[FEATURES.index(kf)] = 1.0
        f[FEATURES.index("entity_exact")] = 1.0 if entity_hit else 0.0
        f[FEATURES.index("prefix4")] = 1.0 if prefix_hit else 0.0
        f[FEATURES.index("correction_flag")] = 1.0 if kind == "correction" else 0.0
        f[FEATURES.index("pinned")] = 1.0 if atom.get("pinned") else 0.0
        f[FEATURES.index("query_len")] = min(1.0, len(qtext) / 120.0)
        f[FEATURES.index("atom_len")] = min(1.0, len(atom["text"]) / 200.0)
        f[FEATURES.index("user_edited")] = 1.0 if atom.get("user_edited") else 0.0
        f[FEATURES.index("scope_global")] = 1.0 if atom.get("scope") == "global" else 0.0
        a_tokens = set(atom["text"].lower().split())
        q_tokens = set(qtext.lower().split())
        f[FEATURES.index("token_overlap")] = len(a_tokens & q_tokens) / max(1, len(q_tokens))
        return f

    def score(self, f: np.ndarray) -> float:
        z = float(self.w @ f + self.b)
        return 1.0 / (1.0 + math.exp(-z))

    # ---- online SGD update (nightly Gym + live feedback) ----
    def update(self, f: np.ndarray, label: int, lr: float = 0.02) -> None:
        p = self.score(f)
        grad = (p - label)
        self.w -= lr * grad * f
        self.b -= lr * grad
        self.n += 1

    def explain(self, f: np.ndarray, top: int = 4) -> list[str]:
        contrib = [(FEATURES[i], self.w[i] * f[i]) for i in range(len(FEATURES)) if abs(f[i]) > 1e-9]
        contrib.sort(key=lambda x: -abs(x[1]))
        return [f"{name} {'+' if v >= 0 else ''}{v:.2f}" for name, v in contrib[:top]]
