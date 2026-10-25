#!/usr/bin/env python3
"""Run one RSIBench-compatible task with local Codex and a remote 181 sandbox.

The model/control plane stays on the local machine. 181 only builds/runs the
Docker task and executes the verifier. No credentials are copied to 181.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CODEX = os.environ.get("CODEX_BIN", str(Path.home() / ".local/bin/codex"))
BATON = ROOT / "skills/baton/bin/baton"
REMOTE = os.environ.get("RSIBENCH_REMOTE", "181")
REMOTE_ROOT = os.environ.get("RSIBENCH_REMOTE_ROOT", "/home/yuzz/baton-rsibench-20261025")
REMOTE_SNAPSHOT = f"{REMOTE_ROOT}/compatible-snapshot"
REGISTRY = ROOT / ".rsibench-local-registry.json"
LOCAL_RUNS = ROOT / ".rsibench-local-runs"


def run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 60, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, input=input_text, text=True, capture_output=True, timeout=timeout)


def ssh(script: str, *, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", REMOTE, f"bash -lc {shlex.quote(script)}"], timeout=timeout)
    if check and p.returncode:
        raise RuntimeError(f"remote command failed ({p.returncode}): {(p.stdout + p.stderr)[-3000:]}")
    return p


def load_registry() -> dict:
    if REGISTRY.exists():
        return json.loads(REGISTRY.read_text())
    # The public registry is already synced to 181; pull only this small file.
    p = ssh(f"cat {shlex.quote(REMOTE_SNAPSHOT + '/rsibench-task-registry.json')}")
    data = json.loads(p.stdout)
    REGISTRY.write_text(json.dumps(data, indent=2) + "\n")
    return data


def executable_task(task: dict) -> tuple[str, str] | None:
    src, slug = task.get("source"), task.get("slug", "")
    if src == "Terminal-Bench 2.1":
        rel = f"terminal-bench-2-1/{slug.removeprefix('terminal-bench-')}"
    elif src == "FrontierBench":
        rel = f"frontier-bench/{slug}"
    else:
        return None
    return rel, f"{REMOTE_SNAPSHOT}/{rel}"


def task_by_id(task_id: str) -> dict:
    for task in load_registry().get("tasks", []):
        if task.get("id") == task_id:
            return task
    raise ValueError(f"unknown task: {task_id}")


def classify_model_failure(text: str) -> str:
    low = text.lower()
    if any(x in low for x in ("capacity", "rate limit", "429", "quota", "overloaded")):
        return "capacity"
    return "model_failure"


def prompt_for(task: dict, cell: str, local_workspace: Path) -> str:
    detail = task.get("detail") or task.get("description") or ""
    return f"""You are the {cell} cell in a paired RSIBench-compatible evaluation.

Task ID: {task['id']}
Work only inside this local workspace: {local_workspace}

Task instruction:
{detail}

Implement the task completely. Inspect the existing files first, make the smallest
correct change, and run any available local checks. Do not modify benchmark tests,
verifier files, or files outside the workspace. Finish with a concise report of
what changed and what checks passed or failed. This is a fixed-budget evaluation;
do not ask for clarification."""


def remote_prepare(task: dict, run_id: str) -> dict:
    found = executable_task(task)
    if not found:
        raise RuntimeError("task has no executable public snapshot")
    rel, task_root = found
    image = "rsibench-local-" + task["id"].replace("/", "-").replace("_", "-")[:90]
    base = f"{REMOTE_ROOT}/runs/local-codex/{run_id}"
    work = f"{base}/workspace"
    logs = f"{base}/logs"
    container = "rsibench-local-" + uuid.uuid4().hex[:12]
    script = f"""
set -euo pipefail
mkdir -p {shlex.quote(work)} {shlex.quote(logs)}
docker build -t {shlex.quote(image)} {shlex.quote(task_root + '/environment')}
seed=$(docker create {shlex.quote(image)} sleep infinity)
docker cp "$seed:/app/." {shlex.quote(work)}/
docker rm "$seed" >/dev/null
docker run -d --name {shlex.quote(container)} \\
  -v {shlex.quote(work)}:/app \\
  -v {shlex.quote(task_root + '/tests')}:/tests:ro \\
  -v {shlex.quote(logs)}:/logs \\
  {shlex.quote(image)} sleep infinity
