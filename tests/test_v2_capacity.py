from platform_fixtures import wait_windows_pid
from platform_fixtures import executable_fixture
import contextlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class CapacityRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.bindir = self.root / "bin"
        self.bindir.mkdir()
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.count_file = self.root / "count"
        self.calls_file = self.root / "calls.jsonl"
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.state),
            "PATH": str(self.bindir) + os.pathsep + self.env.get("PATH", ""),
            "BATON_DESKTOP_NOTIFY": "0",
            "BATON_CAPACITY_COUNT": str(self.count_file),
            "BATON_CAPACITY_CALLS": str(self.calls_file),
        })
        self.addCleanup(self.tmp.cleanup)

    def fake_codex(self, behaviors):
        script = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
out = args[args.index("-o") + 1]
count_path = os.environ["BATON_CAPACITY_COUNT"]
calls_path = os.environ["BATON_CAPACITY_CALLS"]
try:
    n = int(open(count_path).read() or "0")
except OSError:
    n = 0
open(count_path, "w").write(str(n + 1))
prompt = sys.stdin.read()
with open(calls_path, "a") as stream:
    stream.write(json.dumps({"resume": "resume" in args, "args": args, "prompt": prompt}, ensure_ascii=False) + "\n")
behaviors = json.loads(os.environ["BATON_CAPACITY_BEHAVIORS"])
behavior = behaviors[n] if n < len(behaviors) else behaviors[-1]
if behavior == "capacity":
    print(json.dumps({"type": "error", "message": "Selected model is at capacity. Please try a different model."}), flush=True)
    sys.exit(1)
if behavior == "compact-capacity":
    print(json.dumps({"type": "error", "message": "Error running remote compact task: Selected model is at capacity"}), flush=True)
    sys.exit(1)
if behavior == "touch-acceptance-capacity":
    with open(os.environ["BATON_CAPACITY_ACCEPTANCE"], "w") as stream:
        stream.write("changed\n")
    print(json.dumps({"type": "error", "message": "at capacity"}), flush=True)
    sys.exit(1)
if behavior == "output-capacity":
    print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": "at capacity"}}), flush=True)
    sys.exit(1)
if behavior == "failure":
    print(json.dumps({"type": "turn.failed", "error": {"message": "invalid parameter"}}), flush=True)
    sys.exit(1)
if behavior == "thread-capacity":
    print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": "at capacity"}}), flush=True)
    sys.exit(1)
