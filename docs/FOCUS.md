# Friday–Δ — Focus Studio (flagship feature)

> Version 1.0.0 · Focus is the primary feature of Friday: a three-page
> experience (Today → Focus Studio → Garden & Insights) built to be
> peaceful, ADHD-friendly, and something you want to come back to.

---

## 1. The three pages

### 🏠 Today (landing page)

- Time-aware greeting + date + a rotating daily quote.
- Hero: **◎ Start a focus session** — or a live mini ring + "Open studio /
  Finish" when a session is running.
- **Today's plan** — up to 5 intentions (`/api/focus/plan`, date-scoped),
  checkable, persisted.
- This week: garden, streak, minutes, avg score, best hour.
- **✨ Day Review** — after a session, ask the Focus Coach for a warm
  3-line reflection on the day (streams in).
- Quick links: Chat / Garden / Tasks.

### ◎ Focus Studio (the sanctuary)

**Idle — "set the scene" ritual** (three quiet sections):

1. **✳️ intent** — one task + first tiny step.
2. **🎁 care** — reward ("then I watch one episode"), energy picker
   (😴→🚀), mood picker (😖→😄), distraction pre-commit
   ("when I want to check ___, I'll ___ instead").
3. **🌊 scene** — duration presets (15/25/45/60 + custom), pomodoro +
   break minutes, gentle mode, allow-domains, voice nudges, and a
   **soundscape picker** (off · brown noise · rain — generated locally).

CTAs: **Begin — I'm here with you** and **⚡ Just start — 5 minutes**.

**Active session:**

- **Phase-aware timer**: WARM-UP (amber) → DEEP WORK (coral) → FINAL PUSH
  (violet) → BREAK (teal). The aurora background and ring gradient follow
  the phase; Friday says a line at each transition.
- Companion avatar with a **breathing aura**; the whole view has a
  phase-tinted **aurora**.
- Task line in serif, first step + reward + pre-commit visible.
- **🧠 Brain dump** — "I'll hold it for you": thoughts append to the
  session (`/api/focus/thought`) and appear in the thought bank.
- **💬 Coach tab** — the conversational body double (below).
- **📜 Log tab** — Friday's ambient companion lines (start/check-ins/
  drifts/comebacks/break lines), rotated from pools so she never repeats.
- Stats card: energy, mood, drifts, comebacks, elapsed, target.
- Controls: **🧡 I'm back — count it** (after a drift), +5 min,
  soundscape cycle, **🏁 Finish session**.

**Break (pomodoro):**

- **4-7-8 breathing ring** animation, teal phase, "rest · water · breathe",
  drifts are not counted on breaks (server-side).

**Celebration (finish):**

- Confetti + win chime (C-E-G-C arpeggio), soft glow.
- **Focus Score ring** (animated) + grade:
  `FOCUS LEGEND (85+) · FOCUS MASTER (70+) · SOLID FOCUS (50+) ·
  YOU SHOWED UP`.
- **Reflection line** written from the data (drifts vs comebacks).
- Mood-after picker + one-line note → saved.
- "🌱 planted in your garden" + reward callout.

### 🌱 Garden & Insights

- 7-day **focus garden**: every completed session grows
  `· → 🌱 → 🌿 → 🌳` per day.
- Stats: sessions, best/avg score, streak, avg energy, energy delta.
- **Focus-score chart** (last 30 scored sessions).
- **Small wins** — gentle achievements: first seed · 3-day rhythm ·
  comeback heart · thought keeper · focus legend · five hours ·
  three in a day · ten sessions.
- **🧠 Thought bank** — every saved thought, grouped by session.
- **Journal** — sessions with score, drifts, comebacks, task, notes.

---

## 2. The Focus Coach

`POST /api/focus/coach` — an LLM endpoint (SSE) with a warm,
ADHD-friendly persona and **full session context** injected as JSON:

```json
{ "clock", "session": {task, why, first_step, distraction_plan,
  target_min, drift_count, mode, energy, mood},
  "elapsed_min", "comebacks", "today_minutes", "today_sessions",
  "week_minutes", "streak_days", "avg_score", "today_plan", "thoughts",
  "recent_sessions" }
```

Rules baked into the system prompt:

- 1–3 short sentences unless detail is asked.
- Stuck → ONE tiny 2-minute next step.
- Quit → normalize, remind the why, offer 5 more minutes or a graceful
  end — no guilt.
- Never generic: use the context. No headers, no tables, max one emoji.

Used by: the studio Coach tab (with quick chips: *I'm stuck / Give me a
2-minute version / Why am I doing this? / I want to quit / What's next?*)
and the Today **Day Review**.

---

## 3. Focus Score

Computed at finish (`Focus.finish`):

```
score = round(100 × (0.55×completion
                   + 0.25×(1 − drifts/8)
                   + 0.10×min(1, comebacks/2)
                   + 0.10×min(1, minutes/25)))
completion = min(1, elapsed_min / target_min)
```

Analytics (`stats().focus`): avg_score, best_score, series (last 7),
best_hour (+ minutes), avg_energy/mood, energy_delta, total_thoughts.

---

## 4. Data collected per session

| field | source |
|---|---|
| task / why / first_step | intake |
| energy / mood (1–5) | intake pickers |
| distraction_plan | intake pre-commit |
| drift_count | sensor / PWA drifts (breaks excluded) |
| comebacks | `/api/focus/comeback` |
| thoughts | brain-dump captures |
| energy_after / mood_after / notes | finish check-in |
| focus_score | computed |
| status / elapsed | lifecycle |

This is what powers the garden, the insights, the journal, the thought
bank, and the coach's context — every session makes Friday smarter about
you.

---

## 5. Focus in the rest of Friday

- **Chat**: `start focos 25m allow github` / multi-turn
  ("lets start a foscued mode?" → "30 minutes" → "ai agent build" →
  "start the session now") creates a REAL session — the deterministic
  ingress never lets the model fake a start. Active sessions surface in
  the NOW-block so replies know about them.
- **Sensor**: the MV3 extension reports page visits → drifts → nudges
  (toast/chrome/voice, gentle, coalesced).
- **Nudges**: soft two-note bell, music-box chimes, voice only after
  repeated drifts.
- **UI**: the sidebar widget and chat-header button always show the live
  countdown and jump to the studio.
