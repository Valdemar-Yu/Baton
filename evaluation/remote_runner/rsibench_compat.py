#!/usr/bin/env python3
"""Minimal, auditable RSIBench-compatible runner.

The runner is deliberately independent of Harbor. It validates the public
registry/snapshot, produces a paired A0/Baton plan, and only starts a model or
Docker task when the caller passes --execute. Model authentication, capacity,
sandbox, verifier, and task outcomes are stored as separate statuses.
"""
from __future__ import annotations
import argparse, json, os, pathlib, shlex, subprocess, sys, time, uuid, urllib.request
from typing import Any

STATUS = {"ok", "missing_auth", "capacity", "sandbox", "verifier", "task_failure", "adapter_unconfigured", "missing_snapshot"}
DEFAULT_GPU_DEVICES = os.environ.get("RSIBENCH_GPU_DEVICES", "6,7")

def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def load_registry(root: pathlib.Path) -> dict[str, Any]:
    p = root / "rsibench-task-registry.json"
    if not p.exists():
        p = root / "rsibench-public" / "rsibench-task-registry-20261025.json"
    return json.loads(p.read_text())

def command(*args: str, cwd: pathlib.Path | None = None, timeout: int = 30, input_text: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(list(args), cwd=cwd, input=input_text, text=True, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired as e:
        return 124, e.stdout or "", f"timeout after {timeout}s"

def task_path(root: pathlib.Path, task: dict[str, Any]) -> pathlib.Path | None:
    src, slug = task.get("source"), task.get("slug", "")
    if src == "Terminal-Bench 2.1":
        p = root / "terminal-bench-2-1" / slug.removeprefix("terminal-bench-")
    elif src == "FrontierBench":
        p = root / "frontier-bench" / slug
    else:
        return None
    return p if p.exists() else None

def inventory(root: pathlib.Path) -> dict[str, Any]:
    reg = load_registry(root)
    rows = []
    for task in reg["tasks"]:
        p = task_path(root, task)
        if p is None:
            status = "metadata_only" if task["source"] in {"EdgeBench", "ProgramBench", "ScienceAgentBench", "SWE-rebench"} else "missing_snapshot"
            rows.append({"id": task["id"], "source": task["source"], "status": status})
            continue
        dockerfile = p / "environment" / "Dockerfile"
        verifier = next((x for base in (p / "tests", p) for x in base.glob("test.sh")), None)
        rows.append({"id": task["id"], "source": task["source"], "status": "executable_snapshot" if dockerfile.exists() and verifier else "incomplete_snapshot", "path": str(p)})
    counts: dict[str, int] = {}
    for row in rows: counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"timestamp": now(), "registry_tasks": len(rows), "status_counts": counts, "tasks": rows}

def model_preflight() -> dict[str, Any]:
    relay = os.environ.get("LOCAL_CODEX_RELAY_URL")
    if relay:
        try:
            req = urllib.request.Request(relay.rstrip("/") + "/health", headers={"Authorization": "Bearer " + os.environ.get("LOCAL_CODEX_RELAY_TOKEN", "")})
            with urllib.request.urlopen(req, timeout=10) as response: body = response.read().decode()
            return {"driver": "local_codex_relay", "relay": relay, "health": body[-500:], "auth_ok": True, "capacity": False}
        except Exception as exc:
            return {"driver": "local_codex_relay", "relay": relay, "auth_ok": False, "capacity": False, "error": str(exc)}
    default_hermes = "/home/yuzz/.hermes/hermes-agent/venv/bin/hermes" if pathlib.Path("/home/yuzz/.hermes/hermes-agent/venv/bin/hermes").exists() else "hermes"
    hermes = os.environ.get("HERMES_BIN", default_hermes)
    profile = os.environ.get("HERMES_PROFILE", "baton-bench-exec")
    provider = os.environ.get("HERMES_PROVIDER", "openai-codex")
    rc, out, err = command(hermes, "--version")
    result = {"hermes_bin": hermes, "profile": profile, "provider": provider, "hermes_version_rc": rc, "hermes_version": (out + err).strip()[-500:]}
    rc2, out2, err2 = command(hermes, "auth", "status", provider)
    auth_text = (out2 + err2).strip()
    result["auth_status"] = auth_text[-500:]
    result["auth_ok"] = rc2 == 0 and "logged out" not in auth_text.lower() and "no codex credentials" not in auth_text.lower()
    result["capacity"] = False
    return result

def make_prompt(task: dict[str, Any], cell: str) -> str:
    return (f"RSIBench compatible task {task['id']}\n\n{task.get('detail') or task.get('description','')}\n\n"
            "Work only in /app. Implement the requested change and run the supplied verifier. "
            f"Evaluation cell: {cell}. Do not modify verifier files or benchmark metadata.")

def plan(root: pathlib.Path, output: pathlib.Path, ids: list[str] | None = None) -> dict[str, Any]:
    inv = inventory(root); reg = load_registry(root); wanted = set(ids or [])
    tasks = [t for t in reg["tasks"] if not wanted or t["id"] in wanted]
    rows = []
    for t in tasks:
        r = next(x for x in inv["tasks"] if x["id"] == t["id"])
        for cell in ("a0", "baton"):
            rows.append({"task_id": t["id"], "cell": cell, "snapshot_status": r["status"], "prompt": make_prompt(t, cell), "execute": False})
    payload = {"created_at": now(), "protocol": {"train_public_only": True, "heldout_rollouts_per_task": 3, "paired_budgets": True}, "rows": rows}
    output.write_text(json.dumps(payload, indent=2) + "\n")
    return payload

def execute_one(root: pathlib.Path, task_id: str, cell: str, run_root: pathlib.Path) -> dict[str, Any]:
    reg = load_registry(root); task = next((t for t in reg["tasks"] if t["id"] == task_id), None)
    result: dict[str, Any] = {"timestamp": now(), "task_id": task_id, "cell": cell, "status": "missing_snapshot"}
    if task is None: result["error"] = "unknown_task"; return result
    p = task_path(root, task)
    if p is None or not (p / "environment" / "Dockerfile").exists(): result["error"] = "task_environment_unavailable"; return result
    if cell == "baton" and not os.environ.get("BATON_COMMAND"):
        result["status"] = "adapter_unconfigured"; result["error"] = "set BATON_COMMAND to a reviewed Baton adapter command"; return result
    pre = model_preflight(); result["preflight"] = pre
    if not pre["auth_ok"]:
        result["status"] = "missing_auth"; result["error"] = "Hermes openai-codex credentials are not active"; return result
    run_id = f"{task_id}-{cell}-{uuid.uuid4().hex[:8]}"; work = run_root / run_id; work.mkdir(parents=True, exist_ok=True)
    image = "rsibench-compat-" + task_id.replace("/", "-").replace("_", "-")[:100]
    rc, out, err = command("docker", "build", "-t", image, str(p / "environment"), timeout=900)
    if rc != 0:
        result["status"] = "sandbox"; result["error"] = (out + err)[-2000:]; return result
    cname = "rsibench-" + uuid.uuid4().hex[:12]; logs = work / "logs"; logs.mkdir()
    tests = p / "tests"; verifier = tests / "test.sh" if (tests / "test.sh").exists() else p / "run-tests.sh"
    if not verifier.exists(): result["status"] = "verifier"; result["error"] = "no test.sh/run-tests.sh"; return result
    gpu_devices = os.environ.get("RSIBENCH_GPU_DEVICES", DEFAULT_GPU_DEVICES).strip()
    gpu_flags: list[str] = []
    if gpu_devices:
        selected = [item.strip() for item in gpu_devices.split(",") if item.strip().isdigit()]
        for item in selected: gpu_flags += ["--device", f"/dev/nvidia{item}"]
        for special in ("/dev/nvidiactl", "/dev/nvidia-uvm", "/dev/nvidia-uvm-tools"):
            gpu_flags += ["--device", special]
        result["gpu_devices"] = selected
    rc, out, err = command("docker", "run", "-d", "--name", cname, *gpu_flags, "-e", f"CUDA_VISIBLE_DEVICES={gpu_devices}", "-v", f"{work}:/app", "-v", f"{tests}:/tests:ro", "-v", f"{logs}:/logs", image, "sleep", "infinity")
    if rc != 0:
        result["status"] = "sandbox"; result["error"] = (out + err)[-2000:]; return result
    try:
        prompt = make_prompt(task, cell)
        (work / "rsibench_instruction.md").write_text(prompt + "\n")
        os.environ["RSIBENCH_TASK_ID"] = task_id
        os.environ["RSIBENCH_CELL"] = cell
        relay = os.environ.get("LOCAL_CODEX_RELAY_URL")
        if cell == "a0" and relay:
            cmd = [os.environ.get("LOCAL_CODEX_DIRECT", "remote_local_codex_direct.py")]
            rc, out, err = command(*cmd, cwd=work, timeout=3600, input_text=prompt)
        elif cell == "a0":
            cmd = [os.environ.get("HERMES_BIN", "hermes"), "--cli", "--yolo", "--in", str(work), "-z", prompt]
            rc, out, err = command(*cmd, cwd=work, timeout=3600)
        else:
            cmd = shlex.split(os.environ["BATON_COMMAND"].format(workspace=shlex.quote(str(work)), task_id=shlex.quote(task_id)))
            rc, out, err = command(*cmd, cwd=work, timeout=3600)
        (work / "agent.stdout").write_text(out); (work / "agent.stderr").write_text(err)
        if rc != 0:
            result["status"] = "capacity" if "capacity" in (out + err).lower() else "task_failure"; result["error"] = (out + err)[-2000:]; return result
        rc2, out2, err2 = command("docker", "exec", cname, "bash", "/tests/test.sh", timeout=900)
        (work / "verifier.stdout").write_text(out2); (work / "verifier.stderr").write_text(err2)
        result["status"] = "ok" if rc2 == 0 else "task_failure"; result["verifier_rc"] = rc2
    finally:
        command("docker", "rm", "-f", cname, timeout=60)
    return result

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=pathlib.Path, required=True)
    ap.add_argument("--output", type=pathlib.Path, default=pathlib.Path("rsibench-run.json"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("inventory")
    p = sub.add_parser("plan"); p.add_argument("--task-id", action="append")
    e = sub.add_parser("execute-one"); e.add_argument("--task-id", required=True); e.add_argument("--cell", choices=["a0", "baton"], required=True); e.add_argument("--run-root", type=pathlib.Path, default=pathlib.Path("runs")); e.add_argument("--execute", action="store_true")
    args = ap.parse_args(); args.root = args.root.expanduser().resolve(); args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.cmd == "inventory": payload = inventory(args.root); args.output.write_text(json.dumps(payload, indent=2) + "\n")
    elif args.cmd == "plan": payload = plan(args.root, args.output, args.task_id)
    else:
        if not args.execute: print("Refusing model/Docker execution without --execute", file=sys.stderr); return 2
        args.run_root.mkdir(parents=True, exist_ok=True); payload = execute_one(args.root, args.task_id, args.cell, args.run_root); args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload if args.cmd != "inventory" else {"registry_tasks": payload["registry_tasks"], "status_counts": payload["status_counts"]}, indent=2))
    return 0
if __name__ == "__main__": raise SystemExit(main())
