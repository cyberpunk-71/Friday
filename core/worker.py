"""WORKER — background SETTLE: reminders, trackers, focus nudges, nightly
compaction (claims decay + Gym), dreamstate idle loop. All invisible to chat."""
from __future__ import annotations

import asyncio
import json
import time

from .config import cfg
from .db import get_db
from .genome import Genome
from .providers import make_search
from .river import River


class Worker:
    def __init__(self, db=None, search=None, cortex=None) -> None:
        self.db = db or get_db()
        self.search = search or make_search()
        self.cortex = cortex  # optional (used to emit dreamstate questions)
        self._stop = asyncio.Event()

    async def run_forever(self, tick_s: float = 10.0) -> None:
        while not self._stop.is_set():
            try:
                now = time.time()
                await self._check_reminders(now)
                await self._check_trackers(now)
                self._check_focus_completion(now)
                self._nightly_if_due(now)
                await self._dreamstate(now)
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=tick_s)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ #
    async def _check_reminders(self, now: float) -> None:
        due = self.db.q("SELECT * FROM reminders WHERE status='pending' AND due_ts<=? LIMIT 10", (now,))
        for r in due:
            channels = json.loads(r["channels"])
            for ch in channels:
                self.db.exec(
                    "INSERT INTO nudges(kind,channel,message,created_ts) VALUES('reminder',?,?,?)",
                    (ch, r["text"], now))
            self.db.exec("UPDATE reminders SET status='delivered' WHERE reminder_id=?",
                         (r["reminder_id"],))

    async def _check_trackers(self, now: float) -> None:
        trackers = self.db.q("SELECT * FROM trackers WHERE status='active'")
        for t in trackers:
            freq = t["frequency_mins"] * 60
            if t["last_check_ts"] and now - t["last_check_ts"] < freq:
                continue
            try:
                res = await self.search.search(t["query"], 4)
                sig = json.dumps([(r["title"], r["snippet"]) for r in res], ensure_ascii=False)
                import hashlib
                h = hashlib.sha256(sig.encode()).hexdigest()[:16]
                changed = t["last_result_hash"] is not None and h != t["last_result_hash"]
                payload = {"tracker_id": t["tracker_id"], "query": t["query"],
                           "hash": h, "changed": changed, "results": res[:3]}
                self.db.append_event("tool_result", "hermes", payload, 1.0)
                if changed:
                    self.db.exec(
                        "INSERT INTO nudges(kind,channel,message,created_ts) VALUES('research','chrome',?,?)",
                        (f"Tracker update: {t['query'][:80]} changed — check it.", now))
            except Exception:
                self.db.exec("UPDATE trackers SET last_check_ts=? WHERE tracker_id=?",
                             (now, t["tracker_id"]))

    def _check_focus_completion(self, now: float) -> None:
        from .focus import Focus
        f = Focus(self.db)
        s = f.active()
        if s and now - s["start_ts"] >= s["target_min"] * 60:
            f.stop()
            self.db.exec("INSERT INTO nudges(kind,channel,message,created_ts) "
                         "VALUES('focus','chrome',?,?)",
                         ("Focus session complete — nice. Want a break?", now))

    # ------------------------------------------------------------------ #
    def _nightly_if_due(self, now: float) -> None:
        window = cfg.get("genome.nightly_utc", "22:30")
        try:
            hh, mm = window.split(":")
            due_ts = self._next_run(now, int(hh), int(mm))
            if abs(now - due_ts) < 900:  # within 15 min of the window
                last = self.db.get_setting("nightly.last_run_ts", 0)
                if now - last > 20 * 3600:
                    self._nightly(now)
        except Exception:
            pass

    @staticmethod
    def _next_run(now: float, hh: int, mm: int) -> float:
        import datetime
        t = datetime.datetime.now(datetime.timezone.utc).replace(hour=hh, minute=mm, second=0, microsecond=0)
        ts = t.timestamp()
        while ts < now:
            ts += 86400
        return ts

    def _nightly(self, now: float) -> None:
        """Compaction: decay claims → materialize → Gym → 3 mutants → canary."""
        rv = River(self.db)
        rv.decay_claims(now)
        rv.materialize()
        genome = Genome(db=rv.db)
        report = genome.run_gym(dry_run=True)
        self.db.set_setting("nightly.last_run_ts", now)
        self.db.set_setting("nightly.last_report", json.dumps(report, ensure_ascii=False))
        # full evolution only when enabled (VM). In tests dry-run is safe.
        if os.environ.get("FRIDAY_EVOLVE", "0") == "1":
            branches = genome.mutate(n=cfg.get("genome.mutants", 3))
            self.db.set_setting("nightly.mutants", json.dumps(branches))
        # cleanup old events (compaction: keep 90 days)
        cutoff = now - 90 * 86400
        self.db.exec("DELETE FROM events WHERE ts<?", (cutoff,))
        self.db.exec("DELETE FROM working_set WHERE created_ts<?", (cutoff,))

    # ------------------------------------------------------------------ #
    async def _dreamstate(self, now: float) -> None:
        """Idle CPU rehearsal: sample old memories, bank questions on a budget."""
        if not cfg.get("dreamstate.enabled", True):
            return
        last = self.db.get_setting("dream.last_ts", 0)
        if now - last < 1800:  # every 30 min max
            return
        spent = self.db.get_setting("dream.spent_today", 0.0)
        if spent >= cfg.get("dreamstate.max_spend_day_usd", 0.10):
            return
        if self.db.get_setting("dream.bank_count", 0) >= cfg.get("dreamstate.bank_cap", 40):
            return
        # sample 3 semi-related memories
        atoms = self.db.q("SELECT * FROM atoms WHERE status='active' ORDER BY RANDOM() LIMIT 3")
        if len(atoms) < 3:
            self.db.set_setting("dream.last_ts", now)
            return
        texts = " | ".join(a["text"][:80] for a in atoms)
        # offline: bank a deterministic question; VM: LLM-invented (cheap)
        q = f"What connects: {texts[:200]}?"
        self.db.set_setting("dream.bank_count", self.db.get_setting("dream.bank_count", 0) + 1)
        self.db.set_setting("dream.bank_last", json.dumps({"q": q, "atoms": [a["atom_id"] for a in atoms]}))
        self.db.set_setting("dream.spent_today", spent + 0.0004)
        self.db.set_setting("dream.last_ts", now)
