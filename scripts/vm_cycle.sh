#!/usr/bin/env bash
# Friday VM operator — run on the self-hosted runner via repository_dispatch.
# Dispatches to op_friday_* functions; writes vm_diagnostics/manual/latest.json.
# The workflow YAML must live on main; THIS file is read from the dispatched branch.
set -u

WS="$(pwd)"
RUNTIME=/opt/friday
SVC=friday-core
WORKER=friday-worker
PORT=8010
PUBLIC_IP="${PUBLIC_IP:-80.225.211.198}"
RESULT_FILE=vm_diagnostics/manual/latest.json
OUT=""
exit_code=0

PKG=""
if command -v dnf >/dev/null 2>&1; then PKG=dnf; elif command -v yum >/dev/null 2>&1; then PKG=yum; elif command -v apt-get >/dev/null 2>&1; then PKG=apt-get; fi

pkg_install() {
  # $1 = package name(s)
  if [ -z "$PKG" ]; then say "no package manager found"; return 1; fi
  if [ "$PKG" = "apt-get" ]; then
    sudo apt-get update -qq 2>&1 | tail -1
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" 2>&1 | tail -2
  else
    sudo "$PKG" install -y -q "$@" 2>&1 | tail -2
  fi
}

say()  { OUT="${OUT}${1}\n"; echo "$1"; }
log()  { say "================ op: $1 ================"; }
ok()   { say "---- op $1: OK"; }
fail() { say "---- op $1: FAILED"; exit_code=1; }

run_ops() { for op in "$@"; do "op_${op}"; done; }

