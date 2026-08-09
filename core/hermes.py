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
    r"who is|who'?s|who are|current|mayor|minister|president|pm\b|cm\b|"
    r"how much|what's the|research)\b", re.I)

FOCUS_TRIGGERS = re.compile(r"\b(start|begin|stop|end)\b.*\bfoc\w*|foc\w*\b.*\b(min|allow)", re.I)
TASK_TRIGGERS = re.compile(
    r"\b(research|draft|email|write|build|create|organi[sz]e|summar|download|send|"
    r"save|convert|make|generate|buy|purchase|pay|remind|track|monitor)\b", re.I)


@dataclass
class PreFire:
    """Speculative work started at t+0, consumed by the turn if wanted."""
    search: list[dict] = field(default_factory=list)
    web: str = ""               # web-read snippet (e.g. BookMyShow events page)
    sandbox_warm: bool = False
    burned_usd: float = 0.0


# typos / aliases for city names — "ahemedbad" must still search ahmedabad
CITY_FIX = {
    "ahemedbad": "ahmedabad", "ahmedbad": "ahmedabad", "amdavad": "ahmedabad",
    "gandhinagar": "gandhinagar", "hydrabad": "hyderabad", "banglore": "bangalore",
    "bengaluru": "bangalore", "delhi": "delhi", "mumbai": "mumbai", "pune": "pune",
}
EVENT_HINTS = re.compile(
    r"\b(events?|concerts?|shows?|movies?|exhibition|festival|garba|dandiya|"
    r"book ?my ?show|bookmyshow|what'?s on|happening|upcoming|nightlife)\b", re.I)


class Hermes:
    def __init__(self, db=None, search=None) -> None:
        self.db = db or get_db()
        self.search = search
        self.prefire_state: PreFire | None = None

    # ------------------------------------------------------------------ #
    # pre-fire
    # ------------------------------------------------------------------ #
    async def prefire(self, text: str, city: str = "ahmedabad") -> PreFire:
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
            q = self._core_query(text, city)
            # tiny follow-ups ("search", "yes", "go") → reuse the last real
            # question as the query so the search is actually meaningful
            if len(q) < 6 or q.lower() in ("search", "yes", "go", "ok", "run",
                                            "look", "find", "events"):
                last = self.db.q1(
                    "SELECT user_text FROM turns ORDER BY turn_id DESC LIMIT 1")
                if last and last["user_text"]:
                    q = self._core_query(last["user_text"], city)
            pf.search = await self.search.search(q, 5)
            pf.burned_usd = 0.0001
        except Exception:
            pass
        low = text.lower()
        # events/movies/BookMyShow → also fetch the city's BMS events page so
        # the model has REAL listings, not just news headlines
        if EVENT_HINTS.search(low):
            try:
                from .providers import web_read
                pf.web = await web_read(f"https://in.bookmyshow.com/{city}/events", 6000)
            except Exception:
                pass
            if not pf.web:
                try:
                    from .providers import web_read
                    pf.web = await web_read(f"https://www.bookmyshow.com/{city}/events", 6000)
                except Exception:
                    pass
        # who-is / current-office-holder questions → Wikipedia via jina
        # (jina is confirmed reachable from the VM), so "mayor of ahmedabad"
        # gets a REAL answer instead of "search came back empty"
        if re.search(r"\b(who is|who's|current|mayor|minister|president|cm\b|pm\b|"
                     r"chief minister|finance minister)\b", low) and re.search(r"\b(who|current)\b", low):
            q2 = self._core_query(text, city).replace("current ", "").replace("who is ", "").strip()
            try:
                from .providers import web_read
                import urllib.parse
                slug = urllib.parse.quote(q2.replace(" ", "_"))
                pf.web = await web_read(f"https://en.wikipedia.org/wiki/{slug}", 4000)
            except Exception:
                pass
        return pf

    @staticmethod
    def _fix_cities(t: str) -> str:
        """Fuzzy city fix: any token close to a known Indian city gets
        normalized — catches 'ahemedbad', 'ahmedaabd', 'amdavad', 'banglore'."""
        import difflib
        CITIES = ["ahmedabad", "gandhinagar", "mumbai", "delhi", "pune",
                  "hyderabad", "bangalore", "chennai", "kolkata", "jaipur",
                  "surat", "vadodara", "goa", "indore", "lucknow", "kerala"]
        words = re.findall(r"[a-z]{4,}", t.lower())
        for w in set(words):
            close = difflib.get_close_matches(w, CITIES, n=1, cutoff=0.72)
            if close:
                t = re.sub(rf"\b{w}\b", close[0], t, flags=re.I)
        for bad, good in CITY_FIX.items():
            if bad in t.lower():
                t = re.sub(rf"\b{bad}\b", good, t, flags=re.I)
        return t

    @staticmethod
    def _core_query(text: str, city: str = "ahmedabad") -> str:
        t = re.sub(r"\b(hey|friday|please|pls|can you|could you|i want|i need|keep an eye on|"
                   r"search (on|for|up)|look (for|up)|tell me|find me|show me|suggest me|"
                   r"who is|who's|what is|what are|whats|what'?s|current|the|for|"
                   r"any|some|me|about|with)\b",
                   " ", text, flags=re.I)
        # time words don't help the news query — but we DO want the actual
        # date, so translate tomorrow/today/this weekend into a real date
        tlow0 = t.lower()
        date_hint = ""
        import datetime
        now = datetime.date.today()
        if re.search(r"\btomorrow\b", tlow0):
            date_hint = (now + datetime.timedelta(days=1)).strftime("%d %B %Y")
        elif re.search(r"\b(today|tonight)\b", tlow0):
            date_hint = now.strftime("%d %B %Y")
        elif re.search(r"this weekend", tlow0):
            # next Saturday
            days = (5 - now.weekday()) % 7 or 7
            date_hint = (now + datetime.timedelta(days=days)).strftime("%d %B %Y")
        t = re.sub(r"\b(today|tonight|tomorrow|this weekend|this week|right now|"
                   r"happening|going on|available|listings?|please|pls|now)\b",
                   " ", t, flags=re.I)
        # fix city typos/aliases (fuzzy)
        t = Hermes._fix_cities(t)
        t = re.sub(r"\s+", " ", t).strip()
        # city-less event/local queries get the home city appended so the
        # search is meaningful ("good shows" → "good shows in ahmedabad")
        tlow = t.lower()
        has_city = any(c in tlow for c in ("ahmedabad", "gandhinagar", "mumbai", "delhi",
                                           "pune", "hyderabad", "bangalore", "chennai",
                                           "kolkata", "jaipur", "surat", "goa"))
        if not has_city and EVENT_HINTS.search(t):
            t = f"{t} in {city}".strip()
        if date_hint:
            t = f"{t} {date_hint}".strip()
        return t[:180]

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
