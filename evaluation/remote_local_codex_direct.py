#!/usr/bin/env python3
"""Remote-side direct A0 call through the local Codex relay."""
from __future__ import annotations
import json, os, pathlib, sys, urllib.request

def main():
    prompt=sys.stdin.read(); cwd=os.getcwd(); payload={"prompt":prompt,"remote_workspace":cwd,"task_id":os.environ.get("RSIBENCH_TASK_ID","unknown"),"cell":os.environ.get("RSIBENCH_CELL","a0")}
    req=urllib.request.Request(os.environ["LOCAL_CODEX_RELAY_URL"]+"/exec", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["LOCAL_CODEX_RELAY_TOKEN"]})
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get("LOCAL_CODEX_TIMEOUT","3600"))) as r: result=json.loads(r.read())
    except Exception as e: print(str(e),file=sys.stderr); return 2
    sys.stdout.write(result.get("stdout","") or ""); sys.stderr.write(result.get("stderr","") or ""); return int(result.get("returncode",1))
if __name__ == "__main__": raise SystemExit(main())
