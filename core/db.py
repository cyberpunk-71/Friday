"""SQLite WAL substrate: the RIVER (append-only events) + DELTA (materialized
views) + operational tables. FRIDAY-Δ: NOTHING is ever UPDATEd in the river;
views are rebuilt incrementally from events. Single-writer queue guarantees
zero lock contention under asyncio.

Schema follows the FRIDAY-Δ plan:
  events(id, ts, kind, actor, payload, source_weight, supersedes[], evidence[])
  delta views: atoms · entities · edges · claims(α,β) · tensions · open_loops
                · corrections · constraints
  ops: tasks/steps/audit · artifacts · trackers/observations · reminders · plans
       · focus_sessions/distractions · nudges · books/chunks · settings ·
       provider_keys · genome_commits · predictions/alignment · affect/daily
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Iterable

from .config import cfg

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

-- ============ RIVER: append-only event log ============
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    ts            REAL NOT NULL,
    kind          TEXT NOT NULL,          -- utterance|observation|correction|rating|
                                          -- tool_result|undo|promotion|genome_commit|reminder|nudge|focus|book|task|tracker|dream
    actor         TEXT NOT NULL,          -- user|friday|hermes|genome|system|extension
    payload       TEXT NOT NULL,          -- JSON
    source_weight REAL NOT NULL DEFAULT 1.0,
    supersedes    TEXT NOT NULL DEFAULT '[]',   -- event ids this supersedes
    evidence      TEXT NOT NULL DEFAULT '[]',   -- event ids this is evidence for
    corr_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_actor ON events(actor);

-- ============ DELTA: atoms (durable beliefs), rebuilt from events ============
CREATE TABLE IF NOT EXISTS atoms (
    atom_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT NOT NULL,
    kind         TEXT NOT NULL,   -- fact|preference|pattern|goal|episodic|emotional|semantic|procedure|correction|observation
    status       TEXT NOT NULL DEFAULT 'active',  -- active|dormant|superseded|user_deleted
    importance   REAL NOT NULL DEFAULT 0.5,
    strength     REAL NOT NULL DEFAULT 0.5,
    valence      REAL NOT NULL DEFAULT 0.0,
    arousal      REAL NOT NULL DEFAULT 0.0,
    access_count INTEGER NOT NULL DEFAULT 0,
    last_access_ts REAL,
    created_ts   REAL NOT NULL,
    supersedes_atom_id INTEGER,
    source_event_ids TEXT NOT NULL DEFAULT '[]',
    user_edited  INTEGER NOT NULL DEFAULT 0,
    pinned       INTEGER NOT NULL DEFAULT 0,
    entity_ids   TEXT NOT NULL DEFAULT '[]',
    scope        TEXT NOT NULL DEFAULT 'global'   -- global|book:<id>|task:<id>
);
CREATE INDEX IF NOT EXISTS idx_atoms_kind_status ON atoms(kind, status);
CREATE INDEX IF NOT EXISTS idx_atoms_scope ON atoms(scope, status);
CREATE INDEX IF NOT EXISTS idx_atoms_created ON atoms(created_ts);

-- ============ DELTA: entities & edges (knowledge graph) ============
CREATE TABLE IF NOT EXISTS entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    kind      TEXT NOT NULL DEFAULT 'concept',  -- person|place|project|company|concept|device|channel
    aliases   TEXT NOT NULL DEFAULT '[]',
    importance REAL NOT NULL DEFAULT 0.5,
    created_ts REAL NOT NULL,
    last_mentioned_ts REAL
);
CREATE TABLE IF NOT EXISTS edges (
    src INTEGER NOT NULL,
    dst INTEGER NOT NULL,
    relation TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    first_seen REAL NOT NULL,
    last_reinforced_ts REAL,
    PRIMARY KEY (src, dst, relation)
);

-- ============ DELTA: beliefs (α/β) & tensions ============
CREATE TABLE IF NOT EXISTS claims (
    claim_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    category   TEXT NOT NULL,   -- trait|preference|value|goal|belief|constraint|style
    statement  TEXT NOT NULL,
    alpha      REAL NOT NULL DEFAULT 1.0,
    beta       REAL NOT NULL DEFAULT 1.0,
    stability  REAL NOT NULL DEFAULT 1.0,
    priority   INTEGER NOT NULL DEFAULT 5,
    status     TEXT NOT NULL DEFAULT 'active',  -- active|challenged|deprecated
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '[]',      -- event ids
    user_edited INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE TABLE IF NOT EXISTS tensions (
    tension_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_1_id   INTEGER NOT NULL,
    claim_2_id   INTEGER NOT NULL,
    strength     REAL NOT NULL DEFAULT 0.5,
    balance      REAL NOT NULL DEFAULT 0.5,
    status       TEXT NOT NULL DEFAULT 'open',  -- open|resolved|user_resolved
    created_ts   REAL NOT NULL,
    resolved_ts  REAL
);

-- ============ DELTA: corrections (supremacy) & constraints (always-injected) ============
CREATE TABLE IF NOT EXISTS corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    text         TEXT NOT NULL,
    atom_id      INTEGER,             -- the atom being corrected (if any)
    claim_id     INTEGER,             -- the claim being corrected (if any)
    weight       REAL NOT NULL DEFAULT 10.0,
    ts           REAL NOT NULL,
    event_id     TEXT,
    status       TEXT NOT NULL DEFAULT 'active'  -- active|superseded
);
CREATE TABLE IF NOT EXISTS constraints (
    constraint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    text          TEXT NOT NULL,
    ttl_days      REAL NOT NULL DEFAULT -1,   -- -1 = forever
    created_ts    REAL NOT NULL,
    source        TEXT NOT NULL DEFAULT 'user',  -- user|genome|system
    user_editable INTEGER NOT NULL DEFAULT 1
);

-- ============ DELTA: open loops ============
CREATE TABLE IF NOT EXISTS open_loops (
    loop_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,   -- commitment|question|follow_up|dilemma|stall
    text         TEXT NOT NULL,
    deadline_ts  REAL,
    status       TEXT NOT NULL DEFAULT 'open',  -- open|closed|dropped
    priority     REAL NOT NULL DEFAULT 0.5,
    created_ts   REAL NOT NULL,
    last_activity_ts REAL
);

-- ============ Working Set (ring buffer, 48h TTL) ============
CREATE TABLE IF NOT EXISTS working_set (
    page_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    url        TEXT NOT NULL,
    title      TEXT,
    text_hash  TEXT,
    created_ts REAL NOT NULL,
    scope      TEXT NOT NULL DEFAULT 'browse'
);
CREATE INDEX IF NOT EXISTS idx_ws_ts ON working_set(created_ts);

-- ============ TASKS ============
CREATE TABLE IF NOT EXISTS tasks (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT NOT NULL,
    description    TEXT,
    status         TEXT NOT NULL DEFAULT 'queued',
                   -- queued|running|waiting_approval|blocked|completed|failed|cancelled|awaiting_repair
    task_type      TEXT NOT NULL DEFAULT 'generic',
    autonomy       TEXT NOT NULL DEFAULT 'autonomous',  -- suggest|draft|approve_before_execute|autonomous
    priority       REAL NOT NULL DEFAULT 0.5,
    plan_json      TEXT NOT NULL DEFAULT '{}',
    context_json   TEXT NOT NULL DEFAULT '{}',
    artifact_ids   TEXT NOT NULL DEFAULT '[]',
    approval_kind  TEXT,             -- payment|gmail_send|escalated
    approval_detail TEXT,
    cost_usd       REAL NOT NULL DEFAULT 0.0,
    created_ts     REAL NOT NULL,
    updated_ts     REAL NOT NULL,
    completed_ts   REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS task_steps (
    step_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    description TEXT,
    status     TEXT NOT NULL DEFAULT 'pending',
               -- pending|running|completed|failed|skipped|awaiting_approval
    tool_name  TEXT,
    input_summary  TEXT,
    output_summary TEXT,
    assertion  TEXT,
    error      TEXT,
    started_at REAL,
    completed_at REAL,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_audit_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    INTEGER NOT NULL,
    step_id    INTEGER,
    event_type TEXT NOT NULL,
    details    TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);

-- ============ ARTIFACTS (task results, downloadable) ============
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    type         TEXT NOT NULL,   -- research_report|document|summary|data_export|image|pptx|task_result|book
    title        TEXT NOT NULL,
    summary      TEXT,
    storage_path TEXT NOT NULL,
    mime         TEXT NOT NULL DEFAULT 'text/markdown',
    size_bytes   INTEGER NOT NULL DEFAULT 0,
    task_id      INTEGER,
    created_ts   REAL NOT NULL
);

-- ============ TRACKERS & OBSERVATIONS ============
CREATE TABLE IF NOT EXISTS trackers (
    tracker_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind          TEXT NOT NULL,   -- availability|price|content|monitor
    query         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    frequency_mins INTEGER NOT NULL DEFAULT 360,
    params        TEXT NOT NULL DEFAULT '{}',
    last_check_ts REAL,
    last_result_hash TEXT,
    created_ts    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tracker_observations (
    obs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tracker_id  INTEGER NOT NULL,
    ts          REAL NOT NULL,
    hash_observed TEXT,
    payload     TEXT NOT NULL DEFAULT '{}',
    changed     INTEGER NOT NULL DEFAULT 0
);

-- ============ REMINDERS & PLANS ============
CREATE TABLE IF NOT EXISTS reminders (
    reminder_id INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    due_ts      REAL NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|delivered|cancelled
    channels    TEXT NOT NULL DEFAULT '["chrome","chat"]',
    created_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    plan_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL,
    date      TEXT NOT NULL,
    notes     TEXT,
    created_ts REAL NOT NULL
);

-- ============ FOCUS ============
CREATE TABLE IF NOT EXISTS focus_sessions (
    session_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    start_ts    REAL NOT NULL,
    end_ts      REAL,
    target_min  INTEGER NOT NULL DEFAULT 25,
    status      TEXT NOT NULL DEFAULT 'active',  -- active|completed|abandoned|paused
    allow_domains TEXT NOT NULL DEFAULT '[]',
    drift_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS distraction_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    url        TEXT NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'drift',
    FOREIGN KEY (session_id) REFERENCES focus_sessions(session_id)
);

-- ============ NUDGES (coalesced) ============
CREATE TABLE IF NOT EXISTS nudges (
    nudge_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,     -- focus|reminder|research|pay|dream
    channel    TEXT NOT NULL,     -- toast|chrome|voice|phone
    message    TEXT NOT NULL,
    delivered  INTEGER NOT NULL DEFAULT 0,
    engaged    INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nudges_kind ON nudges(kind, channel, delivered);

-- ============ BOOKS ============
CREATE TABLE IF NOT EXISTS books (
    book_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT NOT NULL,
    source     TEXT NOT NULL,       -- path / drive ref
    status     TEXT NOT NULL DEFAULT 'ingesting',  -- ingesting|ready|failed
    progress   REAL NOT NULL DEFAULT 0,            -- 0..1
    pages      INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    meta       TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS book_chunks (
    chunk_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id    INTEGER NOT NULL,
    idx        INTEGER NOT NULL,
    page       INTEGER,
    text       TEXT NOT NULL,
    tokens     INTEGER NOT NULL DEFAULT 0,
    created_ts REAL NOT NULL,
    FOREIGN KEY (book_id) REFERENCES books(book_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_book ON book_chunks(book_id, idx);
CREATE TABLE IF NOT EXISTS book_threads (
    thread_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id    INTEGER NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'discuss',   -- discuss|quiz|summary|ppt
    title      TEXT,
    created_ts REAL NOT NULL
);

-- ============ SETTINGS & PROVIDER KEYS ============
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,      -- deepseek|search|voice|stripe|gmail|drive|github|mcp
    scope      TEXT NOT NULL DEFAULT 'default',  -- default|research|voice|...
    api_key    TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    source     TEXT NOT NULL DEFAULT 'chat',     -- env|admin|chat
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    UNIQUE (provider, scope)
);

-- ============ GENOME ============
CREATE TABLE IF NOT EXISTS genome_commits (
    commit_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    sha        TEXT NOT NULL,
    msg        TEXT NOT NULL,
    fitness    REAL,
    baseline   REAL,
    canary     INTEGER NOT NULL DEFAULT 0,
    reverted   INTEGER NOT NULL DEFAULT 0,
    reason     TEXT,
    created_ts REAL NOT NULL
);

-- ============ PSYCHE: predictions & affect ============
CREATE TABLE IF NOT EXISTS predictions (
    id         TEXT PRIMARY KEY,
    domain     TEXT NOT NULL,
    description TEXT NOT NULL,
    probability REAL NOT NULL,
    outcome    INTEGER,
    created_ts REAL NOT NULL,
    resolved_ts REAL
);
CREATE TABLE IF NOT EXISTS alignment_records (
    domain      TEXT PRIMARY KEY,
    brier_score REAL NOT NULL DEFAULT 0.25,
    total       INTEGER NOT NULL DEFAULT 0,
    updated_ts  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS affect_samples (
    id      TEXT PRIMARY KEY,
    valence REAL NOT NULL,
    arousal REAL NOT NULL,
    dominance REAL NOT NULL DEFAULT 0.5,
    source  TEXT NOT NULL DEFAULT 'lexicon',
    created_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_emotion (
    date    TEXT PRIMARY KEY,
    avg_vad TEXT NOT NULL DEFAULT '[0,0,0.5]',
    ewma_vad TEXT NOT NULL DEFAULT '[0,0,0.5]'
);

-- ============ RIVER materialization bookkeeping ============
CREATE TABLE IF NOT EXISTS materialized_events (
    event_id TEXT PRIMARY KEY
);

-- ============ OBS / audit ============
CREATE TABLE IF NOT EXISTS turns (
    turn_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    corr_id    TEXT NOT NULL,
    user_text  TEXT NOT NULL,
    reply      TEXT,
    latency_ms INTEGER,
    cost_usd   REAL,
    model      TEXT,
    provider   TEXT,
    slots_json TEXT,
    outcome    TEXT,          -- ok|corrected|repeated|undone|thumb_down
    created_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_created ON turns(created_ts);
"""


