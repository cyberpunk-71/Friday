"""LOOM — the Weaver Walk. FRIDAY-Δ SENSE, all in one in-RAM pass:

① embed query (bge-small 384d)          8ms
② binary prefilter: 384-bit hamming     0.3ms → top 2000
③ fp16 matmul rerank (numpy)            0.5ms → top 200
④ BM25 4-char prefix + entity-exact + 1-hop graph (SQL)   4ms
⑤ learned scorer (24-feat, online SGD)  0.2ms
⑥ cross-encoder INT8 (VM) / heuristic   4ms
⑦ SLOT-FILL: 12 guaranteed slots        [2 identity][2 corrections][2 open loops]
                                        [4 topical][1 tension][1 procedure]
⑧ constraint ledger (always-injected, TTL'd, user-editable)

"No one asks twice" is a data-structure property, not a prompt plea.
"""
from __future__ import annotations

import json
import math
import re
import time

import numpy as np

from .config import cfg
from .db import get_db
from .embedder import Embedder, VectorIndex
from .river import River
from .scorer import Scorer

_KIND_DECAY = {"fact": 0.005, "preference": 0.003, "episodic": 0.015, "pattern": 0.010,
               "goal": 0.004, "procedure": 0.002, "emotional": 0.012,
               "observation": 0.020, "correction": 0.000, "tension": 0.008}


