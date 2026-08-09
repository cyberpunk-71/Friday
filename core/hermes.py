"""HERMES — demoted to a deterministic pre-fire + governor layer (FRIDAY-Δ).

It no longer picks skills. It:
· pre-fires speculative work at t+0 (regex → warm sandbox + speculative search)
· enforces the $ governor (per turn + per day)
· enforces the ASK-BUDGET (max N clarifying questions/day)
· runs the BLAST-RADIUS classifier on actions (ledger escalation rules)
· kills runaway loops (max repair calls, max DAG steps)
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from .config import cfg
from .db import get_db

LIVE_TRIGGERS = re.compile(
    r"\b(search|find|price|cost|weather|dates?|available|open\b|registration|flights?|"
    r"scooter|phone|yatra|book|buy|compare|track|monitor|watch|news|latest|today|"
    r"tomorrow|tonight|events?|happening|upcoming|shows?|concerts?|exhibition|"
    r"festival|garba|movies?|bookmyshow|schedule|what'?s on|near me|"
    r"how much|what's the|research)\b", re.I)

FOCUS_TRIGGERS = re.compile(r"\b(start|begin|stop|end)\b.*\bfoc\w*|foc\w*\b.*\b(min|allow)", re.I)
TASK_TRIGGERS = re.compile(
    r"\b(research|draft|email|write|build|create|organi[sz]e|summar|download|send|"
    r"save|convert|make|generate|buy|purchase|pay|remind|track|monitor)\b", re.I)


@dataclass
class PreFire:
    """Speculative work started at t+0, consumed by the turn if wanted."""
    search: list[dict] = field(default_factory=list)
    sandbox_warm: bool = False
    burned_usd: float = 0.0


class Hermes:
    def __init__(self, db=None, search=None) -> None:
        self.db = db or get_db()
        self.search = search
        self.prefire_state: PreFire | None = None

    # ------------------------------------------------------------------ #
    # pre-fire
    # ------------------------------------------------------------------ #
    async def prefire(self, text: str) -> PreFire:
        pf = PreFire()
        max_usd = cfg.get("hermes.pre_fire_max_usd", 0.0002)
        if not LIVE_TRIGGERS.search(text):
            return pf
        if not self.search:
            return pf
        # cost of one speculative search ≈ $0.0001 (tokens) — burn only if budget allows
        if not self.governor_allows(pf.burned_usd + 0.0001):
            return pf
        try:
            # strip the user's chatty bits; search the core question
            q = self._core_query(text)
            pf.search = await self.search.search(q, 5)
            pf.burned_usd = 0.0001
        except Exception:
            pass
        return pf

    @staticmethod
    def _core_query(text: str) -> str:
        t = re.sub(r"\b(hey|friday|please|pls|can you|could you|i want|i need|keep an eye on)\b",
                   " ", text, flags=re.I)
        t = re.sub(r"\s+", " ", t).strip()
        return t[:160]

    # ------------------------------------------------------------------ #
    # governors
    # ------------------------------------------------------------------ #
    def spend(self, usd: float, kind: str = "llm") -> None:
        today = time.strftime("%Y-%m-%d")
        self.db.set_setting("spend.today_date", today)
        cur = self.db.get_setting("spend.today_usd", 0.0)
        self.db.set_setting("spend.today_usd", cur + usd)
        self.db.set_setting(f"spend.{kind}_usd", self.db.get_setting(f"spend.{kind}_usd", 0.0) + usd)

    def spend_today(self) -> float:
        if self.db.get_setting("spend.today_date") != time.strftime("%Y-%m-%d"):
            self.db.set_setting("spend.today_usd", 0.0)
            self.db.set_setting("spend.today_date", time.strftime("%Y-%m-%d"))
        return self.db.get_setting("spend.today_usd", 0.0)

    def governor_allows(self, extra: float = 0.0) -> bool:
        daily = cfg.get("budget.daily_usd", 6.0)
        return self.spend_today() + extra <= daily

    def ask_budget_left(self) -> int:
        today = time.strftime("%Y-%m-%d")
        if self.db.get_setting("ask.used_date") != today:
            self.db.set_setting("ask.used_date", today)
            self.db.set_setting("ask.used_today", 0)
        return max(0, cfg.get("hermes.ask_budget_per_day", 2) - int(self.db.get_setting("ask.used_today", 0)))

    def ask_used(self) -> None:
        self.db.set_setting("ask.used_date", time.strftime("%Y-%m-%d"))
        self.db.set_setting("ask.used_today", int(self.db.get_setting("ask.used_today", 0)) + 1)

    # ------------------------------------------------------------------ #
    # blast-radius classifier (ledger escalation)
    # ------------------------------------------------------------------ #
    def classify(self, action: dict) -> dict:
        """action = {action: str, files?, recipients?, amount?, path?, dom_has?[]}
        Returns {class: safe|reversible|blocking, reason}."""
        rules = cfg.get("hermes.blast_radius", {})
        a = action.get("action", "")

        if a in ("payment",) or action.get("amount", 0) or action.get("money", False):
            return {"class": "blocking", "reason": "any money is blocking"}
        if a == "email_send":
            ext = action.get("recipients_ext", 0)
            if ext > cfg.get("hermes.blast_radius.max_ext_recipients", 5):
                return {"class": "blocking", "reason": f"{ext} external recipients"}
            return {"class": "blocking", "reason": "gmail send is a blocking gate"}
        if a == "credential_read":
            return {"class": "blocking", "reason": "credential read"}
        if a in ("browser_write",) and any(
                f in str(action.get("dom_has", [])) for f in rules.get("dom_sensitive_fields", [])):
            return {"class": "blocking", "reason": "DOM has card/password field"}
        files = action.get("files", 1)
        if files > cfg.get("hermes.blast_radius.max_files", 20):
            return {"class": "blocking", "reason": f"{files} files > blast radius"}
        if a == "fs_delete":
            return {"class": "reversible", "reason": "moved to .friday-trash (30d TTL)"}
        if a == "git_push" and action.get("force"):
            return {"class": "blocking", "reason": "force push"}
        return {"class": "reversible" if action.get("writes", False) else "safe",
                "reason": ""}

    # ------------------------------------------------------------------ #
    # runaway-loop killer
    # ------------------------------------------------------------------ #
    def runaway_check(self, task_id: int, step_index: int, repairs: int) -> bool:
        if repairs > 2:
            return True
        if step_index > cfg.get("hermes.max_dag_steps", 24):
            return True
        return False
