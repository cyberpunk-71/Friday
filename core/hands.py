"""HANDS — DAG task engine with POSTCONDITION ASSERTIONS.

FRIDAY-Δ: no Planner/Critic pre-pass. The model emits a DAG whose nodes carry
`assert` (python expression over the step result). A repair call happens ONLY
on failed assert. Speculative results are discarded free.

Free-flow with exactly 2 blocking gates (payment, gmail_send); everything
else executes now but reversible (Ledger: trash/revisions/branch+PR/session
replay) with an inline [↩ Undo] chip; blast-radius auto-escalation.
"""
from __future__ import annotations

import asyncio
import json
import time
import traceback
from typing import AsyncIterator

from .config import cfg
from .db import get_db
from .hermes import Hermes
from .obs import audit
from .tools import Forge


class Hands:
    def __init__(self, db=None, search=None, llm=None) -> None:
        self.db = db or get_db()
        self.hermes = Hermes(self.db, search)
        self.search = search
        self.llm = llm
        self.forge = Forge(search)

    # ------------------------------------------------------------------ #
    # task lifecycle
    # ------------------------------------------------------------------ #
    def create_task(self, title: str, description: str = "",
                    autonomy: str = "autonomous", priority: float = 0.5,
                    corr_id: str | None = None) -> int:
        now = time.time()
        tid = self.db.exec(
            "INSERT INTO tasks(title,description,status,task_type,autonomy,priority,created_ts,updated_ts)"
            " VALUES(?,?, 'queued','chat_request',?,?,?,?)",
            (title, description, autonomy, priority, now, now))
        audit(tid, None, "task_created", {"corr_id": corr_id})
        return tid

    async def plan_task(self, task_id: int, user_text: str, context: dict) -> list[dict]:
        """Model emits the DAG (sim provider emits a canned-but-shaped plan)."""
        steps: list[dict] = []
        if self.llm is not None:
            prompt = (
                "You are the planner. Produce a JSON DAG for this user request.\n"
                f"Request: {user_text}\nContext: {json.dumps(context, ensure_ascii=False)[:1500]}\n"
                'Return: {"steps": [{"description": str, "code": "python using friday SDK", '
                '"assert": "python bool expr over variable `result`", '
                '"blocking": false|"payment"|"gmail_send"}]} max 6 steps. '
                "Use friday.web_search/web_read for research, friday.artifact_save for deliverables, "
                "friday.mail_draft for email drafts, friday.money_quote for purchase cards. "
                "Postcondition asserts MUST be strict (e.g. result and '₹' in result).")
            try:
                out = await self.llm.complete(
                    [{"role": "system", "content": prompt}], json_mode=True, temperature=0.2)
                data = json.loads(out)
                steps = data.get("steps", [])
            except Exception:
                steps = []
        if not steps:
            steps = self._default_plan(user_text)
        self.db.exec("UPDATE tasks SET plan_json=?, status='running', updated_ts=? WHERE task_id=?",
                     (json.dumps(steps, ensure_ascii=False), time.time(), task_id))
        for i, s in enumerate(steps):
            self.db.exec(
                "INSERT INTO task_steps(task_id,step_index,description,status,assertion)"
                " VALUES(?,?,?, 'pending',?)",
                (task_id, i, s.get("description", ""), s.get("assert", "")))
        audit(task_id, None, "task_planned", {"steps": len(steps)})
        return steps

    def _default_plan(self, user_text: str) -> list[dict]:
        """Deterministic fallback plan (offline mode) — mirrors the phone research DAG."""
        low = user_text.lower()
        steps = []
        if any(k in low for k in ("research", "phone", "search", "compare", "find", "scooter")):
            steps.append({
                "description": "Research the topic with web search and produce a cited brief",
                "code": (
                    "results = await friday.web_search('" + user_text[:80].replace("'", "") + "')\n"
                    "rows = '\\n'.join(f\"- {r['title']}: {r['snippet']}\" for r in results)\n"
                    "result = friday.artifact_save('research-brief', '# Research brief\\n\\n' + rows)"),
                "assert": "result and result.get('ok')",
                "blocking": False})
        if any(k in low for k in ("draft", "email", "mail", "sarah", "sara")):
            steps.append({
                "description": "Draft the email",
                "code": (
                    "result = friday.mail_draft('sarah@example.com', 'Recommendations', "
                    "'Here is the comparison table...\\n\\n| Model | Price |\\n|---|---|\\n| Moto G85 | ₹17,999 |\\n| Redmi Note 14 | ₹18,999 |')"),
                "assert": "result and result.get('ok') and 'artifact_id' in result",
                "blocking": False})
        if any(k in low for k in ("buy", "purchase", "pay", "saree")):
            steps.append({
                "description": "Quote the item for approval",
                "code": "result = friday.money_quote('handloom saree', 8499, 'Nalli')",
                "assert": "result and result.get('ok')",
                "blocking": "payment"})
        if any(k in low for k in ("track", "monitor", "keep an eye", "keep eye", "portal")):
            m = __import__("re").search(r"keep (?:an )?eye on (.{4,80})", low)
            target = m.group(1) if m else "registration portal open"
            steps.append({
                "description": "Create a web tracker",
                "code": f"result = friday.tracker_create('availability', '{target[:80]}')",
                "assert": "result and result.get('ok')",
                "blocking": False})
        if not steps:
            steps.append({
                "description": "Research the topic",
                "code": ("results = await friday.web_search('" + user_text[:80].replace("'", "") + "')\n"
                         "result = friday.artifact_save('research-brief', str(results))"),
                "assert": "result and result.get('ok')",
                "blocking": False})
        return steps

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #
    async def execute(self, task_id: int, corr_id: str | None = None,
                      step_ids: list[int] | None = None) -> AsyncIterator[dict]:
        """Executes DAG steps; on failed assert → ONE targeted repair call."""
        steps = self.db.q("SELECT * FROM task_steps WHERE task_id=? ORDER BY step_index", (task_id,))
        plan = json.loads(self.db.q1("SELECT plan_json FROM tasks WHERE task_id=?", (task_id,))["plan_json"])
        for step in steps:
            if step_ids and step["step_id"] not in step_ids:
                continue
            if self.db.q1("SELECT status FROM tasks WHERE task_id=?", (task_id,))["status"] in ("cancelled", "failed"):
                break
            spec = plan[step["step_index"]] if step["step_index"] < len(plan) else {}
            if spec.get("blocking"):
                # the step's code RUNS first (e.g. money_quote produces the
                # approval card) — then the gate halts the DAG
                self.db.exec("UPDATE task_steps SET status='running', started_at=? WHERE step_id=?",
                             (time.time(), step["step_id"]))
                result = await self._run_step(task_id, step, spec, corr_id)
                ok = result.get("ok") and (not spec.get("assert")
                                           or self._check_assert(spec["assert"], result))
                if ok:
                    self.db.exec("UPDATE task_steps SET status='completed', completed_at=?, "
                                 "output_summary=? WHERE step_id=?",
                                 (time.time(), json.dumps(result, ensure_ascii=False)[:500],
                                  step["step_id"]))
                else:
                    self.db.exec("UPDATE task_steps SET status='failed', error=? WHERE step_id=?",
                                 (str(result.get("error", "assert failed"))[:500], step["step_id"]))
                await self._request_approval(task_id, step, spec["blocking"])
                yield {"type": "approval", "task_id": task_id, "step": step["step_index"],
                       "gate": spec["blocking"], "result": result}
                # DAG HALTS here — nothing runs past a blocking gate until approval
                return
            self.db.exec("UPDATE task_steps SET status='running', started_at=? WHERE step_id=?",
                         (time.time(), step["step_id"]))
            self.db.exec("UPDATE tasks SET status='running', updated_ts=? WHERE task_id=?", (time.time(), task_id))
            audit(task_id, step["step_id"], "step_started", {"desc": step["description"]})
            result = await self._run_step(task_id, step, spec, corr_id)
            ok = result.get("ok")
            if ok and spec.get("assert"):
                ok = self._check_assert(spec["assert"], result)
            if ok:
                self.db.exec("UPDATE task_steps SET status='completed', completed_at=?, output_summary=? "
                             "WHERE step_id=?", (time.time(), json.dumps(result, ensure_ascii=False)[:500], step["step_id"]))
                yield {"type": "step_done", "task_id": task_id, "step": step["step_index"],
                       "result": result}
            else:
                err = result.get("error", "assert failed")
                # ONE targeted repair call — only on failure
                repaired = await self._repair(task_id, step, spec, err, corr_id)
                if repaired:
                    self.db.exec("UPDATE task_steps SET status='completed', completed_at=?, output_summary=? "
                                 "WHERE step_id=?", (time.time(), json.dumps(repaired, ensure_ascii=False)[:500], step["step_id"]))
                    yield {"type": "step_repaired", "task_id": task_id, "step": step["step_index"],
                           "result": repaired}
                else:
                    self.db.exec("UPDATE task_steps SET status='failed', error=? WHERE step_id=?",
                                 (str(err)[:500], step["step_id"]))
                    self.db.exec("UPDATE tasks SET status='failed', updated_ts=? WHERE task_id=?",
                                 (time.time(), task_id))
                    audit(task_id, step["step_id"], "step_failed", {"error": str(err)[:400]})
                    yield {"type": "step_failed", "task_id": task_id, "step": step["step_index"],
                           "error": str(err)[:400]}
                    return

        # completion
        self.db.exec("UPDATE tasks SET status='completed', updated_ts=?, completed_ts=? WHERE task_id=?",
                     (time.time(), time.time(), task_id))
        audit(task_id, None, "task_completed", {})
        yield {"type": "done", "task_id": task_id}

    async def _run_step(self, task_id: int, step: dict, spec: dict, corr_id: str | None) -> dict:
        self.forge.task_id = task_id
        self.forge.corr_id = corr_id
        return await self.forge.run(spec.get("code", ""), timeout_s=90)

    @staticmethod
    def _check_assert(expr: str, result: dict) -> bool:
        try:
            return bool(eval(expr, {"result": result, "json": json}))
        except Exception:
            return False

    async def _repair(self, task_id: int, step: dict, spec: dict, err: str,
                      corr_id: str | None) -> dict | None:
        """Targeted repair: only the failing node, with the assert error."""
        repairs = self.db.get_setting(f"repairs.task{task_id}.step{step['step_id']}", 0)
        if repairs >= 2:
            return None
        self.db.set_setting(f"repairs.task{task_id}.step{step['step_id']}", repairs + 1)
        audit(task_id, step["step_id"], "repair_attempt", {"error": str(err)[:300]})
        if self.llm is not None:
            prompt = (
                "Your code step failed its postcondition. Fix ONLY this step.\n"
                f"Error: {err}\nStep code:\n{spec.get('code','')}\n"
                "Return JSON: {\"code\": \"fixed python using friday SDK\"}")
            try:
                out = await self.llm.complete([{"role": "user", "content": prompt}],
                                              json_mode=True, temperature=0.2)
                new_code = json.loads(out).get("code", "")
                if new_code:
                    spec["code"] = new_code
            except Exception:
                pass
        res = await self._run_step(task_id, step, spec, corr_id)
        if res.get("ok") and (not spec.get("assert") or self._check_assert(spec["assert"], res)):
            return res
        return None

    async def _request_approval(self, task_id: int, step: dict, gate: str) -> None:
        self.db.exec("UPDATE tasks SET status='waiting_approval', approval_kind=?, "
                     "approval_detail=?, updated_ts=? WHERE task_id=?",
                     (gate, step["description"], time.time(), task_id))
        # the gated step's code already ran (e.g. money_quote) — mark it
        # awaiting only if it hasn't completed
        st = self.db.q1("SELECT status FROM task_steps WHERE step_id=?", (step["step_id"],))
        if st and st["status"] != "completed":
            self.db.exec("UPDATE task_steps SET status='awaiting_approval' WHERE step_id=?",
                         (step["step_id"],))
        audit(task_id, step["step_id"], "approval_requested", {"gate": gate})

    # ------------------------------------------------------------------ #
    # approvals
    # ------------------------------------------------------------------ #
    async def approve(self, task_id: int) -> dict:
        row = self.db.q1("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if not row or row["status"] != "waiting_approval":
            return {"ok": False, "error": "not awaiting approval"}
        gate = row["approval_kind"]
        step = self.db.q1("SELECT * FROM task_steps WHERE task_id=? AND status='awaiting_approval' "
                          "ORDER BY step_index LIMIT 1", (task_id,))
        audit(task_id, step["step_id"] if step else None, "approval_granted", {"gate": gate})
        if gate == "payment":
            self._simulate_payment(task_id)
        if gate == "gmail_send":
            self._simulate_send(task_id)
        self.db.exec("UPDATE tasks SET status='running', updated_ts=? WHERE task_id=?",
                     (time.time(), task_id))
        # continue the rest of the DAG
        remaining = self.db.q("SELECT step_id FROM task_steps WHERE task_id=? AND status IN "
                              "('pending','awaiting_approval') ORDER BY step_index", (task_id,))
        return {"ok": True, "task_id": task_id, "resume_step_ids": [r["step_id"] for r in remaining]}

    def _simulate_payment(self, task_id: int) -> None:
        row = self.db.q1("SELECT title FROM tasks WHERE task_id=?", (task_id,))
        self.db.append_event("tool_result", "hermes",
                             {"tool": "money.pay", "task_id": task_id,
                              "outputs": {"paid": True, "title": row["title"]}}, 1.0)
        audit(task_id, None, "payment_executed", {"title": row["title"]})

    def _simulate_send(self, task_id: int) -> None:
        self.db.append_event("tool_result", "hermes",
                             {"tool": "mail.send", "task_id": task_id, "outputs": {"sent": True}}, 1.0)
        audit(task_id, None, "email_sent", {})

    def reject(self, task_id: int) -> dict:
        self.db.exec("UPDATE tasks SET status='failed', updated_ts=? WHERE task_id=?",
                     (time.time(), task_id))
        audit(task_id, None, "approval_rejected", {})
        return {"ok": True}

    # ------------------------------------------------------------------ #
    # undo (ledger chip)
    # ------------------------------------------------------------------ #
    def undo_last(self, task_id: int | None = None) -> dict:
        """Reversible actions are undone by restoring from ledger side-effects."""
        rows = self.db.q("SELECT * FROM events WHERE kind='tool_result' "
                         "ORDER BY ts DESC LIMIT 5")
        for ev in rows:
            payload = json.loads(ev["payload"])
            if payload.get("tool") == "files.delete" and task_id in (None, payload.get("task_id")):
                trash_path = payload.get("outputs", {}).get("trashed", "")
                if trash_path and self._restore_from_trash(trash_path):
                    self.db.append_event("undo", "user", {"target_event": ev["id"]}, 1.0)
                    return {"ok": True, "undone": ev["id"], "what": trash_path}
        return {"ok": False, "error": "nothing reversible to undo"}

    def _restore_from_trash(self, trash_path: str) -> bool:
        import os
        p = cfg.data_dir / trash_path
        if not p.exists():
            return False
        # reconstruct original path (trash layout: .friday-trash/<date>/<name>)
        name = p.name
        orig = cfg.data_dir / name
        os.makedirs(orig.parent, exist_ok=True)
        import shutil
        shutil.move(str(p), str(orig))
        return True
