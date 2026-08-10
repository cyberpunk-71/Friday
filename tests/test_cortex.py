"""CORTEX — ⟨CTRL⟩ parsing (incremental across chunks!), config deltas,
memory writes, ask budget, offline degradation, full SSE turn."""
from __future__ import annotations

import asyncio
import json

import pytest

from core.cortex import Cortex, parse_ctrl
from tests.conftest import collect
from core.providers import SimProvider


# --------------------------------------------------------------------------- #
# ⟨CTRL⟩ incremental parsing — the real model streams tokens, the block may
# arrive split across arbitrary chunk boundaries
# --------------------------------------------------------------------------- #
def test_parse_ctrl_whole():
    ctrl, rest = parse_ctrl('{"ctrl":{"depth":0.5,"config_deltas":{},"memory_writes":[],"code_intent":false,"ask":[]}} and then some prose')
    assert ctrl is not None and ctrl["depth"] == 0.5
    assert rest == " and then some prose"


def test_parse_ctrl_split_every_2_chars():
    payload = '{"ctrl":{"depth":0.9,"tooliness":0.8,"emotionality":0.2,"novelty":0.4,"stakes":0.1,"config_deltas":{"ui.theme":"dark"},"memory_writes":[{"kind":"fact","text":"X is Y"}],"code_intent":true,"ask":[]}}Hello world'
    acc, found = "", None
    for i in range(0, len(payload), 2):
        acc += payload[i:i + 2]
        ctrl, rest = parse_ctrl(acc)
        if ctrl is not None:
            found = (ctrl, i + 2)
            break
    assert found is not None, "ctrl never parsed"
    ctrl, consumed = found
    assert ctrl["code_intent"] is True
    assert ctrl["config_deltas"]["ui.theme"] == "dark"
    assert payload[consumed:] == "Hello world"


def test_parse_ctrl_no_block():
    ctrl, rest = parse_ctrl("just normal prose with no json at the start")
    assert ctrl is None


def test_parse_ctrl_string_braces_inside():
    """A JSON string containing braces must not confuse the parser."""
    acc = '{"ctrl":{"ask":["say } this"]}}prose'
    ctrl, rest = parse_ctrl(acc)
    assert ctrl is not None
    assert ctrl["ask"] == ["say } this"]
    assert rest == "prose"


# --------------------------------------------------------------------------- #
# full turn behaviors
# --------------------------------------------------------------------------- #
def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def test_turn_dark_mode(sim_seed, cortex, db):
    events = collect(cortex.turn("dark mode kar do"))
    types = [e["type"] for e in events]
    # deterministic config ingress: ctrl (with deltas) + theme card + done —
    # no LLM needed, and sense may be skipped on this path
    assert "ctrl" in types and "delta" in types and "done" in types
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["config_deltas"]["ui.theme"] == "dark"
    assert db.get_setting("ui.theme") == "dark"
    done = next(e for e in events if e["type"] == "done")
    assert "dark" in done["reply"].lower()
    # and the card tells the UI to flip
    assert any(e["type"] == "card" and e["card"]["type"] == "theme" for e in events)


def test_turn_memory_write_lands_in_river(sim_seed, cortex, db):
    events = collect(cortex.turn("my salary is credited on the 5th of every month"))
    atoms = db.q("SELECT * FROM atoms WHERE status='active' AND text LIKE '%5th%'")
    assert len(atoms) == 1


def test_turn_code_intent_spawns_task(sim_seed, cortex, db):
    from core.cortex import bus
    import uuid
    corr = f"cor_{uuid.uuid4().hex[:8]}"
    events = collect(cortex.turn("research best budget phones under 20k", meta={"corr_id": corr}))
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["code_intent"] is True
    # background job eventually completes — pump the loop so the DAG runs
    from tests.conftest import pump
    loop = asyncio.get_event_loop()
    pump(loop, lambda: any(t["status"] in ("completed", "failed")
                           for t in db.q("SELECT * FROM tasks")), timeout=15)
    t = db.q1("SELECT * FROM tasks ORDER BY task_id DESC LIMIT 1")
    assert t is not None
    assert t["status"] in ("completed", "failed")


