"""FastAPI app — the whole Friday surface.

Chat-first: one chat stream with generative UI cards inline; Admin/Tasks/
Memory/Focus/Books panels are the same app, not separate surfaces. SSE for
everything live; WS for voice barge-in; REST for panels + extension ingress.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from typing import Optional

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import cfg
from .cortex import Cortex, bus
from .db import get_db
from .focus import Focus

# API-key-shaped strings (DeepSeek sk-…, old Gemini AIza…, new Gemini AQ.Ab…)
# must never be stored as a MODEL name — pasting a key into the model field
# is the #1 admin-panel confusion
KEYLIKE_RE = re.compile(r"^(sk-[A-Za-z0-9_\-]{6,}|AIza[A-Za-z0-9_\-]{20,}|AQ\.[A-Za-z0-9_.\-]{20,})$")


def _clear_keylike_models(db) -> list[str]:
    """Self-heal: if any llm.*.model setting holds an API key (user pasted
    the key into the model field), clear it so the provider uses its default
    model chain."""
    fixed = []
    for scope in ("", "research", "books", "eval"):
        key = f"llm{'.' + scope if scope else ''}.model"
        val = db.get_setting(key, "") or ""
        if KEYLIKE_RE.match(val):
            db.set_setting(key, None)
            fixed.append(key)
    return fixed
from .genome import Genome
from .hermes import Hermes
from .obs import audit
from .psyche import Psyche
from .river import River
from .skills import get_registry
from .voice import Voice
from .worker import Worker

APP_VERSION = "0.1.0"

# --------------------------------------------------------------------------- #
# lifespan
# --------------------------------------------------------------------------- #
def _seed_profile_if_fresh(db) -> bool:
    """First-run seed: a brand-new DB gets the canonical user profile so the
    panels and use-case demos work immediately. Disable with FRIDAY_SEED_PROFILE=0."""
    if os.environ.get("FRIDAY_SEED_PROFILE", "1") == "0":
        return False
    if db.q1("SELECT COUNT(*) c FROM events")["c"] > 0:
        return False
    river = River(db)
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
    db.set_setting("seed.profile", "v1")
    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    # live settings → config
    cfg.set_live(db.all_settings())
    # first-run profile seed (canonical user profile for demos)
    _seed_profile_if_fresh(db)
    # skill procedures → river (kind=procedure atoms, retrieved by demand)
    river = River(db)
    river.materialize()
    app.state.cortex = Cortex(db)
    app.state.worker = Worker(db, search=app.state.cortex.search, cortex=app.state.cortex)
    app.state.worker_task = asyncio.create_task(app.state.worker.run_forever())
    yield
    app.state.worker.stop()
    try:
        await asyncio.wait_for(app.state.worker_task, timeout=3)
    except Exception:
        pass


app = FastAPI(title="Friday", version=APP_VERSION, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    """Static assets must never be stale-cached — the UI ships fixes constantly."""
    resp = await call_next(request)
    if request.url.path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return resp

UI_DIR = cfg.root / "ui"
if UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(UI_DIR)), name="static")


def _cortex(request: Request) -> Cortex:
    return request.app.state.cortex


def _admin_auth(request: Request):
    token = os.environ.get("FRIDAY_ACCESS_TOKEN", "")
    if token:
        got = request.headers.get("Authorization", "")
        if got != f"Bearer {token}":
            raise HTTPException(401, "unauthorized")
    return True


# =========================================================================== #
# CHAT
# =========================================================================== #
@app.post("/api/chat")
async def chat(request: Request, payload: dict):
    """SSE stream: sense → ctrl → deltas → cards → done. Never blocks on tasks.
    Optional llm_scope (eval|research|...) runs this turn on that scope's
    routed model (Model routing in Admin) instead of the chat model."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    book_id = payload.get("book_id")
    llm_scope = str(payload.get("llm_scope") or "").strip() or None
    meta = {"corr_id": payload.get("corr_id") or f"cor_{uuid.uuid4().hex[:8]}"}
    cortex = _cortex(request)

    async def gen():
        corr_id = meta["corr_id"]
        queue = bus.subscribe(corr_id)
        prev_llm = cortex.llm
        if llm_scope and llm_scope != "chat":
            try:
                from .providers import make_llm
                cortex.llm = make_llm(llm_scope)
            except Exception:
                pass
        try:
            async for ev in cortex.turn(text, book_id=book_id, meta=meta):
                yield _sse(ev)
            # background task events (FORGE DAG progress)
            while True:
                try:
                    ev = await asyncio.wait_for(queue.get(), timeout=0.05)
                    yield _sse({"type": "task_event", **ev})
                except asyncio.TimeoutError:
                    break
        finally:
            bus.unsubscribe(corr_id, queue)
            if prev_llm is not None:
                cortex.llm = prev_llm   # restore chat model for other turns

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


def _sse(ev: dict) -> str:
    return f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"





@app.get("/api/chat/history")
async def chat_history(limit: int = 50):
    db = get_db()
    turns = db.q("SELECT * FROM turns ORDER BY turn_id DESC LIMIT ?", (limit,))
    turns.reverse()
    return {"turns": turns}


