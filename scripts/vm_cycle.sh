#!/usr/bin/env bash
# Friday VM operator — run on the self-hosted runner via repository_dispatch.
# Dispatches to op_friday_* functions; writes vm_diagnostics/manual/latest.json.
# The workflow YAML must live on main; THIS file is read from the dispatched branch.
set -u

WS="$(pwd)"
RUNTIME=/opt/friday
SVC=friday-core
WORKER=friday-worker
PORT=8000
PUBLIC_IP="${PUBLIC_IP:-130-210-29-238}"
RESULT_FILE=vm_diagnostics/manual/latest.json
OUT=""

say()  { OUT="${OUT}${1}\n"; echo "$1"; }
log()  { say "================ op: $1 ================"; }
ok()   { say "---- op $1: OK"; }
fail() { say "---- op $1: FAILED"; exit_code=1; }
exit_code=0

write_result() {
  mkdir -p vm_diagnostics/manual
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  cat > "$RESULT_FILE" <<EOF
{"ts":"${TS}","command":"${CMD}","branch":"${BRANCH}","status":"$([ $exit_code -eq 0 ] && echo ok || echo fail)","output":$(python3 -c "import json,sys;print(json.dumps(sys.stdin.read()))" <<<"$(printf '%b' "$OUT")")}
EOF
}

health_ok() {
  curl -sf -o /dev/null "http://127.0.0.1:${PORT}/api/health"
}

wait_health() {
  local n=0
  while [ $n -lt 40 ]; do
    health_ok && return 0
    sleep 1; n=$((n+1))
  done
  return 1
}

# ---------------------------------------------------------------- ops -------
op_friday_deploy() {
  log friday_deploy
  [ -d "$WS/friday" ] || [ -d "$WS/core" ] || { fail friday_deploy; say "no friday sources in workspace"; return; }
  sudo mkdir -p "$RUNTIME" && sudo chown -R "$USER" "$RUNTIME" || true
  # rsync everything except the protected paths
  rsync -a --exclude '.git' --exclude '.env' --exclude 'data' --exclude 'backups' \
        --exclude '.venv' --exclude 'models' --exclude '__pycache__' \
        --exclude '.pytest_cache' --exclude 'vm_diagnostics' --exclude '*.zip' \
        "$WS"/ "$RUNTIME"/ 2>/dev/null || { fail friday_deploy; return; }
  # venv + deps (best-effort)
  if [ ! -d "$RUNTIME/.venv" ]; then python3 -m venv "$RUNTIME/.venv"; fi
  "$RUNTIME/.venv/bin/pip" install -q -r "$RUNTIME/requirements.txt" 2>/dev/null || true
  # systemd units
  cat > /tmp/friday-core.service <<UNIT
[Unit]
Description=Friday core (uvicorn)
After=network.target
[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${RUNTIME}
EnvironmentFile=${RUNTIME}/.env
ExecStart=${RUNTIME}/.venv/bin/python run.py
Restart=always
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
  cat > /tmp/friday-worker.service <<UNIT
[Unit]
Description=Friday worker (SETTLE)
After=network.target friday-core.service
[Service]
Type=simple
User=$(whoami)
WorkingDirectory=${RUNTIME}
EnvironmentFile=${RUNTIME}/.env
ExecStart=${RUNTIME}/.venv/bin/python -m core.worker
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
UNIT
  sudo cp /tmp/friday-core.service /etc/systemd/system/
  sudo cp /tmp/friday-worker.service /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable "$SVC" "$WORKER" 2>/dev/null
  sudo systemctl restart "$SVC" "$WORKER"
  if wait_health; then
    say "friday_health=http://127.0.0.1:${PORT}/api/health -> 200"
    ok friday_deploy
  else
    say "health failed; journal:"
    sudo journalctl -u "$SVC" -n 25 --no-pager 2>/dev/null | tail -25
    fail friday_deploy
  fi
}

op_friday_health() {
  log friday_health
  say "unit=$(systemctl is-active $SVC 2>/dev/null) worker=$(systemctl is-active $WORKER 2>/dev/null)"
  say "health_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/api/health)"
  if health_ok; then ok friday_health; else fail friday_health; fi
}

op_friday_test() {
  log friday_test
  cd "$RUNTIME"
  if [ -x .venv/bin/pytest ] || [ -x .venv/bin/python ]; then
    .venv/bin/python -m pytest tests -q 2>&1 | tail -5 | while read -r l; do say "$l"; done
    rc=${PIPESTATUS[0]}
    if [ "$rc" -eq 0 ]; then ok friday_test; else fail friday_test; fi
  else
    say "no venv on VM — run friday_deploy first"
    fail friday_test
  fi
}

op_friday_nginx() {
  log friday_nginx
  cat > /tmp/friday-nginx.conf <<NGINX
server {
    listen 80;
    server_name friday.${PUBLIC_IP}.nip.io friday.*;
    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_buffering off;
        proxy_read_timeout 300s;
    }
    location /api/health {
        proxy_pass http://127.0.0.1:${PORT}/api/health;
        proxy_set_header Host \$host;
        access_log off;
    }
}
NGINX
  sudo cp /tmp/friday-nginx.conf /etc/nginx/sites-available/friday
  sudo ln -sf /etc/nginx/sites-available/friday /etc/nginx/sites-enabled/friday
  sudo nginx -t 2>&1 | while read -r l; do say "$l"; done
  sudo systemctl reload nginx
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: friday.${PUBLIC_IP}.nip.io" http://127.0.0.1/)
  say "nip.io_check=friday.${PUBLIC_IP}.nip.io -> $code"
  if [ "$code" = "200" ]; then ok friday_nginx; else fail friday_nginx; fi
}

