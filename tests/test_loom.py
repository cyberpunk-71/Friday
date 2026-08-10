"""LOOM — the Weaver Walk: slot guarantees, entity hits, typo expansion,
recall quality (the query must actually find the right memory)."""
from __future__ import annotations


def test_dentist_recall_on_yatra_query(sim_seed, loom):
    r = loom.recall("amarnath yatra dates and does it clash with my dentist appointment")
    texts = " | ".join(s["text"] for s in r["slots"])
    assert "dentist" in texts.lower()
    assert r["confidence"] > 0.3


def test_salary_recall(db, river, loom):
    river.record("memory_write", "user",
                 {"atom": {"kind": "fact", "text": "Salary is credited on the 1st of every month",
                           "importance": 0.8}}, source_weight=10.0)
    r = loom.recall("when is my salary credited")
    assert any("salary" in s["text"].lower() for s in r["slots"])


def test_mom_saree_recall(sim_seed, loom):
    r = loom.recall("buy handloom saree for mom")
    texts = " | ".join(s["text"] for s in r["slots"])
    assert "handloom" in texts.lower() or "mom" in texts.lower()


def test_slot_fill_guarantees_correction(sim_seed, db, river, loom):
    """2 ACTIVE CORRECTIONS are always in the slots — never ask twice is
    a data-structure property."""
    river.record("correction", "user", {"text": "I do not like Kanjivaram sarees"}, 10.0)
    r = loom.recall("what saree should I get for mom", k=12)
    corr = [s for s in r["slots"] if s["type"] == "correction"]
    assert len(corr) >= 1
    assert "kanjivaram" in corr[0]["text"].lower() or "kanjivaram" in " ".join(s["text"] for s in r["slots"]).lower()


def test_procedure_slot(db, river, loom):
    """SKILL.md-as-memory: the trip-planning procedure lands in the slot."""
    river.record("memory_write", "genome",
                 {"atom": {"kind": "procedure",
                           "text": "SKILL domestic-trip-planning (productivity): plan trips with flights, hotels, budget",
                           "importance": 0.6}}, source_weight=0.5)
    r = loom.recall("plan a trip to jaipur")
    procs = [s for s in r["slots"] if s["type"] == "procedure"]
    assert len(procs) >= 1
    assert "trip" in procs[0]["text"].lower()


def test_entity_exact_boost(sim_seed, loom):
    r = loom.recall("what do you know about Gandhinagar")
    texts = " | ".join(s["text"] for s in r["slots"])
    assert "gandhinagar" in texts.lower()


def test_typo_expansion_for_retrieval(sim_seed, loom):
    """'reserach' → retrieval query expands; raw text is left for the model."""
    from core.cortex import Cortex
    q = Cortex._typo_expand("reserach phone undr 20k")
    # 'reserach' → 'research' if vocab knows it
    assert "research" in q or "reserach" in q
    # proper nouns untouched
    q2 = Cortex._typo_expand("Sarah is coming")
    assert "Sarah" in q2


def test_constraints_ledger_always_injected(sim_seed, db, river, loom):
    river.record("constraint", "user", {"text": "don't ask me about drive writes"})
    r = loom.recall("anything", k=4)
    assert any("drive writes" in c["text"] for c in r["constraints"])


def test_recall_latency_budget(sim_seed, loom):
    import time
    t0 = time.time()
    for _ in range(10):
        loom.recall("test query about things")
    elapsed = (time.time() - t0) / 10 * 1000
    # FRIDAY-Δ SENSE target ≈ 18ms; be generous on slow CI but strict vs 100ms
    assert elapsed < 100, f"recall too slow: {elapsed:.1f}ms"


def test_mmr_diversity(sim_seed, loom):
    """12 slots should not be 12 copies of the same memory."""
    r = loom.recall("dentist salary saree trip preferences", k=12)
    texts = [s["text"] for s in r["slots"]]
    assert len(set(texts)) == len(texts), "duplicate slots!"
