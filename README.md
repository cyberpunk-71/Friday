# Friday–Δ

**Version 1.0.0** — a self-hosted, personal cognitive companion. One chat for
research, memory, tasks, focus, books and settings — with a flagship,
ADHD-friendly Focus Studio.

> event-sourced · one-pass · code-acting · git-evolving · focus-first

---

## What Friday is

Friday is the fusion of two systems that no longer exist separately:

| | **Anima** (cognitive core) | **Donna / Hermes** (the hands) |
|---|---|---|
| Kept | smart chat, smart memory, beliefs α/β, VAD affect, tensions, 4-probe recall | 73 declarative skills, task execution, operational rigor |
| Absorbed into | RIVER + LOOM + CORTEX (one streamed LLM call per turn) | genome/skills + HANDS DAG engine |

It runs entirely on **your own hardware** (a 2 vCPU / 4 GB Oracle Linux VM in
the reference deployment), uses **DeepSeek or Google Gemini** as the brain,
and is reachable from anywhere through a **Cloudflare quick tunnel**.

### Primary feature: the Focus Studio

Focus is the front door of Friday. A three-page experience:

1. **Today** (landing page) — greeting, daily quote, today's plan
   (intentions), garden, streak, quick links, and a **Day Review** ritual.
2. **Focus Studio** — the sanctuary: ritual "set the scene" intake
   (task · first tiny step · reward · energy · mood · distraction
   pre-commit), phase-aware timer (WARM-UP → DEEP WORK → FINAL PUSH),
   breathing 4-7-8 breaks, brain-dump thought capture, pomodoro, brown
   noise / rain soundscapes, comebacks, confetti celebration with a Focus
   Score, and a **Focus Coach** — a conversational body double with full
   session context.
3. **Garden & Insights** — a 7-day garden that grows with completed
   sessions, focus-score chart, thought bank, session journal, and gentle
   "small wins".

---

## Feature highlights

- **One-pass chat pipeline** — SENSE (0 LLM, ~18 ms) → SPEAK (1 streaming
  LLM call) → SETTLE (async). Control side-effects ride in the first 40
  tokens of the reply (⟨CTRL⟩ block), so "dark mode kar do" applies the
  theme *while the reply is still streaming*.
- **Event-sourced memory** — an append-only RIVER of events materializes
  atoms, beliefs α/β, tensions and open loops. Corrections outrank
  inferences (weight 10). Nothing is overwritten; everything is reversible.
- **Live search & deep research** — Google News RSS → DuckDuckGo → Bing
  chain (Tavily when a key is set), date-aware queries, city typo fixing,
  and a multi-round tool loop (`web_search` / `web_read` /
  `memory_recall`) for complex questions.
- **Tasks with gates** — a DAG engine with postcondition assertions and two
  blocking gates (payment, gmail send). Deterministic fallback steps mean
  tasks always complete.
- **Focus Coach** — a warm, ADHD-friendly conversational body double that
  knows your task, why, energy, drifts and plan. It streams replies on
  Gemini/DeepSeek from inside the session.
- **Books** — upload any-size PDF, OCR → chunks → book-scoped RAG, quiz,
  PPTX export.
- **Genome** — the self lives in a git repo (`genome/`); nightly mutations
  are replayed against a fitness corpus and only promoted if they improve.
- **Admin panel** — every parameter live-tunable, provider keys
  (DeepSeek/Gemini/Tavily/Brave/Exa/Groq/OpenRouter), model routing per
  task (chat/research/books/eval), spend, turns, genome log, eval report.

---

## Repository layout