def test_ask_budget_enforced(cortex, db):
    """max 2 clarifying questions/day — 3rd ask is dropped."""
    from core.config import cfg
    for i in range(4):
        ctrl = {"ask": [f"question {i}"]}
        asyncio.get_event_loop().run_until_complete(cortex._fire_ctrl(ctrl, "corr-x", "ask!", None))
    assert cortex.hermes.ask_budget_left() == 0


def test_stakes_payment_card(sim_seed, cortex, db):
    events = collect(cortex.turn("buy best handloom saree for mom under 10k dont ask just buy"))
    ctrl = next(e["ctrl"] for e in events if e["type"] == "ctrl")
    assert ctrl["stakes"] >= 0.7
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert any(c["type"] == "approvals" for c in cards), "deterministic approvals card"


def test_offline_degradation_mid_stream(cortex, db):
    """If the provider raises network errors mid-stream, the cortex falls
    back to the deterministic provider and still completes the turn."""
    class Exploding(SimProvider):
        name = "exploding"
        calls = 0

        async def stream(self, messages, **kw):
            Exploding.calls += 1
            if Exploding.calls == 1:
                raise RuntimeError("network unreachable")
            yield self._render(messages)

    cortex.llm = Exploding()
    events = collect(cortex.turn("hello"))
    types = [e["type"] for e in events]
    assert "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["reply"]


def test_now_block_deterministic(sim_seed, cortex):
    nb = cortex.loom.now_block()
    assert "clock" in nb and "last_seen_delta_h" in nb
    assert "open_loops" in nb and "last_corrections" in nb
    assert "budget_left_usd" in nb and "ask_budget_left" in nb


def test_conversation_history_injected(sim_seed, cortex, db):
    """The model must receive the last turns as context — that's what makes
    'ahmedabad' after 'events in ahmedabad' coherent."""
    from core.obs import log_turn
    log_turn("cor_hist1", "what are the events in ahmedabad tomorrow",
             "Here are events in Ahmedabad...", 500, 0.001, "sim", "sim", [])
    log_turn("cor_hist2", "search on book my show",
             "BookMyShow events for Ahmedabad...", 500, 0.001, "sim", "sim", [])
    messages = cortex._build_messages_for_test("ahmedabad")
    joined = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
    assert "events in ahmedabad" in joined, "history must be in context"
    assert "book my show" in joined


def test_city_typo_fix():
    from core.hermes import Hermes
    q = Hermes._core_query("events in ahemedbad tomorrow")
    assert "ahmedabad" in q.lower()
    assert "ahemedbad" not in q.lower()


def test_event_prefire_web_read(sim_seed, cortex, db):
    """Event queries should attempt a BookMyShow web read (prefire.web set)."""
    from core.hermes import Hermes
    import asyncio
    from core.providers import SimSearch
    h = Hermes(db, search=SimSearch())
    pf = asyncio.get_event_loop().run_until_complete(
        h.prefire("what are events in ahmedabad tomorrow"))
    # in the sandbox web_read fails (no egress) — but the path must not crash
    assert isinstance(pf.web, str)


def test_city_hint_from_slots(sim_seed, cortex):
    slots = [{"text": "User lives in Gandhinagar"}]
    assert cortex._city_from_slots(slots) == "gandhinagar"
    assert cortex._city_from_slots([]) == "ahmedabad"


def test_tiny_reply_no_task(sim_seed, cortex, db):
    """'run' / 'yes' must NOT spawn a nonsense task."""
    from core.cortex import bus
    import uuid
    corr = f"cor_{uuid.uuid4().hex[:8]}"
    events = collect(cortex.turn("run", meta={"corr_id": corr}))
    tasks = db.q("SELECT * FROM tasks")
    # 'run' alone is not a config/reminder/tracker command and is < 12 chars,
    # so no task should exist even if the model set code_intent
    assert len(tasks) == 0, f"'run' must not create tasks: {tasks}"


