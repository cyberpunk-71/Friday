# Friday–Δ — API Reference

> Version 1.0.0 · All endpoints are JSON unless noted. SSE endpoints return
> `text/event-stream`. Admin endpoints are guarded by
> `FRIDAY_ACCESS_TOKEN` (Bearer) when set.

---

## Chat

### `POST /api/chat` — SSE chat turn

```json
{ "text": "who is the mayor of ahmedabad", "corr_id": "cor_abc", "llm_scope": "chat" }
```

`llm_scope` (optional) routes the turn to that scope's model (eval,
research, …). Streams events:

| event | payload |
|---|---|
| `sense` | slots, confidence, sense_ms, now, constraints |
| `ctrl` | parsed ⟨CTRL⟩ control block |
| `delta` | streamed prose chunks |
| `card` | generative UI card (theme / task / approvals / ask / focus / …) |
| `warning` | live-model issues (throttled to 1/min in the UI) |
| `task_event` | background task progress |
| `done` | reply, model, provider, latency_ms, cost_usd, slots_used |

### `GET /api/chat/history?limit=50`

Recent turns (`user_text`, `reply`, `model`, `latency_ms`, `cost_usd`,
`created_ts`).

---

## Admin

| Endpoint | Purpose |
|---|---|
| `GET /api/admin/overview` | health of the whole system: provider, per-scope models, keys (with `is_chat`), counts, spend, genome, errors, auto-heal notice |
| `GET /api/admin/params` | default + live config tree |
| `GET /api/admin/settings` · `PUT /api/admin/settings` | read / write live settings (theme, focus, style, …) |
| `POST /api/admin/models/configure` | save a provider key: `{provider, scope, api_key}` — live-swaps LLM/search/voice |
| `POST /api/admin/llm` | per-scope model routing: `{scope, provider, model}` (chat/research/books/eval) |
| `POST /api/admin/providers/test` | live key test (DeepSeek or Gemini), falls back to the saved DB key |
| `POST /api/admin/oneclick` | integrations: extension / mobile / search / voice |
| `GET /api/admin/spend` | daily spend series |
| `GET /api/admin/turns?limit=40` | turn audit table |
| `GET /api/admin/searchtest` · `GET /api/admin/prefiretest` | diagnostics for search + pre-fire |
| `GET /api/eval/report` · `GET /api/eval/file/{name}` | eval summary + downloadable HTML/JSON/chat-log |

---

## Tasks

| Endpoint | Purpose |
|---|---|
| `GET /api/tasks` | task list with steps |
| `POST /api/tasks` | create a task |
| `POST /api/tasks/{id}/approve` · `/reject` | resolve a blocking gate |
| `POST /api/tasks/{id}/undo` | undo the last reversible action |
| `GET /api/artifacts/{id}` | download a generated artifact (PPTX etc.) |

---

## Memory

| Endpoint | Purpose |
|---|---|
| `GET /api/memory/atoms?q=&kind=` | atoms (facts/preferences/goals/…) |
| `GET /api/memory/claims` · `/tensions` · `/constraints` · `/open_loops` | delta views |
| `POST /api/memory/rate` | reinforce/weaken a belief (α/β) |
| `POST /api/memory/edit` · `/delete` | append-only edit / tombstone delete |
| `POST /api/memory/claim/edit` | update a belief statement |
| `POST /api/memory/tensions/{id}/resolve` | resolve a tension |
| `GET /api/memory/explain?q=` | why did Friday remember X (recall walk) |
| `POST /api/memory/agent_sql` | read-only SQL over delta views |
| `GET /api/memory/graph` | entity graph |

---

## Focus — the flagship

| Endpoint | Purpose |
|---|---|
| `POST /api/focus/start` | start a session: `{minutes, task, why, first_step, energy, mood, distraction_plan, allow, voice}` |
| `POST /api/focus/stop` | plain stop (legacy) |
| `POST /api/focus/finish` | **flagship end**: `{mood_after, notes}` → computes Focus Score + celebration summary |
| `POST /api/focus/thought` | brain-dump capture: `{text}` (appends to the session) |
| `POST /api/focus/comeback` | count a comeback (drift → return) |
| `GET /api/focus/plan` · `POST /api/focus/plan` | today's intentions (date-scoped, max 5) |
| `POST /api/focus/coach` | **Focus Coach** — SSE LLM reply with full session context |
| `POST /api/focus/mode` | work ↔ break (pomodoro; breaks are drift-free) |
| `GET /api/focus/active` | current session (auto-expires past sessions) |
| `GET /api/focus/stats` | sessions + analytics (scores, series, best hour, energy delta, garden data, thoughts) |
| `POST /api/focus/drift` | drift ingress from the MV3 sensor / PWA |

---

## Extension & nudges

| Endpoint | Purpose |
|---|---|
| `POST /api/ext/observe` | page-visit observation from the extension |
| `GET /api/ext/config` | extension config (focus endpoint + channels) |
| `GET /api/extension/zip` | download the MV3 sensor extension |
| `GET /api/nudges` · `POST /api/nudges/{id}/engage` | nudge feed + engagement |
| `GET /api/nudges/stream` | SSE nudge stream |

---

## Books

| Endpoint | Purpose |
|---|---|
| `POST /api/books/upload` | any-size PDF upload (streamed) |
| `GET /api/books` · `GET /api/books/{id}` | library + status |
| `POST /api/books/{id}/ask` | SSE book Q&A (RAG over chunks, book-scope model) |
| `POST /api/books/{id}/quiz` | generate questions |
| `POST /api/books/{id}/ppt` | generate a PPTX artifact |

---

## Voice & misc

| Endpoint | Purpose |
|---|---|
| `POST /api/voice/tts` | text-to-speech (Groq or fallback) |
| `GET /api/genome/log` · `/api/genome/skills` · `/api/genome/explain/{skill}` | the evolving self |
| `GET /` | the UI |
| `GET /api/health` | health check (`status`, `version`, `model`, `atoms`) |
| `GET /static/*` | UI assets (cache-busted `?v=`) |

---

## WebSocket-free streaming notes

- Chat, coach, nudges and book Q&A stream over **SSE** (`text/event-stream`)
  with `X-Accel-Buffering: no` so nginx never buffers them.
- The UI parses `data:` lines separated by blank lines and handles both
  complete and truncated JSON defensively.
