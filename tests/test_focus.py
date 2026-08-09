"""FOCUS — sessions, drift nudges with mandatory coalescing, learned patterns,
per-kind phone suppression."""
from __future__ import annotations

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