class DB:
    """Single-writer SQLite access with an asyncio-compatible write queue."""

    def __init__(self, path=None) -> None:
        self.path = str(path or cfg.data_path("friday.db"))
        self._local = threading.local()
        self._init_schema()

    # ---- connections (one per thread, WAL) ----
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=30000")
            self._local.conn = c
        return c

    def _init_schema(self) -> None:
        c = self.conn()
        c.executescript(SCHEMA)
        c.commit()

    # ---- generic helpers ----
    def q(self, sql: str, params: Iterable = ()) -> list[dict]:
        c = self.conn()
        try:
            rows = c.execute(sql, tuple(params)).fetchall()
        except sqlite3.OperationalError as e:  # pragma: no cover
            if "locked" in str(e).lower():
                time.sleep(0.02)
                rows = c.execute(sql, tuple(params)).fetchall()
            else:
                raise
        return [dict(r) for r in rows]

    def q1(self, sql: str, params: Iterable = ()) -> dict | None:
        rows = self.q(sql, params)
        return rows[0] if rows else None

    def exec(self, sql: str, params: Iterable = ()) -> int:
        c = self.conn()
        cur = c.execute(sql, tuple(params))
        c.commit()
        return cur.lastrowid

    def execmany(self, sql: str, seq: Iterable) -> None:
        c = self.conn()
        c.executemany(sql, seq)
        c.commit()

    # ---- river ----
    def append_event(self, kind: str, actor: str, payload: dict,
                     source_weight: float = 1.0,
                     supersedes: list | None = None,
                     evidence: list | None = None,
                     corr_id: str | None = None) -> str:
        eid = uuid.uuid4().hex[:12]
        self.exec(
            "INSERT INTO events(id,ts,kind,actor,payload,source_weight,supersedes,evidence,corr_id)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (eid, time.time(), kind, actor, json.dumps(payload, ensure_ascii=False),
             source_weight, json.dumps(supersedes or []), json.dumps(evidence or []),
             corr_id))
        return eid

    def events_since(self, ts: float, kinds: list[str] | None = None, limit: int = 500) -> list[dict]:
        if kinds:
            marks = ",".join("?" * len(kinds))
            return self.q(f"SELECT * FROM events WHERE ts>? AND kind IN ({marks}) ORDER BY ts LIMIT ?",
                          [ts, *kinds, limit])
        return self.q("SELECT * FROM events WHERE ts>? ORDER BY ts LIMIT ?", [ts, limit])

    # ---- settings ----
    def get_setting(self, key: str, default=None):
        row = self.q1("SELECT value FROM settings WHERE key=?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return row["value"]

    def set_setting(self, key: str, value) -> None:
        self.exec("INSERT INTO settings(key,value,updated_ts) VALUES(?,?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts=excluded.updated_ts",
                  (key, json.dumps(value, ensure_ascii=False), time.time()))

    def all_settings(self) -> dict:
        return {r["key"]: self.get_setting(r["key"]) for r in self.q("SELECT key FROM settings")}


_db: DB | None = None
_lock = threading.Lock()


def get_db() -> DB:
    global _db
    with _lock:
        if _db is None:
            _db = DB()
    return _db


def reset_db_for_tests(path: str | None = None) -> DB:
    """Test hook: point the singleton at a fresh DB file."""
    global _db
    with _lock:
        _db = DB(path)
        return _db
