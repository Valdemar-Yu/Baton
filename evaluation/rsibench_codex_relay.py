#!/usr/bin/env python3
"""Local Codex relay for a remote 181 RSIBench runner.

Run this on the local machine. It keeps Codex credentials local and exposes a
loopback HTTP endpoint through an SSH reverse tunnel. The remote shim sends a
prompt; local Codex uses SSH commands to edit the 181 workspace.
"""
from __future__ import annotations
import argparse, json, os, secrets, shlex, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CODEX = os.environ.get("CODEX_BIN", str(Path.home() / ".local/bin/codex"))

class Relay:
    def __init__(self, token: str, model: str, effort: str, timeout: int):
        self.token, self.model, self.effort, self.timeout = token, model, effort, timeout
        self.runs = ROOT / ".rsibench-relay-runs"; self.runs.mkdir(exist_ok=True)

    def execute(self, payload: dict) -> dict:
        prompt = str(payload.get("prompt") or "")
        remote = str(payload.get("remote_workspace") or "")
        task_id = str(payload.get("task_id") or "unknown")
        cell = str(payload.get("cell") or "a0")
        if not remote or not prompt: return {"returncode": 2, "error": "missing remote_workspace or prompt"}
        run_id = f"{task_id}-{cell}-{int(time.time())}-{secrets.token_hex(3)}"
        work = self.runs / run_id; work.mkdir(parents=True, exist_ok=True)
        schema = payload.get("schema")
        schema_path = None
        if schema:
            schema_path = work / "output-schema.json"; schema_path.write_text(json.dumps(schema))
        output = work / "last.json"
        instruction = f"""
You are the local Codex model driving a remote RSIBench task.

The actual editable workspace is on SSH host 181 at:
{remote}
Task ID: {task_id}
Evaluation cell: {cell}

Use only commands of the form `ssh -o BatchMode=yes 181 ...` to inspect and
modify the remote workspace or its Docker task. Do not solve the task by editing
local files. Do not copy credentials to 181. The remote workspace is mounted as
/app inside the task container. Finish the requested work and return the required
structured result. The original Baton instruction follows:

{prompt}
"""
        cmd = [CODEX, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
               "-s", "danger-full-access", "-C", str(work), "-m", self.model,
               "-c", f'model_reasoning_effort="{self.effort}"', "-o", str(output)]
        if schema_path: cmd += ["--output-schema", str(schema_path)]
        cmd += ["-"]
        try:
            p = subprocess.run(cmd, cwd=work, input=instruction, text=True,
                               capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            return {"returncode": 124, "error": "local_codex_timeout", "stdout": e.stdout or "", "stderr": e.stderr or ""}
        result = {"returncode": p.returncode, "stdout": p.stdout[-12000:], "stderr": p.stderr[-12000:], "run_id": run_id}
        if output.exists():
            try: result["last_json"] = json.loads(output.read_text())
            except Exception: result["last_json"] = None
        return result

class Handler(BaseHTTPRequestHandler):
    relay: Relay
    def _send(self, code: int, value: dict):
        raw = json.dumps(value).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/health": self._send(200, {"ok": True, "codex": CODEX})
        else: self._send(404, {"error": "not_found"})
    def do_POST(self):
        if self.headers.get("Authorization", "") != "Bearer " + self.relay.token: self._send(401, {"error": "unauthorized"}); return
        if self.path != "/exec": self._send(404, {"error": "not_found"}); return
        try: payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        except Exception: self._send(400, {"error": "invalid_json"}); return
        self._send(200, self.relay.execute(payload))
    def log_message(self, *_): pass

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--listen", default="127.0.0.1"); ap.add_argument("--port", type=int, default=8765); ap.add_argument("--remote", default="181"); ap.add_argument("--remote-port", type=int, default=18765); ap.add_argument("--token"); ap.add_argument("--model", default=os.environ.get("RSIBENCH_CODEX_MODEL", "gpt-6.1-sol")); ap.add_argument("--effort", default=os.environ.get("RSIBENCH_CODEX_EFFORT", "ultra")); ap.add_argument("--timeout", type=int, default=3600)
    a = ap.parse_args(); token = a.token or secrets.token_urlsafe(32)
    relay = Relay(token, a.model, a.effort, a.timeout); Handler.relay = relay
    tunnel = subprocess.Popen(["ssh", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N", "-R", f"127.0.0.1:{a.remote_port}:{a.listen}:{a.port}", a.remote], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    time.sleep(1)
    if tunnel.poll() is not None: raise SystemExit("reverse SSH tunnel failed: " + (tunnel.stderr.read() if tunnel.stderr else ""))
    print(json.dumps({"relay_url": f"http://127.0.0.1:{a.remote_port}", "token": token, "remote": a.remote, "pid": tunnel.pid}), flush=True)
    try: ThreadingHTTPServer((a.listen, a.port), Handler).serve_forever()
    finally: tunnel.terminate(); tunnel.wait(timeout=5)
if __name__ == "__main__": main()
