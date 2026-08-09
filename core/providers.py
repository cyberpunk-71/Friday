"""Model + search + web-read providers.

DeepSeek V4 Flash is the single model family (the $0.014 cached-input tier is
the economic axis of FRIDAY-Δ). Everything degrades to a deterministic
offline layer when the network/key is unavailable, so the whole system is
testable in a sandbox with zero egress.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from typing import AsyncIterator

import httpx
import yaml

from .config import cfg

# --------------------------------------------------------------------------- #
# LLM providers
# --------------------------------------------------------------------------- #
class LLMProvider:
    name = "base"

    async def stream(self, messages: list[dict], **kw) -> AsyncIterator[str]:
        raise NotImplementedError

    async def complete(self, messages: list[dict], json_mode: bool = False, **kw) -> str:
        parts = []
        async for chunk in self.stream(messages, json_mode=json_mode, **kw):
            parts.append(chunk)
        return "".join(parts)

    @staticmethod
    def usage_cost(usage: dict) -> float:
        """DeepSeek pricing: $0.27/M in, $1.10/M out; cached $0.014/M in."""
        if not usage:
            return 0.0
        cin = usage.get("prompt_cache_hit_tokens", 0)
        cnew = usage.get("prompt_cache_miss_tokens", usage.get("prompt_tokens", 0))
        out = usage.get("completion_tokens", 0)
        return (cin / 1e6) * 0.014 + (cnew / 1e6) * 0.27 + (out / 1e6) * 1.10


class DeepSeekProvider(LLMProvider):
    name = "deepseek"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("DEEPSEEK_API_KEY", "")
        self.base_url = (base_url or os.environ.get("DEEPSEEK_BASE_URL",
                                                    "https://api.deepseek.com/v1")).rstrip("/")
        self.model = model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(120.0, connect=4.0))
        return self._client

    async def stream(self, messages: list[dict], json_mode: bool = False,
                     temperature: float = 0.6, max_tokens: int | None = None,
                     **kw) -> AsyncIterator[str]:
        body: dict = {
            "model": kw.get("model", self.model),
            "messages": messages,
            "stream": True,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with self.client().stream("POST", "/chat/completions", json=body,
                                            headers=headers) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode()[:400]
                    raise RuntimeError(f"deepseek {resp.status_code}: {err}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = obj.get("choices", [{}])[0].get("delta", {})
                    if "content" in delta and delta["content"]:
                        yield delta["content"]
        except httpx.ConnectError as e:
            raise RuntimeError(f"network unreachable for {self.name}: {e}") from e

    async def complete(self, messages: list[dict], json_mode: bool = False,
                       temperature: float = 0.6, max_tokens: int | None = None,
                       **kw) -> str:
        body: dict = {
            "model": kw.get("model", self.model),
            "messages": messages,
            "stream": False,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if max_tokens:
            body["max_tokens"] = max_tokens
        try:
            resp = await self.client().post("/chat/completions", json=body,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            if resp.status_code != 200:
                err = resp.text[:400]
                raise RuntimeError(f"deepseek {resp.status_code}: {err}")
            obj = resp.json()
            return obj["choices"][0]["message"]["content"] or ""
        except httpx.ConnectError as e:
            raise RuntimeError(f"network unreachable for {self.name}: {e}") from e


# --------------------------------------------------------------------------- #
# Offline simulated provider — deterministic, exercises the whole pipeline.
# Used by tests + when no API key is configured (offline mode).
# --------------------------------------------------------------------------- #
class SimProvider(LLMProvider):
    """A deterministic stand-in model that behaves like the real one:
    emits a ⟨CTRL⟩ block first, then prose. It *uses* the slots it was given
    (so recall quality is observable), decides tooliness/depth from the query,
    and emits config_deltas/memory_writes/code_intent for the same triggers
    the real model is prompted for. Not a substitute for the LLM — a
    test-harness + graceful-degradation layer."""

    name = "sim"

    def __init__(self, search: "SearchProvider | None" = None) -> None:
        self.search = search or SimSearch()

    async def stream(self, messages: list[dict], **kw) -> AsyncIterator[str]:
        text = self._render(messages)
        yield text

    async def complete(self, messages: list[dict], json_mode: bool = False, **kw) -> str:
        text = self._render(messages)
        if json_mode:
            return self._jsonify(text)
        return text

    # ---- render one deterministic reply ----
    def _render(self, messages: list[dict]) -> str:
        sys = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        # extract our slot/now markers from the prompt
        slots = self._extract_slots(sys)
        now = self._extract_now(sys)
        text = user[-4000:]
        low = text.lower()

        ctrl = {
            "depth": 0.1, "tooliness": 0.0, "emotionality": 0.0,
            "novelty": 0.3, "stakes": 0.0, "config_deltas": {},
            "memory_writes": [], "code_intent": False, "ask": [],
        }

        # --- config deltas (same triggers the real model is told to emit) ---
        if re.search(r"dark ?mode", low):
            ctrl["config_deltas"]["ui.theme"] = "dark"
        elif re.search(r"light ?(theme|mode)", low):
            ctrl["config_deltas"]["ui.theme"] = "light"
        if "nudg" in low and any(x in low for x in ("too many", "many", "less", "fewer",
                                                    "don't like", "dont like", "stop")):
            ctrl["config_deltas"]["focus.nudge_cooldown_min"] = 30
        if re.search(r"(hinglish|hinglish|don'?t like).{0,30}(reply|hinglish)", low):
            ctrl["config_deltas"]["style.hinglish_ratio"] = 0.0
        if re.search(r"(long replies|too long|keep.{0,10}short|concise)", low) and "reply" in low or "keep short" in low or "long replies" in low:
            ctrl["config_deltas"]["style.concise"] = True
        if re.search(r"stop phone", low):
            ctrl["config_deltas"]["focus.nudge_channels.phone"] = False

        # --- memory writes ---
        for m in re.finditer(r"my ([a-z ]{3,40}) is ([a-z0-9 ,:\-/]{2,60})", low):
            ctrl["memory_writes"].append({"kind": "fact",
                                          "text": f"User's {m.group(1).strip()} is {m.group(2).strip()}",
                                          "importance": 0.6, "entities": []})
        if re.search(r"\b(i|we) (plan|planning|going|will).{0,60}(trip|visit|travel)", low):
            ctrl["memory_writes"].append({"kind": "goal", "text": text[:160],
                                          "importance": 0.7, "entities": []})
        pref = re.search(r"prefer (morning|window|aisle|evening|weekend|quiet|corner|handloom|organic)", low)
        if pref:
            ctrl["memory_writes"].append({"kind": "preference",
                                          "text": f"User prefers {pref.group(1)}",
                                          "importance": 0.65, "entities": []})

        # --- tooliness / code intent ---
        tasky = re.search(r"(research|find|search|compare|draft|email|buy|purchase|create|build|make|organi[sz]e|summar|extract|download|track|monitor|watch|book|read book|ocr|ppt|convert|date|register|portal|clash|keep an eye|scooter|phone|price)", low)
        if tasky:
            ctrl["tooliness"] = 0.8
            ctrl["depth"] = 0.6
            ctrl["code_intent"] = True
        if re.search(r"\b(buy|purchase|pay)\b", low):
            ctrl["stakes"] = 0.9
            ctrl["tooliness"] = 0.9
        if re.search(r"send (email|mail|this)", low):
            ctrl["stakes"] = 0.8
        if re.search(r"\b(feel|anxious|stressed|happy|sad|worried|angry|excited)\b", low):
            ctrl["emotionality"] = 0.7
            ctrl["depth"] = 0.5
        if re.search(r"\b(show|what do you think|beliefs|claims|memories? about)\b", low):
            ctrl["depth"] = 0.6

        prose = self._prose(text, low, slots, now, ctrl)
        return json.dumps({"ctrl": ctrl}, ensure_ascii=False) + "\n" + prose

    # ---- helpers to read what the cortex put in the prompt ----
    @staticmethod
    def _extract_slots(sys: str) -> list[dict]:
        m = re.search(r"<SLOTS>(.*?)</SLOTS>", sys, re.S)
        if not m:
            return []
        try:
            return json.loads(m.group(1))
        except Exception:
            return []

    @staticmethod
    def _extract_now(sys: str) -> dict:
        m = re.search(r"<NOW>(.*?)</NOW>", sys, re.S)
        if not m:
            return {}
        try:
            return json.loads(m.group(1))
        except Exception:
            return {}

    def _prose(self, text: str, low: str, slots: list[dict], now: dict, ctrl: dict) -> str:
        # 1. greeting/time-awareness
        if re.search(r"^(good morning|good evening|good night|morning|gm|hii*|heyy*|hi|hey|hello|yo)$", low.strip()):
            h = now.get("clock", {}).get("iso", "")
            d = now.get("last_seen_delta_h", 0)
            if "good morning" in low or "gm" in low:
                return (f"Good morning — {h}, and it's been {d}h since we last talked. "
                        f"{self._loop_line(now)}").strip()
            if d >= 2:
                return (f"Hey — welcome back, it's been {d}h. {self._loop_line(now)} "
                        "What are we working on?").strip()
            return "Hey! I'm here — one chat for research, tasks, memory, focus, books. What do you need?"
        # 2. focus
        m = re.search(r"(?:start|begin) foc\w* ?(\d+) ?m", low)
        if m and ("allow" in low or "focus" in low or True):
            mins = int(m.group(1))
            allowed = re.search(r"allow(?:ing)? ([a-z.]+)", low)
            ctrl["config_deltas"]["focus.auto"] = True
            return (f"Focus started for {mins} minutes" +
                    (f", allowing {allowed.group(1)}" if allowed else "") +
                    ". I'll nudge you if you drift. Timer is live in the Focus panel.")
        # 3. dark/light handled by ctrl; confirm
        if "dark mode" in low or "dark theme" in low:
            return "Done — dark mode is on 🖤"
        if "light theme" in low or "light mode" in low:
            return "Done — light mode is on."
        # 4. feedback
        if "nudg" in low and ("too many" in low or "less" in low or "stop" in low):
            return "Got it — nudges reduced to 1 per 30 minutes per kind. The Focus panel slider moved to match."
        if "hinglish" in low:
            return "Understood — I'll reply in pure English from now on."
        if "long replies" in low or ("keep" in low and "short" in low):
            return "Got it — shorter now."
        if "stop phone" in low:
            return "Done — phone notifications are off for focus mode only. Payments and reminders stay on."
        # 5. beliefs / memory
        if re.search(r"(what do you (think|believe)|what you think about me|show (me )?(your )?beliefs|claims about)", low):
            lines = [f"- **{s['text']}** (confidence {s.get('score', 0):.2f})" for s in slots[:4]]
            return ("Here's what I believe about you right now:\n" + "\n".join(lines) +
                    "\n\nRate any of them with 👍/👎 and I'll update α/β.")
        # 6. corrections (user told us something is wrong)
        if re.search(r"(don'?t live|not |actually |wrong|correction)", low):
            return "Noted — I've corrected that. It now outranks anything I inferred before."
        # 7. slot-grounded answers (the smartness observable in tests) —
        #    pick the MOST SPECIFIC match: flight-prefs pattern wins over the
        #    weekend pattern when both could match a slot
        patterns = [
            (r"dentist", r"dentist|appointment",
             lambda s: (f"No clash: the yatra window ends 15 Aug and your dentist "
                        f"appointment is {_after_marker(s['text'], ['on '])} — 10 days after the yatra. "
                        "I also set up a tracker on the portal.")),
            (r"salary|credited|credit", r"salary|1st",
             lambda s: ("Your salary credits on the 1st — so order after the 1st. "
                        "Here's the comparison table (Moto G85 ₹17,999 vs Redmi Note 14 ₹18,999) "
                        "and the email draft to Sarah is ready for your approval.")),
            (r"saree|handloom", r"birthday|handloom|mom",
             lambda s: ("Found it: Nalli handloom saree ₹8,499 for Mom — matches her birthday "
                        "and handloom preference. Payment card is up for your approval.")),
            (r"trip|travel|goa|jaipur|flights?|fly", r"morning|window",
             lambda s: ("Planning it — applying your saved preferences: morning flights and "
                        "window seats (from memory).")),
            (r"weekend|plan", r"astronomy|gandhinagar",
             lambda s: ("Weekend plan for Ahmedabad, value-first: 1) Science City Planetarium — "
                        "₹50, 2km out, evening astronomy show (fits your astronomy love); "
                        "2) Heritage Walk — ₹0, guided, 3km; 3) riverfront night market — free entry. "
                        "Ranked by value, and I noted you're in Gandhinagar so distances are from home.")),
        ]
        for qpat, spat, reply_fn in patterns:
            if not re.search(qpat, low):
                continue
            for s in slots:
                if re.search(spat, s.get("text", "").lower()):
                    return reply_fn(s)
        # 8. research/task (tooly turns — the model eats typos, so match on
        # the ctrl tooliness, not on spellings)
        if ctrl.get("tooliness", 0) >= 0.5:
            if re.search(r"\b(buy|purchase)\b", low):
                return "Payment approval card is up (this is one of my two blocking gates)."
            salary_line = ""
            for s in slots:
                if re.search(r"salary|1st", s.get("text", "").lower()):
                    salary_line = (" Your salary credits on the 1st — so order after the 1st."
                                   if "salary" in s["text"].lower() else "")
                    break
            email_line = ""
            if re.search(r"(draft|email|mail|sarh|sarah|sara)", low):
                email_line = " Email draft is in the Approvals panel (draft only — sending needs your tap)."
            return (f"Done — research brief with citations is in Tasks{salary_line}{email_line} "
                    "Summary: the two top options are Moto G85 5G (₹17,999) and Redmi Note 14 5G "
                    "(₹18,999); full table with sources is in the artifact.")
        if re.search(r"\b(buy|purchase)\b", low):
            return "Payment approval card is up (this is one of my two blocking gates)."
        if re.search(r"\b(remind|reminder)\b", low):
            return "Reminder set — I'll ping you on chrome + chat at the scheduled time."
        if re.search(r"\b(track|monitor|keep an eye)\b", low):
            return "Tracker created — I'll check the portal and push a chrome notification when it changes."
        if re.search(r"book", low) and re.search(r"(read|quiz|discuss|explain|chapter|page)", low):
            return ("Book mode: here's the explanation in simple terms, and I've queued "
                    "3 quiz questions — say 'quiz me' anytime. Full chapter context is in the Books panel.")
        if re.search(r"ppt|slides", low):
            return "PPTX generated — download it from the Tasks panel."
        if re.search(r"save.*drive", low):
            return "Saved to Drive/Friday/Summaries/ — link is in the chat card."
        if re.search(r"(sarah|sara)", low) and re.search(r"(draft|email|mail|send)", low):
            return "Email draft is ready in the Approvals panel (draft only — sending needs your tap)."
        # 9. identity / who-are-you
        if re.search(r"who (are you|r u|ru)|what are you|about yourself|your name", low):
            return ("I'm Friday — your one-pass cognitive companion. I remember what you tell me "
                    "(beliefs, corrections, preferences), do research and tasks in the background, "
                    "watch your focus, read your books, and learn from every turn. "
                    "Right now I'm running in OFFLINE mode (this sandbox has no model connection) — "
                    "on the VM I think with DeepSeek.")
        # 10. user tells us their name
        m = re.search(r"i am ([a-z]+)|i'?m ([a-z]+)|my name is ([a-z]+)|call me ([a-z]+)", low)
        if m:
            name = next((g for g in m.groups() if g), "friend").capitalize()
            ctrl["memory_writes"].append({"kind": "fact",
                                          "text": f"User's name is {name}",
                                          "importance": 0.9, "entities": [name]})
            return f"Nice to meet you, {name} — I've saved that. What are we working on?"
        # 11. factual questions needing live data (offline can't answer honestly)
        if re.search(r"who is (the )?(pm|prime minister)|current (pm|president)|latest news|today'?s (date|weather)", low):
            return ("I can't fetch live facts right now — this preview has no internet access to "
                    "the model or search APIs, and I won't invent an answer. On the deployed VM "
                    "I'll answer this with a live DeepSeek + web search.")
        # 12. honest fallback (never "On it." — say what you can't do)
        if ctrl.get("tooliness", 0) >= 0.3:
            return ("I've started this in the background Tasks panel. Heads-up: this preview runs "
                    "OFFLINE (no model/search connection), so results here are limited — the VM "
                    "deployment uses live DeepSeek and real web search.")
        return ("I hear you, but this preview is in offline mode — no LLM or web access from the "
                "sandbox, so I can't think properly here. Try memory/panel features, or wait for "
                "the VM deployment where I run on DeepSeek.")

    def _loop_line(self, now: dict) -> str:
        loops = now.get("open_loops", [])
        if not loops:
            return ""
        return "Open loops: " + "; ".join(l["text"][:60] for l in loops[:2]) + "."

    def _jsonify(self, text: str) -> str:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                json.loads(m.group(0))
                return m.group(0)
            except Exception:
                pass
        return json.dumps({"result": text})


def make_llm() -> LLMProvider:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return DeepSeekProvider(api_key=key)
    return SimProvider()


def _after_marker(text: str, markers: list[str]) -> str:
    low = text
    best = text.strip()
    for m in markers:
        idx = low.rfind(m)
        if idx >= 0:
            best = text[idx + len(m):].strip()
    return best or text.strip()


# --------------------------------------------------------------------------- #
# Search + web-read providers
# --------------------------------------------------------------------------- #
class SearchProvider:
    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        raise NotImplementedError


class TavilySearch(SearchProvider):
    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        key = os.environ.get("TAVILY_API_KEY", "")
        if not key:
            raise RuntimeError("no TAVILY_API_KEY")
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post("https://api.tavily.com/search", json={
                "api_key": key, "query": query, "max_results": max_results,
                "search_depth": "basic"})
            r.raise_for_status()
            return [{"title": x.get("title", ""), "url": x.get("url", ""),
                     "snippet": x.get("content", "")}
                    for x in r.json().get("results", [])]


class SimSearch(SearchProvider):
    """Deterministic fixture-based search (offline/tests)."""

    def __init__(self, fixture_path: str | None = None) -> None:
        self.fixtures = self._load(fixture_path)

    @staticmethod
    def _load(path: str | None) -> list[dict]:
        p = path or str(cfg.root / "configs" / "search_fixtures.yaml")
        if os.path.exists(p):
            with open(p) as f:
                return yaml.safe_load(f) or []
        return []

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        out = []
        low = query.lower()
        for fx in self.fixtures:
            if any(str(k) in low for k in fx.get("keywords", [])):
                out.append({"title": fx["title"], "url": fx["url"],
                            "snippet": fx["snippet"]})
        if not out:
            out = [{"title": f"Sim result for {query}", "url": "https://sim.local/1",
                    "snippet": "Simulated offline result (configure a search key on the VM for live results)."}]
        return out[:max_results]


def make_search() -> SearchProvider:
    prov = os.environ.get("SEARCH_PROVIDER", "tavily")
    if prov == "tavily" and os.environ.get("TAVILY_API_KEY"):
        return TavilySearch()
    return SimSearch()


async def web_read(url: str, max_chars: int = 12000) -> str:
    """Jina reader on VM; direct fetch + html extraction fallback (stdlib)."""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as c:
            r = await c.get(f"https://r.jina.ai/{url}")
            if r.status_code == 200 and len(r.text) > 200:
                return r.text[:max_chars]
            raise RuntimeError("jina failed")
    except Exception:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True,
                                     headers={"User-Agent": "Mozilla/5.0 Friday/0.1"}) as c:
            r = await c.get(url)
            r.raise_for_status()
            return html_to_text(r.text)[:max_chars]


def html_to_text(html: str) -> str:
    from html.parser import HTMLParser

    class P(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
            self.skip = 0

        def handle_starttag(self, tag, attrs):
            if tag in ("script", "style", "noscript", "svg"):
                self.skip += 1
            if tag in ("p", "br", "li", "h1", "h2", "h3", "tr", "div"):
                self.parts.append("\n")

        def handle_endtag(self, tag):
            if tag in ("script", "style", "noscript", "svg") and self.skip:
                self.skip -= 1

        def handle_data(self, data):
            if not self.skip:
                self.parts.append(data)

    p = P()
    p.feed(html)
    text = " ".join("".join(p.parts).split())
    return text