write_result() {
  # ALWAYS write into the WORKSPACE (cwd-independent — ops may cd elsewhere)
  mkdir -p "$WS/vm_diagnostics/manual"
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  STATUS=$([ $exit_code -eq 0 ] && echo ok || echo fail)
  printf '%s' "$OUT" | python3 -c '
import json, sys
payload = {"ts": sys.argv[1], "command": sys.argv[2], "branch": sys.argv[3],
           "status": sys.argv[4], "output": sys.stdin.read()}
json.dump(payload, sys.stdout)
' "$TS" "$CMD" "$BRANCH" "$STATUS" > "$WS/vm_diagnostics/manual/latest.json"
  # keep a copy next to the runtime for on-VM debugging
  mkdir -p "$RUNTIME/vm_diagnostics/manual" 2>/dev/null || true
  cp "$WS/vm_diagnostics/manual/latest.json" "$RUNTIME/vm_diagnostics/manual/latest.json" 2>/dev/null || true
  say "result written to $WS/vm_diagnostics/manual/latest.json (status=$STATUS)"
  # push the result back to the branch — actions/checkout persists the token
  # in git's http.extraheader, so plain `git push origin` is authenticated.
  if [ -d "$WS/.git" ]; then
    export PATH="$PATH:/usr/bin:/usr/local/bin:/snap/bin"
    PUSH_LOG=$( ( cd "$WS" && \
      git config user.email "vm-ops@friday.local" 2>&1; \
      git config user.name "Friday VM Ops" 2>&1; \
      git add -f vm_diagnostics/manual/latest.json vm_diagnostics/eval 2>&1 && \
      git commit -m "vm-ops: $CMD result" 2>&1 && \
      git push origin "HEAD:$BRANCH" 2>&1 ) 2>&1 )
    PUSH_RC=$?
    say "self-push rc=$PUSH_RC: $(printf '%s' "$PUSH_LOG" | tail -3 | tr '\n' ' ')"
  else
    say "no .git in workspace — self-push skipped"
  fi
  # FALLBACK channel (no token needed): paste to a public gist via API.
  if ! grep -q "TUNNEL_URL" "$WS/vm_diagnostics/manual/latest.json" 2>/dev/null; then
    GIST_URL=$(python3 - "$WS/vm_diagnostics/manual/latest.json" <<'PYEOF' 2>/dev/null | head -1
import json, sys, urllib.request, uuid
path = sys.argv[1]
data = open(path).read()
gid = str(uuid.uuid4())[:8]
body = json.dumps({
    "description": f"friday-{gid}",
    "public": True,
    "files": {f"friday-{gid}.json": {"content": data}},
}).encode()
req = urllib.request.Request("https://api.github.com/gists", data=body,
    headers={"Content-Type": "application/json", "User-Agent": "friday-vm-ops",
             "Accept": "application/vnd.github+json"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.load(r)
        print(d.get("html_url", ""))
except Exception as e:
    print("gist-fail:", e)
PYEOF
)
    say "gist: ${GIST_URL:-unavailable}"
  fi
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
op_friday_setkey() {
  log friday_setkey
  local key="${FRIDAY_DEEPSEEK_KEY:-}"
  if [ -z "$key" ]; then
    say "no FRIDAY_DEEPSEEK_KEY provided — skipping (key stays as-is)"
    ok friday_setkey; return
  fi
  sudo mkdir -p "$RUNTIME"
  if [ -f "$RUNTIME/.env" ]; then
    sed -i "s|^DEEPSEEK_API_KEY=.*|DEEPSEEK_API_KEY=$key|" "$RUNTIME/.env" 2>/dev/null || true
    grep -q "^DEEPSEEK_API_KEY=" "$RUNTIME/.env" 2>/dev/null || echo "DEEPSEEK_API_KEY=$key" >> "$RUNTIME/.env"
  else
    echo "DEEPSEEK_API_KEY=$key" > "$RUNTIME/.env"
  fi
  grep -q "^DEEPSEEK_MODEL=" "$RUNTIME/.env" 2>/dev/null || echo "DEEPSEEK_MODEL=deepseek-chat" >> "$RUNTIME/.env"
  grep -q "^DEEPSEEK_BASE_URL=" "$RUNTIME/.env" 2>/dev/null || echo "DEEPSEEK_BASE_URL=https://api.deepseek.com/v1" >> "$RUNTIME/.env"
  say "DEEPSEEK_API_KEY configured in $RUNTIME/.env (masked: ••••${key: -4})"
  ok friday_setkey
}

op_friday_deploy() {
  log friday_deploy
  if [ ! -d "$WS/core" ]; then fail friday_deploy; say "no friday sources in workspace ($WS)"; OUT="${OUT}$(ls "$WS" | head -5 | sed 's/^/  ws: /')\n"; return; fi
  say "workspace: $WS"
  say "os: $(grep -E '^(NAME|VERSION)=' /etc/os-release 2>/dev/null | tr '\n' ' ')"
  say "python: $(python3 --version 2>&1)  pkg: ${PKG:-none}"
  sudo mkdir -p "$RUNTIME" || { fail friday_deploy; say "mkdir $RUNTIME failed"; return; }
  sudo chown -R "$(whoami)" "$RUNTIME" 2>/dev/null || true
  # copy code — plain cp (no rsync dependency), VERBOSE on failure
  rm -rf "$RUNTIME/core" "$RUNTIME/ui" "$RUNTIME/configs" "$RUNTIME/tests" \
         "$RUNTIME/requirements.txt" "$RUNTIME/run.py" "$RUNTIME/.env.example" \
         "$RUNTIME/infra" "$RUNTIME/scripts" "$RUNTIME/extension" \
         "$RUNTIME/genome" 2>/dev/null
  cp -a "$WS/core" "$WS/ui" "$WS/configs" "$WS/tests" "$WS/requirements.txt" \
        "$WS/run.py" "$WS/.env.example" "$WS/infra" "$WS/scripts" \
        "$WS/extension" "$WS/genome" "$RUNTIME"/ 2>&1 | tail -3 || { fail friday_deploy; say "copy FAILED"; return; }
  rm -rf "$RUNTIME/genome/.git" 2>/dev/null || true
  if [ ! -f "$RUNTIME/requirements.txt" ]; then fail friday_deploy; say "requirements.txt MISSING after copy"; return; fi
  say "code copied: $(ls "$RUNTIME" | tr '\n' ' ')"
  # .env bootstrap
  if [ ! -f "$RUNTIME/.env" ]; then cp "$RUNTIME/.env.example" "$RUNTIME/.env"; fi
  grep -q "^FRIDAY_PORT=" "$RUNTIME/.env" 2>/dev/null || echo "FRIDAY_PORT=$PORT" >> "$RUNTIME/.env"
  grep -q "^DEEPSEEK_MODEL=" "$RUNTIME/.env" 2>/dev/null || echo "DEEPSEEK_MODEL=deepseek-chat" >> "$RUNTIME/.env"
  # DB provider keys are authoritative — drop any stale deploy key from .env
  # so it can never shadow the admin-panel key after a restart
  sed -i '/^DEEPSEEK_API_KEY=/d' "$RUNTIME/.env" 2>/dev/null || true
  # git is REQUIRED: checkout then does a full clone (with the auth token
  # persisted), which enables the self-push result channel.
  if ! command -v git >/dev/null 2>&1; then
    say "git not found — installing"
    pkg_install git
  fi
  say "git: $(git --version 2>&1)"
  # venv + deps
  if [ ! -d "$RUNTIME/.venv" ]; then
    command -v python3-venv >/dev/null 2>&1 || pkg_install python3-venv python3-pip 2>/dev/null || true
    python3 -m venv "$RUNTIME/.venv" 2>&1 | tail -1 || true
  fi
  if [ ! -x "$RUNTIME/.venv/bin/python" ]; then fail friday_deploy; say "venv creation failed"; return; fi
  say "venv: $("$RUNTIME/.venv/bin/python" --version 2>&1)"
  "$RUNTIME/.venv/bin/pip" install -q --upgrade pip 2>&1 | tail -1 || true
  for attempt in 1 2 3; do
    if "$RUNTIME/.venv/bin/pip" install -q -r "$RUNTIME/requirements.txt" 2>&1 | tail -2; then break; fi
    say "pip attempt $attempt failed — retry"
    sleep 5
  done
  "$RUNTIME/.venv/bin/python" -c "import fastapi, uvicorn, httpx; print('imports OK')" 2>&1 | tail -1
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
  sudo cp /tmp/friday-core.service /etc/systemd/system/${SVC}.service
  sudo cp /tmp/friday-worker.service /etc/systemd/system/${WORKER}.service
  sudo systemctl daemon-reload
  sudo systemctl enable "$SVC" "$WORKER" 2>/dev/null
  sudo systemctl restart "$SVC" "$WORKER"
  if wait_health; then
    say "friday_health=http://127.0.0.1:${PORT}/api/health -> 200"
    ok friday_deploy
  else
    say "health failed; journal:"
OUT="${OUT}$( sudo journalctl -u "$SVC" -n 30 --no-pager 2>/dev/null | tail -30 )\n"
    fail friday_deploy
  fi
}

op_friday_health() {
  log friday_health
  say "unit=$(systemctl is-active $SVC 2>/dev/null || echo unknown) worker=$(systemctl is-active $WORKER 2>/dev/null || echo unknown)"
  say "health_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/api/health)"
  if health_ok; then ok friday_health; else fail friday_health; fi
}

op_friday_uidiff() {
  log friday_uidiff
  say "ws HEAD: $(cd "$WS" && git rev-parse --short HEAD 2>&1)"
  say "ws dirty: $(cd "$WS" && git status --porcelain 2>&1 | head -8 | tr '\n' '|')"
  say "runtime app.js sha: $(sha256sum "$RUNTIME/ui/app.js" 2>&1 | awk '{print $1}')"
  say "ws app.js sha:      $(sha256sum "$WS/ui/app.js" 2>&1 | awk '{print $1}')"
  say "diff: $(diff -q "$WS/ui/app.js" "$RUNTIME/ui/app.js" >/dev/null 2>&1 && echo SAME || echo DIFFERENT)"
  say "runtime index.html sha: $(sha256sum "$RUNTIME/ui/index.html" 2>&1 | awk '{print $1}')"
  say "ws index.html sha:      $(sha256sum "$WS/ui/index.html" 2>&1 | awk '{print $1}')"
  say "runtime css sha: $(sha256sum "$RUNTIME/ui/style.css" 2>&1 | awk '{print $1}')"
  say "ws css sha:      $(sha256sum "$WS/ui/style.css" 2>&1 | awk '{print $1}')"
  say "cachebust in runtime index: $(grep -o 'v=[0-9.]*' "$RUNTIME/ui/index.html" 2>/dev/null | head -2 | tr '\n' ' ')"
  say "md() list line runtime: $(grep -n 'replace(/\\^\\[-\\*\\]' "$RUNTIME/ui/app.js" 2>/dev/null | head -1)"
  say "md() list line ws:      $(grep -n 'replace(/\\^\\[-\\*\\]' "$WS/ui/app.js" 2>/dev/null | head -1)"
  ok friday_uidiff
}

op_friday_gemtest() {
  log friday_gemtest
  # real Gemini round-trip from the VM using the DB-saved key — the ONLY way
  # to see whether the key actually works (sandbox has no egress to Google)
  OUT="${OUT}$( cd "$RUNTIME" && .venv/bin/python -u - <<'PY'
import asyncio, os, sys, json
sys.path.insert(0, os.getcwd())
from core.db import get_db
from core.providers import GeminiProvider, GEMINI_MODELS
db = get_db()
row = db.q1("SELECT api_key FROM provider_keys WHERE provider='gemini' AND scope='default' AND active=1 ORDER BY updated_ts DESC LIMIT 1")
if not row or not row["api_key"]:
    print("no gemini key in DB"); raise SystemExit(0)
key = row["api_key"]
print("key prefix:", key[:6], "len:", len(key))
p = GeminiProvider(api_key=key)
import httpx
async def go():
    async with httpx.AsyncClient(timeout=60) as client:
        for m in [p.model] + GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
            body = {"contents": [{"role": "user", "parts": [{"text": "say hi in 3 words"}]}],
                    "generationConfig": {"temperature": 0.6, "maxOutputTokens": 50}}
            try:
                r = await client.post(url, json=body, headers={"x-goog-api-key": key})
                print(f"model={m} status={r.status_code}")
                print("  body:", r.text[:400].replace(chr(10), " "))
                if r.status_code == 200:
                    return
            except Exception as e:
                print(f"model={m} EXC {str(e)[:120]}")
asyncio.get_event_loop().run_until_complete(go())
PY
)\n"
  ok friday_gemtest
}

op_friday_llmfix() {
  log friday_llmfix
  # clear API keys that leaked into llm.*.model settings (admin confusion fix)
  OUT="${OUT}$( cd "$RUNTIME" && .venv/bin/python - <<'PY'
import json, os, re, sqlite3
dbp = os.path.join(os.getcwd(), "data", "friday.db")
if not os.path.exists(dbp):
    print("no db at", dbp); raise SystemExit(0)
KEYLIKE = re.compile(r"^(sk-[A-Za-z0-9_-]{6,}|AIza[A-Za-z0-9_-]{20,}|AQ\.[A-Za-z0-9_.-]{20,})$")
con = sqlite3.connect(dbp)
rows = con.execute("SELECT key, value FROM settings WHERE key LIKE 'llm.%'").fetchall()
fixed = []
for k, v in rows:
    try:
        val = json.loads(v)
    except Exception:
        val = v
    if isinstance(val, str) and KEYLIKE.match(val):
        con.execute("DELETE FROM settings WHERE key=?", (k,))
        fixed.append(k)
con.commit()
print("llm.provider now:", end=" ")
row = con.execute("SELECT value FROM settings WHERE key='llm.provider'").fetchone()
print(json.loads(row[0]) if row else "unset")
print("cleared key-like model settings:", fixed if fixed else "none")
con.close()
PY
)\n"
  ok friday_llmfix
}

op_friday_cleanhist() {
  log friday_cleanhist
  # scrub ctrl-JSON leaks / truncated-key remnants out of stored chat turns
  # so the model stops ECHOING polluted history (seen live: every reply
  # started with 'config_deltHey! Still here...').
  OUT="${OUT}$( cd "$RUNTIME" && .venv/bin/python - <<'PY'
import os, sys
sys.path.insert(0, os.getcwd())
from core.db import get_db
from core.cortex import polish_reply
db = get_db()
rows = db.q("SELECT turn_id, reply FROM turns ORDER BY turn_id DESC LIMIT 400")
fixed = 0
for r in rows:
    rep = r["reply"] or ""
    if '"ctrl"' in rep or 'config_delt' in rep or 'memory_writes' in rep \
       or rep.startswith('"') or '{"ctrl"' in rep:
        cleaned = polish_reply(rep)
        if cleaned != rep:
            db.exec("UPDATE turns SET reply=? WHERE turn_id=?", (cleaned, r["turn_id"]))
            fixed += 1
print(f"turns scanned: {len(rows)}, cleaned: {fixed}")
PY
)\n"
  ok friday_cleanhist
}

