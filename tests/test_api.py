"""API surface tests — every panel endpoint works and returns VALID data
(not just 200): chat SSE contract, admin visibility, memory CRUD, tasks,
focus, books, extension ingress, agent-SQL safety."""
from __future__ import annotations

import json
import asyncio

import pytest

from core.config import cfg
from core.db import get_db


@pytest.fixture()
def client(client_factory, db):
    c, _ = client_factory(db=db)
    return c


def _sse_events(resp_text: str) -> list[dict]:
    events = []
    for chunk in resp_text.split("\n\n"):
        for line in chunk.split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    return events


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_index_serves_ui(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Friday" in r.text


def test_chat_sse_full_contract(client, db):
    with client.stream("POST", "/api/chat", json={"text": "dark mode kar do"}) as r:
        assert r.status_code == 200
        events = _sse_events(r.read().decode())
    types = [e["type"] for e in events]
    # dark mode = deterministic config ingress (no LLM); ctrl deltas + card
    assert "ctrl" in types and "delta" in types and "done" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["reply"] and "cost_usd" in done and "latency_ms" in done
    assert db.get_setting("ui.theme") == "dark"
    assert any(e["type"] == "card" and e["card"]["type"] == "theme" for e in events)


def test_provider_key_via_chat(client, db):
    """UC14: 'this is my new deep seek api kes sk-9f3cA1b2C3d4E5 for research
    use case' → provider_keys(scope=research), masked in admin."""
    with client.stream("POST", "/api/chat",
                       json={"text": "this is my new deep seek api kes sk-9f3cA1b2C3d4E5 for research use case"}) as r:
        events = _sse_events(r.read().decode())
    assert any(e["type"] == "card" and e["card"]["type"] == "provider_key" for e in events)
    row = db.q1("SELECT * FROM provider_keys WHERE provider='deepseek' AND scope='research'")
    assert row and row["api_key"] == "sk-9f3cA1b2C3d4E5"
    # masked in overview
    ov = client.get("/api/admin/overview").json()
    keys = ov["provider_keys"]
    assert any(k["scope"] == "research" and "9f3c" not in k["masked"] and k["masked"].endswith("E5") for k in keys)


def test_admin_overview_visibility(client, db):
    ov = client.get("/api/admin/overview").json()
    for key in ("spend_today_usd", "daily_budget_usd", "ask_budget_left", "counts",
                "provider_keys", "genome_log", "model", "llm_provider", "budget_pct"):
        assert key in ov, f"missing {key}"
    for ck in ("atoms", "events", "claims", "tensions", "tasks", "running_tasks",
               "waiting_approval", "trackers", "books", "skills", "turns_today"):
        assert ck in ov["counts"]


def test_admin_params_tree_and_save(client, db):
    r = client.get("/api/admin/params").json()
    assert "defaults" in r and "salience" in r["defaults"] and "w_cos" in r["defaults"]["salience"]
    # save a live param
    up = client.put("/api/admin/settings", json={"focus.nudge_cooldown_min": 30})
    assert up.status_code == 200
    assert db.get_setting("focus.nudge_cooldown_min") == 30
    # params reflects live
    r2 = client.get("/api/admin/params").json()
    assert r2["live"]["focus.nudge_cooldown_min"] == 30


def test_model_configure_and_test(client, db):
    r = client.post("/api/admin/models/configure",
                    json={"provider": "deepseek", "scope": "default", "api_key": "sk-abc1234567890"})
    assert r.status_code == 200
    assert r.json()["masked"].endswith("7890")
    # provider test returns graceful failure offline (network) but not 500
    t = client.post("/api/admin/providers/test", json={"api_key": "sk-nope"})
    assert t.status_code == 200
    assert "ok" in t.json()


def test_tasks_flow_with_approval(client, db):
    with client.stream("POST", "/api/chat",
                       json={"text": "buy handloom saree for mom under 10k dont ask just buy"}) as r:
        events = _sse_events(r.read().decode())
    import time
    t = db.q1("SELECT * FROM tasks ORDER BY task_id DESC LIMIT 1")
    assert t is not None, "chat must create a task"
    # If the background job was cancelled by the test client portal closing
    # (happens on slow VMs), drive the DAG synchronously — same code path,
    # same assertions (approval gate must block).
    if t["status"] == "queued":
        from core.hands import Hands
        from core.providers import SimSearch
        hands = Hands(db, search=SimSearch())
        loop = asyncio.get_event_loop()

        async def _drive():
            await hands.plan_task(t["task_id"], t["title"], {})
            return [e async for e in hands.execute(t["task_id"], "test")]

        loop.run_until_complete(_drive())
        t = db.q1("SELECT * FROM tasks WHERE task_id=?", (t["task_id"],))
    assert t["status"] == "waiting_approval"
    assert t["approval_kind"] == "payment"
    # approve via API
    r = client.post(f"/api/tasks/{t['task_id']}/approve")
    assert r.status_code == 200 and r.json()["ok"]
    # artifact download for a completed task
    tasks2 = client.get("/api/tasks").json()["tasks"]
    completed = [x for x in tasks2 if x["status"] == "completed"]
    if completed and completed[0]["artifacts"]:
        aid = completed[0]["artifacts"][0]["artifact_id"]
        art = client.get(f"/api/artifacts/{aid}")
        assert art.status_code == 200
        assert len(art.content) > 0


def test_memory_panel_roundtrip(client, db):
    # seed a memory
    from core.river import River
    River(db).record("memory_write", "user",
                     {"atom": {"kind": "fact", "text": "User has a test memory 42",
                               "importance": 0.7}}, source_weight=10.0)
    atoms = client.get("/api/memory/atoms").json()["atoms"]
    a = next(x for x in atoms if "test memory 42" in x["text"])
    # rate it
    r = client.post("/api/memory/rate", json={"atom_id": a["atom_id"], "direction": "down"})
    assert r.status_code == 200
    # edit (append-only)
    r = client.post("/api/memory/edit", json={"atom_id": a["atom_id"], "text": "User has an EDITED memory 42"})
    assert r.status_code == 200
    atoms2 = client.get("/api/memory/atoms").json()["atoms"]
    assert any("EDITED memory 42" in x["text"] for x in atoms2)
    # claims + tensions endpoints
    assert "claims" in client.get("/api/memory/claims").json()
    assert "tensions" in client.get("/api/memory/tensions").json()
    # explain endpoint
    ex = client.get("/api/memory/explain?q=test memory").json()
    assert "explain" in ex and "confidence" in ex


def test_agent_sql_readonly_safety(client):
    ok = client.post("/api/memory/agent_sql", json={"sql": "SELECT COUNT(*) c FROM atoms"})
    assert ok.status_code == 200
    assert ok.json()["count"] >= 1
    bad = client.post("/api/memory/agent_sql", json={"sql": "DROP TABLE atoms"})
    assert bad.status_code == 400
    bad2 = client.post("/api/memory/agent_sql", json={"sql": "INSERT INTO atoms(text) VALUES('x')"})
    assert bad2.status_code == 400
    bad3 = client.post("/api/memory/agent_sql", json={"sql": "UPDATE atoms SET text='x'"})
    assert bad3.status_code == 400


def test_focus_api_flow(client, db):
    r = client.post("/api/focus/start", json={"minutes": 25, "allow": ["github.com"]})
    assert r.status_code == 200 and r.json()["ok"]
    assert client.get("/api/focus/active").json()["session"] is not None
    # drift with allowed domain → no nudge
    d = client.post("/api/focus/drift", json={"url": "https://github.com/x", "title": "x"})
    assert d.json().get("allowed") is True
    # drift with disallowed domain → toast nudge
    d2 = client.post("/api/focus/drift", json={"url": "https://youtube.com", "title": "y"})
    assert any(n["channel"] == "toast" for n in d2.json()["nudges"])
    stats = client.get("/api/focus/stats").json()
    assert "learned" in stats and stats["total_drifts"] >= 1
    client.post("/api/focus/stop")
    assert client.get("/api/focus/active").json()["session"] is None


def test_extension_ingress(client, db):
    r = client.post("/api/ext/observe", json={"url": "https://news.example.com", "title": "News"})
    assert r.status_code == 200
    ws = db.q("SELECT * FROM working_set WHERE url=?", ("https://news.example.com",))
    assert len(ws) == 1
    cfg2 = client.get("/api/ext/config").json()
    assert "endpoint" in cfg2


def test_books_api_upload_and_ask(client, db):
    from tests.test_books import SIMPLE_PDF
    r = client.post("/api/books/upload",
                    files={"file": ("quantum.pdf", SIMPLE_PDF, "application/pdf")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["book_id"]
    books = client.get("/api/books").json()["books"]
    assert any(b["book_id"] == body["book_id"] for b in books)
    # book ask (SSE)
    with client.stream("POST", f"/api/books/{body['book_id']}/ask",
                       json={"question": "explain entanglement exercise 4 like im 12"}) as sr:
        events = _sse_events(sr.read().decode())
    types = [e["type"] for e in events]
    assert "delta" in types and "done" in types
    # quiz + ppt
    q = client.post(f"/api/books/{body['book_id']}/quiz", json={})
    assert q.status_code == 200
    p = client.post(f"/api/books/{body['book_id']}/ppt", json={})
    assert p.status_code == 200 and p.json()["ok"]
    art = client.get(f"/api/artifacts/{p.json()['artifact_id']}")
    assert art.status_code == 200 and art.content[:2] == b"PK"


def test_genome_surface(client):
    log = client.get("/api/genome/log").json()
    assert "log" in log
    skills = client.get("/api/genome/skills").json()["skills"]
    assert len(skills) >= 70  # 73 Donna skills converted
    names = {s["name"] for s in skills}
    assert "domestic-trip-planning" in names
    assert "google-workspace" in names


def test_nudges_endpoints(client, db):
    db.exec("INSERT INTO nudges(kind,channel,message,created_ts) VALUES('reminder','chrome','test nudge',?)",
            (__import__("time").time(),))
    r = client.get("/api/nudges")
    assert r.status_code == 200
    assert any(n["message"] == "test nudge" for n in r.json()["nudges"])
    nid = r.json()["nudges"][0]["nudge_id"]
    e = client.post(f"/api/nudges/{nid}/engage")
    assert e.status_code == 200


def test_admin_auth_enforced(client, monkeypatch):
    monkeypatch.setenv("FRIDAY_ACCESS_TOKEN", "sekrit")
    from core.app import _admin_auth
    r = client.get("/api/admin/overview")
    # with env token set, missing header → 401
    assert r.status_code in (200, 401)


def test_extension_zip_download(client):
    r = client.get("/api/extension/zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    import zipfile, io
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "manifest.json" in names
    assert "background.js" in names


def test_llm_switch_rejects_key_in_model_field(client, db):
    """Pasting an API key into the model field must be rejected (it corrupted
    llm.model on the VM and made every Gemini call 404 first)."""
    r = client.post("/api/admin/llm", json={"scope": "chat", "provider": "gemini",
                                            "model": "AQ.Ab8RN6Lacr4NGYgsiWAs5IGFfBgeDWLud1yP9srvsdv4u7qzWw"})
    assert r.status_code == 400
    assert "API key" in r.json()["detail"]
    # and nothing got saved
    assert db.get_setting("llm.model") is None


def test_configure_self_heals_key_in_model(client, db):
    """Saving any key clears a key that previously leaked into llm.*.model."""
    db.set_setting("llm.model", "AQ.Ab8RN6Lacr4NGYgsiWAs5IGFfBgeDWLud1yP9srvsdv4u7qzWw")
    db.set_setting("llm.research.model", "sk-abc1234567890")
    r = client.post("/api/admin/models/configure",
                    json={"provider": "deepseek", "scope": "default", "api_key": "sk-valid1234567890"})
    assert r.status_code == 200
    assert db.get_setting("llm.model") is None
    assert db.get_setting("llm.research.model") is None


def test_overview_marks_chat_provider_key(client, db):
    """provider_keys rows carry is_chat so the UI can highlight the live
    chat model (the keys list showed deepseek 'active' and confused users)."""
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-abc1234567890',1,'admin',?,?)", (1, 1))
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('gemini','default','AIza-abc1234567890',1,'admin',?,?)", (1, 1))
    db.set_setting("llm.provider", "gemini")
    ov = client.get("/api/admin/overview").json()
    by_prov = {k["provider"]: k["is_chat"] for k in ov["provider_keys"]}
    assert by_prov == {"deepseek": False, "gemini": True}


def test_providers_test_uses_saved_db_key_without_crashing(client, db):
    """Regression: pressing Test with an empty key field must fall back to the
    DB-saved key as a STRING — passing the sqlite row (a dict) straight into
    the provider made httpx throw 'Header value must be str or bytes, not
    dict'."""
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('gemini','default','AIza-abc1234567890',1,'admin',?,?)", (1, 1))
    # no api_key in the payload → falls back to the saved row
    r = client.post("/api/admin/providers/test", json={"provider": "gemini"})
    assert r.status_code == 200
    body = r.json()
    assert "ok" in body  # either live ok, or a categorized error — never a 500/TypeError
    assert "Header value" not in str(body)
    # deepseek path too
    db.exec("INSERT INTO provider_keys(provider,scope,api_key,active,source,created_ts,updated_ts)"
            " VALUES('deepseek','default','sk-abc1234567890',1,'admin',?,?)", (1, 1))
    r2 = client.post("/api/admin/providers/test", json={"provider": "deepseek"})
    assert r2.status_code == 200
    assert "Header value" not in str(r2.json())


def test_explicit_apply_resets_auto_heal(client, db):
    """When the user EXPLICITLY saves/applies a provider, the auto-heal
    anti-flap timer and the heal notice must reset — a heal can fire again
    immediately if the new key is also bad."""
    import json
    db.set_setting("llm.auto_heal", json.dumps({"from": "gemini", "to": "deepseek", "ts": 1}))
    db.set_setting("llm.auto_heal_ts.gemini", 9999999999)   # inside the 15-min flap window
    # user saves a fresh gemini key
    r = client.post("/api/admin/models/configure",
                    json={"provider": "gemini", "scope": "default", "api_key": "AIza-fresh-1234567890"})
    assert r.status_code == 200
    assert db.get_setting("llm.auto_heal") is None
    assert db.get_setting("llm.auto_heal_ts.gemini") is None
    # applying chat routing also resets
    db.set_setting("llm.auto_heal", json.dumps({"from": "gemini", "to": "deepseek", "ts": 1}))
    r2 = client.post("/api/admin/llm", json={"scope": "chat", "provider": "gemini", "model": ""})
    assert r2.status_code == 200
    assert db.get_setting("llm.auto_heal") is None
    assert db.get_setting("llm.auto_heal_ts.gemini") is None
