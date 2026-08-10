# Changelog

## v1.0.0 — 2026-08-10

The first released version of Friday–Δ. Focus is the flagship feature.

### Flagship: Focus Studio (v3.6.x line)

- **Today** landing page — greeting, daily quote, today's plan
  (intentions), garden, streak, quick links, Day Review ritual.
- **Focus Studio sanctuary** — ritual intake (task / first tiny step /
  reward / energy / mood / distraction pre-commit), phase-aware timer
  (WARM-UP → DEEP WORK → FINAL PUSH), breathing 4-7-8 breaks, brain-dump
  thought capture, pomodoro, brown-noise/rain soundscapes, comebacks,
  Focus Score + celebration (confetti, grade, reflection line), garden.
- **Focus Coach** — conversational body double with full session context
  (SSE, ADHD-friendly persona, quick chips).
- **Garden & Insights** — 7-day garden, focus-score chart, thought bank,
  journal, small wins.
- Focus Score analytics (avg/best/series/best-hour/energy delta), session
  intake data, `/api/focus/plan`, `/api/focus/coach`, `/api/focus/finish`,
  `/api/focus/thought`, `/api/focus/comeback`.

### Chat & quality

- Gemini support with model-chain 404 fall-forward, dual auth
  (x-goog-api-key / Bearer), streaming→non-streaming fallback, free-tier
  models in Admin routing.
- Deterministic model identity ("which model are you" → exact provider).
- Silent provider failover + auto-heal on rejected keys (15-min anti-flap).
- ⟨CTRL⟩ leak elimination (truncated/double/marked/mid-reply JSON) at
  server + UI layers; history scrubbing (`friday_cleanhist`).
- Stale-focus echo fixed (auto-expiry + history filtering).
- Typing-safe UI re-renders; gentle two-note bell + music-box chimes.

### UI

- SUNLIT design system (warm editorial light + warm dark), serif brand,
  timestamps/copy buttons/day dividers, theme toggle persisted.
- Focus Sanctuary scene: aurora, breathing aura, serif insights.

### Ops & platform

- `vm_cycle.sh` operator suite (deploy/test/tunnel/chattest/eval/
  gemtest/gemon/llmfix/cleanhist/uidiff…), self-push result channel.
- Auto-expiring focus sessions; worker-independent correctness.
- Genome mutation fix (only applicable mutations chosen).
- 175+ offline tests; 50-scenario eval suite (203 prompts).

## v1.1.0-alpha — Friday Focus (dedicated focus website)

- New `focusapp/` — a slim, clean, standalone body-doubling website:
  Focus (ritual plan → phase timer → coach → brain dump → celebration),
  History (garden, scores, thought bank, journal), Settings (LLM keys,
  model, theme, sound).
- Reuses the core focus engine; LLMs kept for the Focus Coach.
- VM: `friday_focus_deploy` op removes unneeded v1 components from
  `/opt/friday` (extension, tests, old UI, genome, books, worker) while
  keeping the focus history DB and the tunnel. Other VM projects untouched.
