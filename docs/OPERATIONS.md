# Friday–Δ — Operations & Troubleshooting

> Version 1.0.0 · The deployment is driven through a **GitHub Actions
> self-hosted runner** on the VM via `repository_dispatch`. You never SSH:
> you dispatch a command and read the result JSON the VM pushes back to the
> branch (`vm_diagnostics/manual/latest.json`).

---

## 1. Mental model

```
push code → gh api repos/<owner>/<repo>/dispatches \
    -f event_type=vm-ops -f "client_payload[branch]=<branch>" \
    -f "client_payload[command]=friday_deploy"
→ runner on the VM runs scripts/vm_cycle.sh
→ writes vm_diagnostics/manual/latest.json and self-pushes it to <branch>
→ read it: git fetch origin <branch> && git show FETCH_HEAD:vm_diagnostics/manual/latest.json
```

`204` from the dispatch API means *accepted*, not *succeeded* — always read
the result JSON.

### The two files that matter

| File | Role | Must live on |
|---|---|---|
| `infra/github/workflows/vm-ops.yml` | receives `repository_dispatch`, runs `vm_cycle.sh`. Copy to `.github/workflows/vm-ops.yml` to activate (pushing it needs a token with the `workflows` scope) | **main** (GitHub only fires dispatches for workflows on the default branch) |
| `scripts/vm_cycle.sh` | the operator — `op_friday_*` functions | the **dispatched branch** |

## 2. Runtime layout on the VM

| Path / unit | Purpose |
|---|---|
| `/opt/friday` | deployed copy (code, `.env`, `data/friday.db`, `.venv`, `tunnel_url`) |
| `friday-core.service` | uvicorn on **0.0.0.0:8010** (`run.py`) |
| `friday-worker.service` | background SETTLE (`python -m core.worker`) |
| `friday-tunnel.service` | `cloudflared tunnel --url http://127.0.0.1:8010` |
| nginx | `friday.<public-ip>.nip.io` → `127.0.0.1:8010` |
| `vm_diagnostics/manual/latest.json` | the result channel |

## 3. Operator commands

| Command | Purpose |
|---|---|
| `friday_setup` | setkey + netcheck + deploy + test + nginx |
| `friday_deploy` | copy code, venv, units, restart, tests, nginx |
| `friday_test` | run the suite on the VM |
| `friday_health` | `systemctl is-active` + `/api/health` code |
| `friday_tunnel` | (re)start the quick tunnel → new URL in the result |
| `friday_uidiff` | byte-compare deployed `ui/` vs the branch |
| `friday_gemon` | route chat → Gemini (`gemini-3.6-flash`), reset heal timers, restart core |
| `friday_gemtest` | live Gemini round-trip from the VM (all models × both auths) |
| `friday_llmfix` | delete key-shaped strings from `llm.*.model` settings |
| `friday_cleanhist` | scrub leaked ⟨CTRL⟩/JSON from stored chat turns |
| `friday_chattest:<query>` | one real chat turn; the saved `LAST_TURN` is the result |
| `friday_eval` | run the 50-scenario suite; report to `data/eval_report/` |
| `friday_netcheck` | egress/port checks |
| `friday_diagnose` | systemd states + journal tail + disk |
| `friday_fix` | restart core, reinstall deps, health |
| `friday_remove` | stop + remove services |

## 4. Troubleshooting

### Chat falls back to another provider or shows a warning

- Check `GET /api/admin/overview` → `llm_last_error` and `llm_auto_heal`.
- `llm_auto_heal` = `{from, to, reason}` means a provider key was rejected
  and routing was switched permanently (once per 15 min). Fix the key in
  Admin → Models & Keys and Apply — the timer resets on explicit action.
- "gemini stream error: ReadError" with an otherwise-working key → the SSE
  endpoint dropped; the provider now falls back to `generateContent`
  automatically. If the key is genuinely rejected (401 with *"Expected
  OAuth 2 access token"*), the key is an expired ephemeral/Vertex token —
  create a fresh restricted key at aistudio.google.com/apikey.

### Tunnel URL died / changed

Quick tunnels are ephemeral. Re-run `friday_tunnel` and read the new
`TUNNEL_URL=...` from the result. For a stable URL, use a named tunnel
(`cloudflared tunnel create` + hostname route).

### UI looks stale despite deploying

1. `friday_uidiff` — confirms whether the VM serves the branch's files.
2. Cache-bust: every release bumps `?v=` in `ui/index.html`; users must
   hard-refresh once (Ctrl/Cmd+Shift+R).

### Deploy marked failed but the app works

`friday_deploy` runs deploy → test → nginx; a flaky test fails the step.
Read the result — if `friday_health=200` and only the test failed, the
code IS deployed. Report the failing test.

### Focus session shows stale "minutes left"

`Focus.active()` auto-expires sessions past their end time (worker-
independent). If a stale line still appears, it's echoed from old history
turns — run `friday_cleanhist` once.

### Worker crash-looping

Chat is unaffected (core is independent). Check
`journalctl -u friday-worker -n 50` via `friday_diagnose`. The worker
handles reminders/trackers/nightly gym; those pause until it recovers.

### OCI inbound firewall blocks :80

Quick tunnels are outbound-only and bypass it. The nip.io URL only works
from networks that can reach the VM's public IP on :80 (needs an NSG
rule allowing it).

## 5. Result-channel details

- `write_result()` in `vm_cycle.sh` writes `latest.json` then
  `git add -f` (the path is gitignored) and self-pushes to the branch.
- If the self-push fails, a public gist URL is printed as a fallback.
- The workflow also uploads `vm_diagnostics/` as a build artifact.
