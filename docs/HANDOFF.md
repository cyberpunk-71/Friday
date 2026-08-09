# HANDOFF — Friday–Δ (read this first, agent)

You are inheriting a working, tested codebase. Everything below is the
operating knowledge you need. **The suite is green (116 tests).** If it is not
green when you arrive, run `pytest tests/ -q` and fix before touching anything
else.

## 1. What this project is

Friday is the user's personal companion — the fusion they asked for:
**Anima's cognitive core** (smart chat, smart memory, beliefs, affect) with
**Donna/Hermes's hands** (73 declarative skills, task execution, autonomy).
The architecture follows the FRIDAY-Δ plan (see `docs/FRIDAY_DELTA_PLAN.md` if
present, else the README): one streamed LLM call per turn with a 40-token
⟨CTRL⟩ control block, an append-only event river instead of UPDATE-in-place,
12 verbs + a sandboxed Python SDK instead of a 150-tool catalog, and a git
genome that mutates nightly and only merges on replay-gym fitness.

User-visible name is **Friday** everywhere (they renamed Luna → Friday).
"Anima" and "Donna" live on only as internal concepts (module names, the
SOUL.md-derived operating style in `genome/policies/style.yaml`).

## 2. Module map (core/)

| File | What it does | Key invariants |
|---|---|---|
| `db.py` | SQLite WAL, RIVER events table + all DELTA/ops tables | NOTHING is UPDATEd in `events`; single-writer queue |
| `river.py` | event → view materialization | Correction weight 10 > inference 0.3; tensions auto-raised at write; evidence decay → uncertainty |
| `loom.py` | SENSE: one in-RAM retrieval walk, 12-slot fill | never-ask-twice is structural: 2 correction + 2 open-loop slots always present |
| `cortex.py` | the one-pass pipeline + ⟨CTRL⟩ parsing + deterministic ingress | first 40 tokens = control plane; side-effects fire before prose ends |
| `psyche.py` | claims α/β, VAD lexicon, EWMA trajectory, postures, Brier | math in Python; LLM only extracts evidence |
| `hands.py` | DAG engine, postcondition assertions, targeted repair | exactly 2 blocking gates: `payment`, `gmail_send`; DAG halts at gate |
| `hermes.py` | pre-fire, $ governor, blast-radius, ask budget (2/day) | no skill picking — that job is gone by design |
| `tools.py` | `friday` SDK (12 verbs, ~40 modules) + FORGE executor | code runs wrapped in `async def _friday_main()`; contract: end with `result = …` |
| `books.py` | upload (no size limit) → OCR → chunks → book RAG | streamed to disk; progress in Books panel |
| `focus.py` | sessions, drift nudges (coalesced: 1 toast + 1 chrome/10m) | voice nudge from drift 3; per-kind channel suppression |
| `genome.py` | git repo = the SELF; nightly mutants; Gym replay; canary + auto-revert | no cassette, no merge; fitness on logged outcomes |
| `worker.py` | reminders, trackers, nightly 22:30 UTC compaction, dreamstate | invisible to chat |
| `providers.py` | DeepSeek client (cached-prefix pricing) + SimProvider + SimSearch | Sim = deterministic test harness, not a toy |
| `app.py` | FastAPI: `/api/chat` SSE + all panels + extension ingress + WS voice | SSE never blocks; background DAGs stream via the EventBus |

## 3. The ⟨CTRL⟩ contract (the most important thing to preserve)

The model's reply MUST start with a JSON object:

```json
{"ctrl":{"depth":0.6,"tooliness":0.8,"emotionality":0.0,"novelty":0.3,"stakes":0.0,
 "config_deltas":{},"memory_writes":[],"code_intent":false,"ask":[]}}
```

- Parsed **incrementally** (`parse_ctrl` in `cortex.py`) — the block can
  arrive split across arbitrary stream chunks.
- `config_deltas` → live settings (e.g. `{"ui.theme":"dark"}` flips the theme
  while prose still streams).
