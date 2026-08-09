#!/usr/bin/env python3
"""Report vm_diagnostics/manual/latest.json to the repo via the GitHub
contents API. Used by the vm-ops workflow.

Token resolution (in order):
  1. $GITHUB_TOKEN env
  2. actions/checkout's persisted git credential (http.extraheader) —
     works without any workflow env changes.
"""
import base64
import json
import os
import subprocess
import sys
import urllib.request

REPO = "cyberpunk-71/Friday"
PATH = "vm_diagnostics/manual/latest.json"


def token_from_git() -> str | None:
    """Parse the Authorization header actions/checkout stored in git config."""
    try:
        out = subprocess.run(
            ["git", "config", "--get", "http.https://github.com/.extraheader"],
            capture_output=True, text=True, timeout=10)
        hdr = (out.stdout or "").strip()
    except Exception:
        return None
    if not hdr:
        return None
    # header looks like: AUTHORIZATION: basic <base64("x-access-token:TOKEN")>
    # or: AUTHORIZATION: bearer <TOKEN>
    scheme = None
    value = None
    for part in hdr.replace(":", " ", 1).split(None, 1):
        if scheme is None:
            scheme = part.lower()
        else:
            value = part
    if not value:
        return None
    if scheme == "basic":
        try:
            return base64.b64decode(value).decode().split(":", 1)[1]
        except Exception:
            return None
    if scheme in ("bearer", "token"):
        return value
    return None


def get_token() -> str:
    t = os.environ.get("GITHUB_TOKEN") or ""
    if t:
        return t
    return token_from_git() or ""


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else "main"
    token = get_token()
    if not token:
        print("report_result: no token (env or git extraheader)", flush=True)
        return 1
    try:
        with open("vm_diagnostics/manual/latest.json", "rb") as f:
            data = f.read()
    except FileNotFoundError:
        print("report_result: no latest.json to report", flush=True)
        return 1
    body = json.dumps({
        "message": "vm-ops: result",
        "content": base64.b64encode(data).decode(),
        "branch": branch,
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/contents/{PATH}",
        data=body, method="PUT",
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "vm-ops"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"report_result: HTTP {r.status} -> {PATH} on {branch}", flush=True)
            return 0
    except urllib.error.HTTPError as e:
        print(f"report_result: HTTP {e.code} {e.read()[:200]}", flush=True)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"report_result: {e}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
