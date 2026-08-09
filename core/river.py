"""RIVER — append-only event log and DELTA materialization.

NOTHING is ever UPDATEd. Every change is an event; views (atoms, entities,
edges, claims α/β, tensions, corrections, constraints, open loops) are
rebuilt incrementally from the event stream. Full rebuild is cheap
(~9s at 50k events) — a bad genome merge is a recoverable incident.

Key invariants
--------------
1. CORRECTION SUPREMACY: user correction = weight 10, decays never. An LLM
   inference (weight 0.3) can NEVER overwrite it — only the user can.
2. Beta with evidence decay: α,β drift toward prior over time ⇒ old beliefs
   become UNCERTAIN, not stale-confident.
3. Tension auto-raise at WRITE time (nearest-claim check) — the memory
   immune system surfaces contradictions instead of overwriting them.
4. Promotion by demand: working-set pages become durable atoms only when
   referenced by a turn.
"""
from __future__ import annotations

import json
import re
import time
from typing import Callable

import numpy as np

from .config import cfg
from .db import get_db

AtomCandidate = dict  # {kind, text, importance, entities[], valence, arousal}

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "am", "at",
    "on", "in", "of", "to", "for", "with", "and", "or", "but", "it", "its",
    "my", "your", "his", "her", "i", "you", "we", "they", "do", "does",
    "did", "this", "that", "these", "those", "me", "him", "them", "not",
    "no", "so", "as", "from", "by", "about", "into", "over", "up", "out",
    "have", "has", "had", "will", "would", "can", "could", "should", "than",
}


class ExtractFn:
    """Signature: (kind, actor, payload, source_weight, corr_id) -> list[AtomCandidate]"""

    def __call__(self, kind: str, actor: str, payload: dict,
                 source_weight: float, corr_id: str | None) -> list[AtomCandidate]:
        raise NotImplementedError