```
Friday/
├── README.md               this file
├── CHANGELOG.md            release history
├── VERSION                 current version
├── requirements.txt        Python dependencies
├── run.py                  entrypoint (uvicorn on FRIDAY_PORT)
├── .env.example            environment template
│
├── core/                   the runtime
│   ├── app.py              FastAPI — chat SSE + all panels/API
│   ├── cortex.py           one-pass pipeline, ⟨CTRL⟩ parsing, ingress
│   ├── river.py            append-only event log → delta views
│   ├── loom.py             SENSE retrieval walk (Weaver Walk)
│   ├── psyche.py           beliefs, VAD, posture, tensions
│   ├── hermes.py           pre-fire, governor, ask budget
│   ├── hands.py            task DAG engine + blocking gates
│   ├── focus.py            sessions, scores, coach data, analytics
│   ├── providers.py        DeepSeek/Gemini + search providers
│   ├── books.py            upload → OCR → RAG
│   ├── genome.py           the evolving SELF
│   ├── db.py               SQLite schema + helpers
│   ├── worker.py           background SETTLE (reminders, trackers…)
│   └── …                   tools.py, voice.py, embedder.py, extract.py …
│
├── ui/                     frontend (vanilla JS, no build step)
│   ├── index.html          the app shell (Today/Chat/Focus/Garden…)
│   ├── app.js              all UI logic
│   ├── style.css           design system (SUNLIT + Focus Sanctuary)
│   └── companion.png       the Friday companion avatar
│
├── tests/                  offline test suite (sim providers, no network)
│   ├── eval_suite.py       50 complex scenarios × 4–5 steps (203 prompts)
│   └── test_*.py           unit + integration tests
│
├── configs/                defaults.yaml, postures, search fixtures
├── genome/                 the SELF — prompts, policies, skills, scorer
├── extension/              MV3 Chrome sensor (page logging + focus nudges)
├── infra/
│   ├── github/workflows/   vm-ops.yml (repository_dispatch runner)
│   ├── nginx/              reverse proxy
│   └── systemd/            friday-core / friday-worker units
├── scripts/
│   ├── vm_cycle.sh         the VM operator (deploy/test/tunnel/eval…)
│   └── report_result.py    workflow result reporter
└── docs/                   THIS IS WHERE YOU ARE — start here
    ├── ARCHITECTURE.md     system design, in depth
    ├── SETUP.md            deploy your own Friday
    ├── API.md              every endpoint
    ├── USE_CASES.md        detailed, realistic use cases
    ├── DATA_MODEL.md       database schema
    ├── FOCUS.md            the flagship feature, end to end
    ├── GENOME.md           the evolving self
    └── OPERATIONS.md       VM ops, troubleshooting
```

## Friday Focus — the dedicated focus website

`focusapp/` is a **slim, standalone body-doubling website** that reuses the
core focus engine: only sessions, the Focus Coach, brain-dump, garden,
history and settings — none of the full app's sprawl. It runs on its own
port (`run_focus.py`, default 8010) and is what the reference VM deploys
as the primary product (`friday-focus.service`).

```
focusapp/
├── server.py    slim FastAPI — focus endpoints + coach + settings
└── ui/          the clean 3-view site (Focus · History · Settings)
```

Deploy with the `friday_focus_deploy` VM op — it removes the old full
Friday's unneeded components (extension, tests, old UI, books, worker)
from `/opt/friday` while **keeping your focus history DB**.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env            # set a DEEPSEEK_API_KEY or GEMINI_API_KEY
.venv/bin/python run.py         # serves on FRIDAY_PORT (default 8010)
```

Then open `http://localhost:8010`. The full self-hosted deployment
(Oracle Linux VM, systemd, nginx, Cloudflare tunnel) is documented in
[docs/SETUP.md](docs/SETUP.md).

### Testing

```bash
.venv/bin/python -m pytest tests/ -q      # 175+ tests, fully offline
.venv/bin/python tests/eval_suite.py      # 50 scenarios × 4–5 steps
```

## Documentation index

| Doc | What it covers |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | The full system design: pipeline, memory, search, tasks, genome, UI |
| [docs/SETUP.md](docs/SETUP.md) | Self-hosting guide (Oracle VM, systemd, nginx, tunnel) |
| [docs/API.md](docs/API.md) | Every HTTP endpoint with payloads |
| [docs/USE_CASES.md](docs/USE_CASES.md) | Detailed use cases + the 50-scenario eval suite |
| [docs/DATA_MODEL.md](docs/DATA_MODEL.md) | SQLite schema, tables, key columns |
| [docs/FOCUS.md](docs/FOCUS.md) | The flagship Focus Studio: data, scoring, coach, garden |
| [docs/GENOME.md](docs/GENOME.md) | The evolving self (mutation, promotion, gym) |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | VM operator commands + troubleshooting |

## Providers

- **LLM**: DeepSeek (`deepseek-chat`, `deepseek-reasoner`) or Google Gemini
  (`gemini-3.6-flash`, free-tier `gemini-3.5-flash` / `gemini-3.1-flash-lite`
  / `gemini-3-flash-preview`). Configurable per task (chat / deep research /
  books / eval) from the Admin panel, live-swappable, with automatic
  failover and auto-heal if a key is rejected.
- **Search**: Google News RSS → DuckDuckGo (html/lite) → Bing, with a
  Tavily key as an optional upgrade. Results are used only when relevant —
  never dumped into replies.

## License

Private project. All rights reserved unless stated otherwise in the repo.
