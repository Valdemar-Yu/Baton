#!/usr/bin/env python3
"""Remote-side `codex` shim. It forwards Baton executor calls to local relay."""
from __future__ import annotations
import json, os, pathlib, sys, urllib.request

def main():
    args=sys.argv[1:]; out=None; schema=None; cwd=os.getcwd()
    for i,a in enumerate(args):
        if a in ("-o","--output") and i+1<len(args): out=args[i+1]
        if a in ("-C","--cd") and i+1<len(args): cwd=args[i+1]
        if a=="--output-schema" and i+1<len(args): schema=args[i+1]
    prompt=sys.stdin.read()
    payload={"prompt":prompt,"remote_workspace":cwd,"task_id":os.environ.get("RSIBENCH_TASK_ID","unknown"),"cell":os.environ.get("RSIBENCH_CELL","baton")}
    if schema and pathlib.Path(schema).exists():
        try: payload["schema"]=json.loads(pathlib.Path(schema).read_text())
        except Exception: pass
    req=urllib.request.Request(os.environ["LOCAL_CODEX_RELAY_URL"]+"/exec", data=json.dumps(payload).encode(), headers={"Content-Type":"application/json","Authorization":"Bearer "+os.environ["LOCAL_CODEX_RELAY_TOKEN"]})
    try:
        with urllib.request.urlopen(req, timeout=int(os.environ.get("LOCAL_CODEX_TIMEOUT","3600"))) as r: result=json.loads(r.read())
    except Exception as e:
        print(json.dumps({"type":"error","error":{"message":str(e)}})); return 2
    if out:
        val=result.get("last_json")
        pathlib.Path(out).write_text(json.dumps(val if isinstance(val,dict) else {"status":"blocked","summary":result.get("stderr",""),"artifact":"","change_size":"none","memory_used":[]})+"\n")
    tid="local-relay-"+str(result.get("run_id","unknown")); print(json.dumps({"type":"thread.started","thread_id":tid})); print(json.dumps({"type":"turn.completed","usage":result.get("usage",{})})); return int(result.get("returncode",1))
if __name__ == "__main__": raise SystemExit(main())
