"""The `friday` SDK — 12 verbs + ~40 modules. FRIDAY-Δ: the model writes PYTHON
against this SDK inside the sandbox (E2B on the VM; in-process restricted
executor in tests/offline). The catalog is tiny in context (12 verbs); the
8000-app MCP catalog is vector-searched INSIDE the sandbox.

Also hosts the DAG step tool registry used by Hands.
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path

from .config import cfg
from .db import get_db
from .hermes import Hermes


class FridaySDK:
    """Exposed to sandbox code as module `friday`."""

    def __init__(self, search=None, task_id: int | None = None, corr_id: str | None = None) -> None:
        self.db = get_db()
        self.search = search
        self.task_id = task_id
        self.corr_id = corr_id
        self.hermes = Hermes(search)
        self._results: list[dict] = []
        self.artifacts_dir = cfg.data_path("artifacts")

    # ---------------- web ----------------
    async def web_search(self, query: str, max: int = 6) -> list[dict]:
        if self.search is None:
            from .providers import make_search
            self.search = make_search()
        res = await self.search.search(query, max)
        self._log_tool("web.search", {"query": query}, res)
        return res

    async def web_read(self, url: str, max_chars: int = 12000) -> str:
        from .providers import web_read
        text = await web_read(url, max_chars)
        self._log_tool("web.read", {"url": url}, {"chars": len(text)})
        return text

    # ---------------- memory ----------------
    def memory_recall(self, query: str, k: int = 8) -> list[dict]:
        from .loom import Loom
        r = Loom(self.db).recall(query, k=k)
        return r["slots"]

    def memory_write(self, kind: str, text: str, importance: float = 0.5, entities: list | None = None) -> str:
        eid = self.db.append_event(
            "utterance" if kind == "observation" else "promotion" if kind == "observation" else "observation",
            "hermes", {"text": text, "kind": kind, "importance": importance,
                       "entities": entities or []}, 0.3, corr_id=self.corr_id)
        return eid

    # ---------------- tasks / plans ----------------
    def task_create(self, title: str, description: str = "", autonomy: str = "autonomous",
                    priority: float = 0.5) -> int:
        from .hands import Hands
        return Hands(self.db).create_task(title, description, autonomy, priority, self.corr_id)

    def task_status(self, task_id: int) -> dict | None:
        return self.db.q1("SELECT task_id,title,status FROM tasks WHERE task_id=?", (task_id,))

    # ---------------- files (reversible) ----------------
    def files_write(self, path: str, content: str) -> dict:
        p = self._safe_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        self._log_tool("files.write", {"path": str(p)}, {"bytes": len(content)})
        return {"ok": True, "path": str(p)}

    def files_read(self, path: str) -> str:
        return self._safe_path(path).read_text(encoding="utf-8")

    def files_list(self, path: str = ".") -> list[str]:
        base = self._safe_path(path)
        return [str(x.relative_to(cfg.data_dir)) for x in base.rglob("*") if x.is_file()][:200]

    def files_delete(self, path: str) -> dict:
        p = self._safe_path(path)
        trash = cfg.data_path(".friday-trash", time.strftime("%Y%m%d"))
        trash.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            dest = trash / p.name
            shutil.move(str(p), str(dest))
        elif p.is_dir():
            dest = trash / p.name
            shutil.move(str(p), str(dest))
        else:
            return {"ok": False, "error": "not found"}
        self._log_tool("files.delete", {"path": str(p)}, {"trashed": str(dest)})
        return {"ok": True, "trash": str(dest), "ttl_days": cfg.get("ledger.trash_ttl_days", 30)}

    def files_organize(self, directory: str, rules: list[dict]) -> dict:
        """rules: [{match:"*.pdf", to:"PDFs"}, ...] — >20 files auto-escalates."""
        base = self._safe_path(directory)
        files = [f for f in base.rglob("*") if f.is_file()]
        verdict = self.hermes.classify({"action": "fs_move", "files": len(files), "writes": True})
        if verdict["class"] == "blocking":
            return {"ok": False, "blocking": True, "reason": verdict["reason"]}
        moved = 0
        for f in files:
            for rule in rules:
                if re.fullmatch(rule.get("match", ".*"), f.name):
                    dest = base / rule["to"] / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(f), str(dest))
                    moved += 1
                    break
        self._log_tool("files.organize", {"dir": str(base), "rules": rules}, {"moved": moved})
        return {"ok": True, "moved": moved}

    # ---------------- artifacts ----------------
    def artifact_save(self, title: str, content: str, mime: str = "text/markdown",
                      type: str = "task_result", ext: str = "md") -> dict:
        fname = re.sub(r"[^a-zA-Z0-9_-]+", "-", title.lower()).strip("-")[:60]
        p = self.artifacts_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{fname}.{ext}"
        p.parent.mkdir(parents=True, exist_ok=True)
        if mime.startswith("image"):
            p.write_bytes(content.encode("latin1") if isinstance(content, str) else content)
        else:
            p.write_text(content, encoding="utf-8")
        aid = self.db.exec(
            "INSERT INTO artifacts(type,title,summary,storage_path,mime,size_bytes,task_id,created_ts)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (type, title, content[:200], str(p.relative_to(cfg.data_dir)), mime,
             p.stat().st_size, self.task_id, time.time()))
        self._log_tool("artifact.save", {"title": title}, {"artifact_id": aid})
        return {"ok": True, "artifact_id": aid, "path": f"/api/artifacts/{aid}"}

    def artifact_pptx(self, title: str, slides: list[dict]) -> dict:
        """Generate a minimal PPTX (OEM package, no external dep)."""
        buf = io.BytesIO()
        from .pptx_min import build_pptx
        build_pptx(buf, slides)
        p = self.artifacts_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{re.sub(r'[^a-z0-9]+','-',title.lower())[:40]}.pptx"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(buf.getvalue())
        aid = self.db.exec(
            "INSERT INTO artifacts(type,title,summary,storage_path,mime,size_bytes,task_id,created_ts)"
            " VALUES('document',?,?,?,?,?,?,?)",
            (title, f"{len(slides)} slides", str(p.relative_to(cfg.data_dir)),
             "application/vnd.openxmlformats-officedocument.presentationml.presentation",
             p.stat().st_size, self.task_id, time.time()))
        return {"ok": True, "artifact_id": aid, "path": f"/api/artifacts/{aid}", "slides": len(slides)}

    # ---------------- mail / money (blocking gates) ----------------
    def mail_draft(self, to: str, subject: str, body: str, register: str = "professional") -> dict:
        content = f"To: {to}\nSubject: {subject}\nRegister: {register}\n\n{body}"
        art = self.artifact_save(f"email-draft-{to.split('@')[0]}", content, type="document")
        self._log_tool("mail.draft", {"to": to, "subject": subject}, {"artifact_id": art["artifact_id"]})
        return {"ok": True, "draft": True, **art}

    def mail_send(self, to: str, subject: str, body: str) -> dict:
        return {"ok": False, "blocking": True, "gate": "gmail_send",
                "reason": "Gmail send is one of exactly 2 blocking gates. Awaiting approval."}

    def money_quote(self, item: str, price: float, vendor: str, image_url: str = "") -> dict:
        art = self.artifact_save(f"quote-{re.sub(r'[^a-z0-9]+','-',item.lower())[:30]}",
                                 f"# {item}\n\nVendor: {vendor}\nPrice: ₹{price}\n", type="document")
        quote = {"item": item, "price": price, "vendor": vendor,
                 "image_url": image_url, "artifact_id": art["artifact_id"]}
        self._log_tool("money.quote", {"item": item, "price": price, "vendor": vendor},
                       {"quote": quote})
        return {"ok": True, "quote": quote}

    def money_pay(self, amount: float, vendor: str, method: str = "card") -> dict:
        return {"ok": False, "blocking": True, "gate": "payment",
                "reason": f"Payment of ₹{amount} to {vendor} — one of exactly 2 blocking gates."}

    # ---------------- schedule / trackers / focus ----------------
    def remind(self, text: str, due_ts: float, channels: list | None = None) -> dict:
        rid = self.db.exec(
            "INSERT INTO reminders(text,due_ts,status,channels,created_ts) VALUES(?,?, 'pending',?,?)",
            (text, due_ts, json.dumps(channels or ["chrome", "chat"]), time.time()))
        self._log_tool("schedule.remind", {"text": text, "due_ts": due_ts}, {"reminder_id": rid})
        return {"ok": True, "reminder_id": rid}

    def event_add(self, title: str, date: str, notes: str = "") -> dict:
        clash = self.db.q1("SELECT * FROM plans WHERE date=? ", (date,))
        self.db.exec("INSERT INTO plans(title,date,notes,created_ts) VALUES(?,?,?,?)",
                     (title, date, notes, time.time()))
        return {"ok": True, "clash": clash if clash else None}

    def tracker_create(self, kind: str, query: str, frequency_mins: int = 360) -> dict:
        tid = self.db.exec(
            "INSERT INTO trackers(kind,query,status,frequency_mins,created_ts) VALUES(?,?, 'active',?,?)",
            (kind, query, frequency_mins, time.time()))
        self._log_tool("tracker.create", {"kind": kind, "query": query}, {"tracker_id": tid})
        return {"ok": True, "tracker_id": tid, "status": "active"}

    def focus_start(self, minutes: int = 25, allow: list | None = None, voice: bool = True) -> dict:
        from .focus import Focus
        return Focus(self.db).start(minutes, allow or [], voice)

    def focus_stop(self) -> dict:
        from .focus import Focus
        return Focus(self.db).stop()

    # ---------------- MCP (catalog inside sandbox) ----------------
    async def mcp_call(self, app: str, action: str, params: dict | None = None) -> dict:
        self._log_tool("mcp.call", {"app": app, "action": action, "params": params or {}}, {"status": "queued"})
        # On the VM this hits the Zapier/MCP gateway; offline we simulate.
        return {"ok": True, "simulated": True, "app": app, "action": action,
                "result": f"{app}.{action} accepted"}

    # ---------------- helpers ----------------
    def _safe_path(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = cfg.data_dir / p
        p = p.resolve()
        if not str(p).startswith(str(cfg.data_dir.resolve())):
            raise PermissionError("path escapes data dir")
        return p

    def _log_tool(self, tool: str, inputs: dict, outputs: dict) -> None:
        self.db.append_event("tool_result", "hermes",
                             {"tool": tool, "inputs": inputs, "outputs": outputs,
                              "task_id": self.task_id}, 1.0, corr_id=self.corr_id)
        self._results.append({"tool": tool, "inputs": inputs, "outputs": outputs})

    def results(self) -> list[dict]:
        return self._results


# --------------------------------------------------------------------------- #
# FORGE — sandboxed python execution
# --------------------------------------------------------------------------- #
class Forge:
    """Executes model-written Python against the friday SDK.
    VM: E2B/Firecracker warm pool. Offline/tests: in-process restricted exec."""

    def __init__(self, search=None, task_id: int | None = None, corr_id: str | None = None) -> None:
        self.search = search
        self.task_id = task_id
        self.corr_id = corr_id

    async def run(self, code: str, timeout_s: int = 60) -> dict:
        sdk = FridaySDK(self.search, self.task_id, self.corr_id)
        namespace: dict = {
            "friday": sdk,
            "json": json,
            "time": time,
            "re": re,
            "asyncio": asyncio,
            "__builtins__": {
                "print": print, "len": len, "range": range, "str": str, "int": int,
                "float": float, "list": list, "dict": dict, "set": set, "sum": sum,
                "min": min, "max": max, "sorted": sorted, "enumerate": enumerate,
                "zip": zip, "abs": abs, "round": round, "isinstance": isinstance,
                "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
                "KeyError": KeyError, "locals": locals, "globals": globals,
                "open": None,  # no raw file IO
            },
        }
        # wrap in an async main so top-level `await friday.*` works.
        # Contract: the code ends with `result = …` (asserts evaluate `result`).
        indented = "\n".join(("    " + ln) if ln.strip() else ln for ln in code.splitlines())
        wrapper = f"async def _friday_main():\n{indented}\n    return locals().get('result')\n"
        try:
            exec(compile(wrapper, "<forge>", "exec"), namespace)
            try:
                result = await asyncio.wait_for(namespace["_friday_main"](), timeout=timeout_s)
            except asyncio.TimeoutError:
                return {"ok": False, "error": f"timeout after {timeout_s}s",
                        "results": sdk.results()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {e}",
                    "results": sdk.results()}
        merged: dict = {"ok": True, "results": sdk.results()}
        if isinstance(result, dict):
            merged.update(result)
        return merged
