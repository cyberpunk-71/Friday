"""PSYCHE + HEART — Bayesian claims, VAD, EWMA trajectory, posture,
predictions/Brier alignment."""
from __future__ import annotations

import pytest

from core.psyche import Heart, Psyche, lexicon_vad


def test_lexicon_vad():
    v, a, d = lexicon_vad("I feel anxious and stressed about the meeting")
    assert v < -0.3
    assert a > 0.5
    v2, a2, _ = lexicon_vad("I am so happy and excited")
    assert v2 > 0.3
    # negation flips
    v3, _, _ = lexicon_vad("I am not happy")
    assert v3 < 0.2


def test_heart_ewma(db):
    h = Heart(db)
    h.record("I feel anxious about work")     # negative
    h.record("I feel anxious about work")     # negative
    h.record("I started meditating and feel calm")  # positive
    cur = h.current()
    # EWMA should have moved positive vs pure negative
    h2 = Heart(db)
    v, _, _ = h2.current()
    assert -1.0 <= v <= 1.0
    traj = h.trajectory(days=7)
    assert len(traj["days"]) >= 1
    assert len(traj["ewma"][-1]) == 3


def test_posture_selection(db):
    p = Psyche(db)
    post = p.posture()
    assert post["name"] in ("Mirror", "Energize", "Steady", "Challenge", "Explore", "Default")
    assert "directive" in post


def test_predictions_brier(db):
    p = Psyche(db)
    pid = p.predict("focus", "user will finish 25m focus", 0.7)
    brier = p.resolve(pid, 1)
    assert brier == pytest.approx(0.09, abs=1e-9)  # (0.7-1)^2
    align = p.alignment("focus")
    assert align["brier_score"] == pytest.approx(brier, abs=1e-9)


def test_claim_confidence(sim_seed, db, river):
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User prefers window seats",
                           "importance": 0.7}}, source_weight=10.0)
    cards = Psyche(db).belief_cards()
    assert any("window seats" in c["statement"] for c in cards)
    for c in cards:
        assert 0 <= c["confidence"] <= 1
        assert c["alpha"] >= 1 and c["beta"] >= 1