class River:
    def __init__(self, db=None, extractor: ExtractFn | None = None) -> None:
        self.db = db or get_db()
        self.extractor: ExtractFn | None = extractor

    # ------------------------------------------------------------------ #
    # event ingestion
    # ------------------------------------------------------------------ #
    def record(self, kind: str, actor: str, payload: dict, source_weight: float = 1.0,
               supersedes: list | None = None, corr_id: str | None = None) -> str:
        eid = self.db.append_event(kind, actor, payload, source_weight, supersedes, corr_id=corr_id)
        self.apply_event(self.db.q1("SELECT * FROM events WHERE id=?", (eid,)))
        return eid

    def record_many(self, events: list[dict]) -> list[str]:
        ids = []
        for ev in events:
            ids.append(self.db.append_event(
                ev["kind"], ev.get("actor", "system"), ev.get("payload", {}),
                ev.get("source_weight", 1.0), ev.get("supersedes"),
                corr_id=ev.get("corr_id")))
        self.materialize()
        return ids

    # ------------------------------------------------------------------ #
    # materialization
    # ------------------------------------------------------------------ #
    def materialize(self) -> int:
        """Incrementally apply all events not yet materialized. Returns count."""
        applied = {r["event_id"] for r in self.db.q("SELECT event_id FROM materialized_events")}
        rows = self.db.q("SELECT * FROM events ORDER BY ts, rowid")
        n = 0
        for row in rows:
            if row["id"] in applied:
                continue
            try:
                self.apply_event(row)
            except Exception:
                # never let one bad event break the river — mark it done, keep going
                pass
            self.db.exec("INSERT OR IGNORE INTO materialized_events(event_id) VALUES(?)", (row["id"],))
            n += 1
        return n

    def rebuild_full(self) -> None:
        self.db.exec("DELETE FROM materialized_events")
        self.db.exec("DELETE FROM atoms")
        self.db.exec("DELETE FROM entities")
        self.db.exec("DELETE FROM edges")
        self.db.exec("DELETE FROM claims")
        self.db.exec("DELETE FROM tensions")
        self.db.exec("DELETE FROM corrections")
        self.db.exec("DELETE FROM open_loops")
        self.db.exec("DELETE FROM constraints")
        self.materialize()

    # ------------------------------------------------------------------ #
    # per-event application
    # ------------------------------------------------------------------ #
    def apply_event(self, ev: dict) -> None:
        kind, actor = ev["kind"], ev["actor"]
        payload = json.loads(ev["payload"] or "{}")
        w = ev["source_weight"]

        if kind == "utterance":
            self._apply_utterance(ev, payload, w)
        elif kind == "memory_write":
            atom = payload.get("atom", {})
            if atom.get("text"):
                self._upsert_atom(ev, {
                    "kind": atom.get("kind", "fact"),
                    "text": atom["text"],
                    "importance": atom.get("importance", 0.3),
                    "entities": atom.get("entities", []),
                    "valence": atom.get("valence", 0.0),
                    "arousal": atom.get("arousal", 0.0),
                    "scope": atom.get("scope", "global"),
                }, w)
        elif kind == "correction":
            self._apply_correction(ev, payload, w)
        elif kind == "rating":
            self._apply_rating(ev, payload, w)
        elif kind == "observation":
            self._apply_observation(ev, payload, w)
        elif kind == "promotion":
            self._apply_promotion(ev, payload)
        elif kind == "undo":
            self._apply_undo(ev, payload)
        elif kind == "tool_result":
            self._apply_tool_result(ev, payload)
        elif kind == "genome_commit":
            self._apply_genome_commit(ev, payload)
        elif kind == "constraint":
            self._apply_constraint(ev, payload)

    # ---- utterance → atoms + claims + open loops ----
    def _apply_utterance(self, ev: dict, payload: dict, w: float) -> None:
        text = payload.get("text", "")
        if not text or len(text) < 4:
            return
        candidates: list[AtomCandidate] = []
        if self.extractor is not None:
            candidates = self.extractor(ev["kind"], ev["actor"], payload, w, ev["corr_id"])
        else:
            candidates = self._heuristic_extract(text, w)
        for c in candidates:
            self._upsert_atom(ev, c, w)

        # open-loop detection (deterministic — never ask twice is structural)
        loops = self._detect_open_loops(text, ev)
        for lp in loops:
            self.db.exec(
                "INSERT INTO open_loops(kind,text,deadline_ts,priority,created_ts,last_activity_ts)"
                " VALUES(?,?,?,?,?,?)",
                (lp["kind"], lp["text"], lp.get("deadline"), lp.get("priority", 0.6),
                 ev["ts"], ev["ts"]))

    def _heuristic_extract(self, text: str, w: float) -> list[AtomCandidate]:
        """Sandbox/offline extraction — deterministic. VM uses the LLM extractor."""
        out: list[AtomCandidate] = []
        t = text.strip()
        low = t.lower()

        prefs = re.findall(r"i (?:prefer|love|hate|like|enjoy|want|need|use)\s+(.{4,80}?)(?:\.|$| and |, )", low)
        for p in prefs:
            out.append({"kind": "preference", "text": f"User prefers {p.strip().rstrip('.').strip()}",
                        "importance": 0.6, "entities": self._entities(text), "valence": 0.4, "arousal": 0.2})

        facts = re.findall(r"(?:my|i(?:'m| am))\s+(.{6,120}?)(?:\.|$)", low)
        for f in facts:
            f = f.strip().rstrip(".")
            if any(x in f for x in ("prefer", "love", "hate", "like", "want", "need")):
                continue
            out.append({"kind": "fact", "text": f"User: {f}", "importance": 0.55,
                        "entities": self._entities(text), "valence": 0.0, "arousal": 0.0})

        if re.search(r"\b(anxious|stressed|worried|happy|excited|sad|angry)\b", low):
            out.append({"kind": "emotional",
                        "text": f"User expressed emotion: {t[:140]}",
                        "importance": 0.7, "entities": self._entities(text),
                        "valence": self._valence(low), "arousal": 0.6})
        return out

    @staticmethod
    def _valence(low: str) -> float:
        neg = sum(low.count(x) for x in ("anxious", "stressed", "worried", "sad", "angry", "hate"))
        pos = sum(low.count(x) for x in ("happy", "excited", "love", "like", "great"))
        return max(-0.9, min(0.9, (pos - neg) * 0.2))

    @staticmethod
    def _entities(text: str) -> list[str]:
        names = re.findall(r"\b[A-Z][a-z]{2,}\b", text)
        return [n for n in names if n.lower() not in {"the", "and", "this", "that", "with", "from", "what", "when", "friday"}]

    def _detect_open_loops(self, text: str, ev: dict) -> list[dict]:
        low = text.lower()
        loops = []
        for trigger, kind, prio in (
                (r"keep an eye|keep eye|monitor|watch", "follow_up", 0.8),
                (r"remind", "commitment", 0.8),
                (r"\b(due|deadline|by tomorrow|by monday)\b", "commitment", 0.7)):
            if re.search(trigger, low):
                loops.append({"kind": kind, "text": text[:200], "priority": prio})
        if "?" in text and len(text) < 160:
            loops.append({"kind": "question", "text": text[:200], "priority": 0.5})
        return loops

    # ---- upsert atom (dedupe by near-identical text; supersede marked) ----
    def _upsert_atom(self, ev: dict, c: AtomCandidate, w: float) -> int:
        text = c["text"]
        similar = self.db.q(
            "SELECT atom_id FROM atoms WHERE status='active' AND kind=? AND text=?",
            (c["kind"], text))
        if similar:
            return similar[0]["atom_id"]
        # near-duplicate guard: same kind + high token overlap (heuristic
        # extractor and ⟨CTRL⟩ writes often produce paraphrases of each other).
        # Numeric tokens participate so "1st" vs "5th" is a material diff.
        tw = set(re.findall(r"[a-z0-9]{3,}", text.lower()))
        if len(tw) >= 4:
            candidates = self.db.q(
                "SELECT atom_id,text FROM atoms WHERE status='active' AND kind=? "
                "ORDER BY created_ts DESC LIMIT 50", (c["kind"],))
            for row in candidates:
                ow = set(re.findall(r"[a-z0-9]{3,}", row["text"].lower()))
                inter = len(tw & ow)
                # ≥90% of the LONGER set must overlap — "salary 1st" vs
                # "salary 5th" differ in a key token and stay distinct
                if inter >= 3 and inter / max(len(tw), len(ow)) >= 0.9:
                    return row["atom_id"]
        atom_id = self.db.exec(
            "INSERT INTO atoms(text,kind,importance,strength,valence,arousal,created_ts,"
            "source_event_ids,entity_ids,scope) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (text, c["kind"], c.get("importance", 0.5), 0.5,
             c.get("valence", 0.0), c.get("arousal", 0.0), ev["ts"],
             json.dumps([ev["id"]]), json.dumps(c.get("entities", [])),
             c.get("scope", "global")))
        self._ensure_entities(c.get("entities", []), ev["ts"])
        if c["kind"] in ("preference", "pattern", "trait", "goal"):
            self._propose_claim(ev, c, w)
        return atom_id

    def _ensure_entities(self, names: list[str], ts: float) -> None:
        for name in names:
            row = self.db.q1("SELECT entity_id FROM entities WHERE name=?", (name,))
            if row:
                self.db.exec("UPDATE entities SET last_mentioned_ts=MAX(last_mentioned_ts,?) "
                             "WHERE entity_id=?", (ts, row["entity_id"]))
            else:
                kind = "person" if name[0].isupper() and len(name) > 1 else "concept"
                self.db.exec("INSERT INTO entities(name,kind,created_ts,last_mentioned_ts) VALUES(?,?,?,?)",
                             (name, kind, ts, ts))

    # ---- claims: propose from strong preference/trait atoms ----
    def _propose_claim(self, ev: dict, c: AtomCandidate, w: float) -> None:
        statement = c["text"]
        row = self.db.q1("SELECT claim_id FROM claims WHERE statement=? AND status!='deprecated'",
                         (statement,))
        ts = ev["ts"]
        if row:
            cid = row["claim_id"]
            self._add_evidence(cid, ev, w)
            return
        category = c["kind"] if c["kind"] in ("preference", "goal", "constraint", "style") else "belief"
        cid = self.db.exec(
            "INSERT INTO claims(category,statement,alpha,beta,stability,priority,created_ts,updated_ts,evidence)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (category, statement, max(1.0, 1.0 + w), 1.0, 1.0, 5, ts, ts,
             json.dumps([ev["id"]])))
        self._tension_immune_system(cid, ts)

    def _add_evidence(self, claim_id: int, ev: dict, w: float) -> None:
        row = self.db.q1("SELECT * FROM claims WHERE claim_id=?", (claim_id,))
        if not row:
            return
        evidence = json.loads(row["evidence"])
        evidence.append(ev["id"])
        self.db.exec(
            "UPDATE claims SET alpha=alpha+?, beta=beta, stability=?, updated_ts=?, evidence=? WHERE claim_id=?",
            (max(0.1, w), min(1.0, row["stability"] + 0.05), ev["ts"],
             json.dumps(evidence[-40:]), claim_id))

    def _tension_immune_system(self, claim_id: int, ts: float) -> None:
        """Nearest-claim contradiction check at WRITE time."""
        row = self.db.q1("SELECT * FROM claims WHERE claim_id=?", (claim_id,))
        if not row:
            return
        others = self.db.q("SELECT * FROM claims WHERE claim_id!=? AND status!='deprecated'", (claim_id,))
        stmt = row["statement"]
        ents = set(self._entities(stmt))
        negators = ("not", "never", "hate", "don't", "dont", "avoid", "stop", "but not")
        for o in others:
            oents = set(self._entities(o["statement"]))
            overlap = ents & oents
            if len(overlap) < 1:
                continue
            a_neg = any(n in stmt.lower() for n in negators)
            b_neg = any(n in o["statement"].lower() for n in negators)
            if a_neg != b_neg:  # one affirms, one negates, same entity
                existing = self.db.q1(
                    "SELECT tension_id FROM tensions WHERE status='open' AND "
                    "((claim_1_id=? AND claim_2_id=?) OR (claim_1_id=? AND claim_2_id=?))",
                    (row["claim_id"], o["claim_id"], o["claim_id"], row["claim_id"]))
                if existing:
                    continue
                strength = 0.7 if a_neg else 0.55
                self.db.exec(
                    "INSERT INTO tensions(claim_1_id,claim_2_id,strength,balance,status,created_ts)"
                    " VALUES(?,?,?,?, 'open',?)",
                    (row["claim_id"], o["claim_id"], strength, 0.5, ts))

    # ---- correction supremacy ----
    def _apply_correction(self, ev: dict, payload: dict, w: float) -> None:
        text = payload.get("text", "")
        if not text:
            return
        ts = ev["ts"]
        # 1. find atoms this correction targets — content words only (the
        # event is ALREADY classified as a correction, so one shared content
        # word or entity is enough to target the old memory)
        words = [x for x in re.findall(r"[a-z0-9]{2,}", text.lower())
                 if x not in _STOPWORDS]
        candidates = self.db.q(
            "SELECT * FROM atoms WHERE status='active' AND scope='global'")
        targets = []
        for a in candidates:
            atext = a["text"].lower()
            hits = sum(1 for x in words if x in atext)
            if hits >= 1 and len(words) >= 2:
                targets.append(a)
        # also match by entity overlap
        for a in candidates:
            if a in targets:
                continue
            aents = set(json.loads(a["entity_ids"]))
            if aents & set(self._entities(text)):
                targets.append(a)

        # 2. supersede the targeted atoms + record correction (weight 10, decays never)
        cid = self.db.exec(
            "INSERT INTO corrections(text,atom_id,claim_id,weight,ts,event_id,status)"
            " VALUES(?,?,?,?,?,?,'active')",
            (text, targets[0]["atom_id"] if targets else None, None, 10.0, ts, ev["id"]))
        for a in targets:
            self.db.exec("UPDATE atoms SET status='superseded', supersedes_atom_id=? "
                         "WHERE atom_id=?", (cid, a["atom_id"]))
        # the correction itself becomes a durable atom (kind=correction) so the
        # SLOT-FILL can guarantee it is always recalled ("never ask twice")
        self._upsert_atom(ev, {
            "kind": "correction",
            "text": text[:240],
            "importance": 1.0,
            "entities": self._entities(text),
            "valence": 0.0, "arousal": 0.0,
        }, w=10.0)

        # 3. correct matching claims — user evidence weight 10
        claims = self.db.q("SELECT * FROM claims WHERE status!='deprecated'")
        for cl in claims:
            if self._similar(text, cl["statement"]) > 0.5 or (
                    set(self._entities(text)) & set(self._entities(cl["statement"]))):
                evidence = json.loads(cl["evidence"])
                evidence.append(ev["id"])
                self.db.exec(
                    "UPDATE claims SET alpha=alpha+?, beta=beta, stability=1.0, user_edited=1,"
                    " updated_ts=?, evidence=? WHERE claim_id=?",
                    (3.0, ts, json.dumps(evidence[-40:]), cl["claim_id"]))

        # 4. constraint candidate: "stop asking me about X" / "dont ..."
        if re.search(r"(stop|don'?t|never|quit)\b.*\b(asking|do|show|tell|nudge|remind)", text.lower()):
            self._apply_constraint(ev, {"text": text[:200], "ttl_days": -1})

    @staticmethod
    def _similar(a: str, b: str) -> float:
        sa, sb = set(re.findall(r"[a-z]{4,}", a.lower())), set(re.findall(r"[a-z]{4,}", b.lower()))
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / min(len(sa), len(sb))

    # ---- ratings ----
    def _apply_rating(self, ev: dict, payload: dict, w: float) -> None:
        ts = ev["ts"]
        atom_id = payload.get("atom_id")
        claim_id = payload.get("claim_id")
        score = payload.get("score")       # 1..5 or 0/1 for 👍/👎
        direction = payload.get("direction")  # up|down
        if atom_id:
            if direction == "up":
                self.db.exec("UPDATE atoms SET strength=MIN(1.0,strength+0.2), pinned=1 WHERE atom_id=?", (atom_id,))
            elif direction == "down":
                self.db.exec("UPDATE atoms SET strength=MAX(?,strength-0.2) WHERE atom_id=?",
                             (cfg.get("salience.strength_min", 0.3), atom_id))
            elif score is not None:
                s = float(score) / 5.0
                self.db.exec("UPDATE atoms SET strength=? WHERE atom_id=?", (max(0.1, s), atom_id))
        if claim_id:
            self.db.exec(
                "UPDATE claims SET alpha=alpha+?, beta=beta, stability=1.0, updated_ts=? WHERE claim_id=?",
                (2.0 if direction == "up" else 0.0,
                 ts, claim_id))
            if direction == "down":
                self.db.exec("UPDATE claims SET beta=beta+2 WHERE claim_id=?", (claim_id,))

    # ---- observations (extension page visits) → working set ----
    def _apply_observation(self, ev: dict, payload: dict, w: float) -> None:
        url = payload.get("url", "")
        if not url:
            return
        self.db.exec("INSERT INTO working_set(url,title,text_hash,created_ts,scope) VALUES(?,?,?,?,?)",
                     (url, payload.get("title", ""), payload.get("text_hash", ""),
                      ev["ts"], payload.get("scope", "browse")))
        # ring buffer: keep 2000 pages / 48h TTL
        cutoff = time.time() - 48 * 3600
        self.db.exec("DELETE FROM working_set WHERE created_ts<?", (cutoff,))
        rows = self.db.q("SELECT page_id FROM working_set ORDER BY created_ts DESC LIMIT -1 OFFSET 2000")
        for r in rows:
            self.db.exec("DELETE FROM working_set WHERE page_id=?", (r["page_id"],))

    # ---- promotion by demand ----
    def _apply_promotion(self, ev: dict, payload: dict, w: float = 1.0) -> None:
        url = payload.get("url", "")
        page = self.db.q1("SELECT * FROM working_set WHERE url=? ORDER BY created_ts DESC LIMIT 1", (url,))
        text = payload.get("text", "")
        if not text and page:
            text = page.get("title", "") or ""
        if not text:
            return
        self._upsert_atom(ev, {
            "kind": "observation",
            "text": f"User visited: {text}"[:300],
            "importance": 0.35,
            "entities": self._entities(text),
            "valence": 0.0, "arousal": 0.0,
        }, w)

    # ---- undo ----
    def _apply_undo(self, ev: dict, payload: dict, w: float = 1.0) -> None:
        target_event = payload.get("target_event")
        if not target_event:
            return
        # the superseded atoms of the corrected target come back
        self.db.exec("UPDATE atoms SET status='active', supersedes_atom_id=NULL WHERE "
                     "supersedes_atom_id IN "
                     "(SELECT correction_id FROM corrections WHERE event_id=?)", (target_event,))
        self.db.exec("UPDATE corrections SET status='superseded' WHERE event_id=?", (target_event,))

    # ---- tool results → tracker observations / price watchers ----
    def _apply_tool_result(self, ev: dict, payload: dict, w: float) -> None:
        tr = payload.get("tracker_id")
        if tr:
            h = payload.get("hash")
            changed = 0
            prev = self.db.q1("SELECT hash_observed FROM tracker_observations WHERE tracker_id=? "
                              "ORDER BY ts DESC LIMIT 1", (tr,))
            if prev and prev["hash_observed"] != h:
                changed = 1
            self.db.exec(
                "INSERT INTO tracker_observations(tracker_id,ts,hash_observed,payload,changed) VALUES(?,?,?,?,?)",
                (tr, ev["ts"], h, json.dumps(payload, ensure_ascii=False), changed))
            self.db.exec("UPDATE trackers SET last_check_ts=?, last_result_hash=? WHERE tracker_id=?",
                         (ev["ts"], h, tr))
        price = payload.get("price")
        last_price = payload.get("last_price")
        if price is not None and last_price is not None and price >= last_price:
            # price watcher assertion failed — flag a tension-like observation
            self.db.append_event("observation", "hermes",
                                 {"text": f"Price watcher: {payload.get('query','')} now {price} (was {last_price}) — not a drop, not recommended.",
                                  "kind": "price"}, 0.5)

    # ---- genome commit ----
    def _apply_genome_commit(self, ev: dict, payload: dict, w: float = 1.0) -> None:
        self.db.exec(
            "INSERT INTO genome_commits(sha,msg,fitness,baseline,canary,reverted,reason,created_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (payload.get("sha", ""), payload.get("msg", ""), payload.get("fitness"),
             payload.get("baseline"), 1 if payload.get("canary") else 0,
             1 if payload.get("reverted") else 0, payload.get("reason"), ev["ts"]))

    # ---- constraint ----
    def _apply_constraint(self, ev: dict, payload: dict, w: float = 1.0) -> None:
        text = payload.get("text", "")
        if not text:
            return
        existing = self.db.q1("SELECT constraint_id FROM constraints WHERE text=?", (text,))
        if not existing:
            self.db.exec(
                "INSERT INTO constraints(text,ttl_days,created_ts,source,user_editable) VALUES(?,?,?,?,?)",
                (text, payload.get("ttl_days", -1), ev["ts"],
                 payload.get("source", "user"), 1))

    # ------------------------------------------------------------------ #
    # query helpers for DELTA views
    # ------------------------------------------------------------------ #
    def active_atoms(self, scope: str = "global") -> list[dict]:
        return self.db.q("SELECT * FROM atoms WHERE status='active' AND scope=? ORDER BY strength DESC", (scope,))

    def recent_corrections(self, limit: int = 5) -> list[dict]:
        return self.db.q("SELECT * FROM corrections WHERE status='active' ORDER BY ts DESC LIMIT ?", (limit,))

    def active_constraints(self, now: float | None = None) -> list[dict]:
        now = now or time.time()
        return self.db.q("SELECT * FROM constraints WHERE ttl_days<0 OR created_ts + ttl_days*86400 > ?", (now,))

    def open_loops(self, limit: int = 20) -> list[dict]:
        """Dedupe by normalized text (same loop can be re-uttered)."""
        rows = self.db.q("SELECT * FROM open_loops WHERE status='open' "
                         "ORDER BY priority DESC, created_ts ASC")
        seen, out = set(), []
        for r in rows:
            key = re.sub(r"[^a-z0-9]", "", r["text"].lower())[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def close_loop(self, loop_id: int) -> None:
        self.db.exec("UPDATE open_loops SET status='closed', last_activity_ts=? WHERE loop_id=?",
                     (time.time(), loop_id))

    def claims(self, status: str = "active") -> list[dict]:
        return self.db.q("SELECT * FROM claims WHERE status=? ORDER BY priority DESC, updated_ts DESC", (status,))

    def tensions(self, status: str = "open") -> list[dict]:
        return self.db.q("SELECT * FROM tensions WHERE status=? ORDER BY strength DESC", (status,))

    def decay_claims(self, now: float | None = None) -> None:
        """Evidence decay: α,β → drift toward the prior (0.5) over half-life.
        Old beliefs become UNCERTAIN, not stale-confident."""
        now = now or time.time()
        hl = cfg.get("beliefs.decay_half_life_days", 180.0)
        rows = self.db.q("SELECT * FROM claims WHERE user_edited=0 AND status!='deprecated'")
        for r in rows:
            age_days = (now - r["updated_ts"]) / 86400.0
            if age_days <= 0:
                continue
            k = 0.5 ** (age_days / hl)      # 1 → fresh, →0 old
            # blend (α,β) toward (1,1)
            a = 1.0 + (r["alpha"] - 1.0) * k
            b = 1.0 + (r["beta"] - 1.0) * k
            self.db.exec("UPDATE claims SET alpha=?, beta=?, updated_ts=? WHERE claim_id=?",
                         (a, b, now, r["claim_id"]))

    def claim_confidence(self, r: dict) -> float:
        return r["alpha"] / max(1e-9, r["alpha"] + r["beta"])