# =========================================================================== #
# ADMIN
# =========================================================================== #
@app.get("/api/admin/overview")
async def admin_overview(request: Request, _: bool = Depends(_admin_auth)):
    db = get_db()
    # self-heal the RUNNING provider: if a stale instance was built with an
    # API key as its model (pre-fix), rebuild it from clean DB state so chat
    # stops 404-ing once per call
    llm = getattr(request.app.state, "cortex", None)
    if llm is not None and hasattr(llm.llm, "model"):
        if KEYLIKE_RE.match(str(llm.llm.model or "")):
            try:
                from .providers import make_llm
                llm.llm = make_llm("chat")
            except Exception:
                pass
    hermes = Hermes(db)
    spend = hermes.spend_today()
    daily = cfg.get("budget.daily_usd", 6.0)
    counts = {
        "atoms": db.q1("SELECT COUNT(*) c FROM atoms WHERE status='active'")["c"],
        "events": db.q1("SELECT COUNT(*) c FROM events")["c"],
        "claims": db.q1("SELECT COUNT(*) c FROM claims WHERE status!='deprecated'")["c"],
        "tensions": db.q1("SELECT COUNT(*) c FROM tensions WHERE status='open'")["c"],
        "tasks": db.q1("SELECT COUNT(*) c FROM tasks")["c"],
        "running_tasks": db.q1("SELECT COUNT(*) c FROM tasks WHERE status IN ('queued','running')")["c"],
        "waiting_approval": db.q1("SELECT COUNT(*) c FROM tasks WHERE status='waiting_approval'")["c"],
        "trackers": db.q1("SELECT COUNT(*) c FROM trackers WHERE status='active'")["c"],
        "reminders_pending": db.q1("SELECT COUNT(*) c FROM reminders WHERE status='pending'")["c"],
        "books": db.q1("SELECT COUNT(*) c FROM books")["c"],
        "skills": len(get_registry().all()),
        "turns_today": db.q1("SELECT COUNT(*) c FROM turns WHERE created_ts>?", (time.time() - 86400,))["c"],
    }
    keys = db.q("SELECT provider,scope,substr(api_key,1,4)||'••••'||substr(api_key,-4) masked,"
                "active,source FROM provider_keys")
    chat_provider = db.get_setting("llm.provider", "deepseek")
    keys = [dict(k, is_chat=(k["provider"] == chat_provider)) for k in keys]
    return {
        "version": APP_VERSION, "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "llm_model": (getattr(request.app.state.cortex.llm, "model", "")
                      if getattr(request.app.state, "cortex", None) else ""),
        # report the ACTUAL running provider (from the live cortex), not env
        "llm_provider": (request.app.state.cortex.llm.name
                         if getattr(request.app.state, "cortex", None) else "unknown"),
        # per-scope model routing (admin Model routing card)
        "llm_scopes": {
            "chat": {"provider": db.get_setting("llm.provider", "deepseek"),
                     "model": db.get_setting("llm.model", "") or ""},
            "research": {"provider": db.get_setting("llm.research.provider", ""),
                         "model": db.get_setting("llm.research.model", "") or ""},
            "books": {"provider": db.get_setting("llm.books.provider", ""),
                      "model": db.get_setting("llm.books.model", "") or ""},
            "eval": {"provider": db.get_setting("llm.eval.provider", ""),
                     "model": db.get_setting("llm.eval.model", "") or ""},
        },
        "search_provider": os.environ.get("SEARCH_PROVIDER", "sim"),
        "spend_today_usd": round(spend, 4), "daily_budget_usd": daily,
        "budget_pct": round(100 * spend / max(0.01, daily), 1),
        "ask_budget_left": hermes.ask_budget_left(),
        "counts": counts, "provider_keys": keys,
        "genome_head": db.get_setting("genome.head"),
        "genome_log": Genome(db=db).log(8),
        "gym": db.get_setting("nightly.last_report"),
        "llm_last_error": db.get_setting("llm.last_error"),
        "llm_last_error_ts": db.get_setting("llm.last_error_ts"),
        "latency": db.q1("SELECT AVG(latency_ms) a FROM turns WHERE created_ts>?", (time.time() - 86400,)),
        "cost_avg": db.q1("SELECT AVG(cost_usd) a FROM turns WHERE created_ts>?", (time.time() - 86400,)),
    }


@app.get("/api/admin/params")
async def admin_params(_: bool = Depends(_admin_auth)):
    """Full configurable parameter tree (defaults + live overrides)."""
    return {"defaults": cfg._defaults, "live": cfg._live}


@app.get("/api/admin/settings")
async def admin_settings_get(_: bool = Depends(_admin_auth)):
    return get_db().all_settings()


@app.put("/api/admin/settings")
async def admin_settings(payload: dict, _: bool = Depends(_admin_auth)):
    db = get_db()
    for key, value in payload.items():
        db.set_setting(key, value)
        cfg.set_live_value(key, value)
    return {"ok": True, "updated": list(payload.keys())}


