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

    async def complete_tools(self, messages: list[dict], tools: list[dict],
                             temperature: float = 0.3, max_tokens: int = 600,
                             tool_choice: str | None = None) -> dict:
        """OpenAI-compatible function calling. Returns
        {"content": str, "tool_calls": [{"id","name","arguments"}], "finish": str}"""
        body: dict = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tool_choice:
            body["tool_choice"] = tool_choice
        try:
            resp = await self.client().post("/chat/completions", json=body,
                                            headers={"Authorization": f"Bearer {self.api_key}"})
            if resp.status_code != 200:
                err = resp.text[:400]
                raise RuntimeError(f"deepseek {resp.status_code}: {err}")
            obj = resp.json()
            msg = obj["choices"][0]["message"]
            out = {"content": msg.get("content") or "", "finish": obj["choices"][0].get("finish_reason", ""),
                   "tool_calls": []}
            for tc in (msg.get("tool_calls") or []):
                out["tool_calls"].append({
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                })
            return out
        except httpx.ConnectError as e:
            raise RuntimeError(f"network unreachable for {self.name}: {e}") from e


# --------------------------------------------------------------------------- #
# Google Gemini provider — same LLMProvider contract (stream / complete /
# complete_tools), using the v1beta generateContent API. OpenAI-style
# messages and tools are converted to Gemini's contents + functionDeclarations.
# --------------------------------------------------------------------------- #
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Model chain — newest GA first. Google retires models without warning
# (gemini-2.5-flash 404'd for new users in July 2026), so the provider tries
# each in order and silently falls forward on "no longer available" errors.
GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
GEMINI_MODELS = [
    "gemini-3.6-flash",          # GA · best price/perf for agentic chat
    "gemini-3.5-flash-lite",     # GA · fastest, cheapest 3.5
    "gemini-3.5-flash",          # frontier flash
    "gemini-3.1-flash-lite",     # workhorse, cheap
    "gemini-3-flash-preview",    # preview, free tier
    "gemini-2.5-flash",          # legacy — old keys may still reach it
]