def test_tracker_ingress_creates_real_tracker(sim_seed, cortex, db):
    events = collect(cortex.turn("set up a daily tracker for bookmyshow events"))
    done = next(e for e in events if e["type"] == "done")
    assert "Tracker #" in done["reply"]
    trackers = db.q("SELECT * FROM trackers WHERE status='active'")
    assert len(trackers) == 1
    assert trackers[0]["frequency_mins"] == 1440  # daily
    assert any(e["type"] == "card" and e["card"]["type"] == "tracker" for e in events)


def test_core_query_appends_city():
    from core.hermes import Hermes
    q = Hermes._core_query("suggest me any good shows", "ahmedabad")
    assert "ahmedabad" in q, q
    q2 = Hermes._core_query("who is mayor of ahmedbad", "ahmedabad")
    assert "ahmedabad" in q2 and "ahemedbad" not in q2


def test_plan_validation_rejects_garbage(db):
    from core.hands import Hands
    import asyncio, json
    h = Hands(db)
    tid = h.create_task("t", "x", corr_id="pv")
    # simulate an LLM returning a garbage plan (no friday. calls)
    h.llm = None
    plan = [{"description": "dance", "code": "print('hello')",
             "assert": "True", "blocking": False}]
    db.exec("UPDATE tasks SET plan_json=? WHERE task_id=?",
            (json.dumps(plan), tid))
    steps = asyncio.get_event_loop().run_until_complete(h.plan_task(tid, "x", {}))
    plan2 = json.loads(db.q1("SELECT plan_json FROM tasks WHERE task_id=?", (tid,))["plan_json"])
    assert all("friday." in s.get("code", "") for s in plan2), "garbage plan must be replaced"


def test_fuzzy_city_typos():
    from core.hermes import Hermes
    for bad in ("ahmedaabd", "ahemedbad", "ahmedbad", "amdavad"):
        q = Hermes._core_query(f"events in {bad} tomorrow", "ahmedabad")
        assert "ahmedabad" in q and bad not in q, f"{bad} -> {q}"


def test_tiny_followup_reuses_last_question(sim_seed, cortex, db):
    """'search' after 'what events in ahmedabad' must search the last question."""
    from core.obs import log_turn
    from core.hermes import Hermes
    import asyncio
    from core.providers import SimSearch
    log_turn("cor_f1", "what are events in ahmedabad tomorrow", "stuff",
             100, 0, "sim", "sim", [])
    h = Hermes(db, search=SimSearch())
    pf = asyncio.get_event_loop().run_until_complete(h.prefire("search"))
    # sim search returns fixtures for 'events in ahmedabad' — proving the
    # query reused the last question instead of searching 'search'
    joined = " ".join(r["title"].lower() for r in pf.search)
    assert "ahmedabad" in joined or "weekend" in joined, joined


def test_cortex_hermes_has_search_provider(db):
    """Regression: Hermes must receive the resolved search provider, not the
    raw constructor arg (which was None → pre-fire silently disabled)."""
    from core.cortex import Cortex
    c = Cortex(db)
    assert c.search is not None, "cortex search provider must exist"
    assert c.hermes.search is c.search, "hermes must share the resolved provider"


def test_buy_ingress_payment_gate(sim_seed, cortex, db):
    """'buy X under 10k' must deterministically create a payment-gated task."""
    events = collect(cortex.turn("buy best handloom saree for mom under 10k"))
    done = next(e for e in events if e["type"] == "done")
    assert "approval" in done["reply"].lower() or "pay" in done["reply"].lower()
    t = db.q1("SELECT * FROM tasks ORDER BY task_id DESC LIMIT 1")
    assert t["status"] == "waiting_approval"
    assert t["approval_kind"] == "payment"
    cards = [e["card"] for e in events if e["type"] == "card"]
    assert any(c["type"] == "approvals" for c in cards)


