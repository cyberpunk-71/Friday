"""RIVER — append-only event log, materialization, correction supremacy,
tension immune system, evidence decay, undo."""
from __future__ import annotations

import time

import pytest


def test_append_only_no_updates(db, river):
    """Events are immutable rows; a 'correction' supersedes, never updates."""
    e1 = river.record("memory_write", "user",
                      {"atom": {"kind": "fact", "text": "User lives in Ahmedabad", "importance": 0.8}},
                      source_weight=10.0)
    row1 = db.q1("SELECT * FROM events WHERE id=?", (e1,))
    assert row1["kind"] == "memory_write"

    # correction arrives
    e2 = river.record("correction", "user",
                      {"text": "I don't live in Ahmedabad, I live in Gandhinagar"}, 10.0)
    row2 = db.q1("SELECT * FROM events WHERE id=?", (e2,))
    assert row2["kind"] == "correction"
    # the old event is untouched
    row1b = db.q1("SELECT * FROM events WHERE id=?", (e1,))
    assert row1b["payload"] == row1["payload"]

    # atom superseded (the correction itself becomes a correction-kind atom,
    # but the original fact must NOT be active anymore)
    atoms = db.q("SELECT * FROM atoms WHERE status='active' AND text LIKE '%lives in Ahmedabad%'")
    assert atoms == []
    superseded = db.q("SELECT * FROM atoms WHERE status='superseded'")
    assert len(superseded) == 1
    corrections = db.q("SELECT * FROM corrections WHERE status='active'")
    assert len(corrections) == 1
    assert corrections[0]["weight"] == 10.0


def test_correction_supremacy_weight(db, river):
    """User correction (10) outranks inference (0.3) — check weights in claims."""
    river.record("memory_write", "friday",
                 {"atom": {"kind": "preference", "text": "User prefers Kanjivaram sarees",
                           "importance": 0.3}}, source_weight=0.3)
    river.record("memory_write", "friday",
                 {"atom": {"kind": "preference", "text": "User prefers Kanjivaram sarees",
                           "importance": 0.3}}, source_weight=0.3)
    claim = db.q1("SELECT * FROM claims WHERE statement LIKE '%Kanjivaram%'")
    assert claim is not None
    a_before = claim["alpha"]

    river.record("correction", "user",
                 {"text": "User hates Kanjivaram, prefers handloom"}, 10.0)
    claim2 = db.q1("SELECT * FROM claims WHERE statement LIKE '%Kanjivaram%'")
    # user evidence hits the claim — user_edited flag + big alpha jump
    assert claim2["user_edited"] == 1
    assert claim2["alpha"] >= a_before + 3.0
    assert claim2["stability"] == 1.0


def test_tension_immune_system(db, river):
    """Contradictory claims about the same entity auto-raise a Tension."""
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User loves handloom sarees",
                           "importance": 0.7}}, source_weight=10.0)
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User hates handloom sarees",
                           "importance": 0.7}}, source_weight=10.0)
    tensions = db.q("SELECT * FROM tensions WHERE status='open'")
    assert len(tensions) >= 1
    assert tensions[0]["strength"] >= 0.5


def test_evidence_decay(db, river):
    """Old beliefs become UNCERTAIN: α,β drift toward 1,1 (E→0.5)."""
    river.record("memory_write", "user",
                 {"atom": {"kind": "preference", "text": "User prefers X brand phones",
                           "importance": 0.7}}, source_weight=10.0)
    claim = db.q1("SELECT * FROM claims WHERE statement LIKE '%X brand%'")
    # simulate age: backdate updated_ts 200 days with strong evidence both ways
    db.exec("UPDATE claims SET updated_ts=?, alpha=10, beta=5 WHERE claim_id=?",
            (time.time() - 200 * 86400, claim["claim_id"]))
    river.decay_claims(now=time.time())
    c2 = db.q1("SELECT * FROM claims WHERE claim_id=?", (claim["claim_id"],))
    # half-life 180d → k≈0.46; alpha→1+9*0.46≈5.1, beta→1+4*0.46≈2.85
    assert c2["alpha"] < 10 and c2["beta"] < 5
    assert c2["alpha"] > 1 and c2["beta"] > 1
    assert 0.5 < c2["alpha"] / (c2["alpha"] + c2["beta"]) < 0.95


def test_open_loops_detection(db, river):
    river.record("utterance", "user", {"text": "keep an eye on the yatra portal and remind me of the dentist"})
    loops = db.q("SELECT * FROM open_loops WHERE status='open'")
    assert len(loops) >= 2
    kinds = {l["kind"] for l in loops}
    assert "follow_up" in kinds


def test_undo_restores_superseded(db, river):
    e1 = river.record("memory_write", "user",
                      {"atom": {"kind": "fact", "text": "User's car is a Honda", "importance": 0.8}},
                      source_weight=10.0)
    e2 = river.record("correction", "user", {"text": "Actually the car is a Toyota"}, 10.0)
    assert db.q1("SELECT COUNT(*) c FROM atoms WHERE status='active' AND text LIKE '%Honda%'")["c"] == 0
    river.record("undo", "user", {"target_event": e2})
    # the superseded Honda atom is reactivated; the correction row is marked superseded
    assert db.q1("SELECT COUNT(*) c FROM atoms WHERE status='active' AND text LIKE '%Honda%'")["c"] == 1
    assert db.q1("SELECT status FROM corrections WHERE event_id=?", (e2,))["status"] == "superseded"


def test_working_set_ring(db, river):
    for i in range(15):
        river.record("observation", "extension", {"url": f"https://site{i}.com", "title": f"Page {i}",
                                                  "text_hash": f"h{i}"})
    ws = db.q("SELECT COUNT(*) c FROM working_set")
    assert ws[0]["c"] == 15
    # promotion by demand
    river.record("promotion", "friday", {"url": "https://site0.com", "text": "ML research paper"})
    atoms = db.q("SELECT * FROM atoms WHERE kind='observation'")
    assert len(atoms) == 1


def test_constraints(db, river):
    river.record("constraint", "user", {"text": "never ask me about drive writes"})
    cs = river.active_constraints()
    assert any("drive writes" in c["text"] for c in cs)
    # TTL'd constraint expires
    river.record("constraint", "user", {"text": "temp rule", "ttl_days": 1e-7})
    import time as _t
    _t.sleep(0.05)
    assert not any(c["text"] == "temp rule" for c in river.active_constraints())