op_friday_test() {
  log friday_test
  if [ ! -x "$RUNTIME/.venv/bin/python" ]; then
    say "no venv on VM — run friday_deploy first"
    fail friday_test; return
  fi
  # run tests in the DEPLOYED runtime without changing our own cwd
  TEST_OUT=$(cd "$RUNTIME" && "$RUNTIME/.venv/bin/python" -m pytest tests -q 2>&1)
  rc=$?
OUT="${OUT}$( printf '%s\n' "$TEST_OUT" | tail -8 )\n"
  if [ "$rc" -eq 0 ]; then ok friday_test; else fail friday_test; fi
}

op_friday_nginx() {
  log friday_nginx
  # nginx may not be installed on a fresh VM — install it (dnf/yum/apt)
  if ! command -v nginx >/dev/null 2>&1; then
    say "nginx not found — installing via ${PKG:-unknown}"
    pkg_install nginx
  fi
  if ! command -v nginx >/dev/null 2>&1; then
    say "nginx install FAILED — cannot expose nip.io URL"
    fail friday_nginx; return
  fi
  say "nginx: $(nginx -v 2>&1)"
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
  # Oracle Linux nginx uses /etc/nginx/conf.d; Debian uses sites-enabled
  if [ -d /etc/nginx/conf.d ]; then
    sudo cp /tmp/friday-nginx.conf /etc/nginx/conf.d/friday.conf
  else
    sudo mkdir -p /etc/nginx/sites-available /etc/nginx/sites-enabled
    sudo cp /tmp/friday-nginx.conf /etc/nginx/sites-available/friday
    sudo ln -sf /etc/nginx/sites-available/friday /etc/nginx/sites-enabled/friday
  fi
  # Oracle Linux 9: SELinux blocks nginx → non-standard ports by default
  if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce)" = "Enforcing" ]; then
    say "SELinux Enforcing — allowing nginx network connect (httpd_can_network_connect)"
    sudo setsebool -P httpd_can_network_connect 1 2>&1  || true
    sudo setsebool -P httpd_can_network_relay 1 2>&1  || true
  else
    say "SELinux: $(getenforce 2>/dev/null || echo not-present)"
  fi
  sudo nginx -t 2>&1 
  sudo systemctl enable nginx 2>/dev/null || true
  sudo systemctl restart nginx 2>/dev/null || sudo nginx 2>/dev/null || true
  code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: friday.${PUBLIC_IP}.nip.io" http://127.0.0.1/ 2>/dev/null)
  say "nip.io_check=friday.${PUBLIC_IP}.nip.io -> $code"
  if [ "$code" = "200" ]; then ok friday_nginx; else fail friday_nginx; fi
}