def test_polish_reply_strips_robotic_furniture():
    """The user-facing safety net: no name openers, no meta-headers, no ---
    dividers, no search-result dump tables."""
    from core.cortex import polish_reply
    assert polish_reply("**FRIDAY**\n\nWho Am I?\n\nI am Friday.") == "I am Friday."
    out = polish_reply(
        "## Movies This Week\n\nBased on the live search results, here is what is "
        "in the news:\n\n**DC** — new.\n\n---\n\n### The Caveat\n\nNews only.\n\n"
        "---\n\n### What I'd Do\n\nCheck the app.")
    assert "Based on the live search results" not in out
    assert "The Caveat" not in out
    assert "What I'd Do" not in out
    assert "---" not in out
    assert "**DC** — new." in out and "News only." in out and "Check the app." in out
    # search-result dump table (Source column) → whole block dropped
    out2 = polish_reply(
        "| Result | Source | Date | Takeaway |\n| --- | --- | --- | --- |\n"
        "| WHO study | HT | 08 Aug | risk |\n\nSo movement matters.")
    assert "|" not in out2 and "So movement matters." in out2
    # imitated section titles ("### The Live Search Results — What They Tell
    # Us", "### What I Actually Do") get stripped, content stays
    out3 = polish_reply(
        "### The Live Search Results — What They Tell Us\n\nThese are news "
        "items, not a direct answer.\n\n### What I Actually Do\n\n- **Answer**"
        " — direct replies")
    assert "Live Search Results" not in out3 and "What I Actually Do" not in out3
    assert "These are news items" in out3 and "- **Answer**" in out3
    # colon lead-ins keep the content ("The Honest Answer: X" → "X")
    assert polish_reply("The Honest Answer: I can't find showtimes.") == \
        "I can't find showtimes."
    # plain reply untouched
    assert polish_reply("hey, all good here") == "hey, all good here"


def test_self_ref_queries_skip_web_search(db):
    """'who are you' / greetings must never burn a speculative search — the
    model used to feel obliged to dump the results as a news table."""
    import asyncio
    from core.hermes import Hermes, SELF_REF
    from core.providers import SimSearch
    for q in ("who are you", "hii", "what can you do", "tell me about yourself",
              "are you a bot", "hello"):
        assert SELF_REF.search(q), q
    h = Hermes(db, search=SimSearch())
    for q in ("who are you", "hii", "hello", "what can you do"):
        pf = asyncio.get_event_loop().run_until_complete(h.prefire(q))
        assert not pf.search, (q, pf.search[:1])
        assert "self-referential" in (pf.error or ""), (q, pf.error)
    # real live questions must NOT be self-ref
    for q in ("who is the mayor of ahmedabad", "what is the weather today",
              "movies showing today"):
        assert not SELF_REF.search(q), q


def test_focus_flow_typo_and_staged_start(cortex, db):
    """The exact flow the user hit: 'lets start a foscued mode?' → '30 minutes'
    → 'ai agent build' → 'start the session now'. The old ingress only matched
    'start foc\\w* Nm', so these fell to the LLM which CLAIMED the session
    started without one existing. Now it must produce a REAL session."""
    from core.focus import Focus
    f = Focus(db)
    e1 = collect(cortex.turn("lets start a foscued mode?"))
    d1 = next(e for e in e1 if e["type"] == "done")
    assert "minutes" in d1["reply"].lower()
    assert f.active() is None
    e2 = collect(cortex.turn("30 minutes"))
    d2 = next(e for e in e2 if e["type"] == "done")
    assert "working on" in d2["reply"]
    assert f.active() is None
    e3 = collect(cortex.turn("ai agent build"))
    d3 = next(e for e in e3 if e["type"] == "done")
    assert "ai agent build" in d3["reply"]
    assert f.active() is None
    e4 = collect(cortex.turn("yes nudge me if i get distracted, start the session now"))
    d4 = next(e for e in e4 if e["type"] == "done")
    assert "started" in d4["reply"].lower()
    s = f.active()
    assert s is not None, "focus session must actually exist (was hallucinated before)"
    assert s["target_min"] == 30
    assert s["task"] == "ai agent build"
    # stop works too
    e5 = collect(cortex.turn("stop the session"))
    next(e for e in e5 if e["type"] == "done")
    assert f.active() is None


