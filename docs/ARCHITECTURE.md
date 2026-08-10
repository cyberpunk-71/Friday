# Friday–Δ — Architecture

> Version 1.0.0 · This document describes the full system design so that
> anyone with repository access can understand, extend and operate Friday.

---

## 1. System overview

Friday is a **self-hosted FastAPI application** with a **vanilla-JS single
page frontend** and an **SQLite event-sourced store**. There is no build
step for the UI, no external database, and no third-party runtime beyond a
Python 3.9+ venv.

```
                    ┌────────────────────────────────────────────┐
   Browser (PWA)    │                FRIDAY (FastAPI)             │
 ┌───────────────┐  │  ┌────────┐   ┌─────────────────────────┐   │
 │ index.html    │◄─┼──│ /api/* │◄──│  CORTEX (one-pass)      │   │
 │ app.js        │  │  │  SSE   │   │  SENSE → SPEAK → SETTLE │   │
 │ style.css     │  │  └────────┘   └──────┬──────────┬───────┘   │
 └───────┬───────┘  │        │             │          │           │
         │ MV3      │  ┌─────▼─────┐  ┌────▼────┐  ┌──▼─────┐     │
         │ sensor   │  │  RIVER    │  │  LOOM   │  │ HANDS  │     │
         │ (Chrome) │  │ event log │  │ recall  │  │ task   │     │
         └──────────┘  └─────┬─────┘  └────┬────┘  │ DAG    │     │
                             │             │       └───┬────┘     │
                      ┌──────▼──────┐  ┌───▼──────┐   ┌─▼────────┐│
                      │   SQLite    │  │ providers│   │  worker  ││
                      │  (data/)    │  │ LLM+web │   │ (SETTLE) ││
                      └─────────────┘  └──────────┘   └──────────┘│
   Cloudflare tunnel ◄────────────────────────────────────────────┘
   (outbound-only, no open inbound ports)
```

### The three phases of a chat turn

1. **SENSE** — 0 LLM calls, ~18 ms.
   - Typo-expanded retrieval query.
   - One in-RAM "Weaver Walk": binary hamming prefilter → fp16 matmul
     rerank → SQL probes → learned scorer.
   - 12 guaranteed slots: `[2 identity][2 corrections][2 open loops]
     [4 topical][1 tension][1 procedure]` (fewer where data is thin).
   - Deterministic NOW-block: clock, last-seen Δ, geo, focus state,
     open loops, last corrections, budget, ask-budget, live VAD affect.
   - Hermes **pre-fire**: a speculative web search starts at t+0 for
     live-looking queries (regex-gated, budget-capped).

2. **SPEAK** — 1 LLM call, streaming.
   - The first ~40 tokens are the **⟨CTRL⟩ control block** — a JSON object
     parsed *incrementally* as tokens arrive:
     `{"depth", "tooliness", "emotionality", "novelty", "stakes",
       "config_deltas", "memory_writes", "code_intent", "ask"}`
   - Side-effects fire while prose still streams: theme changes at
     ~120 ms, memory writes are fused into the one call, tasks can spawn.
   - Deterministic **ingresses** short-circuit before the LLM for things
     the model must never decide: provider keys in chat (`sk-…`, `AIza…`,
     `AQ.Ab…`), focus start/stop (typo-tolerant multi-turn), theme
     commands, reminders, trackers, payment-gated buys, and
     `which model are you` (truthful identity).
   - Post-generation **polish_reply** is a safety net: strips robotic
     headers, search-dump tables, mid-reply and truncated ⟨CTRL⟩ JSON,
     the literal `⟨CTRL⟩` marker, double-emitted blocks, and stray JSON
     residue. The same cleanup is applied to history fed back to the model
     (prevents style echo).

3. **SETTLE** — async, invisible.
   - River materialization (events → delta views), affect rollup, spend
     ledger, turn audit (for the Gym), and the outcome label
     (`corrected` / `repeated` / `done`).

### Resilience rules (learned the hard way)

- **Failover**: if the live LLM dies mid-turn (401, `httpx.ReadError`,
  timeout), the cortex fails over to the other provider using a saved key,
  silently — then records the reason for the admin panel.
- **Auto-heal**: a rejected provider key permanently switches chat routing
  to the other provider (anti-flap: once per 15 min; explicit user action
  resets the timer). Verified behavior: one clean notice, then silence.