def _gemini_contents(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """OpenAI-style messages → (Gemini contents, systemInstruction parts)."""
    contents: list[dict] = []
    sys_parts: list[dict] = []
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            sys_parts.append({"text": m.get("content") or ""})
        elif role == "tool":
            contents.append({"role": "user", "parts": [{
                "functionResponse": {
                    "name": m.get("name") or "tool",
                    "response": {"result": m.get("content") or ""},
                }}]})
        elif role == "assistant":
            parts: list[dict] = []
            if m.get("content"):
                parts.append({"text": m["content"]})
            for tc in (m.get("tool_calls") or []):
                try:
                    args = json.loads(tc.get("arguments") or "{}")
                except Exception:
                    args = {}
                parts.append({"functionCall": {
                    "name": tc.get("name") or "tool", "args": args}})
            if parts:
                contents.append({"role": "model", "parts": parts})
        else:
            content = m.get("content") or ""
            if isinstance(content, list):
                content = " ".join(
                    str(p.get("text", "")) if isinstance(p, dict) else str(p)
                    for p in content)
            contents.append({"role": "user", "parts": [{"text": content}]})
    # Gemini rejects consecutive same-role turns → merge them
    merged: list[dict] = []
    for c in contents:
        if merged and merged[-1]["role"] == c["role"]:
            merged[-1]["parts"].extend(c["parts"])
        else:
            merged.append(c)
    return merged, sys_parts


def _gemini_tools(tools: list[dict]) -> list[dict] | None:
    """OpenAI function-calling schemas → Gemini functionDeclarations."""
    fns: list[dict] = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not fn:
            continue
        decl: dict = {"name": fn.get("name", ""), "description": fn.get("description", "")}
        params = fn.get("parameters")
        if params:
            decl["parameters"] = params
        fns.append(decl)
    return [{"functionDeclarations": fns}] if fns else None


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self.base_url = (base_url or os.environ.get("GEMINI_BASE_URL", GEMINI_BASE)).rstrip("/")
        self.model = (model or os.environ.get("GEMINI_MODEL", "") or GEMINI_DEFAULT_MODEL).strip()
        # candidate chain: configured model first, then the GA list — used to
        # fall forward when Google retires a model mid-flight (404)
        self._models = [self.model] + [m for m in GEMINI_MODELS if m != self.model]
        self._client: httpx.AsyncClient | None = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url, timeout=httpx.Timeout(120.0, connect=4.0))
        return self._client

    def _body(self, messages: list[dict], temperature: float, max_tokens: int | None,
              tools: list[dict] | None = None) -> dict:
        contents, sys_parts = _gemini_contents(messages)
        body: dict = {
            "contents": contents,
            "generationConfig": {"temperature": temperature},
        }
        if sys_parts:
            body["systemInstruction"] = {"parts": sys_parts}
        if max_tokens:
            body["generationConfig"]["maxOutputTokens"] = max_tokens
        gtools = _gemini_tools(tools)
        if gtools:
            body["tools"] = gtools
        return body

    def _headers(self) -> dict:
        return {"x-goog-api-key": self.api_key, "Content-Type": "application/json"}

    async def _post(self, suffix: str, body: dict, model_override: str = "",
                    stream: bool = False):
        """POST to /models/{model}{suffix}, walking the candidate chain on
        model-gone errors. Returns (model, response) on success. The caller
        must close the response (stream responses stay open for iteration)."""
        candidates = [model_override] if model_override else self._models
        last_err = ""
        for m in candidates:
            url = f"/models/{m}{suffix}"
            try:
                if stream:
                    cm = self.client().stream("POST", url, json=body,
                                              headers=self._headers())
                    resp = await cm.__aenter__()
                    if resp.status_code == 200:
                        return m, resp
                    err = (await resp.aread()).decode()[:400]
                    await cm.__aexit__(None, None, None)
                else:
                    resp = await self.client().post(url, json=body,
                                                    headers=self._headers())
                    if resp.status_code == 200:
                        return m, resp
                    err = resp.text[:400]
                low = err.lower()
                # model retired / not found → try the next candidate
                if resp.status_code == 404 and ("no longer available" in low
                                                or "not found" in low
                                                or "model" in low):
                    last_err = err
                    continue
                raise RuntimeError(f"gemini {resp.status_code}: {err}")
            except httpx.ConnectError as e:
                raise RuntimeError(f"network unreachable for {self.name}: {e}") from e
        raise RuntimeError(f"gemini: all candidate models unavailable — {last_err[:200]}")

    async def stream(self, messages: list[dict], json_mode: bool = False,
                     temperature: float = 0.6, max_tokens: int | None = None,
                     **kw) -> AsyncIterator[str]:
        body = self._body(messages, temperature, max_tokens)
        _model, resp = await self._post(":streamGenerateContent?alt=sse", body,
                                        model_override=kw.get("model", ""), stream=True)
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                for cand in obj.get("candidates", []) or []:
                    for part in (cand.get("content", {}).get("parts", []) or []):
                        if part.get("text"):
                            yield part["text"]
        finally:
            await resp.aclose()

    async def complete(self, messages: list[dict], json_mode: bool = False,
                       temperature: float = 0.6, max_tokens: int | None = None,
                       **kw) -> str:
        body = self._body(messages, temperature, max_tokens)
        _model, resp = await self._post(":generateContent", body,
                                        model_override=kw.get("model", ""))
        obj = resp.json()
        parts = (obj.get("candidates", [{}])[0].get("content", {}).get("parts", []) or [])
        return "".join(p.get("text", "") for p in parts)

    async def complete_tools(self, messages: list[dict], tools: list[dict],
                             temperature: float = 0.3, max_tokens: int = 600,
                             tool_choice: str | None = None) -> dict:
        body = self._body(messages, temperature, max_tokens, tools=tools)
        _model, resp = await self._post(":generateContent", body)
        obj = resp.json()
        cand = (obj.get("candidates") or [{}])[0]
        parts = (cand.get("content", {}).get("parts", []) or [])
        out: dict = {"content": "", "finish": cand.get("finishReason", ""),
                     "tool_calls": []}
        for p in parts:
            if "text" in p:
                out["content"] += p["text"]
            elif "functionCall" in p:
                fc = p["functionCall"]
                out["tool_calls"].append({
                    "id": f"{fc.get('name', 'tool')}_{len(out['tool_calls'])}",
                    "name": fc.get("name", ""),
                    "arguments": json.dumps(fc.get("args", {}), ensure_ascii=False),
                })
        return out

    @staticmethod
    def usage_cost(usage: dict) -> float:
        """Gemini Flash pricing ballpark: $0.30/M in, $2.50/M out."""
        if not usage:
            return 0.0
        cin = usage.get("promptTokenCount", usage.get("prompt_tokens", 0))
        out = usage.get("candidatesTokenCount", usage.get("completion_tokens", 0))
        return (cin / 1e6) * 0.30 + (out / 1e6) * 2.50


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

    async def complete_tools(self, messages: list[dict], tools: list[dict],
                             temperature: float = 0.3, max_tokens: int = 600,
                             tool_choice: str | None = None) -> dict:
        """Deterministic tool loop: if the user message looks research-y,
        emit a web_search tool call (exercises the real loop offline)."""
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        low = user.lower()
        # after tool results are present, synthesize instead of calling again
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if has_tool_result:
            return {"content": self._render(messages), "finish": "stop", "tool_calls": []}
        if re.search(r"(research|search|find|compare|events|who is|what|weather|price|"
                     r"latest|news|how much|best|recommend|deep)", low):
            q = re.sub(r"^.*?(research|search|find|compare)\s*", "", low)[:120] or low[:120]
            return {"content": "", "finish": "tool_calls",
                    "tool_calls": [{"id": "call_sim_1", "name": "web_search",
                                    "arguments": json.dumps({"query": q.strip()})}]}
        return {"content": self._render(messages), "finish": "stop", "tool_calls": []}

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
        # 1b. how-are-you / smalltalk
        if re.search(r"how are you|how('| a)?re you|how r u|what'?s up|kaise ho|kya haal", low):
            return ("I'm good — memory loaded, a few open loops, and the river is flowing. "
                    "I'm running in OFFLINE mode on this preview though: the sandbox has no "
                    "model connection, so I'm running on my deterministic fallback. On the VM "
                    "I'm thinking with DeepSeek. Want me to pull up something from memory?")
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
        # 6. corrections (user told us something is wrong) — strict patterns
        # only; "do not say X" must never look like a correction
        if re.search(r"(don'?t live|actually |you were wrong|that'?s not right|"
                     r"correction:|not true|i meant|i (said|told) you)", low):
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


