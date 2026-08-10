"""Observability: correlation ids + turn audit. Guarantees 100% explainability
for every response and memory update (the Anima `core/obs.py` equivalent)."""
from __future__ import annotations

import time
import uuid

from .db import get_db


class Tracer:
    def __init__(self) -> None:
        self.corr_id: str = ""
        self.start: float = time.time()

    @classmethod
    def new(cls) -> "Tracer":
        t = cls()
        t.corr_id = f"cor_{uuid.uuid4().hex[:8]}"
        return t

    def elapsed_ms(self) -> int:
        return int((time.time() - self.start) * 1000)


def audit(task_id: int | None, step_id: int | None, event_type: str, details: dict) -> int:
    db = get_db()
    return db.exec(
        "INSERT INTO task_audit_events(task_id,step_id,event_type,details,created_at)"
        " VALUES(?,?,?,?,?)",
        (task_id, step_id, event_type,
         __import__("json").dumps(details, ensure_ascii=False, default=str), time.time()))


def log_turn(corr_id: str, user_text: str, reply: str | None, latency_ms: int,
             cost_usd: float, model: str, provider: str, slots: list | None,
             outcome: str | None = None) -> None:
    db = get_db()
    db.exec(
        "INSERT INTO turns(corr_id,user_text,reply,latency_ms,cost_usd,model,provider,slots_json,outcome,created_ts)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (corr_id, user_text, reply, latency_ms, cost_usd, model, provider,
         __import__("json").dumps(slots or [], ensure_ascii=False), outcome, time.time()))
