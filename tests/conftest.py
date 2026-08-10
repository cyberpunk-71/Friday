"""Test fixtures — fresh isolated DB per test, sim provider, seeded memories.

The whole suite runs OFFLINE: SimProvider (deterministic stand-in that still
emits ⟨CTRL⟩ blocks and consumes slots) + SimSearch (fixture results). The
DeepSeek/Tavily code paths are covered by unit tests against a local mock SSE
server (test_providers.py) so the real-wire format is verified without egress.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("FRIDAY_EMBED_BACKEND", "hash")
os.environ.setdefault("FRIDAY_RERANK_BACKEND", "heuristic")
os.environ.setdefault("SEARCH_PROVIDER", "sim")

from core.config import cfg  # noqa: E402
from core.db import reset_db_for_tests  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    # hermetic: admin tests set os.environ["DEEPSEEK_API_KEY"] when saving a
    # key — clear any leak so make_llm(scope) never hits the network in tests
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    d = tmp_path / "data"
    d.mkdir()
    cfg.reset_for_tests(str(d), str(ROOT / "genome"))
    return reset_db_for_tests(str(d / "test.db"))


@pytest.fixture()
def river(db):
    from core.river import River
    return River(db)


@pytest.fixture()
def loom(db):
    from core.loom import Loom
    return Loom(db)


@pytest.fixture()
def cortex(db):
    from core.cortex import Cortex
    from core.providers import SimProvider, SimSearch
    c = Cortex(db)
    c.llm = SimProvider(search=SimSearch())
    c.search = SimSearch()
    return c


@pytest.fixture()
def sim_seed(db, river):
    """Seed the canonical use-case memories (dentist, salary, prefs, mom)."""
    seeds = [
        ("fact", "User has a dentist appointment on 2026-08-25 at 11:00 AM", 0.9, ["Dentist"]),
        ("fact", "Salary is credited on the 1st of every month", 0.8, []),
        ("preference", "User prefers morning flights and window seats", 0.7, []),
        ("preference", "User loves handloom sarees for Mom", 0.75, ["Mom"]),
        ("fact", "Mom's birthday is on 12 September", 0.85, ["Mom"]),
        ("fact", "User lives in Gandhinagar", 0.9, ["Gandhinagar"]),
        ("preference", "User loves astronomy", 0.7, []),
        ("fact", "User works on ML research between 9 and 11 am", 0.6, []),
        ("preference", "User values budget over luxury", 0.5, []),
    ]
    for kind, text, imp, ents in seeds:
        river.record("memory_write", "user",
                     {"atom": {"kind": kind, "text": text, "importance": imp,
                               "entities": ents}}, source_weight=10.0)
    river.materialize()
    return river


def run(coro):
    """Robust runner: always returns a usable loop for this thread/test."""
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


def pump(loop, cond, timeout=12.0):
    """Pump the event loop until cond() is true — lets background DAG tasks
    actually run while tests wait for them."""
    import time as _t
    deadline = _t.time() + timeout
    while _t.time() < deadline:
        if cond():
            return True
        try:
            loop.run_until_complete(asyncio.sleep(0.05))
        except RuntimeError:
            break
    return cond()


def collect(agen):
    """Run an ASYNC GENERATOR to completion and return its events."""
    async def _a():
        return [e async for e in agen]
    return run(_a())


@pytest.fixture()
def client_factory():
    """Builds a fresh FastAPI TestClient against the app with a tmp db."""
    from fastapi.testclient import TestClient

    def make(tmp_path=None, db=None):
        if db is None:
            d = Path(tmp_path) if tmp_path else Path("/tmp/friday-test-" + os.urandom(4).hex())
            d.mkdir(parents=True, exist_ok=True)
            cfg.reset_for_tests(str(d))
            db = reset_db_for_tests(str(d / "t.db"))
        from core.app import app as fastapi_app
        from core.cortex import Cortex
        from core.providers import SimProvider, SimSearch

        class _Ctx:
            state = type("S", (), {})()

        _Ctx.state.cortex = Cortex(db)
        _Ctx.state.cortex.llm = SimProvider(search=SimSearch())
        _Ctx.state.cortex.search = SimSearch()
        _Ctx.state.worker = None
        fastapi_app.state = _Ctx.state
        # lifespan is bypassed by TestClient context manager; wire worker off
        return TestClient(fastapi_app), db

    return make
