"""Psyche — Bayesian identity canon: claims (α/β), tensions, posture selection.
Port of Anima's `core/psyche.py` + `core/heart.py` on top of the RIVER views.
The math is always computed in Python; the LLM only extracts evidence."""
from __future__ import annotations

import json
import math
import re
import time
import uuid

import numpy as np

from .config import cfg
from .db import get_db

# ---- VAD lexicon (Anima Heart) ----
VAD_LEXICON = {
    "anxious": (-0.62, 0.74, 0.28), "stressed": (-0.60, 0.72, 0.25),
    "worried": (-0.55, 0.60, 0.30), "happy": (0.80, 0.55, 0.70),
    "excited": (0.70, 0.85, 0.60), "sad": (-0.70, 0.25, 0.20),
    "angry": (-0.70, 0.80, 0.55), "calm": (0.40, 0.15, 0.60),
    "tired": (-0.35, 0.15, 0.25), "grateful": (0.75, 0.30, 0.55),
    "frustrated": (-0.65, 0.70, 0.30), "confused": (-0.30, 0.40, 0.35),
    "proud": (0.65, 0.55, 0.80), "lonely": (-0.60, 0.20, 0.15),
    "hopeful": (0.55, 0.50, 0.50), "guilty": (-0.55, 0.40, 0.20),
    "love": (0.85, 0.60, 0.60), "hate": (-0.75, 0.75, 0.50),
    "great": (0.70, 0.50, 0.60), "bad": (-0.55, 0.35, 0.30),
    "good": (0.55, 0.30, 0.50), "amazing": (0.85, 0.70, 0.65),
    "nervous": (-0.45, 0.65, 0.30), "relaxed": (0.50, 0.10, 0.65),
    "bored": (-0.40, 0.10, 0.30), "curious": (0.30, 0.55, 0.50),
}

NEGATORS = {"not", "no", "never", "don't", "dont", "cannot", "can't", "isn't", "wasn't"}


def lexicon_vad(text: str) -> tuple[float, float, float]:
    """VAD from lexicon hits, negated by nearby negators. Matches Anima Heart."""
    low = text.lower()
    words = re.findall(r"[a-z']+", low)
    v, a, d, n = 0.0, 0.0, 0.0, 0
    for i, w in enumerate(words):
        if w not in VAD_LEXICON:
            continue
        neg = any(words[j] in NEGATORS for j in range(max(0, i - 2), i))
        lv, la, ld = VAD_LEXICON[w]
        if neg:
            lv, la = -lv * 0.6, la * 0.7
        v, a, d = v + lv, a + la, d + ld
        n += 1
    if n == 0:
        return 0.0, 0.35, 0.5
    return max(-1.0, min(1.0, v / n)), max(0.0, min(1.0, a / n)), max(0.0, min(1.0, d / n))


class Heart:
    """Affect tracking: samples → EWMA trajectory → daily aggregates."""

    def __init__(self, db=None) -> None:
        self.db = db or get_db()

    def record(self, text: str, source: str = "lexicon") -> str:
        v, a, d = lexicon_vad(text)
        sid = uuid.uuid4().hex[:12]
        self.db.exec("INSERT INTO affect_samples(id,valence,arousal,dominance,source,created_ts) VALUES(?,?,?,?,?,?)",
                     (sid, v, a, d, source, time.time()))
        self._rollup()
        return sid

    def _rollup(self) -> None:
        day = time.strftime("%Y-%m-%d")
        rows = self.db.q("SELECT valence,arousal,dominance FROM affect_samples WHERE created_ts>?",
                         (time.time() - 86400,))
        if not rows:
            return
        avg = [sum(r[k] for r in rows) / len(rows) for k in ("valence", "arousal", "dominance")]
        prev = self.db.q1("SELECT ewma_vad FROM daily_emotion WHERE date=?", (day,))
        alpha = 0.30
        if prev and prev["ewma_vad"]:
            pv = json.loads(prev["ewma_vad"])
            ewma = [alpha * avg[i] + (1 - alpha) * pv[i] for i in range(3)]
        else:
            prev2 = self.db.q1("SELECT ewma_vad FROM daily_emotion ORDER BY date DESC LIMIT 1", ())
            pv = json.loads(prev2["ewma_vad"]) if prev2 and prev2["ewma_vad"] else avg
            ewma = [alpha * avg[i] + (1 - alpha) * pv[i] for i in range(3)]
        self.db.exec(
            "INSERT INTO daily_emotion(date,avg_vad,ewma_vad) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET avg_vad=excluded.avg_vad, ewma_vad=excluded.ewma_vad",
            (day, json.dumps(avg), json.dumps(ewma)))

    def trajectory(self, days: int = 21) -> dict:
        rows = self.db.q("SELECT date,ewma_vad FROM daily_emotion ORDER BY date DESC LIMIT ?", (days,))
        rows.reverse()
        return {"days": [r["date"] for r in rows],
                "ewma": [json.loads(r["ewma_vad"]) for r in rows]}

    def current(self) -> tuple[float, float, float]:
        row = self.db.q1("SELECT ewma_vad FROM daily_emotion ORDER BY date DESC LIMIT 1", ())
        if row and row["ewma_vad"]:
            return tuple(json.loads(row["ewma_vad"]))
        return (0.0, 0.35, 0.5)