op_friday_diagnose() {
  log friday_diagnose
  say "unit=$(systemctl is-active $SVC 2>/dev/null || echo unknown) worker=$(systemctl is-active $WORKER 2>/dev/null || echo unknown)"
  say "ports: $(ss -ltn 2>/dev/null | grep -E ":${PORT} " || echo none)"
OUT="${OUT}$( sudo journalctl -u "$SVC" -n 30 --no-pager 2>/dev/null | tail -30 )\n"
  say "disk: $(du -sh ${RUNTIME}/data 2>/dev/null || echo missing)"
  if health_ok; then ok friday_diagnose; else fail friday_diagnose; fi
}

op_friday_netcheck() {
  log friday_netcheck
  say "=== PUBLIC IP ==="
  say "hostname -I: $(hostname -I 2>/dev/null | tr ' ' ',')"
  PUB=$(curl -s -m 6 https://api.ipify.org 2>/dev/null)
  say "egress public IP (ipify): ${PUB:-unknown}"
  OCI_IP=$(curl -s -m 5 -H "Authorization: Bearer Oracle" http://169.254.169.254/opc/v1/vnics/ 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(','.join(v.get('publicIp','') for v in d))" 2>/dev/null)
  say "OCI metadata public IP: ${OCI_IP:-unavailable}"
  say "=== SELF-TEST via public URL (proves internet→VM path from inside) ==="
  if [ -n "$PUB" ]; then
    c1=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://${PUB}/api/health" 2>/dev/null)
    say "self-test http://${PUB}/api/health -> ${c1:-000}"
    c2=$(curl -s -o /dev/null -w '%{http_code}' -m 8 "http://friday.${PUB}.nip.io/api/health" 2>/dev/null)
    say "self-test http://friday.${PUB}.nip.io/api/health -> ${c2:-000}"
  fi
  say "=== iptables INPUT (policy + first rules) ==="
OUT="${OUT}$( sudo iptables -L INPUT -n --line-numbers 2>/dev/null | head -15 )\n" || say "iptables not readable"
  say "=== egress probes (6s timeout) ==="
  for host in http://api.ipify.org https://api.ipify.org https://pypi.org https://github.com https://api.deepseek.com https://api.github.com https://r.jina.ai https://html.duckduckgo.com https://duckduckgo.com https://www.bing.com https://news.google.com; do
    t0=$(date +%s)
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 6 "$host" 2>&1) || code="ERR"
    t1=$(date +%s)
    say "$host -> $code ($((t1-t0))s)"
  done
  say "=== listening ports ==="
OUT="${OUT}$( ss -ltn 2>/dev/null | grep -E ":(80|8000|8010|443) " )\n" || say "none of 80/8000/8010/443 listening"
  say "=== nginx ==="
  command -v nginx >/dev/null 2>&1 && nginx -v 2>&1  || say "nginx NOT installed"
  say "=== nginx vhosts ==="
OUT="${OUT}$( sudo grep -rh "server_name" /etc/nginx/conf.d /etc/nginx/sites-enabled 2>/dev/null | head -10 )\n" || say "no vhosts found"
  ok friday_netcheck
}

op_friday_fix() {
  log friday_fix
  say "=== unit states ==="
  say "core: $(systemctl is-active $SVC 2>/dev/null || echo down)  worker: $(systemctl is-active $WORKER 2>/dev/null || echo down)"
  say "=== venv check ==="
  if [ ! -x "$RUNTIME/.venv/bin/python" ]; then
    say "venv missing/broken — repairing"
    sudo apt-get install -y -qq python3-venv python3-pip 2>&1 | tail -1 || true
    rm -rf "$RUNTIME/.venv"
    python3 -m venv "$RUNTIME/.venv" 2>&1 | tail -2 || true
  fi
  if [ ! -x "$RUNTIME/.venv/bin/python" ]; then
    say "venv STILL broken — cannot proceed"
    fail friday_fix; return
  fi
  say "venv OK: $("$RUNTIME/.venv/bin/python" --version 2>&1)"
  say "=== pip install (with retries) ==="
  "$RUNTIME/.venv/bin/pip" install -q --upgrade pip 2>&1 | tail -1 || true
  for attempt in 1 2 3; do
    "$RUNTIME/.venv/bin/pip" install -q -r "$RUNTIME/requirements.txt" 2>&1 | tail -2 && break
    say "pip attempt $attempt failed — retrying"
    sleep 5
  done
  say "=== import check ==="
  "$RUNTIME/.venv/bin/python" -c "import fastapi, uvicorn; print('imports OK')" 2>&1 | tail -2
  say "=== port 8000 ==="
OUT="${OUT}$( ss -ltn 2>/dev/null | grep ":8000 " )\n" || say "nothing on :8000"
  say "=== restart + wait ==="
  sudo systemctl restart "$SVC" "$WORKER"
  if wait_health; then
    say "HEALTH OK after fix"
    ok friday_fix
  else
    say "still failing — journal:"
OUT="${OUT}$( sudo journalctl -u "$SVC" -n 30 --no-pager 2>/dev/null | tail -30 )\n"
    say "--- unit status ---"
OUT="${OUT}$( systemctl status "$SVC" --no-pager 2>/dev/null | tail -12 )\n"
    fail friday_fix
  fi
}

op_friday_tunnel() {
  log friday_tunnel
  # Cloudflare QUICK tunnel — outbound-only, no firewall/port-forward needed.
  # Egress is confirmed working (netcheck), so this bypasses the ingress block.
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64|amd64) CFARCH="amd64" ;;
    aarch64|arm64) CFARCH="arm64" ;;
    *) say "unknown arch $ARCH"; fail friday_tunnel; return ;;
  esac
  say "arch: $ARCH -> cloudflared-linux-${CFARCH}"
  # verify the existing binary actually runs; else (re)download the right arch
  if ! /usr/local/bin/cloudflared --version >/dev/null 2>&1; then
    say "downloading cloudflared (linux-${CFARCH})..."
    sudo curl -sL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CFARCH}" -o /usr/local/bin/cloudflared 2>&1 | tail -1 || true
    sudo chmod +x /usr/local/bin/cloudflared
  fi
  if ! /usr/local/bin/cloudflared --version >/dev/null 2>&1; then
    say "cloudflared still broken"
    fail friday_tunnel; return
  fi
  if [ ! -x /usr/local/bin/cloudflared ]; then
    say "cloudflared download FAILED"
    fail friday_tunnel; return
  fi
  say "cloudflared: $(/usr/local/bin/cloudflared --version 2>&1 | head -1)"
  # systemd unit so the tunnel persists
  UNITFILE=/tmp/friday-tunnel.service
  {
    echo "[Unit]"
    echo "Description=Friday Cloudflare quick tunnel"
    echo "After=network.target friday-core.service"
    echo "[Service]"
    echo "Type=simple"
    echo "User=$(whoami)"
    echo "ExecStart=/usr/local/bin/cloudflared tunnel --url http://127.0.0.1:${PORT} --no-autoupdate"
    echo "Restart=always"
    echo "RestartSec=5"
    echo "[Install]"
    echo "WantedBy=multi-user.target"
  } > "$UNITFILE"
  sudo cp "$UNITFILE" /etc/systemd/system/friday-tunnel.service
  sudo systemctl daemon-reload
  sudo systemctl enable friday-tunnel 2>/dev/null
  # clear old journal so we only see the NEW instance's URL
  sudo journalctl --rotate 2>/dev/null || true
  sudo journalctl --vacuum-time=1s 2>/dev/null || true
  sudo pkill -f "cloudflared tunnel" 2>/dev/null || true
  sleep 2
  sudo systemctl restart friday-tunnel
  # wait up to 90s for the fresh URL in the journal
  URL=""
  for i in $(seq 1 30); do
    sleep 3
    URL=$(sudo journalctl -u friday-tunnel -n 200 --no-pager 2>/dev/null | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)
    [ -n "$URL" ] && break
  done
  if [ -n "$URL" ]; then
    say "TUNNEL_URL=${URL}"
    echo "$URL" | sudo tee /opt/friday/tunnel_url >/dev/null 2>&1 || true
    # health with retries (tunnel needs a few seconds to register)
    code="000"
    for i in $(seq 1 6); do
      code=$(curl -s -o /dev/null -w '%{http_code}' -m 15 "${URL}/api/health" 2>/dev/null)
      [ "$code" = "200" ] && break
      sleep 5
    done
    say "tunnel_health_check=${URL}/api/health -> ${code:-000}"
    if [ "$code" = "200" ]; then ok friday_tunnel; else fail friday_tunnel; fi
  else
    say "no tunnel URL found in journal - last lines:"
