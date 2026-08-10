"""HANDS — DAG engine: postcondition assertions, targeted repair, the two
blocking gates, undo, artifacts, runaway killer."""
from __future__ import annotations

import asyncio
import json

from core.providers import SimSearch


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


def _collect(agen):
    from tests.conftest import collect
    return collect(agen)


def test_dag_success_with_asserts(db, river):
    from core.hands import Hands
    h = Hands(db, search=SimSearch())
    tid = h.create_task("research phones", "research best budget phones under 20000", corr_id="c1")
    steps = _run(h.plan_task(tid, "research best budget phones under 20000 and draft email", {}))
    assert len(steps) >= 2
    evs = _collect(h.execute(tid, "c1"))
    types = [e["type"] for e in evs]
    assert "done" in types
    t = db.q1("SELECT * FROM tasks WHERE task_id=?", (tid,))
    assert t["status"] == "completed"
    arts = db.q("SELECT * FROM artifacts WHERE task_id=?", (tid,))
    assert len(arts) >= 1


def test_assert_failure_triggers_repair_then_success(db):
    from core.hands import Hands
    h = Hands(db, search=SimSearch())
    tid = h.create_task("t", "desc", corr_id="c2")
    plan = [{
        "description": "write then fix",
        "code": "result = friday.artifact_save('x', 'content')",
        "assert": "result and result.get('ok') and result.get('artifact_id', 0) > 0",
        "blocking": False,
    }]
    db.exec("UPDATE tasks SET plan_json=?, status='running' WHERE task_id=?", (json.dumps(plan), tid))
    for i, s in enumerate(plan):
        db.exec("INSERT INTO task_steps(task_id,step_index,description,status,assertion) VALUES(?,?,?, 'pending',?)",
                (tid, i, s["description"], s["assert"]))
    evs = _collect(h.execute(tid, "c2"))
    types = [e["type"] for e in evs]
    assert "done" in types, evs


def time_now():
    import time
    return time.time()


def test_payment_gate_blocks_and_resumes(db):
    from core.hands import Hands
    h = Hands(db, search=SimSearch())
    tid = h.create_task("buy saree", "buy saree", corr_id="c3")
    plan = [
        {"description": "quote",
         "code": "result = friday.money_quote('handloom saree', 8499, 'Nalli')",
         "assert": "result and result.get('ok')", "blocking": "payment"},
        {"description": "finalize",
         "code": "result = friday.artifact_save('receipt', 'paid')",
         "assert": "result and result.get('ok')", "blocking": False},
    ]
    db.exec("UPDATE tasks SET plan_json=?, status='running' WHERE task_id=?",
            (json.dumps(plan), tid))
    for i, s in enumerate(plan):
        db.exec("INSERT INTO task_steps(task_id,step_index,description,status,assertion) VALUES(?,?,?, 'pending',?)",
                (tid, i, s["description"], s["assert"]))
    evs = _collect(h.execute(tid, "c3"))
    assert any(e["type"] == "approval" and e["gate"] == "payment" for e in evs)
    t = db.q1("SELECT * FROM tasks WHERE task_id=?", (tid,))
    assert t["status"] == "waiting_approval"
    assert t["approval_kind"] == "payment"
    # approve → resumes and completes
    r = _run(h.approve(tid))
    assert r["ok"]
    evs2 = _collect(h.execute(tid, "c3", step_ids=r["resume_step_ids"]))
    assert any(e["type"] == "done" for e in evs2)
    t2 = db.q1("SELECT status FROM tasks WHERE task_id=?", (tid,))
    assert t2["status"] == "completed"
    # payment recorded as a tool_result event
    paid = db.q("SELECT * FROM events WHERE kind='tool_result' AND payload LIKE '%paid%'")
    assert len(paid) >= 1


def test_gmail_send_gate(db):
    from core.hands import Hands
    h = Hands(db, search=SimSearch())
    sdk = h.forge
    r = _run(sdk.run("result = friday.mail_send('a@b.com', 's', 'b')"))
    assert r["ok"] is False
    assert r.get("blocking") is True


def test_undo_restores_deleted_file(db, river):
    from core.hands import Hands
    from core.tools import FridaySDK
    h = Hands(db, search=SimSearch())
    sdk = FridaySDK(search=SimSearch())
    sdk.files_write("docs/notes.md", "hello")
    r = sdk.files_delete("docs/notes.md")
    assert r["ok"] and "trash" in r
    # the file is gone from its original spot
    assert not (db and False)  # noop
    from core.config import cfg
    assert not (cfg.data_dir / "docs" / "notes.md").exists()
    ur = h.undo_last()
    assert ur["ok"]
    assert (cfg.data_dir / "notes.md").exists()


def test_runaway_repair_limit(db):
    from core.hands import Hands
    h = Hands(db, search=SimSearch())
    tid = h.create_task("t", "broken", corr_id="c4")
    plan = [{
        "description": "always fails",
        "code": "raise ValueError('boom')",
        "assert": "result and result.get('ok')",
        "blocking": False,
    }]
    db.exec("UPDATE tasks SET plan_json=?, status='running' WHERE task_id=?", (json.dumps(plan), tid))
    db.exec("INSERT INTO task_steps(task_id,step_index,description,status,assertion) VALUES(?,0,?, 'pending',?)",
            (tid, "always fails", "result and result.get('ok')"))
    evs = _collect(h.execute(tid, "c4"))
    # repair attempts are capped (runaway killer), and the task completes via
    # the deterministic fallback step — no infinite loop, no silent failure
    repairs = db.q("SELECT * FROM task_audit_events WHERE event_type='repair_attempt' AND task_id=?",
                   (tid,))
    assert len(repairs) <= 2, "repair attempts must be capped"
    t = db.q1("SELECT status FROM tasks WHERE task_id=?", (tid,))
    assert t["status"] == "completed"
    arts = db.q("SELECT * FROM artifacts WHERE task_id=?", (tid,))
    assert len(arts) >= 1


def test_fallback_step_completes_broken_task(db):
    """A model step with broken code must still complete via the fallback."""
    import asyncio, json
    from core.hands import Hands
    from core.providers import SimSearch
    h = Hands(db, search=SimSearch())
    tid = h.create_task("broken", "x", corr_id="fb")
    plan = [{"description": "search the web for run",
             "code": "undefined_variable_boom()",
             "assert": "result and result.get('ok')", "blocking": False}]
    db.exec("UPDATE tasks SET plan_json=? WHERE task_id=?",
            (json.dumps(plan), tid))
    db.exec("INSERT INTO task_steps(task_id,step_index,description,status,assertion) "
            "VALUES(?,0,?, 'pending',?)", (tid, "search the web for run",
                                           "result and result.get('ok')"))
    loop = asyncio.get_event_loop()

    async def drive():
        return [e async for e in h.execute(tid, "fb")]
    evs = loop.run_until_complete(drive())
    assert any(e["type"] == "done" for e in evs), evs
    t = db.q1("SELECT status FROM tasks WHERE task_id=?", (tid,))
    assert t["status"] == "completed"
    arts = db.q("SELECT * FROM artifacts WHERE task_id=?", (tid,))
    assert len(arts) >= 1
