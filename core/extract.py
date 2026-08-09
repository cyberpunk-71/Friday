"""Extraction pipeline — SETTLE phase. Turns utterances into atom candidates.

LLMExtractor (VM): batched DeepSeek JSON extraction, called async after the
turn. HeuristicExtractor (offline): deterministic regex fallback used by the
river when no LLM is available. The river applies whichever is registered.
"""
from __future__ import annotations

import json
import re
import time

from .providers import LLMProvider

EXTRACT_SYSTEM = """You extract durable facts about a user from their chat message.
Return ONLY JSON: {"atoms": [{"kind": "fact|preference|pattern|goal|episodic|emotional|observation",
"text": "one-line statement about the user", "importance": 0.0-1.0,
"entities": ["ProperNoun", ...], "valence": -1..1, "arousal": 0..1}]}
Rules: no greeting/smalltalk atoms; skip ephemeral requests; max 4 atoms;
text must be self-contained; kind=observation for transient states."""


class LLMExtractor:
    def __init__(self, llm: LLMProvider) -> None:
        self.llm = llm

    def __call__(self, kind: str, actor: str, payload: dict,
                 source_weight: float, corr_id: str | None) -> list[dict]:
        text = payload.get("text", "")
        if actor != "user" or len(text) < 8:
            return []
        try:
            out = self.llm.complete(
                [{"role": "system", "content": EXTRACT_SYSTEM},
                 {"role": "user", "content": text}],
                json_mode=True, temperature=0.1, max_tokens=300)
            data = json.loads(out)
            atoms = data.get("atoms", [])
            for a in atoms:
                a["importance"] = min(1.0, max(0.1, float(a.get("importance", 0.5)) * min(1.0, source_weight)))
            return atoms
        except Exception:
            return []


class HeuristicExtractor:
    """Deterministic offline extraction — mirrors Anima's post-turn LEARN."""

    def __call__(self, kind: str, actor: str, payload: dict,
                 source_weight: float, corr_id: str | None) -> list[dict]:
        text = payload.get("text", "")
        if actor != "user" or len(text) < 6:
            return []
        from .river import River
        rv = River()
        return rv._heuristic_extract(text, source_weight)
