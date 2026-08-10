# Friday–Δ — Genome: the evolving SELF

> Version 1.0.0 · The identity of Friday is not a file — it is a **git
> repository** that evolves. `genome/` is a git repo at runtime, and the
> whole system treats it that way: mutate → replay → promote or revert,
> with every step committed.

---

## What lives in the genome

```
genome/
├── prompts/
│   └── self.md              the cached system prompt — byte-identical every turn
├── policies/
│   ├── style.yaml           the VOICE (natural, warm, anti-template)
│   └── escalation.yaml      when to escalate
├── skills/**/SKILL.md       73 declarative Donna procedures (verifiable)
└── scorer.json              fitness corpus for replay evaluation
```

`genome/prompts/self.md` is the heart: identity, operating contract,
⟨CTRL⟩ schema, hard rules, VOICE rules (forbidden phrases, no robotic
headers, no markdown tables), identity truthfulness, focus-state truth.

## The Gym cycle

Runs nightly (window `genome.nightly_utc`, default 22:30 UTC) or on
demand:

1. **Mutate** — `Genome.mutate(n)` creates `n` candidate branches. Each
   branch applies one random applicable mutation (prompt variant, scorer
   tweak, nudge-policy proposal, style variant). Mutations whose target
   file doesn't exist are never chosen (a randomly-picked no-op used to
   make `mutate()` return fewer branches — fixed).
2. **Replay** — the fitness corpus (`scorer.json`) is replayed against
   each candidate; fitness is measured (reply quality, cost, latency,
   corrected/repeated/undone counts).
3. **Promote or revert** — a candidate with fitness > baseline is merged
   (`--no-ff`) and recorded as `canary=1` in `genome_commits`; failures
   are reverted with a reason. Every step is a git commit — `show me how
   you changed` surfaces the log (`GET /api/genome/log`).

## Rules that keep the SELF safe

- Merges only land on improved replay fitness (baseline comparison).
- Reverts are recorded and reversible.
- The prompt prefix stays byte-identical per turn (cache-prefix token
  budget `FRIDAY_CACHED_PREFIX_TOKENS`), so prompt caching keeps costs low
  even as the genome evolves.
- Skills have verification metadata (`has_verify`, `has_cassette`).

## Viewing it

- Admin → Genome: git log + skills (73 Donna procedures).
- `GET /api/genome/log`, `GET /api/genome/skills`,
  `GET /api/genome/explain/{skill}`.