op_friday_diagnose() {
  log friday_diagnose
  say "unit=$(systemctl is-active $SVC 2>/dev/null) worker=$(systemctl is-active $WORKER 2>/dev/null)"
  say "ports: $(ss -ltn 2>/dev/null | grep -E ":${PORT} " || echo none)"
  sudo journalctl -u "$SVC" -n 30 --no-pager 2>/dev/null | tail -30 | while read -r l; do say "$l"; done
  say "disk: $(du -sh ${RUNTIME}/data 2>/dev/null || echo missing)"
  if health_ok; then ok friday_diagnose; else fail friday_diagnose; fi
}

op_friday_remove() {
  log friday_remove
  sudo systemctl stop "$SVC" "$WORKER" 2>/dev/null
  sudo systemctl disable "$SVC" "$WORKER" 2>/dev/null
  sudo rm -f /etc/systemd/system/${SVC}.service /etc/systemd/system/${WORKER}.service
  sudo rm -f /etc/nginx/sites-available/friday /etc/nginx/sites-enabled/friday
  sudo systemctl daemon-reload
  sudo systemctl reload nginx 2>/dev/null
  sudo rm -rf "$RUNTIME"
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/api/health 2>/dev/null || echo 000)
  say "health_after_remove=$code"
  if [ "$code" = "000" ]; then ok friday_remove; else fail friday_remove; fi
}

# ------------------------------------------------------------- dispatch -----
CMD="${1:-}"
BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"

case "$CMD" in
  friday_deploy)      run_ops friday_deploy friday_test friday_nginx ;;
  friday_health)      run_ops friday_health ;;
  friday_test)        run_ops friday_test ;;
  friday_nginx)       run_ops friday_nginx ;;
  friday_diagnose)    run_ops friday_diagnose ;;
  friday_remove)      run_ops friday_remove ;;
  friday_all)         run_ops friday_deploy friday_test friday_health friday_nginx ;;
  *) echo "unknown command: $CMD"; exit 2 ;;
esac

run_ops() { for op in "$@"; do "op_${op}"; done; }

write_result
exit $exit_code