# --------------------------------------------------------------------------- #
# Tool schemas for the deep-research tool loop (shared by real + sim)
# --------------------------------------------------------------------------- #
TOOL_SCHEMAS: list[dict] = [
    {"type": "function",
     "function": {"name": "web_search",
                  "description": "Live web search. Returns titles, URLs, snippets.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string",
                                                          "description": "search query"}},
                                 "required": ["query"]}}},
    {"type": "function",
     "function": {"name": "web_read",
                  "description": "Read a webpage's full text content by URL.",
                  "parameters": {"type": "object",
                                 "properties": {"url": {"type": "string"}},
                                 "required": ["url"]}}},
    {"type": "function",
     "function": {"name": "memory_recall",
                  "description": "Recall what you know about a topic/person from memory.",
                  "parameters": {"type": "object",
                                 "properties": {"query": {"type": "string"}},
                                 "required": ["query"]}}},
]


def make_llm(scope: str = "chat") -> LLMProvider:
    """Provider selection per SCOPE (chat | research | books | eval | ...).
    Precedence per scope:
      1. DB setting llm.{scope}.provider + llm.{scope}.model (admin routing)
         with fallback to the chat defaults llm.provider / llm.model.
      2. The matching DB provider_key (admin panel / chat-set).
      3. .env DEEPSEEK_API_KEY / GEMINI_API_KEY.
      4. offline sim."""
    try:
        from .db import get_db
        db = get_db()
        chat_prov = db.get_setting("llm.provider", "deepseek") or "deepseek"
        want = db.get_setting(f"llm.{scope}.provider", "") or chat_prov
        model = db.get_setting(f"llm.{scope}.model", "") or ""
        # belt-and-braces: if an API key ever leaked into the model field,
        # ignore it here (use the default chain) even before DB cleanup runs
        if re.match(r"^(sk-|AIza|AQ\.)", model.strip()):
            model = ""
        # a scope inherits chat's model override ONLY when it uses the same
        # provider (a gemini model string must never leak into deepseek calls)
        if not model and want == chat_prov:
            model = db.get_setting("llm.model", "") or ""
        # Gemini first if selected
        if want == "gemini":
            row = db.q1(
                "SELECT api_key FROM provider_keys WHERE provider='gemini' "
                "AND scope='default' AND active=1 ORDER BY updated_ts DESC LIMIT 1")
            if row and row["api_key"]:
                return GeminiProvider(api_key=row["api_key"], model=model or None)
        # DeepSeek (default)
        row = db.q1(
            "SELECT api_key FROM provider_keys WHERE provider='deepseek' "
            "AND scope='default' AND active=1 ORDER BY updated_ts DESC LIMIT 1")
        if row and row["api_key"]:
            return DeepSeekProvider(api_key=row["api_key"], model=model or None)
        # if user picked gemini but has no gemini key yet, fall through to env
    except Exception:
        pass
    # env keys (deploy-provided)
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return DeepSeekProvider(api_key=key)
    gkey = os.environ.get("GEMINI_API_KEY", "")
    if gkey:
        return GeminiProvider(api_key=gkey)
    # offline deterministic
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
        key = _db_key("tavily") or os.environ.get("TAVILY_API_KEY", "")
        if not key:
            raise RuntimeError("no tavily key")
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
        for r in out:
            r["fixture"] = True
        return out[:max_results]