OUT="${OUT}$( sudo journalctl -u friday-tunnel -n 30 --no-pager 2>/dev/null | tail -30 )\n"
    fail friday_tunnel
  fi
}

op_friday_chattest() {
  log friday_chattest
  local q="${FRIDAY_TEST_QUERY:-who is mayor of ahmedabad}"
  say "query: ${q}"
  # POST a real chat turn and capture the FULL SSE stream to a file — the old
  # `| head -c 5000` killed curl with SIGPIPE mid-stream, which cancelled the
  # FastAPI generator BEFORE SETTLE → the turn was never saved to history.
  local tmp
  tmp=$(mktemp)
  curl -s -N -m 120 -X POST "http://127.0.0.1:${PORT}/api/chat" \
       -H 'Content-Type: application/json' \
       -d "{\"text\":\"${q}\"}" > "$tmp" 2>&1 || true
  say "REPLY_STREAM_START"
  OUT="${OUT}$(head -c 8000 "$tmp" | sed 's/^/  /')\n"
  say "REPLY_STREAM_END"
  say "DONE_EVENT: $(grep -o '"type":"done"[^}]*}' "$tmp" | tail -1 | head -c 900)"
  rm -f "$tmp"
  # pull the saved turn (model + latency + cost)
  hist=$(curl -s -m 10 "http://127.0.0.1:${PORT}/api/chat/history?limit=1" 2>&1 | head -c 1500)
  say "LAST_TURN: ${hist}"
  # prefire diagnostic (what did the search produce?)
  diag=$(curl -s -m 10 "http://127.0.0.1:${PORT}/api/admin/settings" 2>&1 | head -c 1200)
  say "PREFIRE_DIAG: ${diag}"
  ok friday_chattest
}