@app.post("/api/admin/models/configure")
async def models_configure(payload: dict, request: Request, _: bool = Depends(_admin_auth)):
    """One-click model config: {provider, scope, api_key} → provider_keys."""
    db = get_db()
    provider = payload.get("provider", "deepseek")
    scope = payload.get("scope", "default")
    key = payload.get("api_key", "")
    if not key:
        raise HTTPException(400, "api_key required")
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES(?,?,?,1,'admin',?,?) "
            "ON CONFLICT(provider,scope) DO UPDATE SET api_key=excluded.api_key,"
            " active=1, source='admin', updated_ts=excluded.updated_ts",
            (provider, scope, key, time.time(), time.time()))
    # wire the key live based on provider
    if provider == "deepseek" and scope == "default":
        os.environ["DEEPSEEK_API_KEY"] = key
        db.set_setting("llm.provider", "deepseek")
        from .providers import DeepSeekProvider
        if getattr(request.app.state, "cortex", None):
            request.app.state.cortex.llm = DeepSeekProvider(
                api_key=key, model=db.get_setting("llm.model", "") or None)
        env_path = cfg.root / ".env"
        if env_path.exists():
            lines = [l for l in env_path.read_text().splitlines()
                     if not l.startswith("DEEPSEEK_API_KEY=")]
            env_path.write_text("\n".join(lines) + "\n")
    elif provider == "gemini" and scope == "default":
        os.environ["GEMINI_API_KEY"] = key
        db.set_setting("llm.provider", "gemini")
        from .providers import GeminiProvider
        if getattr(request.app.state, "cortex", None):
            request.app.state.cortex.llm = GeminiProvider(
                api_key=key, model=db.get_setting("llm.model", "") or None)
    elif provider in ("tavily", "brave", "exa", "search"):
        # live-swap the search provider so the new key takes effect immediately
        os.environ["SEARCH_PROVIDER"] = "tavily" if provider == "tavily" else "duckduckgo"
        from .providers import make_search
        request.app.state.cortex.search = make_search()
        request.app.state.cortex.hermes.search = request.app.state.cortex.search
        request.app.state.cortex.hands.search = request.app.state.cortex.search
        request.app.state.worker.search = request.app.state.cortex.search
    elif provider in ("groq", "voice"):
        os.environ["GROQ_API_KEY"] = key
        from .voice import Voice
        # voice reads the key live on next TTS call
    # self-heal: if a key ever leaked into a model field, clear it now
    _clear_keylike_models(db)
    return {"ok": True, "provider": provider, "scope": scope, "masked": f"••••{key[-4:]}"}


@app.post("/api/admin/llm")
async def admin_llm_switch(payload: dict, request: Request, _: bool = Depends(_admin_auth)):
    """Set the model for a SCOPE (chat | research | books | eval) — provider
    (deepseek ↔ gemini) + optional model override, using keys already saved
    in Models & Keys. No key re-entry."""
    db = get_db()
    scope = str(payload.get("scope", "chat"))
    provider = str(payload.get("provider", "deepseek"))
    model = str(payload.get("model", "") or "").strip()
    if provider not in ("deepseek", "gemini"):
        raise HTTPException(400, "provider must be deepseek or gemini")
    if model and KEYLIKE_RE.match(model):
        raise HTTPException(400, "that looks like an API key, not a model name — paste it in the key field instead")
    if scope == "chat":
        db.set_setting("llm.provider", provider)
        db.set_setting("llm.model", model or None)
    else:
        db.set_setting(f"llm.{scope}.provider", provider)
        db.set_setting(f"llm.{scope}.model", model or None)
    from .providers import make_llm
    llm = make_llm(scope)
    if scope == "chat" and getattr(request.app.state, "cortex", None):
        request.app.state.cortex.llm = llm
    return {"ok": True, "scope": scope, "provider": llm.name,
            "model": getattr(llm, "model", ""),
            "note": f"{scope} now runs on {llm.name}"}


@app.post("/api/admin/providers/test")
async def providers_test(payload: Optional[dict] = None, _: bool = Depends(_admin_auth)):
    """Test the configured LLM provider (deepseek or gemini). Returns
    categorized diagnostics: blocked (no egress / TLS), invalid_key, or ok."""
    from .providers import DeepSeekProvider, GeminiProvider
    provider = (payload.get("provider") if payload else None) or "deepseek"
    if provider == "gemini":
        key = (payload.get("api_key") if payload else None) or os.environ.get("GEMINI_API_KEY", "")
        if not key:
            return {"ok": False, "error": "no Gemini key — add it in Models & Keys first"}
        p = GeminiProvider(api_key=key)
        tag = "Gemini"
    else:
        key = (payload.get("api_key") if payload else None) or os.environ.get("DEEPSEEK_API_KEY", "")
        if not key:
            return {"ok": False, "error": "no key configured — set it in Models & Keys or tell Friday in chat"}
        p = DeepSeekProvider(api_key=key)
        tag = "DeepSeek"
    try:
        out = await p.complete([{"role": "user", "content": "ping"}], max_tokens=5)
        return {"ok": True, "reply": out[:50], "note": f"live {tag} call succeeded"}
    except RuntimeError as e:
        msg = str(e)
        if "network unreachable" in msg or "TLS" in msg or "SSL" in msg or "ConnectError" in msg:
            return {"ok": False, "error": "network blocked from this environment",
                    "detail": msg[:160],
                    "fix": "This sandbox only allows pypi/github egress. On the VM, run "
                           "friday_netcheck and open the OCI security list to 443 outbound "
                           "if needed — the VM is where live DeepSeek calls run."}
        return {"ok": False, "error": msg[:200]}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}


