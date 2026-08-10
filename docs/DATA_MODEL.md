# Friday–Δ — Data Model

> Version 1.0.0 · SQLite, one file: `data/friday.db` (configurable via
> `FRIDAY_DATA_DIR`). Created idempotently by `core/db.py` — every table is
> `CREATE TABLE IF NOT EXISTS`, and focus columns added after launch are
> migrated automatically by `Focus._migrate_cols()`.

---

## Event-sourced core

### `events` — the append-only log (source of truth)

| column | meaning |
|---|---|
| `id` | event id (uuid) |
| `ts` | unix timestamp |
| `kind` | utterance · observation · correction · rating · tool_result · undo · promotion · genome_commit · reminder · nudge · focus · book · task · tracker · dream |
| `actor` | user · friday · hermes · genome · system · extension |
| `payload` | JSON |
| `source_weight` | evidence weight (1.0 default; corrections = 10) |
| `supersedes` | JSON list of event ids this overrides |
| `evidence` | JSON list of event ids this supports |
| `corr_id` | conversation correlation id |

### `atoms` — durable beliefs, rebuilt from events

`kind`: fact · preference · pattern · goal · episodic · emotional ·
semantic · procedure · correction · observation.
`status`: active · dormant · superseded · user_deleted.
Plus importance, strength, valence/arousal (VAD), access counters,
`supersedes_atom_id` and entity links (`entities`, `edges`).

### Delta views

| table | purpose |
|---|---|
| `entities` / `edges` | entity graph |
| `claims` | beliefs with α/β (`strength`, `confidence`, `source`) |
| `tensions` | contradictory claims, status open/resolved |
| `corrections` | user corrections (weight 10, outrank inferences) |
| `constraints` | hard constraints the user set |
| `open_loops` | follow-ups/commitments (kind follow_up/commitment) |
| `working_set` | 48h TTL observation ring from the extension |
| `predictions` / `alignment_records` | self-evaluation (Brier) |
| `affect_samples` | VAD time series |

---

## Tasks

| table | purpose |
|---|---|
| `tasks` | title, status (queued/running/waiting_approval/done/…), `plan_json`, approval_kind |
| `task_steps` | step index, description, assertion (postcondition), status |
| `task_audit_events` | undo log |
| `artifacts` | generated files (PPTX etc.) served via `/api/artifacts/{id}` |
| `trackers` / `tracker_observations` | periodic research loops + observations |
| `reminders` | time-based nudges |
| `plans` | saved plans |

---

## Focus (flagship)

### `focus_sessions`

| column | meaning |
|---|---|
| `session_id`, `start_ts`, `end_ts` | lifecycle |
| `target_min` | planned duration |
| `status` | active · completed · abandoned · paused |
| `allow_domains` | JSON list — drift-free domains |
| `drift_count` | drifts (breaks don't count) |
| `task`, `why`, `first_step` | the intent (what / reward / tiny start) |
| `energy`, `mood` | pre-session intake (1–5) |
| `energy_after`, `mood_after` | post-session check-in |
| `distraction_plan` | the pre-commit |
| `thoughts` | brain-dump captures (newline-separated) |
| `notes` | end-of-session note |
| `comebacks` | returns after drift (counted server-side) |
| `focus_score` | 0–100 computed at finish |
| `mode` | work · break (pomodoro) |
| `break_end_ts` | when the break ends |

### Supporting tables

- `distraction_events` — every drift (session, url, ts).
- `nudges` — coalesced nudges (kind focus/reminder/research/pay/dream;
  channel toast/chrome/voice/phone; delivered/engaged).

### Settings keys used by focus

- `focus.pending` — multi-turn start staging (task/minutes/ts)
- `focus.plan` — today's intentions `{date, items:[{text,done}]}`
- `llm.auto_heal` / `llm.auto_heal_ts.{provider}` — key-rejection healing
- `focus.nudge_channels.*` — per-channel toggles

---

## Providers & settings

| table | purpose |
|---|---|
| `provider_keys` | provider (deepseek/gemini/tavily/brave/exa/groq/openrouter), scope (default/research/voice/search), api_key, active, source (env/admin/chat), timestamps |
| `settings` | key/value JSON store — live config, theme, plans, diagnostics |
| `genome_commits` | genome git history (sha, msg, fitness, canary/reverted) |

Routing state: `llm.provider`, `llm.model`, `llm.{scope}.provider`,
`llm.{scope}.model` — resolved by `make_llm(scope)`.

---

## Books

| table | purpose |
|---|---|
| `books` | title, source, status (ingesting/ready/failed), progress, pages, meta |
| `book_chunks` | page-indexed text chunks (RAG) |
| `book_threads` | per-book Q&A threads |

---

## Configuration precedence

1. **DB settings** (`settings` table) — live, admin-editable, wins.
2. **`.env`** — deploy-provided keys/defaults (`config.py` loads only keys
   not already in the environment).
3. **`configs/defaults.yaml`** — defaults for the parameter tree.

Model keys: DB `provider_keys` → env (`DEEPSEEK_API_KEY`,
`GEMINI_API_KEY`) → SimProvider (offline mode).
