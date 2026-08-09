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
    assert any(c["type"] == "approval_hint" for c in cards)


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
