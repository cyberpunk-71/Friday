"""FOCUS — sessions, drift nudges with mandatory coalescing, learned patterns,
per-kind phone suppression."""
from __future__ import annotations

import time

from core.focus import Focus


def test_start_stop(db):
    f = Focus(db)
    r = f.start(25, allow=["github.com"])
    assert r["ok"]
    assert f.active() is not None
    r2 = f.start(10)
    assert r2["ok"] is False  # already active
    f.stop()
    assert f.active() is None


def test_drift_allowed_domain_no_nudge(db):
    f = Focus(db)
    f.start(25, allow=["github.com"])
    r = f.log_drift("https://github.com/cyberpunk-71/Friday")
    assert r.get("allowed") is True
    assert r["nudges"] == []


def test_drift_coalescing_toast_chrome_voice(db):
    """Coalescing is mandatory: toast every drift, chrome ≤1/10min, voice from drift 3."""
    f = Focus(db)
    f.start(25, allow=[])
    r1 = f.log_drift("https://youtube.com/shorts")
    kinds1 = [n["channel"] for n in r1["nudges"]]
    assert "toast" in kinds1
    assert "chrome" in kinds1
    assert "voice" not in kinds1
    # second drift within cooldown → toast only (chrome coalesced)
    r2 = f.log_drift("https://instagram.com")
    kinds2 = [n["channel"] for n in r2["nudges"]]
    assert kinds2 == ["toast"]
    # third drift → voice joins
    r3 = f.log_drift("https://netflix.com")
    kinds3 = [n["channel"] for n in r3["nudges"]]
    assert "voice" in kinds3
    # dedupe: same url within 60s → no nudge
    r4 = f.log_drift("https://netflix.com")
    assert r4["nudges"] == []


def test_phone_channel_suppression(db):
    """'stop phone notificaton for focus' → focus.nudge_channels.phone=false."""
    from core.config import cfg
    db.set_setting("focus.nudge_channels.phone", False)
    cfg.set_live_value("focus.nudge_channels.phone", False)
    f = Focus(db)
    f.start(25, allow=[])
    r = f.log_drift("https://x.com")
    assert all(n["channel"] != "phone" for n in r["nudges"])
    db.set_setting("focus.nudge_channels.phone", True)
    cfg.set_live_value("focus.nudge_channels.phone", True)


def test_learned_patterns(db):
    f = Focus(db)
    f.start(25, allow=[])
    for url in ["https://a.com", "https://b.com", "https://c.com"]:
        f.log_drift(url)
    stats = f.stats()
    assert stats["total_drifts"] >= 3
    assert len(stats["sessions"]) >= 1
    assert stats["today"]["count"] >= 1
    assert "top_distraction_domains" in stats["learned"]


def test_focus_completion_nudge(db):
    """Ended session emits a completion nudge (worker path)."""
    from core.worker import Worker
    f = Focus(db)
    f.start(25, allow=[])
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE status='active'",
            (__import__("time").time() - 26 * 60,))
    w = Worker(db)
    w._check_focus_completion(__import__("time").time())
    nudges = db.q("SELECT * FROM nudges WHERE kind='focus' AND channel='chrome' ORDER BY nudge_id DESC LIMIT 1")
    assert nudges and "complete" in nudges[0]["message"].lower()


def test_focus_stats_body_double_fields(db):
    """stats() must power the body-double UI: week totals, streak, best day,
    avg, completed count, recent drift feed with domains."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="build the agent ui")
    # a couple of drifts so the feed has entries
    f.log_drift("https://youtube.com/shorts/abc")
    f.log_drift("https://instagram.com/reel")
    # backdate so the session "lasts" 25 real minutes (tests run in ms)
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE session_id=?",
            (time.time() - 25 * 60, 1))
    f.stop()
    f.start(45, allow=[], task="write tests")
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE session_id=?",
            (time.time() - 45 * 60, 2))
    f.stop()
    st = f.stats()
    assert st["week"]["count"] == 2
    assert st["week"]["minutes"] >= 70
    assert st["streak_days"] >= 1
    assert st["best_day_min"] >= 70
    assert st["avg_min"] >= 35
    assert st["total_completed"] == 2
    assert st["recent_drifts"], "drift feed must be populated"
    assert any("youtube.com" in d["domain"] for d in st["recent_drifts"])
    assert st["nudges_total"] >= 1
    assert "task" in st["sessions"][0]  # task label persisted


def test_focus_start_accepts_task(db):
    """Start endpoint payload carries task + voice through to the session."""
    from core.focus import Focus
    f = Focus(db)
    r = f.start(30, allow=["github.com"], voice=False, task="ship the UI")
    assert r["ok"] and r["task"] == "ship the UI"
    s = f.active()
    assert s["task"] == "ship the UI"
    assert "github.com" in s["allow_domains"]
    f.stop()


def test_focus_break_mode_skips_nudges(db):
    """Pomodoro: during break, drifts must NOT count or nudge (wandering on a
    break is allowed)."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="work", why="then I watch an episode",
            first_step="open the editor")
    r = f.set_mode("break", break_min=5)
    assert r["ok"] and r["mode"] == "break"
    assert r["break_end_ts"] and r["break_end_ts"] > time.time()
    s = f.active()
    assert s["mode"] == "break"
    # drift during break → no nudges, no drift_count bump
    r2 = f.log_drift("https://youtube.com/shorts/abc")
    assert r2.get("on_break") is True
    assert r2["nudges"] == []
    s2 = f.active()
    assert s2["drift_count"] == 0
    # back to work → drifts count again
    f.set_mode("work")
    r3 = f.log_drift("https://youtube.com/shorts/abc")
    assert r3["nudges"], "work mode must nudge again"
    assert f.active()["drift_count"] == 1


