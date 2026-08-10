# Friday–Δ — Setup & Deployment

> Version 1.0.0 · Reference deployment: **Oracle Cloud VM** (2 vCPU / 4 GB,
> Oracle Linux 9.8, aarch64), systemd services, nginx reverse proxy and a
> **Cloudflare quick tunnel** for public access (outbound-only — no inbound
> ports need to be open).

---

## 1. Requirements

- Python **3.9+** (the reference VM runs 3.9.25).
- `dnf`/`yum` package manager (Oracle/RHEL-family). `apt` is also handled.
- Internet egress (pypi, GitHub, api.deepseek.com, generativelanguage…,
  tunnel edge).
- An LLM key: **DeepSeek** (`sk-…`) or **Google Gemini**
  (`AIza…` / `AQ.Ab…`). Gemini free-tier models are
  `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-3-flash-preview`.

## 2. Local run (development)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env          # add DEEPSEEK_API_KEY or GEMINI_API_KEY
.venv/bin/python run.py       # FRIDAY_PORT default 8000 (VM uses 8010)
```

Open `http://localhost:8000`. Without a key Friday runs in **offline/sim**
mode (deterministic stub replies — enough to explore panels).

Run tests (fully offline):

```bash
.venv/bin/python -m pytest tests/ -q
```

## 3. VM deployment (the reference setup)

### 3.1 Provision

Oracle Linux 9.8 aarch64, 2 vCPU / 4 GB. Ensure egress to
pypi/github/LLM APIs (default OCI egress is open).

### 3.2 The operator script

Everything is driven by `scripts/vm_cycle.sh` — it runs inside a GitHub
Actions **self-hosted runner** on the VM, triggered by
`repository_dispatch` (workflow: `infra/github/workflows/vm-ops.yml`, must
live on `main`):

```bash
# from any machine with gh access to the repo
gh api repos/<owner>/<repo>/dispatches \
  -f event_type=vm-ops \
  -f "client_payload[branch]=<branch>" \
  -f "client_payload[command]=friday_deploy"
```

The script:

1. Copies `core ui configs tests requirements.txt run.py .env.example
   infra scripts extension genome` into `/opt/friday`.
2. Creates/updates the venv and installs dependencies (3 retries).
3. Bootstraps `.env` (`FRIDAY_PORT=8010`, model defaults) — but **strips
   `DEEPSEEK_API_KEY`** so the admin-panel key (DB) is authoritative.
4. Installs systemd units `friday-core.service` + `friday-worker.service`.
5. Restarts services, waits for `/api/health` → 200.
6. Runs the full test suite on the VM, then nginx checks.
7. Writes `vm_diagnostics/manual/latest.json` and **self-pushes it** to the
   branch (the result channel; the workflow's API-report step is a backup).

### 3.3 systemd units

`infra/systemd/`:

- `friday-core.service` — `ExecStart=.venv/bin/python run.py`,
  `EnvironmentFile=/opt/friday/.env`, `Restart=always`.
- `friday-worker.service` — `ExecStart=.venv/bin/python -m core.worker`
  (background SETTLE: reminders, trackers, focus completion, nightly gym).

### 3.4 nginx

`infra/nginx/friday-proxy.conf` — reverse proxy `friday.<public-ip>.nip.io`
→ `127.0.0.1:8010`. On Oracle Linux with SELinux enforcing, allow the
network connect:

```bash
sudo setsebool -P httpd_can_network_connect 1
```

> Note: OCI security lists/NSGs may still block inbound :80 — the
> **Cloudflare quick tunnel** below is the dependable public path.

### 3.5 Public access — Cloudflare quick tunnel

`friday_tunnel` op installs `cloudflared` (arm64 binary) and a systemd
unit `friday-tunnel.service`:

```bash
cloudflared tunnel --url http://127.0.0.1:8010
```

The ephemeral URL (`https://<random>.trycloudflare.com`) is written to
`/opt/friday/tunnel_url` and reported via the op result. **The URL rotates
on restart** — re-run `friday_tunnel` and read the new URL.

For a **permanent URL**, use a named tunnel:
`cloudflared tunnel create friday` → route a hostname → point
`friday-tunnel.service` at it.

## 4. Operator commands (`scripts/vm_cycle.sh`)

| command | does |
|---|---|
| `friday_deploy` | deploy code + venv + units + restart + tests + nginx |
| `friday_test` | run the VM test suite |
| `friday_health` | service states + health code |
| `friday_tunnel` | (re)start the tunnel, report URL |
| `friday_gemon` | switch chat routing to Gemini (`gemini-3.6-flash`), restart core |
| `friday_gemtest` | live Gemini round-trip from the VM with the DB key |
| `friday_llmfix` | clear key-shaped strings from model settings |
| `friday_cleanhist` | scrub leaked ctrl JSON out of stored turns |
| `friday_chattest:<query>` | run one real chat turn, report the saved turn |
| `friday_eval` | run the 50-scenario eval suite, store the report |
| `friday_netcheck` | egress/port diagnostics |
| `friday_diagnose` | systemd + journal + disk |
| `friday_fix` | restart + pip reinstall + health |
| `friday_uidiff` | byte-compare deployed UI vs the branch |

See [docs/OPERATIONS.md](OPERATIONS.md) for troubleshooting.

## 5. Environment variables

See `.env.example` for the full list. Key ones:

| var | default | meaning |
|---|---|---|
| `DEEPSEEK_API_KEY` / `GEMINI_API_KEY` | — | LLM keys (DB keys win when set) |
| `FRIDAY_PORT` | 8000 | HTTP port (VM: 8010) |
| `FRIDAY_HOST` | 0.0.0.0 | bind |
| `FRIDAY_ACCESS_TOKEN` | — | Bearer auth for admin endpoints |
| `FRIDAY_DAILY_BUDGET_USD` | 6.0 | daily spend cap |
| `FRIDAY_DATA_DIR` | ./data | SQLite + artifacts |
| `FRIDAY_EMBED_BACKEND` | hash | hash / fastembed |
| `TAVILY_API_KEY` / `SEARCH_PROVIDER` | — / tavily | web search |
| `GROQ_API_KEY` | — | voice/STT |

## 6. First-run checklist

1. Deploy (`friday_deploy`) — health 200.
2. Set an LLM key in **Admin → Models & Keys** (or chat: *"my deepseek key
   is sk-…"*). The key in the DB is authoritative.
3. Optionally set a **Gemini** key and flip the Chat row to Gemini
   (free-tier: `gemini-3.1-flash-lite`).
4. Start the tunnel (`friday_tunnel`) and bookmark the URL.
5. Run `friday_eval` once to generate proof reports (Admin → Eval 50).
6. Install the **MV3 sensor extension** (Admin → One-Click) for focus
   drift tracking across all tabs.
