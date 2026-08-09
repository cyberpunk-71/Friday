# VM Operations — Friday deployment (no SSH, repository_dispatch loop)

> Mental model: you never SSH. The VM runs a self-hosted GitHub Actions runner
> (label `anima-vm`). You push code, POST a `repository_dispatch`, the workflow
> runs `scripts/vm_cycle.sh` on the VM, which writes
> `vm_diagnostics/manual/latest.json` back to your branch — **that JSON is your
> terminal**. `204` means "accepted", not "succeeded" — always read the result.

## The two files that matter

| File | Role | Must live on |
|---|---|---|
| `.github/workflows/vm-ops.yml` | receives `repository_dispatch` and runs `vm_cycle.sh` | **main** (GitHub only fires dispatches for workflows on the default branch) |
| `scripts/vm_cycle.sh` | the operator; dispatches to `op_friday_*`; writes `latest.json` | the **dispatched branch** (reads work from any branch you name) |

## Friday runtime layout on the VM

| Path / unit | Purpose |
|---|---|
| `/opt/friday` | deployed copy (`run.py` lives here) |
| `/opt/friday/.env` | secrets — **never committed**, preserved by deploy |
| `/opt/friday/data` | SQLite DB + artifacts + books — **never overwritten by deploy** |
| `/opt/friday/genome` | the SELF — inner git repo initialized at runtime |
| `friday-core.service` | uvicorn `core.app:app` on `0.0.0.0:8000` |
| `friday-worker.service` | `python -m core.worker` (background SETTLE) |
| nginx :80 | `friday.<IP>.nip.io` → `127.0.0.1:8000` |

Deploy excludes: `.git`, `.env`, `data`, `backups`, `.venv`, `models`,
`__pycache__`, `vm_diagnostics`, `*.zip`. **Deploy refreshes code, never
touches the DB or secrets.**

## Dispatch recipe

```bash
# 1. push your branch
git push origin arena/<session>-friday

# 2. dispatch
gh api repos/cyberpunk-71/Friday/dispatches \
  -f event_type=vm-ops \
  -f "client_payload[command]=friday_deploy" \
  -f "client_payload[branch]=arena/<session>-friday"

# 3. read the result (deploy can take 2-4 min for pip install)
sleep 120 && git fetch origin arena/<session>-friday \
  && git show FETCH_HEAD:vm_diagnostics/manual/latest.json
```

## Friday command catalog (add to `vm_cycle.sh`)

| Command | Composes |
|---|---|
| `friday_deploy` | `friday_deploy friday_test friday_nginx` |
| `friday_health` | unit state + `/api/health` code |
| `friday_test` | `pytest tests -q` on the VM |
| `friday_nginx` | nginx block `friday.<IP>.nip.io` → :8000 |
| `friday_diagnose` | journalctl + port binding + disk |
| `friday_remove` | full teardown (stop unit, `rm -rf /opt/friday`, remove nginx block) |

## Ports on this VM

Anima `:8000` · Nova `:3001` · Aion `:8002` — Friday takes **:8000** (the user
asked for port 8000 or 80; nginx fronts it on :80 via nip.io).

## Checklist before dispatching a deploy

1. `pytest tests/ -q` green in sandbox.
2. `bash -n scripts/vm_cycle.sh`.
3. Smoke in sandbox: `/api/health`, one chat turn, one task.
4. Remember: the workflow YAML must be on `main`; everything else on your branch.