- `memory_writes` → river at **inference weight 0.3** (never beats a user
  correction at 10).
- `code_intent:true` → background FORGE DAG; chat never waits.
- The SimProvider emits the same contract, so tests exercise it.

If you change the prompt, keep `prompts/self.md` + `genome/verbs/*.md` in
sync — they are the cached prefix and the 12-verb context.

## 4. Deterministic ingress (0 LLM) — do not regress

These run BEFORE the model in `Cortex.turn` and return instantly:

- `sk-…` keys via chat → `provider_keys` (scoped: research/voice/search/default)
- `start focos 25m allow github` (typos!) → focus session
- `remind me … at HH:MM` / missed-reminder corrections → dual-channel reminder

They exist because typos are guaranteed and the user asked for
"reply within 1 second" — don't route these through the LLM.

## 5. Testing discipline (what "tested" means here)

- Offline = SimProvider + SimSearch fixtures. Assert **quality**: the right
  memory was recalled, the right gate raised, the right view changed.
- `tests/test_use_cases.py` = the 30 use cases. `tests/test_api.py` = panel
  endpoints with valid-data assertions (not just 200).
- Before claiming done: `pytest tests/ -q` green, then boot `run.py`, then
  one live smoke: dark-mode turn, yatra turn (tracker), saree turn
  (payment gate), focus typo turn.
- The sandbox has NO egress except pypi/github — the DeepSeek/Tavily paths
  are verified against a local mock server (`tests/test_providers.py`).

## 6. Known TODOs / next steps

1. **VM deployment** — `scripts/vm_cycle.sh` + `infra/` + workflow YAML are
   wired per the repository_dispatch pattern (see `docs/VM_OPERATIONS.md`),
   but the workflow YAML must be merged to `main` before any dispatch fires.
   Verify `op_friday_deploy` / `op_friday_test` / `op_friday_nginx` on the VM.
2. **DeepSeek live validation** — run the suite with `DEEPSEEK_API_KEY` set
   (the user's key is available) and watch for real-model ⟨CTRL⟩ compliance;
   tighten `prompts/self.md` if the model skips the block (the cortex already
   degrades gracefully: no block ⇒ deterministic fallback ctrl).
3. **fastembed / cross-encoder** — `FRIDAY_EMBED_BACKEND=fastembed` and the
   INT8 reranker are hooked but need the VM model download + a perf pass.
4. **E2B sandbox** — FORGE runs in-process today; swap in the E2B/Firecracker
   executor on the VM (same `Forge.run` interface).
5. **MV3 extension** — functional; needs the server URL set in options and a
   real-browser test (drift → chrome notification → voice nudge).
6. **Gym evolution** — `FRIDAY_EVOLVE=1` enables nightly mutants + canary;
   watch `nightly.last_report` in admin for the first real fitness numbers.
7. **Dreamstate** — banks questions on a budget; wiring the top question into
   a natural conversation is the remaining piece.
8. **Mobile monitoring** — the `/api/admin/oneclick` slot exists; a PWA
   install link + sensor is the natural next step.

## 7. Rules you must not break (they are the design)

- Never write to `events` (append only). A "change" is a new event.
- Never add a third blocking gate. Escalation is governed by
  `genome/policies/escalation.yaml`, editable by chat.
- Never inject the 73 skill titles into the model prompt — a SKILL.md is a
  memory with `kind=procedure`, retrieved into exactly one slot.
- Never make the chat wait on a task. Background DAGs stream via the bus.
- Keep `genome/` files tracked in the outer repo but WITHOUT `genome/.git`
  (runtime re-inits it). `git rm -r --cached genome` if it ever sneaks back.

## 8. Branch/deploy discipline

- Always work on `arena/<session>-friday`. Push only there.
- Commit granular, message-first commits. The genome has its own git log —
  `"show me how you changed"` is a real user feature, keep commits honest.
- `main` should only ever contain green, smoke-tested states.
