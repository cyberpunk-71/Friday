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
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (Depends, FastAPI, File, Form, HTTPException, Query, Request,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import cfg
from .cortex import Cortex, bus
from .db import get_db
from .focus import Focus
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
    """SSE stream: sense → ctrl → deltas → cards → done. Never blocks on tasks."""
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "empty text")
    book_id = payload.get("book_id")
    meta = {"corr_id": payload.get("corr_id") or f"cor_{uuid.uuid4().hex[:8]}"}
    cortex = _cortex(request)

    async def gen():
        corr_id = meta["corr_id"]
        queue = bus.subscribe(corr_id)
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
async def admin_overview(_: bool = Depends(_admin_auth)):
    db = get_db()
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
    return {
        "version": APP_VERSION, "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "llm_provider": os.environ.get("DEEPSEEK_API_KEY", "") and "deepseek" or "sim(offline)",
        "search_provider": os.environ.get("SEARCH_PROVIDER", "sim"),
        "spend_today_usd": round(spend, 4), "daily_budget_usd": daily,
        "budget_pct": round(100 * spend / max(0.01, daily), 1),
        "ask_budget_left": hermes.ask_budget_left(),
        "counts": counts, "provider_keys": keys,
        "genome_head": db.get_setting("genome.head"),
        "genome_log": Genome(db=db).log(8),
        "gym": db.get_setting("nightly.last_report"),
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
    # if this is THE deepseek key, wire it live
    if provider == "deepseek" and scope == "default":
        os.environ["DEEPSEEK_API_KEY"] = key
        from .providers import DeepSeekProvider
        request.app.state.cortex.llm = DeepSeekProvider(api_key=key)
    return {"ok": True, "provider": provider, "scope": scope, "masked": f"••••{key[-4:]}"}


@app.post("/api/admin/providers/test")
async def providers_test(payload: dict | None = None, _: bool = Depends(_admin_auth)):
    from .providers import DeepSeekProvider
    key = payload.get("api_key") if payload else None
    p = DeepSeekProvider(api_key=key)
    if not p.available:
        return {"ok": False, "error": "no key configured"}
    try:
        out = await p.complete([{"role": "user", "content": "ping"}], max_tokens=5)
        return {"ok": True, "reply": out[:50]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


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
async def tasks_list(status: str | None = None):
    db = get_db()
    if status:
        rows = db.q("SELECT * FROM tasks WHERE status=? ORDER BY updated_ts DESC LIMIT 100", (status,))
    else:
        rows = db.q("SELECT * FROM tasks ORDER BY updated_ts DESC LIMIT 100")
    for r in rows:
        r["steps"] = db.q("SELECT * FROM task_steps WHERE task_id=? ORDER BY step_index", (r["task_id"],))
        r["artifacts"] = db.q("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_ts DESC", (r["task_id"],))
    return {"tasks": rows}


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
async def tasks_undo(task_id: int | None = None):
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
async def tension_resolve(tension_id: int, payload: dict | None = None):
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
                                 payload.get("allow", []), payload.get("voice", True))


@app.post("/api/focus/stop")
async def focus_stop():
    return Focus(get_db()).stop()


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
        # deterministic context + model answer
        prompt = (f"Book context:\n{context}\n\nQuestion: {q}\n\n"
                  "Answer in simple terms, cite page numbers, keep it short.")
        if cortex.llm.name == "sim":
            reply = f"From the book (pages {', '.join(str(c['page']) for c in chunks)}): here's the answer — {q[:120]}...\n\n" + chunks[0]["text"][:500] if chunks else "I don't have that in the book yet."
        else:
            reply = await cortex.llm.complete(
                [{"role": "system", "content": "You answer from book context only. Cite pages. Be concise."},
                 {"role": "user", "content": prompt}])
        yield _sse({"type": "delta", "text": reply})
        yield _sse({"type": "card", "card": {"type": "book_sources",
                                             "chunks": [{"page": c["page"], "idx": c["idx"]} for c in chunks]}})
        yield _sse({"type": "done", "reply": reply, "latency_ms": 0, "cost_usd": 0,
                    "corr_id": corr, "model": cortex.llm.name, "slots_used": len(chunks)})
    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/books/{book_id}/quiz")
async def books_quiz(book_id: int, payload: dict | None = None):
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
async def books_ppt(book_id: int, payload: dict | None = None):
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
    return FileResponse(str(UI_DIR / "index.html")) if (UI_DIR / "index.html").exists() else {"status": "ok"}


@app.get("/api/health")
async def health():
    db = get_db()
    return {"status": "ok", "version": APP_VERSION,
            "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
            "time": time.time(),
            "atoms": db.q1("SELECT COUNT(*) c FROM atoms")["c"]}