def test_focus_one_shot_typo(cortex, db):
    """'start focos 25m allow github' (typo + one-shot) still starts for real."""
    from core.focus import Focus
    f = Focus(db)
    e = collect(cortex.turn("start focos 25m allow github"))
    d = next(e for e in e if e["type"] == "done")
    assert "started" in d["reply"].lower()
    s = f.active()
    assert s and s["target_min"] == 25
    assert "github" in (s["allow_domains"] or "")


def test_failover_to_other_provider_on_401(cortex, db, monkeypatch):
    """If the live LLM 401s mid-turn, cortex must fail over to the OTHER
    provider (using a saved key) instead of dropping to the offline stub."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class BoomGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            raise RuntimeError("gemini 401: Request had invalid authentication credentials")
            yield  # pragma: no cover — makes this an async generator (raise at __anext__)

    c = Cortex(db, llm=BoomGemini(), search=SimSearch())

    async def fake_stream(self, messages, **kw):
        yield "I'm here on DeepSeek instead."

    monkeypatch.setattr(DeepSeekProvider, "stream", fake_stream)
    events = collect(c.turn("hello there"))
    warns = [e for e in events if e["type"] == "warning"]
    assert warns and ("switched chat to deepseek" in warns[0]["message"].lower()
                      or "fail" in warns[0]["message"].lower())
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] == "deepseek"
    assert "DeepSeek instead" in done["reply"]


def test_failover_on_httpx_readexpress_non_runtime_error(cortex, db, monkeypatch):
    """httpx.ReadError is NOT a RuntimeError — it used to escape the stream
    loop and kill the whole turn after 'sense' (the exact bug on the VM).
    Now ANY provider exception must fail over, not die."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class ReadErrorGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            import httpx
            raise httpx.ReadError("connection closed by remote")
            yield  # pragma: no cover

    c = Cortex(db, llm=ReadErrorGemini(), search=SimSearch())

    async def fake_stream(self, messages, **kw):
        yield "Recovered via failover."

    monkeypatch.setattr(DeepSeekProvider, "stream", fake_stream)
    events = collect(c.turn("are you there"))
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] == "deepseek"
    assert "Recovered via failover" in done["reply"]
    # the REAL reason is recorded for the admin overview
    assert "connection closed" in (db.get_setting("llm.last_error") or "").lower()


def test_auto_heal_switches_chat_routing(cortex, db, monkeypatch):
    """When the active chat provider's key is REJECTED (401), chat routing
    must switch to the other provider PERMANENTLY — not just fail over for
    one turn — so every future turn stops re-trying the dead key."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.set_setting("llm.provider", "gemini")
    db.set_setting("llm.model", "gemini-3.1-flash-lite")
    db.set_setting("llm.research.provider", "gemini")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class DeadGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            raise RuntimeError("gemini 401: invalid authentication credentials")
            yield  # pragma: no cover

    c = Cortex(db, llm=DeadGemini(), search=SimSearch())

    async def fake_stream(self, messages, **kw):
        yield "Healed onto DeepSeek."

    monkeypatch.setattr(DeepSeekProvider, "stream", fake_stream)
    events = collect(c.turn("hello"))
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] == "deepseek"
    # routing is now permanently deepseek — next turns skip gemini entirely
    assert db.get_setting("llm.provider") == "deepseek"
    assert db.get_setting("llm.model") is None
    assert db.get_setting("llm.research.provider") == "deepseek"
    heal = db.get_setting("llm.auto_heal")
    assert heal and '"from": "gemini"' in heal
    warns = [e for e in events if e["type"] == "warning"]
    assert warns and "switched chat to deepseek" in warns[0]["message"]


def test_no_auto_heal_on_transient_error(cortex, db, monkeypatch):
    """A transient network error must NOT permanently switch routing (would
    flap on flaky networks)."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.set_setting("llm.provider", "gemini")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class FlakyGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            raise RuntimeError("gemini network error: ConnectError: timed out")
            yield  # pragma: no cover

    c = Cortex(db, llm=FlakyGemini(), search=SimSearch())

    async def fake_stream(self, messages, **kw):
        yield "ok for now"

    monkeypatch.setattr(DeepSeekProvider, "stream", fake_stream)
    collect(c.turn("hi"))
    assert db.get_setting("llm.provider") == "gemini"   # routing untouched
    assert db.get_setting("llm.auto_heal") is None


