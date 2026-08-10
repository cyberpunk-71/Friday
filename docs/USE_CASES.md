# Friday–Δ — Use Cases

> Version 1.0.0 · Realistic, detailed use cases — each one is exercised by
> the automated eval suite (`tests/eval_suite.py`, 50 scenarios × 4–5
> steps = 203 prompts). Scenarios marked ⚙ run against the live server and
> are verified with multi-step check functions.

---

## 1. Live research & deep browsing

| # | Scenario | What Friday does |
|---|---|---|
| 1 | **Amarnath Yatra full chain** | Plans the pilgrimage end-to-end: route, permits, weather, dates — multi-hop across several searches |
| 4 | **Mayor deep chain** | "who is the mayor of ahmedabad" → name, party, election date, recent controversies — with sources |
| 5 | **CM Gujarat deep chain** | Current CM, portfolio, recent news — cross-checked across sources |
| 6 | **PM + finance minister chain** | National leadership + finance minister, policy context |
| 7 | **Ahmedabad events deep chain** | "events in ahmedbad today" — typo-corrected city, real events with venues |
| 8 | **BookMyShow multi-turn chain** | Movies showing today — BMS URL fallbacks (city root → /movies → /cinemas → Paytm), graceful when pages 403/404 |
| 9 | **Astronomy chain** | Uses the user's recalled astronomy preference + live data |
| 10 | **Perseid meteor shower chain** | When/where/how to watch, from live news |
| 13 | **Weather deep chain** | Today + forecast for the user's city |
| 14 | **Cricket deep chain** | Current series, scores, schedule |
| 15 | **Gold price deep chain** | Today's rate, trend, factors |
| 40 | **Top news deep chain** | Today's top stories, summarized with sources |
| 41 | **Stock market deep chain** | Indices, movers, context |

## 2. Shopping & comparison research

| # | Scenario |
|---|---|
| 16 | **EV scooter deep compare** — two scooters side by side: price, range, service network |
| 17 | **Budget phone camera deep chain** — camera-focused picks under a budget |
| 18 | **ML laptop deep chain** — specs for local ML work, alternatives |
| 20 | **Local LLM on 4GB deep chain** — can a 4 GB VM run an LLM? qwen/llama/phi/gemma options |
| 22 | **Handloom shops chain** — real shops in the user's city (memory: loves handloom sarees) |
| 39 | **Scooter research + email chain** — research then draft a comparison email |

## 3. Travel & local life

| # | Scenario |
|---|---|
| 11 | **Jaipur trip full planning** — itinerary, hotels, transport, budget |
| 12 | **Goa weekend full plan** — 3-day plan with costs |
| 23 | **Restaurant deep chain** — best picks by cuisine/area |
| 24 | **Street food trail chain** — a walkable food trail |
| 25 | **Heritage walk deep browse** — routes + history |
| 26 | **Old city history chain** — deep browse of the old city |
| 29 | **Goa trip budget chain** — line-item budget |
| 45 | **Weekend value plan deep chain** — value-for-money weekend, location-aware |
| 47 | **Itinerary artifact deep chain** — produce a downloadable itinerary |
| 48 | **Ahmedabad vs Surat deep chain** — structured city comparison |

## 4. Money & planning

| # | Scenario |
|---|---|
| 27 | **Salary savings plan chain** — savings % from memory, builds a plan |
| 28 | **Mom birthday gift chain** — gift ideas using memory (mom's birthday) |
| 42 | **In-hand salary deep chain** — CTC → in-hand math |
| 43 | **Tax saving deep chain** — 80C etc. (user preference recalled) |

## 5. Tasks, gates & follow-ups

| # | Scenario |
|---|---|
| 3 | **Mom saree purchase with payment gate** — "buy best handloom saree for mom under 10k" → task created, **payment gate blocks** until approval |
| 2 | **Deep phone research + salary + email** — research, remember salary fact, draft email |
| 30 | **Dentist reminder deep chain** — reminder set (memory has the appointment) |
| 31 | **Call Mom deep chain** — follow-up loop created |
| 33 | **Tracker deep chain** — "keep an eye on X" → real tracker in Tasks → Trackers |
| 38 | **Multi-hop gift chain** — nested research → recommendation |
| 39 | **Scooter research + email chain** — research + draft |

## 6. Memory & identity

| # | Scenario |
|---|---|
| 34 | **Identity + profile deep chain** — "who am I" answered from memory, no fabrication |
| 35 | **Memory correction deep chain** — user corrects; correction wins and persists |
| 36 | **Beliefs deep chain** — α/β beliefs updated, tensions surfaced |
| 37 | **Saree tension deep chain** — contradictory memories → tension surfaced |
| 46 | **Multi-turn context deep chain** — "ahmedabad" after "events in ahmedabad" stays coherent |

## 7. Focus & wellbeing (the flagship)

| # | Scenario |
|---|---|
| 32 | **Focus session deep chain** — "lets start a foscued mode?" → "30 minutes" → "ai agent build" → "start the session now" → a REAL session exists (never a fake claim) |
| 50 | **Health + focus deep chain** — energy/mood awareness, gentle coaching |

---

## The eval suite — how proof is generated

`tests/eval_suite.py` runs each scenario as a **multi-step conversation**
(4–5 prompts per scenario), checks every reply against **step-specific
check functions** (minimum keyword hits, forbidden patterns, JSON absence),
and emits:

- `eval_report.html` — human-readable report (downloadable from Admin → Eval 50)
- `eval_report.json` — machine-readable results
- `eval_chat_log.md` — the full conversation log, prompt by prompt

Run it:

```bash
.venv/bin/python tests/eval_suite.py            # against http://127.0.0.1:8010
```

The VM operator (`friday_eval` in `scripts/vm_cycle.sh`) runs the suite on
the deployed server and stores the report in `data/eval_report/`.

---

## Deterministic behaviors that are tested as use cases

- `start focos 25m allow github` (typo) → real session
- `dark mode kar do` / `light mode kar do` → theme applied, persists
- `my new deepseek api kes sk-xxx for research` → key saved scoped
- `which model are you` → truthful `deepseek · deepseek-chat` / `gemini · …`
- `buy X under 10k dont ask just buy` → payment-gated task (question forms
  like "is it a good time to buy" do **not** fire the gate)
- `stop phone notificaton for focus` → config delta, not a focus session
- `remind me at 14:00` → reminder row + nudge
- "keep an eye on gold price" → tracker row