print(json.dumps({"type": "thread.started", "thread_id": "fake-thread"}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}), flush=True)
json.dump({"status": "done", "summary": "fake", "artifact": "", "change_size": "minor"}, open(out, "w"))
'''
        path = self.bindir / "codex"
        path.write_text(script, encoding="utf-8")
        path = executable_fixture(path)
        self.env["BATON_CAPACITY_BEHAVIORS"] = json.dumps(behaviors)

    def cli(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(self.project_root), *args],
            cwd=str(ROOT), env=self.env, capture_output=True, text=True, timeout=timeout,
        )

    def start(self, behaviors, retry=None, supervision=None, acceptance=None):
        self.fake_codex(behaviors)
        (self.project_root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        self.assertEqual(self.cli("init", "--no-statusline").returncode, 0)
        config = {"monitor": {"enabled": False}}
        if retry:
            config["executor"] = {"capacity_retry": retry}
        if supervision:
            config["supervision"] = supervision
        if config:
            (self.project_root / "baton.json").write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.cli("new", "capacity").returncode, 0)
        brief = self.project_root / ".baton" / "tasks" / "capacity" / "brief.md"
        acceptance_paths = acceptance or []
        brief.write_text(
            '```baton-scope\n' + json.dumps({"allow": [], "deny": [], "acceptance": acceptance_paths,
                                                   "frozen": [], "verify": "none"}) +
            '\n```\n# capacity\n',
            encoding="utf-8",
        )
        if acceptance_paths:
            for path in acceptance_paths:
                target = self.project_root / path
                target.write_text("baseline\n", encoding="utf-8")
                self.env["BATON_CAPACITY_ACCEPTANCE"] = str(target)
        started = self.cli("start", "capacity")
        self.assertEqual(started.returncode, 0, started.stderr)
        self.runner_pid = int(re.search(r"runner_pid=(\d+)", started.stdout)[1])
        if os.name == "nt":
            self.addCleanup(wait_windows_pid, self.runner_pid)
        state_dir = next((self.state / "projects").iterdir())
        state_path = state_dir / "tasks" / "capacity" / "state.json"
        return state_path

    def wait_until_done(self, state_path, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = json.loads(baton.platform_support.read_text(state_path))
            if state.get("status") != "running":
                if os.name == "nt":
                    wait_windows_pid(self.runner_pid)
                return state
            time.sleep(0.05)
        self.fail("fake codex runner did not finish: " + json.dumps(state, ensure_ascii=False))

    def test_capacity_failure_two_times_then_success(self):
        path = self.start(["capacity", "capacity", "success"])
        state = self.wait_until_done(path)
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "done", leg)
        self.assertEqual(len(leg["capacity_retries"]), 2)
        self.assertNotIn("failure_kind", leg)
        self.assertEqual(int(self.count_file.read_text()), 3)

    def test_three_immediate_then_delayed_retry_succeeds(self):
        path = self.start(["capacity", "capacity", "capacity", "capacity", "success"],
                          {"immediate": 3, "delayed": 1, "delayed_minutes": 0.001})
        state = self.wait_until_done(path)
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "done", leg)
        self.assertEqual([x["phase"] for x in leg["capacity_retries"]],
                         ["immediate", "immediate", "immediate", "delayed"])
        self.assertEqual(int(self.count_file.read_text()), 5)

    def test_four_capacity_failures_end_with_capacity_event_and_kind(self):
        path = self.start(["capacity"], {"immediate": 3, "delayed": 1, "delayed_minutes": 0.001})
        state = self.wait_until_done(path)
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "failed")
        self.assertEqual(leg["failure_kind"], "capacity")
        self.assertEqual(leg["capacity_retry_count"], 4)
        self.assertNotIn("error", leg)
        out = self.cli("wait", "capacity")
        self.assertIn("BATON::EVENT FAILED", out.stdout)
        self.assertIn("容量不足，已自动重试 4 次", out.stdout)
        self.assertIn("第 4 次", out.stdout)
        self.assertIn("不要接手写代码", out.stdout)

    def test_new_output_resets_capacity_counter(self):
        path = self.start(["capacity", "output-capacity", "success"],
                          {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001})
        state = self.wait_until_done(path)
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "done", leg)
        self.assertEqual(len(leg["capacity_retries"]), 2)

    def test_non_capacity_failure_is_not_retried(self):
        path = self.start(["failure"])
        state = self.wait_until_done(path)
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "failed")
        self.assertNotIn("failure_kind", leg)
        self.assertEqual(int(self.count_file.read_text()), 1)

    def test_no_thread_retries_original_command_and_prompt(self):
        path = self.start(["capacity", "success"], {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001})
        self.assertEqual(self.wait_until_done(path)["legs"][-1]["status"], "done")
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        self.assertFalse(calls[0]["resume"])
        self.assertFalse(calls[1]["resume"])
        self.assertEqual(calls[1]["prompt"], calls[0]["prompt"])

    def test_thread_resume_uses_short_prompt_and_not_original(self):
        path = self.start(["thread-capacity", "success"], {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001})
        state = self.wait_until_done(path)
        self.assertEqual(state["legs"][-1]["status"], "done")
        calls = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        self.assertFalse(calls[0]["resume"])
        self.assertTrue(calls[1]["resume"])
        self.assertIn("上一次调用因模型容量不足中断", calls[1]["prompt"])
        self.assertNotIn("# capacity", calls[1]["prompt"])

    def test_compact_capacity_error_is_detected(self):
        path = self.start(["compact-capacity", "success"], {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001})
        state = self.wait_until_done(path)
        self.assertEqual(state["legs"][-1]["status"], "done")

    def test_freeze_violation_before_retry_ends_violation(self):
        path = self.start(["touch-acceptance-capacity", "success"],
                          {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001},
                          acceptance=["acceptance.txt"])
        state = self.wait_until_done(path)
        self.assertEqual(state["legs"][-1]["status"], "violation")
        self.assertEqual(int(self.count_file.read_text()), 1)
        self.assertNotIn("failure_kind", state["legs"][-1])

    def test_capacity_wait_does_not_count_toward_unsupervised_limit(self):
        path = self.start(["capacity", "success"],
                          {"immediate": 0, "delayed": 1, "delayed_minutes": 0.002},
                          {"max_unsupervised_minutes": 0.001, "interval_minutes": 0})
        state = self.wait_until_done(path)
        self.assertEqual(state["legs"][-1]["status"], "done")

    def test_capacity_wait_seconds_uses_wait_intervals(self):
        leg = {"started_at": 100, "capacity_wait_intervals": [
            {"started_at": 110, "ended_at": 130}, {"started_at": 150, "ended_at": 180}]}
        self.assertEqual(baton.capacity_wait_seconds(leg, at=200, since=120), 10 + 30)
        self.assertEqual(baton.capacity_wait_seconds(leg, at=200, since=160), 20)

    def test_stop_during_capacity_wait_ends_stopped(self):
        path = self.start(["capacity"], {"immediate": 0, "delayed": 1, "delayed_minutes": 0.05})
        deadline = time.time() + 10
        while time.time() < deadline:
            state = json.loads(baton.platform_support.read_text(path))
            if state["legs"][-1].get("capacity_wait"):
                break
            time.sleep(0.05)
        else:
            self.fail("runner did not enter capacity wait")
        self.assertEqual(self.cli("stop", "capacity").returncode, 0)
        state = self.wait_until_done(path)
        self.assertEqual(state["legs"][-1]["status"], "stopped")

    def test_capacity_detector_uses_only_new_offsets(self):
        events = self.root / "events.jsonl"
        stderr = self.root / "stderr"
        events.write_text('{"type":"error","message":"at capacity"}\n', encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        off = events.stat().st_size
        self.assertFalse(baton.is_capacity_failure(str(events), str(stderr), off, 0, 1, None))
        events.write_text(events.read_text() + '{"type":"turn.failed","error":{"message":"at capacity"}}\n', encoding="utf-8")
        self.assertTrue(baton.is_capacity_failure(str(events), str(stderr), off, 0, 1, None))

    def test_capacity_retry_config_validation_and_summary(self):
        project = baton.Project(str(self.project_root))
        for bad in ({"immediate": -1}, {"delayed": True}, {"delayed_minutes": 0}):
            cfg = json.loads(json.dumps(baton.read_json(str(ROOT / "skills/baton/config.json"))))
            cfg["executor"]["capacity_retry"].update(bad)
            with self.assertRaises(baton.ConfigError):
                baton.validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