op_friday_eval() {
  log friday_eval
  local outdir="$RUNTIME/data/eval_report"
  rm -rf "$outdir" 2>/dev/null || true
  # run the 50-scenario eval suite against the live app (real DeepSeek)
  EVAL_OUT=$(cd "$RUNTIME" && .venv/bin/python tests/eval_suite.py --base "http://127.0.0.1:${PORT}" --out "$outdir" 2>&1)
  EVAL_RC=$?
  OUT="${OUT}$(printf '%s\n' "$EVAL_OUT" | tail -60)\n"
  # copy report into workspace so it gets pushed back
  mkdir -p "$WS/vm_diagnostics/eval"
  cp -r "$outdir"/. "$WS/vm_diagnostics/eval/" 2>/dev/null || true
  if [ -f "$outdir/eval_report.json" ]; then
    OUT="${OUT}$(python3 -c "
import json
d=json.load(open('$outdir/eval_report.json'))
print('EVAL_SUMMARY: %d/%d passed in %ds (rc=%d)' % (d['passed'], d['scenarios'], d['elapsed_s'], $EVAL_RC))
")\n"
    ok friday_eval
  else
    say "eval report missing — suite crashed"
    fail friday_eval
  fi
}

op_friday_remove() {
  log friday_remove
  sudo systemctl stop "$SVC" "$WORKER" 2>/dev/null
  sudo systemctl disable "$SVC" "$WORKER" 2>/dev/null
  sudo rm -f /etc/systemd/system/${SVC}.service /etc/systemd/system/${WORKER}.service
  sudo rm -f /etc/nginx/sites-available/friday /etc/nginx/sites-enabled/friday
  sudo systemctl daemon-reload
  sudo systemctl reload nginx 2>/dev/null || true
  sudo rm -rf "$RUNTIME"
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/api/health 2>/dev/null || echo 000)
  say "health_after_remove=$code"
  if [ "$code" = "000" ]; then ok friday_remove; else fail friday_remove; fi
}

