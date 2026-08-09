# Friday–Δ

**event-sourced · one-pass · code-acting · git-evolving**

Friday is a self-hosted personal cognitive companion — the fusion of two
systems:

| | Anima (the cognitive core) | Donna / Hermes (the hands) |
|---|---|---|
| Kept | smart chat, smart memory, beliefs α/β, VAD/EWMA affect, tensions, 4-probe recall | 73 declarative skills (SKILL.md), autonomy, task-execution posture |
| Absorbed into | RIVER + LOOM + CORTEX (one streamed LLM call per turn) | genome/skills (procedures retrieved by demand) + HANDS DAG engine |

## The three phases (not seven stages)

1. **SENSE** — 0 LLM calls, ~18ms. One in-RAM retrieval walk: binary hamming
   prefilter → fp16 matmul rerank → SQL probes → learned scorer →
   12 guaranteed slots ([2 identity][2 corrections][2 open loops][4 topical]
   [1 tension][1 procedure]) + deterministic NOW-block + constraint ledger.
2. **SPEAK** — 1 LLM call, streaming. Tokens 1..40 are the ⟨CTRL⟩ control
   block (depth·tooliness·emotionality·novelty·stakes + config_deltas +
   memory_writes + code_intent + ask) parsed incrementally — side-effects
   (dark mode, settings, FORGE tasks) fire BEFORE the prose finishes.
3. **SETTLE** — async, invisible. River materialization, affect rollup,
   spend ledger, turn audit.

## Repository layout

```
core/           the runtime (see core/README.md for module map)
  app.py        FastAPI — chat SSE + Admin/Tasks/Memory/Focus/Books panels
  cortex.py     one-pass pipeline + ⟨CTRL⟩ parsing + deterministic ingress
  river.py      append-only event log → delta views (atoms/claims/tensions)
  loom.py       the Weaver Walk (SENSE retrieval)
  psyche.py     Bayesian claims, VAD lexicon, EWMA, posture, Brier
  hands.py      DAG engine with postcondition assertions + 2 blocking gates
  hermes.py     pre-fire, $ governor, blast-radius classifier, ask budget
  tools.py      the `friday` SDK (12 verbs) + FORGE sandbox executor
  books.py      no-limit upload → OCR → chunk → book-scoped RAG
  focus.py      sessions, coalesced nudges, learned distraction patterns
  voice.py      TTS + barge-in protocol (STT is client-side)
  genome.py     git repo = the SELF; Gym replay fitness; canary/revert
  worker.py     reminders, trackers, nightly compaction, dreamstate
genome/         the SELF — git repo (prompts/ · verbs/ · policies/ · skills/ · scorer.json)
ui/             the single chat-first surface (all panels inline)
extension/      MV3 sensor: page learning + focus drift nudges
scripts/        VM ops (vm_cycle.sh — repository_dispatch loop)
infra/          systemd units + nginx for the VM
tests/          116 tests — offline-deterministic (SimProvider + SimSearch)
configs/        defaults.yaml (every knob live-editable) + search fixtures
```

## Quickstart (sandbox / dev, no keys needed)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py          # → http://0.0.0.0:8000
```

No API keys configured ⇒ the deterministic **SimProvider** runs the whole
pipeline (it still emits ⟨CTRL⟩ blocks and consumes the 12 slots), and
**SimSearch** answers from `configs/search_fixtures.yaml`. Everything is
therefore testable with zero egress. Set `DEEPSEEK_API_KEY` (+ optional
`TAVILY_API_KEY`) for the live provider — same code path.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q     # 116 passed
```

- `test_use_cases.py` — the 30 end-to-end use cases, asserting *quality*
  (right memory recalled, right gate raised, right view changed), not just
  that output exists.
- `test_providers.py` — DeepSeek wire-format verified against a local mock
  SSE server (auth header, json_mode, error surfacing, usage math).
- `test_river.py` — correction supremacy (user weight 10 vs inference 0.3),
  tension immune system, evidence decay, undo.
- `test_api.py` — every panel endpoint with valid-data assertions.

## Branch / deploy discipline

- Work on `arena/<session>-friday`; never merge to `main` without a green
  suite + a smoke of `/api/health` and one chat turn.
- The genome directory contains its own inner git repo at runtime (created
  lazily by `core/genome.py`). Do not commit `genome/.git` into the outer
  repo (it is gitignored via the inner `.git` removal rule).
- VM deployment follows the repository_dispatch pattern — see
  `docs/VM_OPERATIONS.md`. `scripts/vm_cycle.sh` on the dispatched branch is
  the operator; the workflow YAML must live on `main`.
- Never commit `.env`, `data/`, `*.db` (gitignored).