class Psyche:
    """Canon claims + tensions + posture + predictions (Brier alignment)."""

    def __init__(self, db=None) -> None:
        self.db = db or get_db()
        self.heart = Heart(self.db)

    # ---- posture ----
    def posture(self) -> dict:
        v, a, d = self.heart.current()
        postures = cfg._defaults.get("postures", [])  # loaded from configs/postures.yaml below
        for p in _POSTURES:
            m = p.get("match")
            if not m:
                continue
            vlo, vhi = m.get("valence", [-1, 1])
            alo, ahi = m.get("arousal", [0, 1])
            if vlo <= v <= vhi and alo <= a <= ahi:
                return {"name": p["name"], "directive": p["directive"],
                        "vad": [v, a, d]}
        return {"name": "Default", "directive": "Be sharp, warm, concise, quietly confident.",
                "vad": [v, a, d]}

    # ---- predictions / Brier (Anima alignment) ----
    def predict(self, domain: str, description: str, probability: float) -> str:
        pid = uuid.uuid4().hex[:10]
        self.db.exec("INSERT INTO predictions(id,domain,description,probability,created_ts) VALUES(?,?,?,?,?)",
                     (pid, domain, description, probability, time.time()))
        return pid

    def resolve(self, pid: str, outcome: int) -> float:
        row = self.db.q1("SELECT * FROM predictions WHERE id=?", (pid,))
        if not row or row["outcome"] is not None:
            return 0.0
        self.db.exec("UPDATE predictions SET outcome=?, resolved_ts=? WHERE id=?",
                     (outcome, time.time(), pid))
        brier = (row["probability"] - outcome) ** 2
        rec = self.db.q1("SELECT * FROM alignment_records WHERE domain=?", (row["domain"],))
        if rec:
            n = rec["total"] + 1
            bs = (rec["brier_score"] * rec["total"] + brier) / n
            self.db.exec("UPDATE alignment_records SET brier_score=?, total=?, updated_ts=? WHERE domain=?",
                         (bs, n, time.time(), row["domain"]))
        else:
            self.db.exec("INSERT INTO alignment_records(domain,brier_score,total,updated_ts) VALUES(?,?,?,?)",
                         (row["domain"], brier, 1, time.time()))
        return brier

    def alignment(self, domain: str) -> dict | None:
        return self.db.q1("SELECT * FROM alignment_records WHERE domain=?", (domain,))

    # ---- belief card helpers (for chat + Memory panel) ----
    def belief_cards(self, limit: int = 12) -> list[dict]:
        from .river import River
        rv = River(self.db)
        rows = rv.claims()
        out = []
        for r in rows[:limit]:
            conf = rv.claim_confidence(r)
            out.append({
                "claim_id": r["claim_id"],
                "category": r["category"],
                "statement": r["statement"],
                "alpha": round(r["alpha"], 2),
                "beta": round(r["beta"], 2),
                "confidence": round(conf, 3),
                "stability": round(r["stability"], 2),
                "priority": r["priority"],
                "user_edited": bool(r["user_edited"]),
                "updated_ts": r["updated_ts"],
            })
        return out

    def tension_cards(self) -> list[dict]:
        from .river import River
        rv = River(self.db)
        out = []
        for t in rv.tensions():
            c1 = self.db.q1("SELECT * FROM claims WHERE claim_id=?", (t["claim_1_id"],))
            c2 = self.db.q1("SELECT * FROM claims WHERE claim_id=?", (t["claim_2_id"],))
            if c1 and c2:
                out.append({
                    "tension_id": t["tension_id"],
                    "a": c1["statement"], "b": c2["statement"],
                    "strength": round(t["strength"], 2),
                    "balance": round(t["balance"], 2),
                    "status": t["status"],
                })
        return out

    def resolve_tension(self, tension_id: int, winner_claim_id: int | None = None) -> None:
        self.db.exec("UPDATE tensions SET status='user_resolved', resolved_ts=? WHERE tension_id=?",
                     (time.time(), tension_id))


# postures loaded from genome/policies/style-adjacent YAML
import yaml as _yaml
from pathlib import Path as _Path

_POSTURES: list[dict] = []
_pf = _Path(__file__).resolve().parent.parent / "configs" / "postures.yaml"
if _pf.exists():
    with open(_pf) as _f:
        _POSTURES = _yaml.safe_load(_f).get("postures", [])