def test_focus_start_why_first_step(db):
    """ADHD fields (why/reward + first tiny step) persist on the session."""
    from core.focus import Focus
    f = Focus(db)
    r = f.start(15, allow=[], task="write report", why="then chai + episode",
                first_step="open the doc and write the title")
    assert r["ok"]
    s = f.active()
    assert s["why"] == "then chai + episode"
    assert s["first_step"] == "open the doc and write the title"
    assert s["mode"] == "work"
    f.stop()


def test_active_auto_expires_stale_session(db):
    """A session past its end time must NOT stay 'active' — the worker that
    used to auto-complete sessions crash-loops on some VMs, and every reply
    kept reporting stale '18 minutes left'. active() auto-expires now."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="ai agent build")
    sid = f.active()["session_id"]
    # backdate: session started 3 hours ago → 3h > 25min
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE session_id=?",
            (time.time() - 3 * 3600, sid))
    assert f.active() is None
    row = db.q1("SELECT status, end_ts FROM focus_sessions WHERE session_id=?", (sid,))
    assert row["status"] == "completed"     # 3h elapsed ≥ 80% of 25min
    assert row["end_ts"] is not None
    # starting a new session right after works (no 'already active' error)
    r = f.start(25, allow=[], task="new task")
    assert r["ok"]


def test_now_block_reports_llm_identity(db):
    """<NOW> must carry the exact provider + model so the model can answer
    'which model are you' truthfully instead of hedging."""
    import os as _os
    _os.environ.pop("DEEPSEEK_API_KEY", None)
    _os.environ.pop("GEMINI_API_KEY", None)
    from core.loom import Loom
    nb = Loom(db).now_block()
    assert "llm" in nb
    assert nb["llm"]["provider"] in ("sim", "deepseek", "gemini")
    assert isinstance(nb["llm"]["model"], str)


def test_focus_start_rich_intake(db):
    """Flagship intake: energy, mood, distraction pre-commit persist."""
    from core.focus import Focus
    f = Focus(db)
    r = f.start(30, allow=[], task="write report", why="then chai",
                first_step="open doc", energy=4, mood=5,
                distraction_plan="when I want to check insta, I'll drink water")
    assert r["ok"]
    s = f.active()
    assert s["energy"] == 4 and s["mood"] == 5
    assert "insta" in s["distraction_plan"]
    f.stop()


def test_focus_thought_capture(db):
    """Brain-dump capture appends to the session (working-memory offload)."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="build ui")
    r1 = f.add_thought("reply to mom")
    assert r1["ok"] and "reply to mom" in r1["thoughts"]
    r2 = f.add_thought("buy milk")
    assert r2["thoughts"] == "reply to mom\nbuy milk"
    # no active session → graceful
    f.stop()
    assert f.add_thought("x")["ok"] is False


def test_focus_comeback_and_score(db):
    """Comebacks count; finish() computes the FOCUS SCORE + rich summary."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="agent", why="episode", first_step="open",
            energy=3, mood=4)
    sid = f.active()["session_id"]
    f.add_comeback()
    f.add_comeback()
    # backdate 20 of 25 min → completed, near-full completion
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE session_id=?",
            (time.time() - 20 * 60, sid))
    r = f.finish(energy_after=2, mood_after=5, notes="great session")
    assert r["ok"] and r["status"] == "completed"
    assert 60 <= r["focus_score"] <= 100
    assert r["comebacks"] == 2
    assert r["energy_before"] == 3 and r["mood_after"] == 5
    assert r["task"] == "agent" and "great" in r["notes"]
    row = db.q1("SELECT * FROM focus_sessions WHERE session_id=?", (sid,))
    assert row["focus_score"] == r["focus_score"]
    assert row["status"] == "completed"


def test_focus_stats_flagship_analytics(db):
    """stats() exposes avg/best focus score, series, best hour, energy."""
    from core.focus import Focus
    f = Focus(db)
    f.start(25, allow=[], task="a", energy=4, mood=3)
    db.exec("UPDATE focus_sessions SET start_ts=? WHERE session_id=?",
            (time.time() - 20 * 60, f.active()["session_id"]))
    f.finish(mood_after=4)
    st = f.stats()
    assert st["focus"]["avg_score"] > 0
    assert st["focus"]["best_score"] >= st["focus"]["avg_score"]
    assert st["focus"]["series"], "score series must be non-empty"
    assert st["focus"]["avg_energy"] == 4.0
    assert st["focus"]["total_thoughts"] == 0
    assert "best_hour" in st["focus"]


def test_focus_plan_api(client, db):
    """Today's plan persists per date via settings."""
    r = client.get("/api/focus/plan").json()
    assert r["items"] == []
    r2 = client.post("/api/focus/plan", json={"items": [
        {"text": "write the agent UI", "done": False},
        {"text": "walk", "done": True}]})
    assert r2.status_code == 200 and len(r2.json()["items"]) == 2
    r3 = client.get("/api/focus/plan").json()
    assert r3["items"][0]["text"] == "write the agent UI"
    assert r3["items"][1]["done"] is True
