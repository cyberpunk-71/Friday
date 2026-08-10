# HANDOFF — Friday–Δ v1.0.0 (read this first, agent)

You are inheriting a working, tested codebase. **The suite is green
(175+ tests, fully offline).** If it is not green when you arrive, run
`pytest tests/ -q` and fix before touching anything else.

## 1. What this project is

Friday is the user's personal cognitive companion — the fusion of
**Anima's cognitive core** (smart chat, smart memory, beliefs, affect) with
**Donna/Hermes's hands** (73 declarative skills, task execution). One
streamed LLM call per turn with a 40-token ⟨CTRL⟩ control block, an
append-only event river, and a git genome that mutates nightly and only
merges on replay-gym fitness. **Focus is the flagship feature** — a
three-page experience (Today / Focus Studio / Garden & Insights) with a
Focus Coach.

## 2. Where the docs live

| Doc | Read it when… |
|---|---|
| `docs/ARCHITECTURE.md` | you need the full system design |
| `docs/API.md` | you touch any endpoint |
| `docs/DATA_MODEL.md` | you touch the database |
| `docs/USE_CASES.md` | you change behavior (50 eval scenarios cover it) |
| `docs/FOCUS.md` | you touch the flagship feature |
| `docs/GENOME.md` | you touch the evolving self |
| `docs/SETUP.md` | you deploy or change deployment |
| `docs/OPERATIONS.md` | you operate the VM (dispatch → read latest.json) |

## 3. Hard-won invariants (do not regress)

- **Python 3.9 compatibility**: `from __future__ import annotations`
  everywhere; no `dict | None` in FastAPI annotations (`Optional[...]`);
  no `int.bit_count()` / `anext()`.
- **One-pass chat**: deterministic ingresses fire BEFORE the LLM (keys,
  focus start/stop, theme, reminders, trackers, payment gate, model
  identity). The model must never fake an action.
- **⟨CTRL⟩ can never render**: strip complete/truncated/double/marked
  ctrl JSON server-side AND client-side (`polish_reply`,
  `strip_truncated_ctrl`, `_cut_double_ctrl`, UI `stripLeadingCtrl`).
- **No provider can kill chat**: every httpx error is a RuntimeError,
  failover is silent, auto-heal switches a rejected key's routing once per
  15 min, Gemini streaming falls back to `generateContent`.
- **Focus auto-expiry is in `Focus.active()`** — never depend on the
  worker for state correctness.
- **Typing-safe UI**: never re-render over an input the user is typing in
  (snapshot/restore + typing guard).
- **Tests must stay offline**: SimProvider + SimSearch; the `db` fixture
  is hermetic (env keys removed).
- **Deploy hygiene**: `vm_diagnostics/` and `*.zip` are gitignored but
  force-added for results; `genome/` must never be committed as a
  submodule (remove `genome/.git` before committing).

## 4. Quick operational loop

```bash
# dispatch an op to the VM
gh api repos/<owner>/<repo>/dispatches -f event_type=vm-ops \
  -f "client_payload[branch]=<branch>" -f "client_payload[command]=friday_deploy"
# read the result the VM pushes back
git fetch origin <branch> && git show FETCH_HEAD:vm_diagnostics/manual/latest.json
```

See `docs/OPERATIONS.md` for every command and troubleshooting.

## 5. Release process (v1.0.0 and beyond)

1. Run `pytest tests/ -q` locally and on the VM (deploy does the latter).
2. Bump `VERSION` + `CHANGELOG.md` + the UI cache-buster in
   `ui/index.html`.
3. Commit to the working branch, open a PR to `main`, merge.
4. Tag `v<major>.<minor>.<patch>` and create a GitHub release from the
   merge commit, attaching the eval report if fresh.