def test_double_failure_terminates_with_sim(cortex, db, monkeypatch):
    """If BOTH providers fail, the turn must terminate with the sim fallback
    — never loop forever (the failover chain had no cap)."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class DeadGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            raise RuntimeError("gemini 401: bad key")
            yield  # pragma: no cover

    class DeadDeepSeek:
        name = "deepseek"
        async def stream(self, messages, **kw):
            raise RuntimeError("deepseek network error: ReadError: closed")
            yield  # pragma: no cover

    c = Cortex(db, llm=DeadGemini(), search=SimSearch())
    monkeypatch.setattr(DeepSeekProvider, "stream",
                        lambda self, messages, **kw: DeadDeepSeek().stream(messages, **kw))
    events = collect(c.turn("ping"))
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] in ("sim", "sim-fallback")   # terminated, no hang


def test_polish_strips_mid_reply_ctrl():
    """The model sometimes re-emits {"ctrl":{...}} MID-reply (seen live:
    '...about 1{"ctrl":...}Hey! Still here'). polish_reply must remove it
    anywhere in the text, not just at the start."""
    from core.cortex import polish_reply
    text = ('Still here, and you have got about 1'
            '{"ctrl":{"depth":0.05,"tooliness":0.0,"config_deltas":{},'
            '"memory_writes":[],"code_intent":false,"ask":[]}}'
            'Hey! Still here')
    out = polish_reply(text)
    assert '"ctrl"' not in out
    assert "Still here" in out
    assert "Hey!" in out
    # nested braces + strings with braces inside are handled
    out2 = polish_reply('a {"ctrl":{"config_deltas":{"focus.nudge_cooldown_min":30},"ask":["why {x}?"]}} b')
    assert '"ctrl"' not in out2 and out2.strip() == "a  b"


def test_transient_failover_is_silent(cortex, db, monkeypatch):
    """A TRANSIENT provider failure must fail over SILENTLY — no 'failing
    over to X' toast. Only a permanent auto-heal (rejected key) may tell the
    user, and the raw per-turn failover spam is gone entirely."""
    from core.cortex import Cortex
    from core.providers import DeepSeekProvider, SimSearch
    db.set_setting("llm.provider", "gemini")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-valid1234567890',1,'admin',?,?)", (1, 1))

    class FlakyGemini:
        name = "gemini"
        async def stream(self, messages, **kw):
            raise RuntimeError("gemini network error: ConnectError: timed out")
            yield  # pragma: no cover

    c = Cortex(db, llm=FlakyGemini(), search=SimSearch())

    async def fake_stream(self, messages, **kw):
        yield "quietly recovered"

    monkeypatch.setattr(DeepSeekProvider, "stream", fake_stream)
    events = collect(c.turn("hi"))
    warns = [e for e in events if e["type"] == "warning"]
    assert warns == [], "transient failover must not spam warnings"
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] == "deepseek"
    assert db.get_setting("llm.provider") == "gemini"   # routing untouched


def test_strip_truncated_ctrl_cases():
    """The model emits {"ctrl":{... then jumps to prose WITHOUT closing the
    braces (seen live: '"config_deltHey! Still here...'). strip_truncated_ctrl
    must remove the JSON prefix in all shapes; complete JSON is left for the
    balanced stripper; mid-stream JSON must NOT be cut (no prose yet)."""
    from core.cortex import strip_truncated_ctrl, _truncated_ctrl_with_prose
    multi = ('{"ctrl":{"depth":0.05,"tooliness":0.0,"emotionality":0.3,"novelty":0.0,'
             '"stakes":0.0,\n"config_deltHey! Still here, still in focus mode with you'
             ' — about 18 minutes left on the AI agent build. What\'s up?')
    out = strip_truncated_ctrl(multi)
    assert out.startswith("Hey! Still here") and '"ctrl"' not in out
    single = '{"ctrl":{"depth":0.05,"stakes":0.0,"config_deltHey! one line'
    out2 = strip_truncated_ctrl(single)
    assert out2.startswith("Hey! one line") and '"ctrl"' not in out2
    # mid-stream (no prose yet) → stripped to nothing → NOT flagged as prose
    assert strip_truncated_ctrl('{"ctrl":{"depth":0.05,') == ""
    assert _truncated_ctrl_with_prose('{"ctrl":{"depth":0.05,') is False
    assert _truncated_ctrl_with_prose(multi) is True


def test_truncated_ctrl_never_leaks_into_turn(cortex, db, monkeypatch):
    """A provider that emits a TRUNCATED ctrl JSON followed by prose must
    produce a clean reply — no raw JSON in deltas, done.reply, or history."""
    from core.cortex import Cortex
    from core.providers import SimSearch

    class TruncatedCtrlProvider:
        name = "deepseek"
        async def stream(self, messages, **kw):
            yield '{"ctrl":{"depth":0.05,"tooliness":0.0,"emotionality":0.3,"novelty":0.0,'
            yield '"stakes":0.0,\n"config_delt'
            yield 'Hey! Still here, still in focus mode with you.'
            yield ' What\'s up?'

    c = Cortex(db, llm=TruncatedCtrlProvider(), search=SimSearch())
    events = collect(c.turn("hey"))
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert '"ctrl"' not in deltas and '"config_delt' not in deltas
    assert "Hey! Still here" in deltas
    done = next(e for e in events if e["type"] == "done")
    assert '"ctrl"' not in done["reply"]
    assert "Hey! Still here" in done["reply"]
    # history is clean too (stored reply feeds the next turn)
    row = db.q1("SELECT reply FROM turns ORDER BY turn_id DESC LIMIT 1")
    assert '"ctrl"' not in (row["reply"] or "")


def test_strip_leading_key_remnant():
    """The model echoed just the truncated key remnant without the JSON
    prefix ('config_deltHey! Still here...') — strip it, but never touch
    normal prose (lowercase continuations like 'asking' are safe)."""
    from core.cortex import strip_leading_key_remnant, polish_reply
    assert strip_leading_key_remnant('config_deltHey! Still here') == 'Hey! Still here'
    assert strip_leading_key_remnant('"config_deltHey! Still here') == 'Hey! Still here'
    assert strip_leading_key_remnant('asking questions is fine') == 'asking questions is fine'
    assert strip_leading_key_remnant('Hey normal reply') == 'Hey normal reply'
    out = polish_reply('config_deltHey! Still here, still in focus mode with you.')
    assert 'config_delt' not in out and out.startswith('Hey! Still here')


def test_identity_ingress_answers_real_model(cortex, db):
    """'which model are you' must deterministically answer with the ACTUAL
    provider+model — never the LLM's 'frontier-class' hedge."""
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-abc1234567890',1,'admin',?,?)", (1, 1))
    db.set_setting("llm.provider", "deepseek")
    for q in ("which model are you", "what model are you using", "what are you running on",
              "which provider are you using", "which llm is this"):
        events = collect(cortex.turn(q))
        done = next(e for e in events if e["type"] == "done")
        assert done["model"] == "deterministic", q
        assert "deepseek" in done["reply"].lower(), (q, done["reply"])
        assert "frontier" not in done["reply"].lower(), q
    # a phone-shopping question must NOT be intercepted
    events = collect(cortex.turn("which model of phone should I buy under 20k"))
    done = next(e for e in events if e["type"] == "done")
    assert done["model"] != "deterministic"