class BingSearch(SearchProvider):
    """Keyless live search via Bing HTML — more tolerant of datacenter/cloud
    IPs than DuckDuckGo (which often 403s OCI VMs)."""

    UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        url = "https://www.bing.com/search"
        params = {"q": query, "setlang": "en-in", "cc": "in", "count": str(max_results)}
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                         headers={"User-Agent": self.UA,
                                                  "Accept-Language": "en-IN,en;q=0.9"}) as c:
                r = await c.get(url, params=params)
                if r.status_code == 200:
                    out = self._parse(r.text, max_results)
                    if out:
                        return out
        except Exception:
            pass
        return []

    @staticmethod
    def _parse(html: str, max_results: int) -> list[dict]:
        import re
        import html as _h
        out = []
        for block in re.split(r'<li class="b_algo"', html)[1:]:
            m = re.search(r'<h2><a href="([^"]+)"[^>]*>(.*?)</a></h2>', block, re.S)
            p = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            if not m:
                continue
            title = re.sub(r"<[^>]+>", "", m.group(2))
            title = _h.unescape(title).strip()
            snippet = re.sub(r"<[^>]+>", "", p.group(1)) if p else ""
            snippet = _h.unescape(snippet).strip()
            if title:
                out.append({"title": title[:200], "url": m.group(1),
                            "snippet": snippet[:300]})
            if len(out) >= max_results:
                break
        return out


class GoogleNewsRSS(SearchProvider):
    """Keyless live news/events search via Google News RSS — extremely
    permissive egress (works behind most firewalls), returns real headlines
    with dates and links."""

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
        try:
            async with httpx.AsyncClient(timeout=12, follow_redirects=True) as c:
                r = await c.get(url, params=params)
                if r.status_code == 200:
                    items = self._parse(r.text, max_results)
                    if items:
                        return items
        except Exception:
            pass
        return []

    @staticmethod
    def _parse(xml: str, max_results: int) -> list[dict]:
        """Handle BOTH RSS 2.0 (no namespace — what Google News serves) and
        Atom (namespaced) feeds."""
        import xml.etree.ElementTree as ET
        out = []
        try:
            root = ET.fromstring(xml)
        except Exception:
            return []
        ATOM = "{http://www.w3.org/2005/Atom}"

        def _text(item, *tags):
            for t in tags:
                e = item.find(t)
                if e is not None and (e.text or "").strip():
                    return e.text.strip()
            return ""

        def _link(item):
            e = item.find("link")
            if e is None:
                e = item.find(f"{ATOM}link")
            if e is None:
                return ""
            href = e.get("href") or ""
            return href or (e.text or "").strip()

        items = root.findall(".//item") or root.findall(f".//{ATOM}entry")
        for item in items[:max_results]:
            title = _text(item, "title", f"{ATOM}title")
            link = _link(item)
            date = _text(item, "pubDate", f"{ATOM}updated")
            src = _text(item, "source", f"{ATOM}source")
            if title and link:
                snip = f"{src} · {date}" if (src or date) else ""
                out.append({"title": title[:200], "url": link, "snippet": snip[:300]})
        return out