@app.post("/api/admin/oneclick")
async def oneclick(payload: dict, _: bool = Depends(_admin_auth)):
    """One-click integrations: extension, mobile monitor, search, voice."""
    kind = payload.get("kind")
    db = get_db()
    if kind == "search":
        db.set_setting("integration.search", json.dumps(payload))
        return {"ok": True, "kind": "search", "note": "restart worker to apply"}
    if kind == "extension":
        db.set_setting("integration.extension", json.dumps(payload))
        return {"ok": True, "kind": "extension",
                "note": "Install the MV3 extension from /extension (load unpacked)."}
    if kind == "mobile":
        db.set_setting("integration.mobile", json.dumps(payload))
        return {"ok": True, "kind": "mobile", "note": "scan the QR / add the URL on the phone"}
    if kind == "voice":
        db.set_setting("integration.voice", json.dumps(payload))
        return {"ok": True, "kind": "voice"}
    return {"ok": False, "error": "unknown kind"}


@app.get("/api/admin/spend")
async def admin_spend(days: int = 14, _: bool = Depends(_admin_auth)):
    db = get_db()
    rows = db.q("SELECT date(created_ts,'unixepoch','localtime') d, SUM(cost_usd) s, COUNT(*) n "
                "FROM turns WHERE created_ts>? GROUP BY d ORDER BY d", (time.time() - days * 86400,))
    return {"series": [{"date": r["d"], "usd": round(r["s"] or 0, 4), "turns": r["n"]} for r in rows]}


@app.get("/api/admin/turns")
async def admin_turns(limit: int = 50, _: bool = Depends(_admin_auth)):
    db = get_db()
    return {"turns": db.q("SELECT * FROM turns ORDER BY turn_id DESC LIMIT ?", (limit,))}


# =========================================================================== #
# TASKS
# =========================================================================== #
@app.get("/api/tasks")
async def tasks_list(status: Optional[str] = None):
    db = get_db()
    if status:
        rows = db.q("SELECT * FROM tasks WHERE status=? ORDER BY updated_ts DESC LIMIT 100", (status,))
    else:
        rows = db.q("SELECT * FROM tasks ORDER BY updated_ts DESC LIMIT 100")
    for r in rows:
        r["steps"] = db.q("SELECT * FROM task_steps WHERE task_id=? ORDER BY step_index", (r["task_id"],))
        r["artifacts"] = db.q("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_ts DESC", (r["task_id"],))
    trackers = db.q("SELECT * FROM trackers ORDER BY created_ts DESC LIMIT 20")
    return {"tasks": rows, "trackers": trackers}


@app.post("/api/tasks")
async def tasks_create(payload: dict):
    """Manual task create + run (used by UI Task panel and tests)."""
    from .hands import Hands
    db = get_db()
    hands = Hands(db)
    corr = f"cor_{uuid.uuid4().hex[:8]}"
    tid = hands.create_task(payload.get("title", "task"), payload.get("description", ""),
                            payload.get("autonomy", "autonomous"), payload.get("priority", 0.5), corr)
    steps = await hands.plan_task(tid, payload.get("description") or payload.get("title", ""), {})
    async for ev in hands.execute(tid, corr):
        if ev["type"] == "approval":
            pass
    return {"ok": True, "task_id": tid,
            "status": db.q1("SELECT status FROM tasks WHERE task_id=?", (tid,))["status"]}


@app.post("/api/tasks/{task_id}/approve")
async def tasks_approve(task_id: int):
    from .hands import Hands
    hands = Hands(get_db())
    return await hands.approve(task_id)


@app.post("/api/tasks/{task_id}/reject")
async def tasks_reject(task_id: int):
    from .hands import Hands
    return Hands(get_db()).reject(task_id)


@app.post("/api/tasks/{task_id}/undo")
async def tasks_undo(task_id: Optional[int] = None):
    from .hands import Hands
    return Hands(get_db()).undo_last(task_id)