printf '%s\\n' {shlex.quote(json.dumps({"run_id": run_id, "container": container, "remote_work": work, "remote_logs": logs, "task_root": task_root, "task_rel": rel}))}
"""
    p = ssh(script, timeout=1200)
    meta = json.loads(p.stdout.strip().splitlines()[-1])
    return meta


def remote_sync(local_workspace: Path, remote_work: str) -> None:
    # The workspace is a dedicated run directory; .git/.baton are control-plane state.
    p = run([
        "rsync", "-avc", "--exclude=.git/", "--exclude=.baton/", "--exclude=baton.json",
        f"{local_workspace}/", f"{REMOTE}:{remote_work}/",
    ], timeout=300)
    if p.returncode:
        raise RuntimeError(f"rsync to 181 failed: {(p.stdout + p.stderr)[-3000:]}")


def remote_verify(meta: dict) -> tuple[int, str]:
    cmd = f"docker exec {shlex.quote(meta['container'])} bash /tests/test.sh"
    p = ssh(cmd, timeout=1200, check=False)
    return p.returncode, (p.stdout + p.stderr)[-8000:]


def remote_cleanup(meta: dict) -> None:
    ssh(f"docker rm -f {shlex.quote(meta['container'])} >/dev/null 2>&1 || true", timeout=60, check=False)


def run_codex(task: dict, cell: str, workspace: Path, output: Path) -> tuple[int, str]:
    prompt = prompt_for(task, cell, workspace)
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        CODEX, "exec", "--json", "--ephemeral", "--skip-git-repo-check",
        "-s", "workspace-write", "-C", str(workspace), "-m", os.environ.get("RSIBENCH_CODEX_MODEL", "gpt-6.1-sol"),
        "-c", f'model_reasoning_effort="{os.environ.get("RSIBENCH_CODEX_EFFORT", "ultra")}"',
        "-o", str(output), "-",
    ]
    p = run(cmd, cwd=workspace, timeout=3600, input_text=prompt)
    (output.with_suffix(".stdout")).write_text(p.stdout)
    (output.with_suffix(".stderr")).write_text(p.stderr)
    return p.returncode, (p.stdout + p.stderr)[-8000:]


def run_baton(task: dict, workspace: Path, output: Path) -> tuple[int, str]:
    # Baton runs locally with its Codex backend.  The final workspace is synced to
    # 181 before the remote verifier runs.
    (workspace / "rsibench_instruction.md").write_text(prompt_for(task, "baton", workspace) + "\n")
    (workspace / "baton.json").write_text(json.dumps({
        "executor": {
            "backend": "codex", "model": os.environ.get("RSIBENCH_CODEX_MODEL", "gpt-6.1-sol"),
            "reasoning_effort": os.environ.get("RSIBENCH_CODEX_EFFORT", "ultra"),
            "service_tier": "priority", "sandbox": "workspace-write", "network_access": False,
            "escalation": {"enabled": False},
        },
        "mode": "lean",
        "supervision": {"test_command": "true"},
    }, indent=2) + "\n")
    run(["git", "init", "-q"], cwd=workspace)
    run(["git", "config", "user.email", "rsibench-local@localhost"], cwd=workspace)
    run(["git", "config", "user.name", "RSIBench Local"], cwd=workspace)
    run(["git", "add", "-A"], cwd=workspace)
    run(["git", "commit", "-qm", "baseline"], cwd=workspace)
    p = run([str(BATON), "--root", str(workspace), "init", "--no-statusline", "--fresh"], cwd=workspace, timeout=120)
    if p.returncode:
        output.write_text(p.stdout + p.stderr); return p.returncode, (p.stdout + p.stderr)[-8000:]
    p = run([str(BATON), "--root", str(workspace), "new", task["id"], "--brief", str(workspace / "rsibench_instruction.md")], cwd=workspace, timeout=120)
    if p.returncode:
        output.write_text(p.stdout + p.stderr); return p.returncode, (p.stdout + p.stderr)[-8000:]
    p = run([str(BATON), "--root", str(workspace), "start", task["id"]], cwd=workspace, timeout=120)
    if p.returncode:
        output.write_text(p.stdout + p.stderr); return p.returncode, (p.stdout + p.stderr)[-8000:]
    p = run([str(BATON), "--root", str(workspace), "wait", task["id"]], cwd=workspace, timeout=3900)
    output.write_text(p.stdout + p.stderr)
    return p.returncode, (p.stdout + p.stderr)[-8000:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--cell", choices=["a0", "baton"], required=True)
    ap.add_argument("--execute", action="store_true", help="start local Codex/Baton and a remote Docker task")
    ap.add_argument("--output", type=Path)
    args = ap.parse_args()
    task = task_by_id(args.task_id)
    found = executable_task(task)
    if not found:
        print(json.dumps({"status": "missing_snapshot", "task_id": args.task_id}, indent=2)); return 2
    if not args.execute:
        print(json.dumps({"status": "dry_run", "remote": REMOTE, "cell": args.cell, "task_id": args.task_id,
                          "local_codex": CODEX, "remote_snapshot": found[1]}, indent=2)); return 0
    run_id = f"{args.task_id}-{args.cell}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:6]}"
    local_workspace = LOCAL_RUNS / run_id / "workspace"
    local_workspace.mkdir(parents=True, exist_ok=True)
    output = args.output or (LOCAL_RUNS / run_id / "codex.json")
    meta = None
    result = {"run_id": run_id, "task_id": args.task_id, "cell": args.cell, "started_at": time.time(), "remote": REMOTE}
    try:
        meta = remote_prepare(task, run_id)
        result["remote"] = meta
        # Pull the seed /app snapshot into the local control-plane workspace.
        p = run(["rsync", "-avc", f"{REMOTE}:{meta['remote_work']}/", f"{local_workspace}/"], timeout=300)
        if p.returncode: raise RuntimeError(f"seed rsync failed: {(p.stdout + p.stderr)[-2000:]}")
        if args.cell == "a0":
            rc, tail = run_codex(task, args.cell, local_workspace, output)
        else:
            rc, tail = run_baton(task, local_workspace, output)
        result["model_returncode"] = rc
        if rc:
            result["status"] = classify_model_failure(tail)
            result["tail"] = tail
            return 3
        remote_sync(local_workspace, meta["remote_work"])
        vrc, verifier_tail = remote_verify(meta)
        result["verifier_returncode"] = vrc
        result["verifier_tail"] = verifier_tail
        result["status"] = "ok" if vrc == 0 else "task_failure"
        return 0 if vrc == 0 else 4
    except Exception as e:
        result["status"] = "driver_failure"; result["error"] = str(e); return 5
    finally:
        result["finished_at"] = time.time()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2) + "\n")
        if meta: remote_cleanup(meta)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