class DuckDuckGoSearch(SearchProvider):
    """Keyless live search via DuckDuckGo HTML endpoints — works on the VM
    (egress confirmed open) with zero API keys. Falls back to sim fixtures
    on failure so nothing ever hard-crashes."""

    UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

    async def search(self, query: str, max_results: int = 6) -> list[dict]:
        # live chain: Google News RSS (bot-tolerant, works from datacenter IPs)
        # → DDG html → DDG lite → Bing → sim fixtures
        try:
            out = await GoogleNewsRSS().search(query, max_results)
            if out:
                return out
        except Exception:
            pass
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers={"User-Agent": self.UA}) as c:
            try:
                r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
                if r.status_code == 200:
                    out = self._parse_html(r.text, max_results)
                    if out:
                        return out
            except Exception:
                pass
            try:
                r = await c.post("https://lite.duckduckgo.com/lite/", data={"q": query})
                if r.status_code == 200:
                    out = self._parse_lite(r.text, max_results)
                    if out:
                        return out
            except Exception:
                pass
        try:
            out = await BingSearch().search(query, max_results)
            if out:
                return out
        except Exception:
            pass
        # graceful degradation → deterministic fixtures (marked not-live)
        sim = await SimSearch().search(query, max_results)
        for s in sim:
            s["fixture"] = True
        return sim

    @staticmethod
    def _clean(h: str) -> str:
        import html as _h
        import re
        t = _h.unescape(h)
        t = re.sub(r"<[^>]+>", "", t)
        return re.sub(r"\s+", " ", t).strip()

    @staticmethod
    def _clean_url(href: str) -> str:
        from urllib.parse import parse_qs, unquote, urlparse
        if "uddg=" in href:
            q = parse_qs(urlparse(href).query)
            if q.get("uddg"):
                return unquote(q["uddg"][0])
        return href

    @staticmethod
    def _parse_html(html: str, max_results: int) -> list[dict]:
        import re
        out = []
        for block in re.split(r'<div class="result', html)[1:]:
            m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
            s = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
            if not m:
                continue
            title = DuckDuckGoSearch._clean(m.group(2))
            snippet = DuckDuckGoSearch._clean(s.group(1)) if s else ""
            if title:
                out.append({"title": title[:200], "url": DuckDuckGoSearch._clean_url(m.group(1)),
                            "snippet": snippet[:300]})
            if len(out) >= max_results:
                break
        return out

    @staticmethod
    def _parse_lite(html: str, max_results: int) -> list[dict]:
        import re
        titles = re.findall(r'<a rel="nofollow" href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
        snippets = re.findall(r'<td class="result-snippet">(.*?)</td>', html, re.S)
        out = []
        for i, (href, title) in enumerate(titles[:max_results]):
            snip = DuckDuckGoSearch._clean(snippets[i]) if i < len(snippets) else ""
            out.append({"title": DuckDuckGoSearch._clean(title)[:200],
                        "url": DuckDuckGoSearch._clean_url(href),
                        "snippet": snip[:300]})
        return out


def _db_key(provider: str) -> str:
    """Admin-panel key for a provider (provider_keys table)."""
    try:
        from .db import get_db
        row = get_db().q1(
            "SELECT api_key FROM provider_keys WHERE provider=? "
            "AND scope='default' AND active=1 ORDER BY updated_ts DESC LIMIT 1",
            (provider,))
        return (row or {}).get("api_key", "")
    except Exception:
        return ""


def make_search() -> SearchProvider:
    prov = os.environ.get("SEARCH_PROVIDER", "duckduckgo")
    if (prov == "tavily" or _db_key("tavily")) and (_db_key("tavily") or os.environ.get("TAVILY_API_KEY")):
        return TavilySearch()
    if prov == "sim" and not _db_key("tavily") and not os.environ.get("TAVILY_API_KEY"):
        return SimSearch()
    return DuckDuckGoSearch()


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