@app.get("/api/artifacts/{artifact_id}")
async def artifact_download(artifact_id: int):
    db = get_db()
    a = db.q1("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
    if not a:
        raise HTTPException(404, "artifact not found")
    p = cfg.data_path(a["storage_path"])
    if not p.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(str(p), media_type=a["mime"], filename=Path(a["storage_path"]).name)


# =========================================================================== #
# MEMORY
# =========================================================================== #
@app.get("/api/memory/atoms")
async def memory_atoms(q: str = "", kind: str = "", limit: int = 100):
    db = get_db()
    sql = "SELECT * FROM atoms WHERE status='active'"
    params: list = []
    if kind:
        sql += " AND kind=?"
        params.append(kind)
    if q:
        sql += " AND text LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY strength DESC, created_ts DESC LIMIT ?"
    params.append(limit)
    return {"atoms": db.q(sql, params)}


@app.get("/api/memory/claims")
async def memory_claims():
    return {"claims": Psyche(get_db()).belief_cards(50)}


@app.get("/api/memory/tensions")
async def memory_tensions():
    return {"tensions": Psyche(get_db()).tension_cards()}


@app.get("/api/memory/constraints")
async def memory_constraints():
    return {"constraints": River(get_db()).active_constraints()}


@app.get("/api/memory/open_loops")
async def memory_open_loops():
    return {"loops": River(get_db()).open_loops(50)}


@app.post("/api/memory/rate")
async def memory_rate(payload: dict):
    db = get_db()
    river = River(db)
    score = payload.get("score")
    direction = payload.get("direction")
    if payload.get("atom_id"):
        river.record("rating", "user",
                     {"atom_id": payload["atom_id"], "score": score, "direction": direction},
                     source_weight=cfg.get("beliefs.rating_weight", 2.0))
    if payload.get("claim_id"):
        river.record("rating", "user",
                     {"claim_id": payload["claim_id"], "score": score, "direction": direction},
                     source_weight=cfg.get("beliefs.rating_weight", 2.0))
    return {"ok": True}


@app.post("/api/memory/edit")
async def memory_edit(payload: dict):
    """One-click edit — an APPEND (supersede event), never an UPDATE."""
    db = get_db()
    atom = db.q1("SELECT * FROM atoms WHERE atom_id=?", (payload["atom_id"],))
    if not atom:
        raise HTTPException(404, "atom not found")
    river = River(db)
    eid = river.record("correction", "user",
                       {"text": f"Edit: {atom['text']} → {payload['text']}",
                        "atom_id": atom["atom_id"]}, 10.0)
    river.record("memory_write", "user",
                 {"atom": {"kind": atom["kind"], "text": payload["text"],
                           "importance": atom["importance"],
                           "entities": json.loads(atom["entity_ids"] or "[]"),
                           "scope": atom["scope"]}},
                 source_weight=10.0)
    return {"ok": True, "event_id": eid}


@app.post("/api/memory/delete")
async def memory_delete(payload: dict):
    db = get_db()
    atom = db.q1("SELECT * FROM atoms WHERE atom_id=?", (payload["atom_id"],))
    if not atom:
        raise HTTPException(404, "atom not found")
    River(db).record("correction", "user",
                     {"text": f"User deleted memory: {atom['text']}",
                      "atom_id": atom["atom_id"]}, 10.0)
    return {"ok": True}


@app.post("/api/memory/claim/edit")
async def memory_claim_edit(payload: dict):
    db = get_db()
    claim = db.q1("SELECT * FROM claims WHERE claim_id=?", (payload["claim_id"],))
    if not claim:
        raise HTTPException(404, "claim not found")
    River(db).record("correction", "user",
                     {"text": f"Edit claim: {claim['statement']} → {payload['statement']}",
                      "claim_id": claim["claim_id"]}, 10.0)
    db.exec("INSERT INTO claims(category,statement,alpha,beta,stability,priority,status,"
            "created_ts,updated_ts,evidence,user_edited) VALUES(?,?,?,?,1.0,?, 'active',?,?,?,1)",
            (claim["category"], payload["statement"], 4.0, 1.0, claim["priority"],
             time.time(), time.time(), json.dumps([str(uuid.uuid4())])))
    db.exec("UPDATE claims SET status='deprecated', updated_ts=? WHERE claim_id=?", (time.time(), claim["claim_id"]))
    return {"ok": True}


@app.post("/api/memory/tensions/{tension_id}/resolve")
async def tension_resolve(tension_id: int, payload: Optional[dict] = None):
    winner = payload.get("winner_claim_id") if payload else None
    Psyche(get_db()).resolve_tension(tension_id, winner)
    return {"ok": True}


@app.get("/api/memory/explain")
async def memory_explain(q: str = "this"):
    """'I remembered that because: entity-exact +1.4, correction-flag +2.1...'"""
    from .loom import Loom
    loom = Loom(get_db())
    recall = loom.recall(q, k=5)
    return {"explain": [{"text": s["text"], "type": s["type"], "score": s["score"],
                         "why": s["explain"]} for s in recall["slots"]],
            "confidence": recall["confidence"]}


@app.post("/api/memory/agent_sql")
async def agent_sql(payload: dict):
    """Retrieval-as-code: read-only SQL over DELTA views when recall
    confidence is low ('show me every belief you formed about me in July'
    is a GROUP BY, not a kNN)."""
    sql = payload.get("sql", "").strip().rstrip(";")
    if not sql:
        raise HTTPException(400, "empty sql")
    low = sql.lower()
    if not low.startswith("select"):
        raise HTTPException(400, "read-only: SELECT only")
    for bad in ("insert", "update", "delete", "drop", "alter", "attach", "pragma", "create"):
        if bad in low:
            raise HTTPException(400, f"read-only: {bad} not allowed")
    db = get_db()
    try:
        rows = db.q(sql, ())
    except Exception as e:
        raise HTTPException(400, f"sql error: {e}")
    return {"rows": rows[:200], "count": len(rows)}


@app.get("/api/memory/graph")
async def memory_graph(limit: int = 200):
    db = get_db()
    entities = db.q("SELECT * FROM entities ORDER BY last_mentioned_ts DESC LIMIT ?", (limit,))
    edges = db.q("SELECT * FROM edges LIMIT ?", (limit * 2,))
    return {"entities": entities, "edges": edges}


# =========================================================================== #
# FOCUS
# =========================================================================== #
@app.post("/api/focus/start")
async def focus_start(payload: dict):
    return Focus(get_db()).start(payload.get("minutes", 25),
                                 payload.get("allow", []),
                                 payload.get("voice", True),
                                 payload.get("task") or None,
                                 payload.get("why") or None,
                                 payload.get("first_step") or None)


@app.post("/api/focus/stop")
async def focus_stop():
    return Focus(get_db()).stop()


@app.post("/api/focus/mode")
async def focus_mode(payload: dict):
    """work ↔ break toggle (pomodoro). During break, drifts are not nudged."""
    return Focus(get_db()).set_mode(payload.get("mode", "work"),
                                    int(payload.get("break_min", 5)))


@app.get("/api/focus/active")
async def focus_active():
    return {"session": Focus(get_db()).active()}


@app.get("/api/focus/stats")
async def focus_stats():
    return Focus(get_db()).stats()


@app.post("/api/focus/drift")
async def focus_drift(payload: dict):
    """Ingress from the MV3 extension (also used by the panel for manual testing)."""
    return Focus(get_db()).log_drift(payload.get("url", ""), payload.get("title", ""))


# =========================================================================== #
# EXTENSION ingress
# =========================================================================== #
@app.post("/api/ext/observe")
async def ext_observe(payload: dict):
    """Every page visit → working-set ring (48h TTL). Promotion by demand only."""
    db = get_db()
    text = payload.get("text", "")
    import hashlib
    h = hashlib.sha256((payload.get("url", "") + text).encode()).hexdigest()[:16]
    River(db).record("observation", "extension",
                     {"url": payload.get("url", ""), "title": payload.get("title", ""),
                      "text_hash": h, "text": text[:400]}, 0.5)
    return {"ok": True}


@app.get("/api/ext/config")
async def ext_config():
    """Extension reads its config (focus endpoint + notification prefs)."""
    db = get_db()
    return {"endpoint": "/api/focus/drift", "channels": cfg.get("focus.nudge_channels", {})}


@app.get("/api/extension/zip")
async def extension_zip():
    """Serve the MV3 sensor extension as a zip for one-click install."""
    import io as _io
    import zipfile
    base = cfg.root / "extension"
    if not base.exists():
        raise HTTPException(404, "extension not bundled")
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.name.endswith(".pem"):
                z.writestr(f.relative_to(base).as_posix(), f.read_bytes())
    return Response(content=buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=friday-sensor.zip"})


# =========================================================================== #
# BOOKS
# =========================================================================== #
@app.post("/api/books/upload")
async def books_upload(file: UploadFile = File(...)):
    data = await file.read()
    from .books import Books
    result = await Books(get_db()).ingest(data, file.filename or "book.pdf")
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "ingest failed"))
    return result


