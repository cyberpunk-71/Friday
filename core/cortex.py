"""CORTEX — the 3-phase, one-pass pipeline (FRIDAY-Δ).

  SENSE  (0 LLM · ~18ms): retrieval walk + NOW-block + Hermes pre-fire
  SPEAK  (1 LLM · streaming): ⟨CTRL⟩ tokens 1..40 parsed incrementally →
         side-effects fire while prose still streams (dark mode at +120ms)
  SETTLE (async · invisible): river materialization, extraction, affect,
         nudges, spend ledger

No 7 stages. No second opinion. No tool router.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import AsyncIterator

from .config import cfg
from .db import get_db
from .embedder import Embedder
from .extract import HeuristicExtractor, LLMExtractor
from .hands import Hands
from .hermes import Hermes
from .loom import Loom
from .obs import Tracer, log_turn
from .providers import LLMProvider, make_llm, make_search
from .psyche import Heart, Psyche
from .river import River

# --------------------------------------------------------------------------- #
# ⟨CTRL⟩ block parsing — incremental, never blocks prose
# --------------------------------------------------------------------------- #
CTRL_RE = re.compile(r"^\s*\{.*?\}\s*", re.S)


def parse_ctrl(accumulated: str) -> tuple[dict | None, str]:
    """Try to parse the leading JSON object (the ⟨CTRL⟩ block).
    Returns (ctrl_dict_or_None, remaining_text). Greedy-balanced scan."""
    if not accumulated.lstrip().startswith("{"):
        return None, accumulated
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(accumulated):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                block = accumulated[: i + 1]
                try:
                    obj = json.loads(block)
                    if "ctrl" in obj:
                        return obj["ctrl"], accumulated[i + 1:]
                except json.JSONDecodeError:
                    return None, accumulated
                return None, accumulated
    return None, accumulated


def strip_card_tags(prose: str) -> tuple[str, list[dict]]:
    """Extract <card>...</card> blocks from prose; return (clean_prose, cards)."""
    cards = []
    def _pull(m):
        try:
            cards.append(json.loads(m.group(1)))
        except Exception:
            pass
        return ""
    clean = re.sub(r"<card>(.*?)</card>", _pull, prose, flags=re.S)
    return clean, cards


# --------------------------------------------------------------------------- #
# CORTEX
# --------------------------------------------------------------------------- #
class Cortex:
    def __init__(self, db=None, llm: LLMProvider | None = None, search=None) -> None:
        self.db = db or get_db()
        self.river = River(self.db)
        self.loom = Loom(self.db)
        self.hermes = Hermes(self.db, search)
        self.psyche = Psyche(self.db)
        self.heart = Heart(self.db)
        self.llm = llm or make_llm()
        self.search = search or make_search()
        # DeepSeek is the default for EVERYTHING when a key is configured:
        # SETTLE extraction, task planning, and repair all use the same model.
        if self.llm.name == "deepseek":
            self.river.extractor = LLMExtractor(self.llm)
        self.hands = Hands(self.db, self.search, None if self.llm.name == "sim" else self.llm)
        self._jobs: dict[str, asyncio.Task] = {}

    # ------------------------------------------------------------------ #
    # SENSE
    # ------------------------------------------------------------------ #
    def _sense(self, text: str, book_id: int | None = None) -> dict:
        t0 = time.time()
        q = self._typo_expand(text)
        recall = self.loom.recall(q, scope="global" if not book_id else f"book:{book_id}")
        now = self.loom.now_block()
        recall["now"] = now
        recall["sense_ms"] = int((time.time() - t0) * 1000)
        return recall

    @staticmethod
    def _typo_expand(text: str) -> str:
        """SymSpell-ish expansion for the RETRIEVAL query only — raw text goes
        to the model untouched. Never touches proper nouns."""
        db = get_db()
        vocab: set[str] = set()
        for a in db.q("SELECT text FROM atoms WHERE status='active' LIMIT 2000"):
            vocab.update(re.findall(r"[a-z]{5,}", a["text"].lower()))
        if not vocab:
            return text
        words = re.findall(r"[A-Za-z]{5,}", text)
        out_words = []
        for w in words:
            if w[0].isupper() or w.lower() in vocab or len(w) < 6:
                out_words.append(w)
                continue
            lw = w.lower()
            best, bd = w, 3
            for v in vocab:
                if abs(len(v) - len(lw)) > 2:
                    continue
                d = _edit_dist(lw, v)
                if d < bd:
                    best, bd = v, d
            out_words.append(best if bd <= 1 else w)
        i = 0
        result = text
        for w in words:
            r = re.search(rf"\b{w}\b", result)
            if r:
                result = result[: r.start()] + out_words[i] + result[r.end():]
            i += 1
        return result

    # ------------------------------------------------------------------ #
    # SPEAK
    # ------------------------------------------------------------------ #
    def _system_prompt(self, sense: dict) -> str:
        self_md = self._genome_text("prompts/self.md")
        verbs = self._genome_verbs()
        style = self._genome_text("policies/style.yaml")
        slots_json = json.dumps(sense["slots"], ensure_ascii=False)
        now_json = json.dumps(sense["now"], ensure_ascii=False)
        constraints = json.dumps(sense["constraints"], ensure_ascii=False)
        prefire = json.dumps(self.hermes.prefire_state.search if self.hermes.prefire_state else [],
                             ensure_ascii=False)
        posture = self.psyche.posture()
        return (
            f"{self_md}\n\n## 12 VERBS (context, always cached)\n{verbs}\n\n"
            f"## Style genome\n{style}\n\n"
            f"## Posture: {posture['name']}\n{posture['directive']}\n\n"
            f"## <SLOTS>{slots_json}</SLOTS>\n"
            f"## <NOW>{now_json}</NOW>\n"
            f"## CONSTRAINT LEDGER\n{constraints}\n"
            f"## SPECULATIVE PRE-FIRE (may be stale; verify before citing)\n{prefire}")

    def _genome_text(self, rel: str) -> str:
        p = cfg.genome_path(rel)
        if p.exists():
            return p.read_text(encoding="utf-8")
        return ""

    def _genome_verbs(self) -> str:
        vdir = cfg.genome_path("verbs")
        if not vdir.exists():
            return ""
        parts = []
        for f in sorted(vdir.glob("*.md")):
            parts.append(f.read_text(encoding="utf-8"))
        return "\n\n".join(parts)

    async def _stream(self, text: str, sense: dict, corr_id: str,
                      book_id: int | None = None) -> AsyncIterator[dict]:
        sys_p = self._system_prompt(sense)
        messages = [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": text[:4000]},
        ]
        buffer, ctrl, prose_started = "", None, False
        try:
            async for chunk in self.llm.stream(
                    messages, temperature=cfg.get("speak.temperature", 0.6),
                    max_tokens=cfg.get("speak.max_turn_tokens", 1200)):
                if ctrl is None and not prose_started:
                    buffer += chunk
                    parsed, rest = parse_ctrl(buffer)
                    if parsed is not None:
                        ctrl = parsed
                        buffer = rest
                        yield {"type": "ctrl", "ctrl": ctrl}
                        await self._fire_ctrl(ctrl, corr_id, text, book_id)
                        if rest:
                            prose_started = True
                            yield {"type": "delta", "text": rest}
                            buffer = ""
                    elif len(buffer) > 4000:
                        # no CTRL block — treat everything as prose
                        prose_started = True
                        yield {"type": "delta", "text": buffer}
                        buffer = ""
                    continue
                prose_started = True
                if buffer:
                    yield {"type": "delta", "text": buffer}
                    buffer = ""
                yield {"type": "delta", "text": chunk}
        except RuntimeError as e:
            # network unavailable — degrade to sim provider mid-turn
            self.llm = SimProviderFallback()
            async for chunk in self.llm.stream(messages):
                yield {"type": "delta", "text": chunk}

        if buffer:
            yield {"type": "delta", "text": buffer}
        if ctrl is None:
            ctrl = {"depth": 0.1, "tooliness": 0.0, "emotionality": 0.0,
                    "novelty": 0.3, "stakes": 0.0, "config_deltas": {},
                    "memory_writes": [], "code_intent": False, "ask": []}
            yield {"type": "ctrl", "ctrl": ctrl}
            await self._fire_ctrl(ctrl, corr_id, text, book_id)
        yield {"type": "end_ctrl", "ctrl": ctrl}

    # ------------------------------------------------------------------ #
    # ⟨CTRL⟩ side-effects — fire BEFORE prose finishes
    # ------------------------------------------------------------------ #
    async def _fire_ctrl(self, ctrl: dict, corr_id: str, text: str, book_id: int | None) -> None:
        # config deltas → live settings (e.g. dark mode at first-token+120ms)
        for key, value in (ctrl.get("config_deltas") or {}).items():
            self.db.set_setting(key, value)
            cfg.set_live_value(key, value)

        # memory writes → river (inference weight 0.3 — correction supremacy
        # guarantees user weight 10 always outranks these)
        for w in ctrl.get("memory_writes") or []:
            kind = w.get("kind", "fact")
            self.river.record("memory_write", "friday",
                              {"atom": {"kind": kind, "text": w.get("text", ""),
                                        "importance": w.get("importance", 0.3),
                                        "entities": w.get("entities", []),
                                        "scope": f"book:{book_id}" if book_id else "global"}},
                              source_weight=cfg.get("beliefs.inference_weight", 0.3),
                              corr_id=corr_id)

        # asks — subject to ASK-BUDGET
        asks = ctrl.get("ask") or []
        if asks and self.hermes.ask_budget_left() > 0:
            self.hermes.ask_used()
            q = asks[0] if isinstance(asks[0], str) else asks[0].get("q", str(asks[0]))
            await bus.emit(corr_id, {"type": "ask", "question": q})

        # code intent → FORGE DAG, never blocks chat
        if ctrl.get("code_intent"):
            self._spawn_task(corr_id, text, book_id)

    def _spawn_task(self, corr_id: str, text: str, book_id: int | None) -> None:
        """Launch a background DAG job; progress streams via SSE bus."""
        job = asyncio.create_task(self._run_task_job(corr_id, text, book_id))
        self._jobs[corr_id] = job

    async def _run_task_job(self, corr_id: str, text: str, book_id: int | None) -> None:
        tid = self.hands.create_task(text[:200], text, corr_id=corr_id)
        await self._emit(corr_id, {"type": "task", "task_id": tid, "status": "queued",
                                   "title": text[:120]})
        steps = await self.hands.plan_task(tid, text, {"corr_id": corr_id})
        await self._emit(corr_id, {"type": "task", "task_id": tid, "status": "running",
                                   "steps": len(steps)})
        async for ev in self.hands.execute(tid, corr_id):
            ev["task_id"] = ev.get("task_id", tid)
            await self._emit(corr_id, {"type": "task_event", **ev})

    async def _emit(self, corr_id: str, ev: dict) -> None:
        await bus.emit(corr_id, ev)

    # ------------------------------------------------------------------ #
    # SETTLE
    # ------------------------------------------------------------------ #
    def _settle(self, text: str, corr_id: str, book_id: int | None, sense: dict,
                ctrl: dict, reply: str, latency_ms: int, cost_usd: float,
                outcome: str | None = None) -> None:
        # affect
        self.heart.record(text)

        # user utterance → river (extractor materializes atoms at SETTLE)
        ev_kind = "correction" if self._is_correction(text) else "utterance"
        self.river.record(ev_kind, "user", {"text": text}, 1.0, corr_id=corr_id)

        # materialize incrementally
        self.river.materialize()

        # spend
        self.hermes.spend(cost_usd)

        # turn audit (outcome for the Gym)
        log_turn(corr_id, text, reply[:4000], latency_ms, cost_usd,
                 self.llm.name, "deepseek" if self.llm.name == "deepseek" else "sim",
                 sense["slots"], outcome)

    @staticmethod
    def _is_correction(text: str) -> bool:
        low = text.lower().strip()
        pats = [r"^(actually|wait|no|correction|hold on)[,!. ]",
                r"that'?s (not )?right", r"you (were )?wrong", r"i (said|meant) ",
                r"\b(actually|don'?t|not|never|stop|wrong|instead)\b.{0,60}"]
        if not re.search(r"\b(focus|dark|light|nudge|hinglish)\b", low):
            return any(re.search(p, low) for p in pats)
        return False

    # ------------------------------------------------------------------ #
    # deterministic ingress handlers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _provider_key(text: str) -> dict | None:
        """'this is my new deep seek api kes sk-9f3c... for research use case'"""
        m = re.search(r"\b(sk-[A-Za-z0-9_\-]{6,})\b", text)
        if not m:
            return None
        low = text.lower()
        key = m.group(1)
        scope = "default"
        for s in ("research", "voice", "search", "default"):
            if s in low:
                scope = s
                break
        provider = "voice" if "voice" in low and "deep" not in low else "deepseek"
        db = get_db()
        db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
                " VALUES(?,?,?,1,'chat',?,?) "
                "ON CONFLICT(provider,scope) DO UPDATE SET api_key=excluded.api_key,"
                " source='chat', updated_ts=excluded.updated_ts",
                (provider, scope, key, time.time(), time.time()))
        return {"scope": scope, "masked": f"••••{key[-4:]}", "provider": provider,
                "message": f"Got it — {scope} API key updated to ••••{key[-4:]}."}

    def _focus_ingress(self, text: str) -> dict | None:
        low = text.lower()
        from .focus import Focus
        f = Focus(self.db)
        m = re.search(r"(?:start|begin)\s+foc\w*\s*(\d{1,3})\s*m", low)
        if m:
            minutes = min(180, max(1, int(m.group(1))))
            allow = re.search(r"allow(?:ing)?\s+([a-z0-9.\-]+)", low)
            allow_list = [allow.group(1)] if allow else []
            r = f.start(minutes, allow_list, voice=True)
            if r.get("ok"):
                msg = (f"Focus started for {minutes} minutes" +
                       (f", allowing {allow_list[0]}" if allow_list else "") +
                       " — I'll nudge you if you drift.")
                return {"message": msg, "session_id": r["session_id"],
                        "minutes": minutes, "action": "start"}
            return {"message": "A focus session is already active.", "action": "start_error"}
        if re.search(r"(?:stop|end)\s+(?:the\s+)?foc\w*", low):
            r = f.stop()
            if r.get("ok"):
                return {"message": f"Focus stopped after {r['elapsed_min']} min ({r['status']}).",
                        "action": "stop", "session_id": r["session_id"]}
            return {"message": "No active focus session.", "action": "stop_error"}
        return None

    def _reminder_ingress(self, text: str) -> dict | None:
        low = text.lower()
        # missed-reminder correction → dual-channel reminder tomorrow 08:45
        if re.search(r"(did ?not|missed|forgot).{0,30}(remind|reminder)", low):
            m = re.search(r"at\s+(\d{1,2})[:.]?(\d{2})?", low)
            import datetime as _dt
            tomorrow = _dt.date.today() + _dt.timedelta(days=1)
            due = _dt.datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 45).timestamp()
            rid = self.db.exec(
                "INSERT INTO reminders(text,due_ts,status,channels,created_ts) VALUES(?,?, 'pending',?,?)",
                ("Reminder: " + text[:120], due, json.dumps(["chrome", "chat"]), time.time()))
            self.db.append_event("reminder", "friday",
                                 {"reminder_id": rid, "text": text[:200], "missed": True}, 1.0)
            return {"id": rid,
                    "message": "Fixed — dual-channel reminder (chrome + chat) set for tomorrow 08:45, and I've logged today's miss so it doesn't happen again."}
        # "remind me <X> at HH:MM" → parse time
        m = re.search(r"remind (?:me )?(?:to )?(.+?)(?: at |@)(\d{1,2})[:.]?(\d{2})?", low)
        if m:
            what = m.group(1).strip()[:140]
            hh, mm = int(m.group(2)), int(m.group(3) or 0)
            import datetime as _dt
            now = _dt.datetime.now()
            due_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if due_dt < now:
                due_dt += _dt.timedelta(days=1)
            rid = self.db.exec(
                "INSERT INTO reminders(text,due_ts,status,channels,created_ts) VALUES(?,?, 'pending',?,?)",
                (what, due_dt.timestamp(), json.dumps(["chrome", "chat"]), time.time()))
            self.db.append_event("reminder", "friday", {"reminder_id": rid, "text": what}, 1.0)
            return {"id": rid,
                    "message": f"Reminder set: {what} at {hh:02d}:{mm:02d} (chrome + chat)."}
        return None

    # ------------------------------------------------------------------ #
    # public turn API
    # ------------------------------------------------------------------ #
    async def turn(self, text: str, book_id: int | None = None,
                   meta: dict | None = None) -> AsyncIterator[dict]:
        meta = meta or {}
        tracer = Tracer.new()
        corr_id = meta.get("corr_id", tracer.corr_id)

        # ---- deterministic ingress side-effects (0 LLM, fire at t+0) ----
        # scoped API keys via chat — security-sensitive, never model-dependent
        key_event = self._provider_key(text)
        if key_event:
            yield {"type": "ctrl", "ctrl": {"depth": 0.1, "tooliness": 0.0,
                                            "emotionality": 0.0, "novelty": 0.2,
                                            "stakes": 0.0, "config_deltas": {},
                                            "memory_writes": [], "code_intent": False,
                                            "ask": []}}
            yield {"type": "delta", "text": key_event["message"]}
            yield {"type": "card", "card": {"type": "provider_key",
                                            "scope": key_event["scope"],
                                            "masked": key_event["masked"]}}
            yield {"type": "done", "reply": key_event["message"], "latency_ms": 1,
                   "cost_usd": 0.0, "corr_id": corr_id, "model": "deterministic",
                   "slots_used": 0}
            return
        # focus start/stop — deterministic, sub-50ms
        focus_event = self._focus_ingress(text)
        if focus_event:
            yield {"type": "ctrl", "ctrl": {"depth": 0.1, "tooliness": 0.0,
                                            "emotionality": 0.0, "novelty": 0.2,
                                            "stakes": 0.0, "config_deltas": {},
                                            "memory_writes": [], "code_intent": False,
                                            "ask": []}}
            yield {"type": "delta", "text": focus_event["message"]}
            yield {"type": "card", "card": {"type": "focus", **focus_event}}
            yield {"type": "done", "reply": focus_event["message"], "latency_ms": 2,
                   "cost_usd": 0.0, "corr_id": corr_id, "model": "deterministic",
                   "slots_used": 0}
            return
        # reminder creation — deterministic so "remind me at 14:00" never misses
        reminder_event = self._reminder_ingress(text)
        if reminder_event:
            yield {"type": "delta", "text": reminder_event["message"]}
            yield {"type": "card", "card": {"type": "reminder",
                                            "reminder_id": reminder_event["id"]}}
            yield {"type": "done", "reply": reminder_event["message"], "latency_ms": 2,
                   "cost_usd": 0.0, "corr_id": corr_id, "model": "deterministic",
                   "slots_used": 0}
            return

        # ---- SENSE ----
        sense = await asyncio.to_thread(self._sense, text, book_id)
        self.hermes.prefire_state = await self.hermes.prefire(text)
        sense["prefire"] = self.hermes.prefire_state
        yield {"type": "sense", "slots": sense["slots"], "confidence": sense["confidence"],
               "sense_ms": sense["sense_ms"], "now": sense["now"],
               "constraints": sense["constraints"]}

        # ---- SPEAK ----
        reply_parts: list[str] = []
        ctrl: dict = {}
        cards: list[dict] = []
        async for ev in self._stream(text, sense, corr_id, book_id):
            if ev["type"] == "delta":
                reply_parts.append(ev["text"])
            elif ev["type"] == "ctrl":
                ctrl = ev["ctrl"]
            yield ev

        reply = "".join(reply_parts)
        reply, inline_cards = strip_card_tags(reply)
        cards.extend(inline_cards)

        # generative UI cards (deterministic layer, from ctrl + slots)
        for card in self._cards_from_ctrl(ctrl, sense, reply):
            cards.append(card)

        # ---- SETTLE ----
        latency = tracer.elapsed_ms()
        cost = self._estimate_cost(len(sys_text := self._system_prompt(sense)), len(reply))
        outcome = self._outcome(text, ctrl)
        await asyncio.to_thread(self._settle, text, corr_id, book_id, sense,
                                ctrl, reply, latency, cost, outcome)
        for card in cards:
            yield {"type": "card", "card": card}
        yield {"type": "done", "reply": reply, "latency_ms": latency,
               "cost_usd": cost, "corr_id": corr_id, "outcome": outcome,
               "model": self.llm.name, "slots_used": len(sense["slots"])}

    # ------------------------------------------------------------------ #
    # cards + misc
    # ------------------------------------------------------------------ #
    def _cards_from_ctrl(self, ctrl: dict, sense: dict, reply: str) -> list[dict]:
        cards: list[dict] = []
        deltas = ctrl.get("config_deltas") or {}
        if "ui.theme" in deltas:
            cards.append({"type": "theme", "theme": deltas["ui.theme"]})
        if ctrl.get("code_intent"):
            cards.append({"type": "task_chip", "jobs": 1})
        if ctrl.get("stakes", 0) >= 0.7:
            cards.append({"type": "approval_hint", "stakes": ctrl.get("stakes")})
        if sense["now"].get("pending_approvals"):
            cards.append({"type": "approvals",
                          "items": sense["now"]["pending_approvals"]})
        if sense.get("prefire") and sense["prefire"].search:
            cards.append({"type": "prefire", "n": len(sense["prefire"].search)})
        if ctrl.get("ask"):
            cards.append({"type": "ask", "question": ctrl["ask"][0]})
        return cards

    @staticmethod
    def _estimate_cost(sys_tokens: int, out_chars: int) -> float:
        return (sys_tokens / 4) / 1e6 * 0.014 + (out_chars / 4) / 1e6 * 1.10

    @staticmethod
    def _outcome(text: str, ctrl: dict) -> str | None:
        low = text.lower()
        if any(k in low for k in ("correct", "wrong", "actually", "stop", "don't like", "not ")):
            return "corrected"
        if "?" not in text and len(text) < 60 and ctrl.get("depth", 0) < 0.3:
            return None
        return None


class SimProviderFallback:
    """Lets the cortex degrade to the deterministic provider mid-flight."""
    name = "sim-fallback"

    def __init__(self) -> None:
        from .providers import SimProvider
        self._inner = SimProvider()

    async def stream(self, messages, **kw):
        yield self._inner._render(messages)


def _edit_dist(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 3:
        return 9
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


# --------------------------------------------------------------------------- #
# SSE emission bus (chat never blocks on background jobs)
# --------------------------------------------------------------------------- #
class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set] = {}

    def subscribe(self, corr_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subs.setdefault(corr_id, set()).add(q)
        return q

    def unsubscribe(self, corr_id: str, q: asyncio.Queue) -> None:
        self._subs.get(corr_id, set()).discard(q)

    async def emit(self, corr_id: str, ev: dict) -> None:
        for q in list(self._subs.get(corr_id, set())):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass


bus = EventBus()
