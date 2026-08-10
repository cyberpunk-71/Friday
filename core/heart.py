from __future__ import annotations
"""Heart — affect tracking (VAD lexicon, EWMA trajectory). Re-exported from
core.psyche to preserve the Anima module layout (core/heart.py)."""
from .psyche import Heart, VAD_LEXICON, lexicon_vad

__all__ = ["Heart", "VAD_LEXICON", "lexicon_vad"]