@app.get("/api/books")
async def books_list():
    from .books import Books
    return {"books": Books(get_db()).list()}


@app.get("/api/books/{book_id}")
async def books_get(book_id: int):
    b = Books(get_db()).status(book_id)
    if not b:
        raise HTTPException(404, "book not found")
    return {"book": b}


@app.post("/api/books/{book_id}/ask")
async def books_ask(book_id: int, payload: dict, request: Request):
    """Book mode: RAG over chunks + chat. Exercises the same cortex with a
    book scope so memory + chat + voice work inside the book."""
    q = payload.get("question", "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    from .books import Books
    bk = Books(get_db())
    chunks = bk.recall_chunks(book_id, q, k=4)
    context = "\n\n".join(f"[p{int(c['page'] or 0)}] {c['text'][:600]}" for c in chunks)
    cortex = _cortex(request)
    corr = f"cor_{uuid.uuid4().hex[:8]}"
    async def gen():
        yield _sse({"type": "sense", "slots": [], "confidence": 0.9, "sense_ms": 1,
                    "now": {}, "constraints": []})
        # deterministic context + model answer (books scope → its own model)
        from .providers import make_llm as _make_llm
        blm = _make_llm("books")
        prompt = (f"Book context:\n{context}\n\nQuestion: {q}\n\n"
                  "Answer in simple terms, cite page numbers, keep it short.")
        if blm.name == "sim":
            reply = f"From the book (pages {', '.join(str(c['page']) for c in chunks)}): here's the answer — {q[:120]}...\n\n" + chunks[0]["text"][:500] if chunks else "I don't have that in the book yet."
        else:
            reply = await blm.complete(
                [{"role": "system", "content": "You answer from book context only. Cite pages. Be concise."},
                 {"role": "user", "content": prompt}])
        yield _sse({"type": "delta", "text": reply})
        yield _sse({"type": "card", "card": {"type": "book_sources",
                                             "chunks": [{"page": c["page"], "idx": c["idx"]} for c in chunks]}})
        yield _sse({"type": "done", "reply": reply, "latency_ms": 0, "cost_usd": 0,
                    "corr_id": corr, "model": cortex.llm.name, "slots_used": len(chunks)})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/books/{book_id}/quiz")
async def books_quiz(book_id: int, payload: Optional[dict] = None):
    from .books import Books
    bk = Books(get_db())
    chunks = bk.recall_chunks(book_id, "key concepts exercises", k=8)
    questions = []
    for c in chunks[:3]:
        text = c["text"]
        sentences = [s.strip() for s in text.split(". ") if len(s.strip()) > 40][:1]
        if sentences:
            questions.append({"q": f"Based on page {c['page']}: what does the text say here?",
                              "page": c["page"], "chunk_id": c["chunk_id"]})
    return {"ok": True, "questions": questions}


@app.post("/api/books/{book_id}/ppt")
async def books_ppt(book_id: int, payload: Optional[dict] = None):
    from .books import Books
    from .tools import FridaySDK
    bk = Books(get_db())
    book = bk.status(book_id)
    chunks = bk.recall_chunks(book_id, "chapter summary key points", k=12)
    slides = [{"title": book["title"] if book else "Book", "bullets": ["Auto-generated by Friday"]}]
    for c in chunks[:8]:
        sentences = [s.strip() for s in c["text"].split(". ") if len(s.strip()) > 30][:2]
        slides.append({"title": f"Page {c['page']}", "bullets": sentences or [c["text"][:100]]})
    sdk = FridaySDK(task_id=None)
    return sdk.artifact_pptx(f"{book['title'] if book else 'book'}-slides", slides)


# =========================================================================== #
# VOICE (WS barge-in + TTS)
# =========================================================================== #
@app.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):
    await ws.accept()
    voice = Voice(get_db())
    ws_id = f"ws_{uuid.uuid4().hex[:8]}"
    try:
        while True:
            raw = await ws.receive_text()
            try:
                frame = json.loads(raw)
            except json.JSONDecodeError:
                continue
            action = await voice.handle_frame(ws_id, frame)
            if action:
                await ws.send_text(json.dumps(action))
            # transcripts arrive as {type: transcript, text: ...} → run cortex
            if frame.get("type") == "transcript" and frame.get("text", "").strip():
                cortex = app.state.cortex
                corr = f"cor_{uuid.uuid4().hex[:8]}"
                queue = bus.subscribe(corr)
                async for ev in cortex.turn(frame["text"].strip(), meta={"corr_id": corr}):
                    if ev["type"] == "delta":
                        await ws.send_text(json.dumps({"type": "delta", "text": ev["text"]}))
                    elif ev["type"] == "done":
                        await ws.send_text(json.dumps({"type": "done", "reply": ev["reply"],
                                                       "corr_id": corr}))
                bus.unsubscribe(corr, queue)
    except WebSocketDisconnect:
        pass


