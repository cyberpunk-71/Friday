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
        """Current active session — or None. Sessions past their end time are
        auto-expired here (worker-independent): the SETTLE worker crash-loops
        on some VMs, which used to leave 'active' sessions stuck forever and
        every reply kept reporting stale '18 minutes left'."""
        s = self.db.q1("SELECT * FROM focus_sessions WHERE status='active' "
                       "ORDER BY start_ts DESC LIMIT 1")
        if not s:
            return None
        now = time.time()
        # on a break the session keeps running (break_end_ts), otherwise it
        # ends at start_ts + target_min*60
        end = s["start_ts"] + (s.get("target_min") or 25) * 60
        if s.get("mode") == "break" and s.get("break_end_ts"):
            end = max(end, s["break_end_ts"])
        if now >= end:
            elapsed = int((now - s["start_ts"]) / 60)
            status = "completed" if elapsed >= (s.get("target_min") or 25) * 0.8 else "abandoned"
            self.db.exec("UPDATE focus_sessions SET end_ts=?, status=? WHERE session_id=?",
                         (now, status, s["session_id"]))
            self.db.append_event("focus", "system", {"action": "auto_complete",
                                                     "session_id": s["session_id"],
                                                     "status": status})
            return None
        return s

    def _migrate_cols(self) -> None:
        """Add columns added after launch (task, why, first_step, mode,
        break_end_ts, and the flagship session-intake/score columns) to
        older DBs."""
        cols = [r["name"] for r in self.db.q("PRAGMA table_info(focus_sessions)")]
        for col, ddl in (("task", "TEXT"), ("why", "TEXT"), ("first_step", "TEXT"),
                         ("mode", "TEXT NOT NULL DEFAULT 'work'"),
                         ("break_end_ts", "REAL"),
                         ("energy", "INTEGER"), ("mood", "INTEGER"),
                         ("energy_after", "INTEGER"), ("mood_after", "INTEGER"),
                         ("distraction_plan", "TEXT"), ("thoughts", "TEXT"),
                         ("notes", "TEXT"), ("comebacks", "INTEGER NOT NULL DEFAULT 0"),
                         ("focus_score", "INTEGER")):
            if col not in cols:
                try:
                    self.db.exec(f"ALTER TABLE focus_sessions ADD COLUMN {col} {ddl}")
                except Exception:
                    pass

    def start(self, minutes: int = 25, allow: list | None = None, voice: bool = True,
              task: str | None = None, why: str | None = None,
              first_step: str | None = None, energy: int | None = None,
              mood: int | None = None, distraction_plan: str | None = None) -> dict:
        if self.active():
            return {"ok": False, "error": "session already active"}
        self._migrate_cols()
        sid = self.db.exec(
            "INSERT INTO focus_sessions(start_ts,target_min,status,allow_domains,drift_count,"
            "task,why,first_step,mode,energy,mood,distraction_plan)"
            " VALUES(?,?, 'active',?,0,?,?,?,'work',?,?,?)",
            (time.time(), minutes, json.dumps(allow or []), task or None,
             why or None, first_step or None, energy, mood, distraction_plan or None))
        self.db.append_event("focus", "user", {"action": "start", "minutes": minutes,
                                               "allow": allow or [], "task": task,
                                               "why": why, "first_step": first_step,
                                               "energy": energy, "mood": mood,
                                               "session_id": sid})
        return {"ok": True, "session_id": sid, "minutes": minutes,
                "task": task, "why": why, "first_step": first_step,
                "ends_at": time.time() + minutes * 60}

    def add_thought(self, text: str) -> dict:
        """Brain-dump capture during a session — working-memory offload.
        'I'll hold this for you' → appended to the session's thoughts."""
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        self._migrate_cols()
        text = (text or "").strip()[:400]
        if not text:
            return {"ok": False, "error": "empty thought"}
        prev = s.get("thoughts") or ""
        new = (prev + "\n" if prev else "") + text
        self.db.exec("UPDATE focus_sessions SET thoughts=? WHERE session_id=?",
                     (new, s["session_id"]))
        return {"ok": True, "thoughts": new}

    def add_comeback(self) -> dict:
        """A drift followed by returning to work — celebrated, counted."""
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        self._migrate_cols()
        self.db.exec("UPDATE focus_sessions SET comebacks=comebacks+1 WHERE session_id=?",
                     (s["session_id"],))
        return {"ok": True, "comebacks": (s.get("comebacks") or 0) + 1}

    @staticmethod
    def _focus_score(elapsed_min: int, target_min: int, drifts: int,
                     comebacks: int) -> int:
        """0-100: completion matters most, drifts hurt, comebacks redeem."""
        completion = min(1.0, elapsed_min / max(1, target_min))
        drift_penalty = max(0.0, 1.0 - drifts / 8.0)
        comeback_bonus = min(1.0, comebacks / 2.0)
        return int(round(100 * (0.55 * completion + 0.25 * drift_penalty
                                + 0.10 * comeback_bonus + 0.10 * min(1.0, elapsed_min / 25.0))))

    def finish(self, energy_after: int | None = None, mood_after: int | None = None,
               notes: str | None = None) -> dict:
        """End the session with an after-check-in: mood/energy delta, notes,
        and the computed FOCUS SCORE. Returns a rich summary for the
        celebration card."""
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        self._migrate_cols()
        elapsed = int((time.time() - s["start_ts"]) / 60)
        status = "completed" if elapsed >= (s.get("target_min") or 25) * 0.8 else "abandoned"
        score = self._focus_score(elapsed, s.get("target_min") or 25,
                                  s.get("drift_count") or 0, s.get("comebacks") or 0)
        self.db.exec(
            "UPDATE focus_sessions SET end_ts=?, status=?, energy_after=?, mood_after=?, "
            "notes=?, focus_score=? WHERE session_id=?",
            (time.time(), status, energy_after, mood_after,
             (notes or "").strip()[:500] or None, score, s["session_id"]))
        self.db.append_event("focus", "user", {"action": "stop", "session_id": s["session_id"],
                                               "status": status, "focus_score": score})
        return {
            "ok": True, "session_id": s["session_id"], "elapsed_min": elapsed,
            "status": status, "target_min": s.get("target_min"),
            "drifts": s.get("drift_count") or 0, "comebacks": s.get("comebacks") or 0,
            "task": s.get("task"), "why": s.get("why"),
            "energy_before": s.get("energy"), "mood_before": s.get("mood"),
            "energy_after": energy_after, "mood_after": mood_after,
            "thoughts": s.get("thoughts") or "", "notes": notes or "",
            "focus_score": score,
        }

    def set_mode(self, mode: str, break_min: int = 5) -> dict:
        """work ↔ break. During break, drifts are NOT nudged (you're allowed
        to wander on a break — that's the point)."""
        s = self.active()
        if not s:
            return {"ok": False, "error": "no active session"}
        if mode not in ("work", "break"):
            return {"ok": False, "error": "bad mode"}
        self._migrate_cols()
        break_end = (time.time() + break_min * 60) if mode == "break" else None
        self.db.exec("UPDATE focus_sessions SET mode=?, break_end_ts=? WHERE session_id=?",
                     (mode, break_end, s["session_id"]))
        self.db.append_event("focus", "system" if mode == "break" else "user",
                             {"action": "mode", "mode": mode,
                              "break_min": break_min if mode == "break" else 0,
                              "session_id": s["session_id"]})
        return {"ok": True, "mode": mode, "break_end_ts": break_end}

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
        # on a break you're ALLOWED to wander — no nudges, no drift counting
        if s.get("mode") == "break":
            return {"ok": True, "nudges": [], "on_break": True}
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
        sessions = self.db.q("SELECT * FROM focus_sessions ORDER BY start_ts DESC LIMIT 30")
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
            dom = urlparse(d["url"]).netloc or d["url"][:40]
            domains[dom] = domains.get(dom, 0) + 1
        # recent drifts for the companion feed (with domain)
        recent = []
        for d in drifts[:10]:
            dom = urlparse(d["url"]).netloc or d["url"][:40]
            recent.append({"ts": d["ts"], "url": d["url"][:160], "domain": dom,
                           "session_id": d["session_id"]})
        # week stats (Monday start)
        now = time.localtime()
        mon = time.mktime((now.tm_year, now.tm_mon, now.tm_mday - now.tm_wday, 0, 0, 0, 0, 0, -1))
        week_sessions = [s for s in sessions
                         if s["start_ts"] >= mon and s["status"] in ("completed", "abandoned")]
        week_min = sum(max(0, int(((s["end_ts"] or time.time()) - s["start_ts"]) / 60)) for s in week_sessions)
        # day streak: consecutive days (ending today or yesterday) with a session
        done_days = sorted({time.strftime("%Y-%m-%d", time.localtime(s["start_ts"]))
                            for s in sessions if s["status"] in ("completed", "abandoned")})
        streak = 0
        import datetime
        cursor = datetime.date.today()
        if done_days and done_days[-1] != cursor.strftime("%Y-%m-%d"):
            cursor = cursor - datetime.timedelta(days=1)
        seen = set(done_days)
        while cursor.strftime("%Y-%m-%d") in seen:
            streak += 1
            cursor -= datetime.timedelta(days=1)
        # best day + average (minutes actually focused)
        day_min: dict[str, int] = {}
        for s in sessions:
            if s["status"] in ("completed", "abandoned") and s.get("end_ts"):
                day = time.strftime("%Y-%m-%d", time.localtime(s["start_ts"]))
                day_min[day] = day_min.get(day, 0) + int((s["end_ts"] - s["start_ts"]) / 60)
        ended = [s for s in sessions if s["status"] in ("completed", "abandoned") and s.get("end_ts")]
        avg_min = int(sum((s["end_ts"] - s["start_ts"]) / 60 for s in ended) / len(ended)) if ended else 0
        nudges = self.db.q1("SELECT COUNT(*) AS n FROM nudges WHERE kind='focus'")
        # flagship analytics: focus scores, energy/mood, best-hour insights
        scores = [s.get("focus_score") for s in ended if s.get("focus_score") is not None]
        energies = [s.get("energy") for s in sessions if s.get("energy") is not None]
        moods = [s.get("mood") for s in sessions if s.get("mood") is not None]
        by_hour_min: dict[int, int] = {}
        for s in ended:
            if s.get("end_ts"):
                h = time.localtime(s["start_ts"]).tm_hour
                by_hour_min[h] = by_hour_min.get(h, 0) + int((s["end_ts"] - s["start_ts"]) / 60)
        best_hour = max(by_hour_min, key=by_hour_min.get) if by_hour_min else None
        e_before = [s.get("energy") for s in ended if s.get("energy") is not None]
        e_after = [s.get("energy_after") for s in ended if s.get("energy_after") is not None]
        # focus score series (last 7 scored sessions, oldest → newest)
        scored = [x for x in sessions if x.get("focus_score") is not None][:7]
        score_series = [s.get("focus_score") for s in scored][::-1]
        return {
            "sessions": sessions,
            "today": {"count": len(today_sessions),
                      "minutes": sum(int((s["end_ts"] or time.time()) - s["start_ts"]) / 60 for s in today_sessions)},
            "learned": {"peak_hours": sorted(by_hour, key=by_hour.get, reverse=True)[:3],
                        "top_distraction_domains": sorted(domains, key=domains.get, reverse=True)[:5]},
            "total_drifts": len(drifts),
            "recent_drifts": recent,
            "week": {"count": len(week_sessions), "minutes": week_min},
            "streak_days": streak,
            "best_day_min": max(day_min.values()) if day_min else 0,
            "avg_min": avg_min,
            "total_completed": len(ended),
            "nudges_total": (nudges["n"] if nudges else 0),
            "focus": {
                "avg_score": int(round(sum(scores) / len(scores))) if scores else 0,
                "best_score": max(scores) if scores else 0,
                "series": score_series,
                "best_hour": best_hour,
                "best_hour_min": by_hour_min.get(best_hour, 0) if best_hour else 0,
                "avg_energy": round(sum(energies) / len(energies), 1) if energies else 0,
                "avg_mood": round(sum(moods) / len(moods), 1) if moods else 0,
                "energy_delta": (round(sum(e_after) / len(e_after) - sum(e_before) / len(e_before), 1)
                                 if e_after and e_before else 0),
                "total_thoughts": sum(len((s.get("thoughts") or "").splitlines())
                                      for s in sessions),
            },
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