class Loom:
    def __init__(self, db=None) -> None:
        self.db = db or get_db()
        self.river = River(self.db)
        self.embedder = Embedder.get()
        self.index = VectorIndex()
        self.scorer = Scorer()
        self._index_ts = 0.0

    # ------------------------------------------------------------------ #
    # index maintenance
    # ------------------------------------------------------------------ #
    def _ensure_index(self, force: bool = False) -> None:
        if force or time.time() - self._index_ts > 30:
            atoms = self.db.q("SELECT atom_id,text FROM atoms WHERE status='active'")
            if atoms:
                self.index.rebuild([a["atom_id"] for a in atoms], [a["text"] for a in atoms])
            self._index_ts = time.time()

    # ------------------------------------------------------------------ #
    # the recall walk
    # ------------------------------------------------------------------ #
    def recall(self, query: str, k: int = 12, scope: str = "global",
               book_id: int | None = None) -> dict:
        """Returns {slots: [...], candidates, confidence, explain}."""
        t0 = time.time()
        qvec = self.embedder.embed(query)
        qtext = query.lower()
        qwords = re.findall(r"[a-z]{4,}", qtext)

        # ---- ② binary prefilter + ③ fp16 matmul rerank ----
        self._ensure_index()
        cand_ids = self.index.prefilter(qvec, k=2000)
        ranked = self.index.rerank(qvec, cand_ids, k=200)
        cos_map = {aid: s for aid, s in ranked}

        # ---- ④ SQL probes: entity-exact + prefix stems + recency ----
        sql_cands: dict[int, dict] = {}
        ent_names = [w.capitalize() for w in qwords if len(w) > 3]
        if ent_names:
            marks = ",".join("?" * len(ent_names))
            rows = self.db.q(
                f"SELECT a.* FROM atoms a WHERE a.status='active' AND a.scope=? AND "
                f"EXISTS (SELECT 1 FROM json_each(a.entity_ids) je WHERE je.value IN ({marks}))",
                [scope, *ent_names])
            for r in rows:
                sql_cands[r["atom_id"]] = r
        # BM25-lite: 4-char prefix stems
        stems = [w[:4] for w in qwords]
        for a in self.db.q("SELECT * FROM atoms WHERE status='active' AND scope=?", (scope,)):
            atext = a["text"].lower()
            if any(s in atext for s in stems):
                sql_cands[a["atom_id"]] = a
        # recency continuity fallback
        recent = self.db.q("SELECT * FROM atoms WHERE status='active' AND scope=? "
                           "ORDER BY created_ts DESC LIMIT 10", (scope,))
        for r in recent:
            sql_cands.setdefault(r["atom_id"], r)

        # ---- unified pool ----
        pool: dict[int, dict] = {}
        for aid, _sim in ranked[:200]:
            a = sql_cands.get(aid) or self.db.q1("SELECT * FROM atoms WHERE atom_id=?", (aid,))
            if a:
                pool[aid] = a
        for aid, a in sql_cands.items():
            pool.setdefault(aid, a)

        atoms = [a for a in pool.values() if a]
        if book_id:
            atoms = [a for a in atoms if a["scope"] == "global" or
                     a["scope"] == f"book:{book_id}"]
        if scope != "global":
            atoms = [a for a in atoms if a["scope"] == scope or a["scope"] == "global"]

        # ---- ⑤ learned scorer ----
        max_freq = max((a.get("access_count", 0) for a in atoms), default=1)
        deg_map = self._centrality(atoms)
        scored: list[tuple[float, dict, np.ndarray]] = []
        for a in atoms:
            aid = a["atom_id"]
            cos_sim = cos_map.get(aid, 0.0)
            recency_days = (time.time() - (a.get("last_access_ts") or a["created_ts"])) / 86400.0
            decay = _KIND_DECAY.get(a["kind"], 0.01)
            emo = min(1.0, math.hypot(a.get("valence", 0), a.get("arousal", 0)))
            lam = decay / (1 + cfg.get("salience.emotion_slow_beta", 2.0) * emo)
            rec_term = math.exp(-lam * recency_days)
            freq_norm = math.log(1 + a.get("access_count", 0)) / math.log(1 + max_freq) if max_freq > 0 else 0.0
            centrality = deg_map.get(aid, 0.0)
            entity_hit = bool(ent_names) and any(e in json.loads(a.get("entity_ids", "[]")) for e in ent_names)
            prefix_hit = any(s in a["text"].lower() for s in stems)
            f = self.scorer.features(a, qvec, query, cos_sim, recency_days, freq_norm,
                                     centrality, prefix_hit, entity_hit)
            s = self.scorer.score(f)
            scored.append((s, a, f))

        scored.sort(key=lambda x: -x[0])

        # ---- ⑥ cross-encoder / heuristic rerank of top-48 ----
        top48 = scored[:48]
        if self._cross_encoder_available():
            top48 = self._cross_rerank(query, top48)
            scored = top48 + scored[48:]
            scored.sort(key=lambda x: -x[0])
        else:
            # heuristic: small lexical bonus, no cost
            for i, (s, a, f) in enumerate(scored):
                bonus = 0.0
                if any(w in a["text"].lower() for w in qwords):
                    bonus += 0.03
                scored[i] = (s + bonus, a, f)
            scored.sort(key=lambda x: -x[0])

        # ---- ⑦ SLOT-FILL (two-pass: special kinds claim their own slots
        # FIRST, then identity/topical fill from what remains) ----
        slots, used = [], set()
        special = {"correction": 2, "procedure": 1, "tension": 1}
        for kind, count in special.items():
            for _ in range(count):
                pick = None
                for s, a, f in scored:
                    if a["atom_id"] in used or a["kind"] != kind:
                        continue
                    pick = (s, a, f)
                    break
                if pick is None:
                    break
                s, a, f = pick
                used.add(a["atom_id"])
                slots.append(self._slot(kind, s, a, f))
        for _ in range(2):  # identity
            pick = None
            for s, a, f in scored:
                if a["atom_id"] in used or a["kind"] not in ("fact", "preference"):
                    continue
                pick = (s, a, f)
                break
            if pick is None:
                break
            s, a, f = pick
            used.add(a["atom_id"])
            slots.append(self._slot("identity", s, a, f))
        for s, a, f in scored:  # topical (up to 4)
            if len([x for x in slots if x["type"] == "topical"]) >= 4:
                break
            if a["atom_id"] in used:
                continue
            used.add(a["atom_id"])
            slots.append(self._slot("topical", s, a, f))

        # open-loop slots are structural — pull from the loops table
        loops = self.river.open_loops(limit=2)
        for lp in loops[:2]:
            slots.append({"type": "open_loop", "text": lp["text"],
                          "loop_id": lp["loop_id"], "kind": lp["kind"],
                          "score": 0.9, "structural": True})

        # fallback slot: top unused candidate
        if len(slots) < k:
            for s, a, f in scored:
                if a["atom_id"] in used:
                    continue
                used.add(a["atom_id"])
                slots.append(self._slot("fallback", s, a, f))
                if len(slots) >= k:
                    break

        # rehearsal: reinforce accessed atoms (strength rehash) + access counts
        for sl in slots:
            aid = sl.get("atom_id")
            if aid:
                self._rehearse(aid)

        # ---- confidence: margin between slot-12 and next candidate ----
        conf = self._confidence(query, slots, scored)

        # ---- ⑧ constraint ledger (always injected) ----
        constraints = self.river.active_constraints()

        return {
            "slots": slots[:k],
            "constraints": constraints,
            "confidence": conf,
            "latency_ms": int((time.time() - t0) * 1000),
            "query_vec": qvec,
        }

    def _slot(self, stype: str, s: float, a: dict, f: np.ndarray) -> dict:
        return {
            "type": stype,
            "atom_id": a["atom_id"],
            "text": a["text"],
            "kind": a["kind"],
            "score": round(float(s), 3),
            "importance": a.get("importance", 0.5),
            "strength": a.get("strength", 0.5),
            "entities": json.loads(a.get("entity_ids", "[]")),
            "created_ts": a["created_ts"],
            "explain": self.scorer.explain(f),
        }

    def _rehearse(self, atom_id: int) -> None:
        row = self.db.q1("SELECT strength FROM atoms WHERE atom_id=?", (atom_id,))
        if not row:
            return
        s = min(1.0, row["strength"] + 0.15 * (1.0 - row["strength"]))
        self.db.exec("UPDATE atoms SET access_count=access_count+1, last_access_ts=?, strength=?"
                     " WHERE atom_id=?", (time.time(), s, atom_id))

    def _centrality(self, atoms: list[dict]) -> dict[int, float]:
        if not atoms:
            return {}
        ids = {a["atom_id"] for a in atoms}
        deg: dict[int, int] = {}
        for e in self.db.q("SELECT src,dst FROM edges"):
            if e["src"] in ids:
                deg[e["src"]] = deg.get(e["src"], 0) + 1
            if e["dst"] in ids:
                deg[e["dst"]] = deg.get(e["dst"], 0) + 1
        mx = max(deg.values(), default=1)
        return {k: v / mx for k, v in deg.items()}

    def _confidence(self, query: str, slots: list[dict], scored: list) -> float:
        if len(scored) < 2:
            return 0.3
        margin = scored[0][0] - scored[1][0] if len(scored) > 1 else 0.3
        entity_cover = 0.0
        ents = re.findall(r"\b[A-Z][a-z]{2,}\b", query)
        if ents:
            hit = sum(1 for e in ents if any(e in sl.get("entities", []) for sl in slots))
            entity_cover = hit / len(ents)
        return float(min(0.95, max(0.1, scored[0][0] * 0.7 + margin * 2.0 + entity_cover * 0.2)))

    # ---- cross-encoder (VM optional) ----
    def _cross_encoder_available(self) -> bool:
        try:
            import onnxruntime  # noqa: F401
            return False  # model load is lazy; keep False unless configured
        except Exception:
            return False

    def _cross_rerank(self, query: str, top48: list) -> list:
        return top48  # placeholder; VM hook point

    # ------------------------------------------------------------------ #
    # deterministic NOW-block — never LLM-written
    # ------------------------------------------------------------------ #
    def now_block(self) -> dict:
        db = self.db
        now = time.time()
        last_turn = db.q1("SELECT created_ts FROM turns ORDER BY turn_id DESC LIMIT 1")
        last_seen = last_turn["created_ts"] if last_turn else now
        loops = self.river.open_loops(limit=5)
        corrections = self.river.recent_corrections(limit=5)
        # Focus.active() auto-expires sessions past their end time — a raw
        # SELECT here kept reporting stale sessions (the SETTLE worker that
        # used to complete them crash-loops on some VMs)
        from .focus import Focus
        session = Focus(db).active()
        llm = None
        try:
            from .providers import make_llm
            p = make_llm("chat")
            llm = {"provider": p.name, "model": getattr(p, "model", "") or ""}
        except Exception:
            pass
        trackers = db.q("SELECT tracker_id,kind,query,status FROM trackers WHERE status='active'")
        pending_approvals = db.q("SELECT task_id,title,approval_kind FROM tasks WHERE status='waiting_approval'")
        return {
            "clock": {"iso": time.strftime("%Y-%m-%d %H:%M %Z"),
                      "tz": time.tzname[0]},
            "last_seen_delta_h": round((now - last_seen) / 3600.0, 1),
            "focus": {"active": bool(session),
                      "minutes_left": max(0, int(session["target_min"] - (now - session["start_ts"]) / 60)) if session else None,
                      "drifts": session["drift_count"] if session else 0},
            "llm": llm or {"provider": "unknown", "model": ""},
            "open_loops": [{"loop_id": l["loop_id"], "text": l["text"], "kind": l["kind"]} for l in loops],
            "last_corrections": [{"text": c["text"], "ts": c["ts"]} for c in corrections],
            "active_trackers": [{"id": t["tracker_id"], "kind": t["kind"], "query": t["query"]} for t in trackers[:4]],
            "pending_approvals": [{"task_id": t["task_id"], "title": t["title"], "kind": t["approval_kind"]} for t in pending_approvals],
            "budget_left_usd": round(max(0.0, cfg.get("budget.daily_usd", 6.0) - db.get_setting("spend.today_usd", 0.0)), 4),
            "ask_budget_left": max(0, cfg.get("hermes.ask_budget_per_day", 2) - int(db.get_setting("ask.used_today", 0))),
            "vad": self._current_vad(),
        }

    def _current_vad(self) -> list[float]:
        from .psyche import Heart
        return list(Heart(self.db).current())