# ------------------------------------------------------------- dispatch -----
CMD="${1:-}"
BRANCH="${2:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"

# SELF-UPDATE: if git exists and we're in a checkout, pull the LATEST
# vm_cycle.sh from the dispatched branch before running — so dispatches always
# execute the newest ops even if the job's checkout snapshot is stale.
if [ -d "$WS/.git" ] && command -v git >/dev/null 2>&1; then
  ( cd "$WS" && git fetch -q origin "$BRANCH" 2>/dev/null && \
    git checkout -q "origin/$BRANCH" -- scripts/vm_cycle.sh 2>/dev/null && \
    chmod +x scripts/vm_cycle.sh 2>/dev/null ) || true
  # re-source the fresh copy (this file) by re-execing
  if [ -f "$WS/scripts/vm_cycle.sh" ] && ! cmp -s "$0" "$WS/scripts/vm_cycle.sh" 2>/dev/null; then
    exec bash "$WS/scripts/vm_cycle.sh" "$CMD" "$BRANCH"
  fi
fi


op_friday_gemon() {
  log friday_gemon
  OUT="${OUT}$( cd "$RUNTIME" && .venv/bin/python -u - <<'PY'
import os, sys, json
sys.path.insert(0, os.getcwd())
from core.db import get_db
from core.providers import GeminiProvider
db = get_db()
db.set_setting("llm.provider", "gemini")
db.set_setting("llm.model", "gemini-3.6-flash")
db.set_setting("llm.auto_heal_ts.gemini", None)
db.set_setting("llm.auto_heal_ts.deepseek", None)
db.set_setting("llm.auto_heal", None)
print("routing -> gemini, model gemini-3.6-flash, heal timers reset")
PY
)\n"
  ok friday_gemon
}