@app.post("/api/voice/tts")
async def voice_tts(payload: dict):
    text = payload.get("text", "")[:500]
    if not text:
        raise HTTPException(400, "empty text")
    audio = await Voice(get_db()).tts(text)
    if not audio:
        raise HTTPException(503, "tts unavailable — use client speechSynthesis")
    from fastapi.responses import Response
    return Response(content=audio, media_type="audio/mpeg")


# =========================================================================== #
# NUDGES / notifications (SSE for the UI)
# =========================================================================== #
@app.get("/api/nudges")
async def nudges_list(limit: int = 30):
    db = get_db()
    return {"nudges": db.q("SELECT * FROM nudges ORDER BY created_ts DESC LIMIT ?", (limit,))}


@app.post("/api/nudges/{nudge_id}/engage")
async def nudge_engage(nudge_id: int):
    db = get_db()
    db.exec("UPDATE nudges SET engaged=1 WHERE nudge_id=?", (nudge_id,))
    return {"ok": True}


@app.get("/api/nudges/stream")
async def nudges_stream():
    """SSE for live nudges (chrome-like toasts in the UI)."""
    db = get_db()
    last = time.time()

    async def gen():
        nonlocal last
        while True:
            rows = db.q("SELECT * FROM nudges WHERE delivered=0 AND created_ts>? ORDER BY created_ts DESC LIMIT 5",
                        (last - 3600,))
            for r in rows:
                yield _sse({"type": "nudge", "nudge": r})
                db.exec("UPDATE nudges SET delivered=1 WHERE nudge_id=?", (r["nudge_id"],))
            await asyncio.sleep(3)

    return StreamingResponse(gen(), media_type="text/event-stream")


# =========================================================================== #
# GENOME surface
# =========================================================================== #
@app.get("/api/genome/log")
async def genome_log():
    return {"log": Genome(db=get_db()).log(30)}


@app.get("/api/genome/skills")
async def genome_skills():
    reg = get_registry()
    return {"skills": [{"name": s.name, "category": s.category,
                        "description": s.description[:120],
                        "tags": s.tags[:6],
                        "has_verify": s.verify is not None,
                        "has_cassette": s.cassette is not None}
                       for s in reg.all()]}


@app.get("/api/genome/explain/{skill}")
async def genome_skill(skill: str):
    reg = get_registry()
    s = reg.get(skill)
    if not s:
        raise HTTPException(404, "skill not found")
    return {"name": s.name, "category": s.category, "description": s.description,
            "body": s.body[:6000]}


@app.get("/")
async def index():
    if (UI_DIR / "index.html").exists():
        resp = FileResponse(str(UI_DIR / "index.html"))
        resp.headers["Cache-Control"] = "no-store"
        return resp
    return {"status": "ok"}


@app.get("/api/health")
async def health():
    db = get_db()
    return {"status": "ok", "version": APP_VERSION,
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "time": time.time(),
            "atoms": db.q1("SELECT COUNT(*) c FROM atoms")["c"]}