- **Focus auto-expiry**: sessions past their end time are completed by
  `Focus.active()` itself — the SETTLE worker is not required for state
  correctness.
- **Input safety**: the UI never re-renders over a field the user is
  typing in (snapshot/restore + typing guard).

---

## 2. Memory: RIVER + LOOM + PSYCHE

### RIVER (event-sourced core)

`core/river.py` — an append-only event log. Nothing is ever overwritten.

- Events are recorded with `record(kind, actor, payload, importance)`.
- Materialized **delta views**: `atoms` (facts/preferences/goals/…),
  `claims` (beliefs α/β), `tensions`, `open_loops`, `constraints`.
- **Correction supremacy**: a user correction is weight 10 and outranks
  every inference. Corrections are themselves atoms, so the model can be
  shown *what it got wrong*.
- Immutable history makes everything reversible (undo, delete = append a
  tombstone).

### LOOM (retrieval)

`core/loom.py` — the Weaver Walk:

1. **Prefilter** — binary hamming on hashed n-grams over candidate atoms.
2. **Rerank** — fp16 matmul similarity (hash-embedder backend, no heavy
   model; configurable `FRIDAY_EMBED_BACKEND`).
3. **Probe** — SQL probes for corrections, open loops, constraints.
4. **Score** — learned salience scorer (config in `configs/defaults.yaml`).
5. **Slot fill** — the 12-slot guarantee, plus a NOW-block:
   - clock · last-seen Δ · **focus state** (active/minutes-left via
     auto-expiring `Focus.active()`) · **LLM identity** (exact provider +
     model so "which model are you" is truthful) · open loops ·
     corrections · trackers · pending approvals · budget · ask-budget ·
     VAD affect.

### PSYCHE (affect + beliefs)

`core/psyche.py` — Bayesian claims with α/β strength, a VAD (valence–
arousal–dominance) lexicon feeding an EWMA affect state, posture
selection (`configs/postures.yaml`), and a Brier-score self-evaluation.

---

## 3. SPEAK: providers & the ⟨CTRL⟩ protocol

### Providers (`core/providers.py`)

- **DeepSeekProvider** — OpenAI-compatible `/chat/completions` (stream /
  complete / function calling).
- **GeminiProvider** — native `generativelanguage.googleapis.com` v1beta:
  - Message/tool conversion (system instruction, `functionCall` /
    `functionResponse`, consecutive-role merging).
  - **Candidate model chain** with 404 fall-forward (models get retired
    without notice — `gemini-2.5-flash` did in July 2026).
  - **Dual auth** (`x-goog-api-key` then `Bearer`) for `AQ.Ab…` OAuth-style
    keys.
  - **Streaming fallback**: if SSE drops mid-stream, falls back to a single
    `generateContent` call. Chat must work.
  - `thinkingConfig: minimal` for fast text output on 3.x models.
- **SimProvider** — deterministic offline stand-in (used by tests and when
  no key is configured). Emits a valid ⟨CTRL⟩ block and consumes slots.
- **Search**: `GoogleNewsRSS` (RSS 2.0) → `DuckDuckGoSearch` (html + lite)
  → `BingSearch`, with `TavilySearch` when a key exists. All wrapped with
  robust HTML parsing; `[fixture]` results are marked for the model.

### Routing

`make_llm(scope)` resolves per **scope** — `chat`, `research` (deep
research tool loop), `books` (book RAG), `eval` (the eval suite) — from
DB settings `llm.{scope}.provider` + `llm.{scope}.model`, inheriting the
chat defaults only when the provider matches (a Gemini model string must
never leak into a DeepSeek call). Keys live in `provider_keys`; env keys
are fallback; no key → SimProvider.

### The ⟨CTRL⟩ block

The model is instructed to emit a JSON control object as its first ~40
tokens. `parse_ctrl` consumes it incrementally from the stream buffer
(balanced-brace scan, tolerant of the literal `⟨CTRL⟩` marker). The UI
mirrors this cleanup (`stripLeadingCtrl`) so raw JSON can never render.

### Deterministic ingresses (in priority order)

