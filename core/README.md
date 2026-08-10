# core/ — module map (Friday–Δ runtime)

```
app.py        FastAPI surface: /api/chat (SSE) + Admin/Tasks/Memory/Focus/Books
              + extension ingress (/api/ext/*) + voice WS + artifacts download
config.py     YAML defaults + .env + live settings merge (cfg.live_or)
db.py         SQLite WAL; RIVER events + DELTA views + ops tables; single writer
river.py      append-only event log → materialized views (atoms, claims α/β,
              tensions, corrections, constraints, open loops, working set)
embedder.py   bge-small-384d (fastembed on VM) / deterministic hash backend;
              VectorIndex: 384-bit binary prefilter + fp16 matmul rerank
loom.py       SENSE — the Weaver Walk: prefilter → rerank → SQL probes →
              learned scorer → 12-slot fill → constraint ledger → NOW-block
scorer.py     24-feature logistic regression, online SGD, genome weights as
              prior, printable explanations
extract.py    SETTLE extraction: LLMExtractor (VM) / HeuristicExtractor (offline)
psyche.py     Bayesian canon claims (α/β), VAD lexicon, EWMA, postures, Brier
heart.py      re-export of psyche's Heart (Anima module layout preserved)
hermes.py     deterministic pre-fire, $ governor, blast-radius, ask budget
hands.py      DAG engine + postcondition assertions + repair; 2 blocking gates
tools.py      `friday` SDK (12 verbs) + Forge sandbox executor + pptx_min
pptx_min.py   zero-dependency OOXML PPTX generator
focus.py      sessions, drift nudges (coalesced), learned patterns
voice.py      TTS (edge-tts on VM) + barge-in protocol frames
books.py      no-limit upload → OCR (Docling/Paddle/Tesseract on VM) → chunks → RAG
skills.py     SkillRegistry over genome/skills (73 Donna SKILL.md procedures)
genome.py     git repo = the SELF; mutants; Gym replay fitness; canary+revert
worker.py     reminders, trackers, nightly 22:30 compaction, dreamstate
providers.py  DeepSeek client + SimProvider/SimSearch (offline deterministic)
obs.py        correlation ids + turn audit (the "why did you say that" ledger)
```