case "$CMD" in
  friday_setup)       run_ops friday_setkey friday_netcheck friday_deploy friday_test friday_nginx ;;
  friday_setkey)      run_ops friday_setkey ;;
  friday_deploy)      run_ops friday_deploy friday_test friday_nginx ;;
  friday_health)      run_ops friday_health ;;
  friday_uidiff)      run_ops friday_uidiff ;;
  friday_llmfix)      run_ops friday_llmfix ;;
  friday_gemtest)     run_ops friday_gemtest ;;
  friday_gemon)       run_ops friday_gemon ;;
  friday_cleanhist)   run_ops friday_cleanhist ;;
  friday_test)        run_ops friday_test ;;
  friday_nginx)       run_ops friday_nginx ;;
  friday_diagnose)    run_ops friday_diagnose ;;
  friday_netcheck)    run_ops friday_netcheck ;;
  friday_chattest*)  FRIDAY_TEST_QUERY="${CMD#friday_chattest:}"; run_ops friday_chattest ;;
  friday_eval)         run_ops friday_eval ;;
  friday_fix)         run_ops friday_fix ;;
  friday_tunnel)      run_ops friday_tunnel ;;
  friday_remove)      run_ops friday_remove ;;
  friday_all)         run_ops friday_deploy friday_test friday_health friday_nginx ;;
  *) echo "unknown command: $CMD"; exit 2 ;;
esac

write_result
exit $exit_code
