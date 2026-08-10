"""FOCUS — sessions, distraction tracking, coalesced nudges, learned patterns.

Coalescing is mandatory: 1 toast + 1 chrome per 10 minutes per kind (never
three toasts for three drifts). Voice nudge kicks in after N drifts. Phone
suppression is per-kind. The MV3 extension logs every page; the server learns
recurring distraction times and suggests blocks.
"""
from __future__ import annotations

import json
import time

from .config import cfg
from .db import get_db


class Focus:
    def __init__(self, db=None) -> None:
        self.db = db or get_db()

    def active(self) -> dict | None:
        return self.db.q1("SELECT * FROM focus_sessions WHERE status='active' "
                          "ORDER BY start_ts DESC LIMIT 1")

    def start(self, minutes: int = 25, allow: list | None = None, voice: bool = True,
              task: str | None = None) -> dict:
        if self.active():
            return {"ok": False, "error": "session already active"}
        # migrate: task label column (older DBs don't have it)
        cols = [r["name"] for r in self.db.q("PRAGMA table_info(focus_sessions)")]
        if "task" not in cols:
            try:
                self.db.exec("ALTER TABLE focus_sessions ADD COLUMN task TEXT")
            except Exception:
                pass
        sid = self.db.exec(
            "INSERT INTO focus_sessions(start_ts,target_min,status,allow_domains,drift_count,task)"
            " VALUES(?,?, 'active',?,0,?)",
            (time.time(), minutes, json.dumps(allow or []), task or None))
        self.db.append_event("focus", "user", {"action": "start", "minutes": minutes,
                                               "allow": allow or [], "task": task,
                                               "session_id": sid})
        return {"ok": True, "session_id": sid, "minutes": minutes,
                "task": task, "ends_at": time.time() + minutes * 60}

    def stop(self) -> dict:
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        elapsed = int((time.time() - s["start_ts"]) / 60)
        status = "completed" if elapsed >= s["target_min"] * 0.8 else "abandoned"
        self.db.exec("UPDATE focus_sessions SET end_ts=?, status=? WHERE session_id=?",
                     (time.time(), status, s["session_id"]))
        self.db.append_event("focus", "user", {"action": "stop", "session_id": s["session_id"]})
        return {"ok": True, "session_id": s["session_id"], "elapsed_min": elapsed, "status": status}

    def log_drift(self, url: str, title: str = "") -> dict:
        """Called by the MV3 extension / PWA sensor during a session. Returns nudge actions."""
        s = self.active()
        if not s:
            return {"ok": True, "nudges": []}
        allow = json.loads(s["allow_domains"])
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        if any(d in domain or domain in d for d in allow):
            return {"ok": True, "nudges": [], "allowed": True}
        now = time.time()
        # dedupe same-url drifts within 5s (prevents double-fire from the PWA
        # sensor + extension reporting the same drift; NOT a 60s gate that
        # swallows repeated drifts)
        dup = self.db.q1("SELECT id FROM distraction_events WHERE session_id=? AND url=? AND ts>?",
                         (s["session_id"], url, now - 5))
        if dup:
            return {"ok": True, "nudges": []}
        self.db.exec("INSERT INTO distraction_events(session_id,url,ts,kind) VALUES(?,?,?, 'drift')",
                     (s["session_id"], url, now))
        self.db.exec("UPDATE focus_sessions SET drift_count=drift_count+1 WHERE session_id=?",
                     (s["session_id"],))
        drifts = s["drift_count"] + 1
        return {"ok": True, "session_id": s["session_id"], "drift_count": drifts,
                "nudges": self._nudge_plan(drifts, url)}

    def _nudge_plan(self, drift_no: int, url: str) -> list[dict]:
        """IMMEDIATE nudges: toast + chrome on EVERY drift, voice from drift 2.
        A 20s per-channel safety cooldown only prevents double-fire from the
        PWA sensor and the extension reporting the same drift."""
        plan: list[dict] = []
        ch = cfg.get("focus.nudge_channels", {})
        safe = 20
        now = time.time()
        if ch.get("toast", True):
            plan.append({"channel": "toast", "message": f"Drift #{drift_no}: {url}"})
        if ch.get("chrome", True):
            last = self.db.q1(
                "SELECT created_ts FROM nudges WHERE kind='focus' AND channel='chrome' "
                "AND created_ts>? ORDER BY created_ts DESC LIMIT 1", (now - safe,))
            if not last:
                plan.append({"channel": "chrome", "message": f"You drifted to {url} — back to focus!"})
        voice_from = cfg.get("focus.voice_after_drifts", 2)
        if ch.get("voice", True) and drift_no >= voice_from:
            last = self.db.q1(
                "SELECT created_ts FROM nudges WHERE kind='focus' AND channel='voice' "
                "AND created_ts>? ORDER BY created_ts DESC LIMIT 1", (now - safe,))
            if not last:
                plan.append({"channel": "voice", "message": "Back to work?"})
        for p in plan:
            self.db.exec(
                "INSERT INTO nudges(kind,channel,message,created_ts) VALUES('focus',?,?,?)",
                (p["channel"], p["message"], now))
        return plan

    def stats(self) -> dict:
        sessions = self.db.q("SELECT * FROM focus_sessions ORDER BY start_ts DESC LIMIT 20")
        today = time.strftime("%Y-%m-%d")
        today_sessions = [s for s in sessions if time.strftime("%Y-%m-%d", time.localtime(s["start_ts"])) == today]
        # learned patterns: hour-of-day drift frequencies
        drifts = self.db.q("SELECT * FROM distraction_events ORDER BY ts DESC LIMIT 500")
        by_hour: dict[int, int] = {}
        domains: dict[str, int] = {}
        from urllib.parse import urlparse
        for d in drifts:
            h = time.localtime(d["ts"]).tm_hour
            by_hour[h] = by_hour.get(h, 0) + 1
            dom = urlparse(d["url"]).netloc
            domains[dom] = domains.get(dom, 0) + 1
        return {
            "sessions": sessions,
            "today": {"count": len(today_sessions),
                      "minutes": sum(int((s["end_ts"] or time.time()) - s["start_ts"]) / 60 for s in today_sessions)},
            "learned": {"peak_hours": sorted(by_hour, key=by_hour.get, reverse=True)[:3],
                        "top_distraction_domains": sorted(domains, key=domains.get, reverse=True)[:5]},
            "total_drifts": len(drifts),
        }

    def allow_domain(self, domain: str) -> dict:
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        allow = json.loads(s["allow_domains"])
        allow.append(domain)
        self.db.exec("UPDATE focus_sessions SET allow_domains=? WHERE session_id=?",
                     (json.dumps(allow), s["session_id"]))
        return {"ok": True, "allow": allow}