| Ingress | Matches | Effect |
|---|---|---|
| `_provider_key` | `sk-…`, `AIza…`, `AQ.Ab…` in chat | saves key, live-swaps LLM |
| `_identity_ingress` | "which model are you" | truthful provider+model, 1 ms |
| `_focus_ingress` | "start focos 25m allow github", "lets start a foscued mode?" → multi-turn staging | real session, never a fake claim |
| `_config_ingress` | "dark mode kar do", "stop phone notificaton for focus" | theme + settings deltas |
| `_buy_ingress` | imperative "buy X under 10k" (question-guarded) | payment-gated task |
| `_tracker_ingress` | "keep an eye on X" | real tracker rows |
| `_reminder_ingress` | "remind me at 14:00" | reminder row + chrome nudge |

---

## 4. Tasks: HANDS

`core/hands.py` — a **DAG engine**:

- Steps with `description`, `code` (against the `friday` SDK in
  `core/tools.py`), and **postcondition `assert`** expressions.
- **Two blocking gates** that can never be skipped: `payment` and
  `gmail_send` (approval required before the step runs).
- Deterministic fallback step so a task always terminates.
- Task events stream back through SSE (`task_event`) and land in the
  Tasks panel kanban with undo support.

---

## 5. The flagship: Focus Studio

See [docs/FOCUS.md](FOCUS.md) for the full end-to-end design. Summary:

- `focus_sessions` carries rich intake: task, why (reward), first_step,
  energy, mood, distraction_plan, pomodoro mode/break, and live `thoughts`
  (brain-dump capture) + `comebacks`.
- **Focus Score** (0–100) computed at finish:
  `0.55·completion + 0.25·(1−drifts/8) + 0.10·comebacks/2 + 0.10·min(1,min/25)`.
- Analytics: avg/best score, series, best hour, energy delta, streak,
  week totals, garden data, thought bank.
- **Focus Coach** (`POST /api/focus/coach`) — an LLM endpoint with full
  session context and an ADHD-friendly persona; used in the studio chat
  and the Day Review ritual.

---

## 6. Genome — the evolving SELF

`core/genome.py` + `genome/` — the identity lives in a **git repository**
inside the app:

- `genome/prompts/self.md` — the cached system prompt (byte-identical
  every turn).
- `genome/policies/*.yaml` — style, escalation.
- `genome/skills/**/SKILL.md` — 73 declarative Donna procedures.
- `genome/scorer.json` — fitness corpus for replay.

**Gym cycle**: nightly (or on demand), the genome *mutates* (prompt /
scorer / nudge / style variants — only mutations whose target file exists
are picked), replays the fitness corpus, and **promotes only if fitness
improves**, else reverts. Every step is a git commit — `show me how you
changed` surfaces the log.

---

## 7. Frontend

`ui/` — vanilla JS, zero build step.

- **Design system** — "SUNLIT": warm editorial light theme (default) +
  warm dark, coral/sunset accents, serif brand, flat cards. The Focus
  experience adds the **Sanctuary**: phase-tinted aurora background,
  breathing companion aura, music-box chimes, generated soundscapes
  (brown noise / rain), confetti, serif-italic reflections.
- **Views** — Today (landing), Chat, Focus Studio, Garden & Insights,
  Tasks, Memory, Books, Admin.
- **Chat engine** — SSE consumption, streaming markdown-lite renderer,
  copy buttons, timestamps, day dividers, history restore on load.
- **Focus panel** — intake ritual, phase timer, coach chat, brain dump,
  celebration, garden; typing-safe re-renders.
- **Theme** — persisted to the server; `dark mode kar do` works.

---

## 8. Background worker

`core/worker.py` — SETTLE loop: reminders, trackers, focus completion
nudges, nightly genome window. The core service is independent of the
worker (focus auto-expiry and nudges work even if the worker is down).

---

## 9. Security & key handling

- Keys live in `provider_keys` (DB) and/or `.env`; chat-set keys are
  live-swapped.
- Key-shaped strings are **never** stored as model names (admin + API
  guard + self-heal).
- Admin endpoints are guarded by `FRIDAY_ACCESS_TOKEN` when set.
- Free-tier Gemini prompts may be used by Google — surfaced in the UI.

---

## 10. Configuration

Everything is live-tunable through `PUT /api/admin/settings` (the Admin →
Parameters tree) — defaults live in `configs/defaults.yaml` and can be
overridden at runtime per-key (retrieval salience, speak temperature,
hermes pre-fire budget, focus nudges, genome window, budget, …).

See `configs/defaults.yaml` for the canonical list.
