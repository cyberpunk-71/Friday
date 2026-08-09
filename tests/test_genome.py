"""GENOME + GYM — git repo, mutation, replay fitness, canary, revert."""
from __future__ import annotations

import json

from core.genome import Genome


def test_genome_repo_initialized(db, tmp_path):
    gdir = tmp_path / "genome"
    gdir.mkdir()
    g = Genome(gdir, db)
    assert g.head() is not None
    assert len(g.log()) >= 1


def test_genome_commit_and_log(db, tmp_path):
    gdir = tmp_path / "genome2"
    gdir.mkdir()
    g = Genome(gdir, db)
    (gdir / "test.txt").write_text("hello")
    sha = g.commit("test commit", fitness=0.7, baseline=0.6)
    assert sha
    log = g.log()
    assert any("test commit" in e["msg"] for e in log)


def test_gym_fitness_math():
    g = Genome()
    turns = [
        {"outcome": "corrected", "cost_usd": 0.001, "latency_ms": 500},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": "corrected", "cost_usd": 0.001, "latency_ms": 500},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
        {"outcome": None, "cost_usd": 0.0005, "latency_ms": 400},
    ]
    f, stats = g.gym_fitness(turns)
    assert 0 <= f <= 1
    assert stats["n"] == 10
    assert stats["corrected"] == 2
    # worse turns → lower fitness
    bad = [dict(t, outcome="corrected", cost_usd=0.5, latency_ms=9000) for t in turns]
    f2, _ = g.gym_fitness(bad)
    assert f2 < f


def test_run_gym_dry_run(db, tmp_path):
    gdir = tmp_path / "genome3"
    gdir.mkdir()
    g = Genome(gdir, db)
    report = g.run_gym(dry_run=True)
    assert "fitness" in report and "improved" in report


def test_mutation_produces_branches(db, tmp_path):
    gdir = tmp_path / "genome4"
    gdir.mkdir()
    g = Genome(gdir, db)
    (gdir / "prompts").mkdir()
    (gdir / "prompts" / "self.md").write_text("Never ask twice is structural.\n")
    branches = g.mutate(n=3)
    assert len(branches) == 3
    import subprocess
    out = subprocess.run(["git", "-C", str(gdir), "branch"], capture_output=True, text=True).stdout
    for b in branches:
        assert b in out
    # back on main
    head = subprocess.run(["git", "-C", str(gdir), "rev-parse", "--abbrev-ref", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert head == "main"


def test_canary_promote_and_revert(db, tmp_path):
    gdir = tmp_path / "genome5"
    gdir.mkdir()
    g = Genome(gdir, db)
    (gdir / "prompts").mkdir()
    (gdir / "prompts" / "self.md").write_text("version 1\n")
    g.commit("v1")
    # a real mutant branch with better fitness
    branches = g.mutate(n=1)
    assert len(branches) == 1
    sha = g.promote_canary(branches[0], 0.9, 0.5)
    assert sha is not None
    commits = db.q("SELECT * FROM genome_commits ORDER BY commit_id DESC LIMIT 1")
    assert commits[0]["canary"] == 1
    # revert
    g.revert(sha, "regression")
    reverted = db.q("SELECT * FROM genome_commits ORDER BY commit_id DESC LIMIT 1")
    assert reverted[0]["reverted"] == 1