@app.get("/api/admin/searchtest")
async def searchtest(q: str = "events in ahmedabad today", debug: bool = False,
                     provider: str = "", _: bool = Depends(_admin_auth)):
    """Live-search self-test: proves real web search works from this host.
    Reports per-provider errors so blocked domains are diagnosable.
    debug=true also returns raw response samples to debug parsers."""
    import asyncio as _aio
    import httpx as _httpx
    from .providers import (BingSearch, DuckDuckGoSearch, GoogleNewsRSS)

    if debug:
        # raw probes: status + first bytes of each provider's response
        _raw_cache: dict = {}

        async def raw(name, url, params=None):
            try:
                async with _httpx.AsyncClient(timeout=15, follow_redirects=True,
                                              headers={"User-Agent": DuckDuckGoSearch.UA}) as c:
                    if params is None:
                        r = await c.post(url, data={"q": q})
                    else:
                        r = await c.get(url, params=params)
                    _raw_cache[name] = r.text
                    return {"provider": name, "status": r.status_code,
                            "len": len(r.text)}
            except Exception as e:
                return {"provider": name, "error": str(e)[:150]}

        targets = [
            ("DDG-html", "https://html.duckduckgo.com/html/", None),
            ("Bing", "https://www.bing.com/search", {"q": q}),
            ("GoogleNews", "https://news.google.com/rss/search",
             {"q": q, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}),
        ]
        if provider:
            targets = [t for t in targets if t[0].lower() == provider.lower()]
        res = await _aio.gather(*[raw(*t) for t in targets])
        # parser diagnostics: how many of each pattern matched + what parsed
        import re as _re
        for item in res:
            if "status" not in item or item["status"] != 200:
                continue
            html = _raw_cache.get(item["provider"], "")
            if not html:
                continue
            b_algo = len(_re.findall(r'<li class="b_algo"', html))
            h2a = len(_re.findall(r'<h2><a href="', html))
            ddg_result = len(_re.findall(r'class="result', html))
            ddg_a = len(_re.findall(r'class="result__a"', html))
            gnews_item = len(_re.findall(r"<item>", html))
            item["parser_diag"] = {"b_algo": b_algo, "h2_a": h2a,
                                   "ddg_result": ddg_result, "ddg_result_a": ddg_a,
                                   "gnews_item": gnews_item,
                                   "b_algo_any": len(_re.findall(r"b_algo", html)),
                                   "h2_any": len(_re.findall(r"<h2", html)),
                                   "a_href_any": len(_re.findall(r'<a href="http', html)),
                                   "captcha": len(_re.findall(r"(captcha|consent|unusual|robot)", html, _re.I)),
                                   "b_results": len(_re.findall(r'id="b_results"', html))}
        return {"debug": True, "query": q, "raw": res}

    async def try_prov(prov, name):
        try:
            res = await _aio.wait_for(prov.search(q, 5), timeout=15)
            live = [r for r in res if not r.get("fixture")]
            if live:
                return {"provider": name, "ok": True, "live": True,
                        "results": [{**r, "snippet": r.get("snippet", "")[:120]} for r in live[:3]]}
            return {"provider": name, "ok": True, "live": False,
                    "error": "fell through to fixtures"}
        except Exception as e:
            return {"provider": name, "ok": False, "error": str(e)[:150]}

    results = await _aio.gather(
        try_prov(DuckDuckGoSearch(), "DuckDuckGo"),
        try_prov(BingSearch(), "Bing"),
        try_prov(GoogleNewsRSS(), "GoogleNews"),
    )
    any_live = any(r.get("live") for r in results)
    return {"ok": any_live, "query": q, "providers": results,
            "live": any_live}


@app.get("/api/admin/prefiretest")
async def prefiretest(q: str = "what are events in ahmedabad tomorrow",
                      _: bool = Depends(_admin_auth)):
    """Direct pre-fire test: runs the REAL prefire() path (same code the chat
    uses) and reports what it produced — n, titles, error."""
    from .providers import make_search
    from .hermes import Hermes
    h = Hermes(get_db(), search=make_search())
    pf = await h.prefire(q, city="ahmedabad")
    return {"query": q,
            "core_query": h._core_query(q, "ahmedabad"),
            "n": len(pf.search),
            "titles": [r.get("title", "")[:100] for r in pf.search[:5]],
            "web_len": len(pf.web),
            "error": pf.error}


@app.get("/api/eval/report")
async def eval_report():
    """Serve the latest eval report (downloadable proof)."""
    rdir = cfg.data_path("eval_report")
    if not rdir.exists():
        return {"ok": False, "error": "no eval report yet — run friday_eval"}
    import json as _json
    j = rdir / "eval_report.json"
    if j.exists():
        d = _json.loads(j.read_text())
        return {"ok": True, "summary": {k: d[k] for k in ("generated", "scenarios", "passed", "failed", "elapsed_s")},
                "files": {f.name: f"/api/eval/file/{f.name}" for f in rdir.iterdir() if f.is_file()}}
    return {"ok": True, "files": {f.name: f"/api/eval/file/{f.name}" for f in rdir.iterdir()}}


@app.get("/api/eval/file/{name}")
async def eval_file(name: str):
    rdir = cfg.data_path("eval_report")
    p = (rdir / name).resolve()
    if not str(p).startswith(str(rdir.resolve())) or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p), filename=name)
