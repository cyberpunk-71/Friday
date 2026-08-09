#!/usr/bin/env python3
"""Report vm_diagnostics/manual/latest.json to the repo via the GitHub
contents API. Used by the vm-ops workflow (v4+) — no heredocs in YAML."""
import base64
import json
import os
import sys
import urllib.request

REPO = "cyberpunk-71/Friday"
PATH = "vm_diagnostics/manual/latest.json"


def main() -> int:
    branch = sys.argv[1] if len(sys.argv) > 1 else "main"
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("report_result: no GITHUB_TOKEN", flush=True)
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
