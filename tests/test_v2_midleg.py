from platform_fixtures import wait_windows_pid
from platform_fixtures import executable_fixture
import argparse
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


def wait_for_pid(pid, timeout=20):
    if os.name == "nt":
        return wait_windows_pid(pid, timeout)
    if not pid:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return
        except ChildProcessError:
            pass
        if not baton.pid_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        os.waitpid(pid, 0)
    except (ChildProcessError, ProcessLookupError):
        pass
    raise AssertionError(f"后台进程 {pid} 在 {timeout} 秒内没有退出")


class MidlegTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"HOME": str(base / "home"), "USERPROFILE": str(base / "home"), "BATON_HOME": str(base / "state"),
                                           "PYTHONDONTWRITEBYTECODE": "1", "BATON_DESKTOP_NOTIFY": "0"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        self.project = baton.Project(str(self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(self.project, argparse.Namespace(no_statusline=True, accept_boundary=True,
                                                             relink=False, fresh=False))
        task_dir = self.root / ".baton" / "tasks" / "t"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": ["*"], "deny": [], "acceptance": [],
                                                 "frozen": [], "verify": "none"}) + "\n```\n# t\n",
            encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(task="t", status="idle", legs=[],
                                                           snapshot_store="shadow", scope={
                                                               "allow": ["*"], "deny": [], "acceptance": [],
                                                               "frozen": [], "verify": "none"}))

    def running_leg(self):
        store = self.project.store("t")
        base = store.snapshot("base")
        leg = {"n": 1, "status": "running", "started_at": baton.now(), "start_ref": base,
               "events": str(Path(self.project.runs_dir("t")) / "leg-001.events.jsonl"),
               "last_rule_terminal_at": baton.now(), "last_file_change_at": baton.now(),
               "alerts": [], "alert_keys": [], "probe_hashes": {}, "check_interval_minutes": 10,
               "next_check_at": baton.now() + 600}
        Path(self.project.runs_dir("t")).mkdir(parents=True, exist_ok=True)
        self.project.update_state("t", lambda s: s.update(base_ref=base, legs=[leg], status="running"))
        return store, leg

    def make_probe(self):
        store, _ = self.running_leg()
        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        commit = store.snapshot("probe")
        store.git("update-ref", "refs/baton/t/probe", commit)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            probe_commit=commit, probe_ref="refs/baton/t/probe"))
        return store

    def configure(self, **values):
        cfg = json.loads(json.dumps(self.project.config))
        for section, updates in values.items():
            cfg.setdefault(section, {}).update(updates)
        self.project.update_state("t", lambda s: s.update(config_snapshot=cfg))
        return cfg

    def run_rule_change(self, path, old, new, **config):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(old, encoding="utf-8")
        self.configure(**config) if config else None
        store, _ = self.running_leg()
        target.write_text(new, encoding="utf-8")
        state = self.project.load_state("t")
        return baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))

    def make_check(self, value):
        state = self.project.load_state("t")
        leg = baton.current_leg(state)
        if not leg.get("probe_ref"):
            self.make_probe()
            state = self.project.load_state("t")
            leg = baton.current_leg(state)
        dossier = baton.build_check_dossier(self.project, "t", state, leg)
        self.assertTrue(dossier)
        baton.apply_monitor_check_result(self.project, "t", dossier, value, leg["n"])
        return dossier

    def cli(self, root, env, *args, timeout=20):
        return subprocess.run([sys.executable, str(ROOT / "skills" / "baton" / "scripts" / "baton.py"),
                               "--root", str(root), *args], env=env, capture_output=True,
                              text=True, timeout=timeout)

    def test_test_file_matching_and_non_test_false_positives(self):
        self.assertTrue(baton._is_test_file("test_api.py"))
        self.assertTrue(baton._is_test_file("pkg/tests/api.py"))
        self.assertTrue(baton._is_test_file("pkg/api_test.py"))
        self.assertFalse(baton._is_test_file("latest.py"))
        self.assertFalse(baton._is_test_file("contest.py"))

        store, _ = self.running_leg()
        (self.root / "latest.py").write_text("def f():\n assert 1\n", encoding="utf-8")
        store.snapshot("add source")
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            probe_commit=store.snapshot("source probe"), probe_ref="refs/baton/t/probe"))
        alerts = baton.run_rule_checks(self.project, "t", self.project.load_state("t"),
                                       self.project.load_state("t")["legs"][0])
        self.assertFalse(any(item["rule"] == "test_count_decreased" for item in alerts))

        self.project.update_state("t", lambda s: s.update(legs=[]))
        test_file = self.root / "test_flags.py"
        test_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        self.running_leg()
        test_file.write_text("def test_a():\n skip_header = True\n assert 1\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertNotIn("test_disabled", {item["rule"] for item in alerts})

    def test_rules_alert_and_deduplicate(self):
        store, _ = self.running_leg()
        test_file = self.root / "test_api.py"
        test_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        commit = store.snapshot("test baseline")
        store.git("update-ref", "refs/baton/t/probe", commit)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            probe_commit=commit, probe_ref="refs/baton/t/probe"))
        test_file.write_text("def test_a():\n pytest.mark.skip\n pass\n", encoding="utf-8")
        leg = self.project.load_state("t")["legs"][0]
        first = baton.run_rule_checks(self.project, "t", self.project.load_state("t"), leg)
        self.assertEqual({item["rule"] for item in first}, {"test_count_decreased", "test_disabled"})
        second = baton.run_rule_checks(self.project, "t", self.project.load_state("t"),
                                       self.project.load_state("t")["legs"][0])
        self.assertEqual(second, [])

    def test_task_created_test_file_shrinking_is_a_record_not_breaking(self):
        old_file = self.root / "test_old.py"
        old_file.write_text("def test_a():\n assert 1\n\ndef test_b():\n assert 2\n", encoding="utf-8")
        store, _ = self.running_leg()
        new_file = self.root / "test_new.py"
        new_file.write_text("def test_a():\n assert 1\n\ndef test_b():\n assert 2\n", encoding="utf-8")
        commit = store.snapshot("probe baseline")
        store.git("update-ref", "refs/baton/t/probe", commit)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            probe_commit=commit, probe_ref="refs/baton/t/probe"))
        old_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        new_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        by_path = {item["evidence"].split(":")[0]: item for item in alerts if item["rule"] == "test_count_decreased"}
        self.assertEqual(by_path["test_old.py"]["category"], "breaking")
        self.assertEqual(by_path["test_new.py"]["category"], "record")
        self.assertIn("本任务新建", by_path["test_new.py"]["evidence"])
        store2 = self.project.store("t")
        end = store2.snapshot("end")
        self.assertEqual(baton._task_new_test_files(store2, self.project.load_state("t"), end), ["test_new.py"])

    def test_deleted_test_file_hit_and_non_test_delete_misses(self):
        test_path = self.root / "test_api.py"
        test_path.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        self.running_leg()
        test_path.unlink()
        state = self.project.load_state("t")
        self.assertIn("deleted_test_file", {item["rule"] for item in baton.run_rule_checks(
            self.project, "t", state, baton.current_leg(state))})

        other = self.root / "contest.py"
        other.write_text("x = 1\n", encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(legs=[]))
        self.running_leg()
        other.unlink()
        state = self.project.load_state("t")
        self.assertNotIn("deleted_test_file", {item["rule"] for item in baton.run_rule_checks(
            self.project, "t", state, baton.current_leg(state))})

    def test_swallowed_exception_hit_and_handled_exception_misses(self):
        hit = self.run_rule_change("src.py", "x = 1\n", "try:\n x = 1\nexcept Exception:\n pass\n")
        self.assertIn("swallowed_exception", {item["rule"] for item in hit})

        self.project.update_state("t", lambda s: s.update(legs=[]))
        miss = self.run_rule_change("src.py", "x = 1\n", "try:\n x = 1\nexcept ValueError:\n handle()\n")
        self.assertNotIn("swallowed_exception", {item["rule"] for item in miss})

    def test_delete_ratio_requires_twenty_deleted_lines(self):
        old = "\n".join(f"line_{i} = {i}" for i in range(30)) + "\n"
        hit = self.run_rule_change("src.py", old, "line_0 = 0\n", supervision={"alerts": {"delete_ratio": 2}})
        self.assertIn("delete_ratio", {item["rule"] for item in hit})

        self.project.update_state("t", lambda s: s.update(legs=[]))
        short_old = "\n".join(f"line_{i} = {i}" for i in range(10)) + "\n"
        miss = self.run_rule_change("src.py", short_old, "line_0 = 0\n", supervision={"alerts": {"delete_ratio": 1}})
        self.assertNotIn("delete_ratio", {item["rule"] for item in miss})

    def test_content_hash_cycle_hit(self):
        path = self.root / "src.py"
        path.write_text("value = 'a'\n", encoding="utf-8")
        self.running_leg()
        path.write_text("value = 'b'\n", encoding="utf-8")
        state = self.project.load_state("t")
        first = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertNotIn("content_hash_cycle", {item["rule"] for item in first})
        path.write_text("value = 'a'\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("content_hash_cycle", {item["rule"] for item in alerts})

    def test_no_file_change_can_alert_again_after_recovery(self):
        self.configure(supervision={"alerts": {"stall_minutes": 0.1}})
        self.running_leg()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(last_file_change_at=baton.now()))
        state = self.project.load_state("t")
        self.assertNotIn("no_file_change", {item["rule"] for item in baton.run_rule_checks(
            self.project, "t", state, baton.current_leg(state))})
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            last_file_change_at=baton.now() - 10))
        state = self.project.load_state("t")
        first = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertEqual([item["rule"] for item in first].count("no_file_change"), 1)

        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            last_file_change_at=baton.now() - 10))
        state = self.project.load_state("t")
        second = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertEqual([item["rule"] for item in second].count("no_file_change"), 1)

    def test_repeat_command_failure_resets_after_success(self):
        store, leg = self.running_leg()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            last_rule_terminal_at=baton.current_leg(s)["started_at"] - 1))
        events = Path(leg["events"])
        events.write_text("\n".join(json.dumps({"type": "item.completed", "item": {
            "type": "command_execution", "command": "pytest", "exit_code": code}})
            for code in (1, 1, 0, 1)) + "\n", encoding="utf-8")
        state = self.project.load_state("t")
        first = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertNotIn("repeat_command_failure", {item["rule"] for item in first})
        with events.open("a", encoding="utf-8") as stream:
            for _ in range(3):
                stream.write(json.dumps({"type": "item.completed", "item": {
                    "type": "command_execution", "command": "pytest", "exit_code": 1}}) + "\n")
        state = self.project.load_state("t")
        second = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("repeat_command_failure", {item["rule"] for item in second})

    def test_log_stale_and_major_without_report(self):
        self.configure(change_size={"major_files": 2, "major_lines": 200})
        self.running_leg()
        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        self.assertNotIn("log_stale", {item["rule"] for item in baton.run_rule_checks(
            self.project, "t", state, baton.current_leg(state))})
        old = baton.now() - 25 * 60
        os.utime(self.project.p("log.md"), (old, old))
        (self.root / "a.py").write_text("value = 3\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("log_stale", {item["rule"] for item in alerts})

        self.project.update_state("t", lambda s: s.update(legs=[]))
        for name in ("one.py", "two.py"):
            (self.root / name).write_text("x = 1\n", encoding="utf-8")
        self.running_leg()
        (self.root / "one.py").write_text("x = 2\n", encoding="utf-8")
        (self.root / "two.py").write_text("x = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("major_without_report", {item["rule"] for item in alerts})

        self.project.update_state("t", lambda s: s.update(legs=[]))
        (self.root / "one.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "two.py").write_text("x = 1\n", encoding="utf-8")
        self.running_leg()
        report = Path(self.project.p("reports")) / "R999-manual.html"
        report.write_text("report", encoding="utf-8")
        (self.root / "one.py").write_text("x = 3\n", encoding="utf-8")
        (self.root / "two.py").write_text("x = 3\n", encoding="utf-8")
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertNotIn("major_without_report", {item["rule"] for item in alerts})

    def test_probe_is_hidden_from_refs_and_dossier_is_sealed(self):
        store = self.make_probe()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_refs(self.project, argparse.Namespace(task="t"))
        self.assertNotIn("/probe", output.getvalue())
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        self.assertTrue(dossier.endswith("-c01"))
        self.assertEqual(baton.dossier_manifest(self.project, dossier)["type"], "check")
        self.assertEqual(baton.verify_dossier(self.project, dossier), (True, ""))

    def test_probe_ref_does_not_change_rollback_result_or_ref_listing(self):
        store, leg = self.running_leg()
        base = leg["start_ref"]
        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertEqual((self.root / "a.py").read_text(), "value = 2\n")
        self.project.update_state("t", lambda s: baton.current_leg(s).update(status="stopped"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_rollback(self.project, argparse.Namespace(task="t", ref=base, yes=True))
        self.assertEqual((self.root / "a.py").read_text(), "value = 1\n")
        self.assertNotIn("/probe", output.getvalue())
        refs = io.StringIO()
        with contextlib.redirect_stdout(refs):
            baton.cmd_refs(self.project, argparse.Namespace(task="t"))
        self.assertNotIn("/probe", refs.getvalue())

    def test_late_check_of_an_earlier_leg_does_not_stop_the_current_leg(self):
        self.make_probe()
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        second = dict(leg, n=2, status="running", alerts=[], alert_keys=[])
        self.project.update_state("t", lambda s: s.update(legs=[dict(leg, status="breaking"), second]))
        with patch.object(baton, "kill_codex") as kill:
            baton.apply_monitor_check_result(self.project, "t", dossier,
                                              {"conclusion": "breaking", "summary": "late", "findings": []}, 1)
        state = self.project.load_state("t")
        self.assertNotIn("supervision_action", state)
        self.assertFalse(kill.called)
        self.assertFalse(Path(self.project.leg_path("t", 2, "stop")).exists())
        self.assertTrue(state["legs"][0]["monitor_check"]["stale"])
        self.assertEqual(state["legs"][1]["status"], "running")
        baton.apply_monitor_check_result(self.project, "t", dossier,
                                          {"conclusion": "drifting", "summary": "late", "findings": []}, 1)
        self.assertFalse((self.project.load_state("t").get("pending_drift") or {}).get("dossier"))

    def test_breaking_without_a_breaking_signal_is_downgraded_to_drifting(self):
        self.make_probe()
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        gap = {"id": "F1", "category": "acceptance_gap", "severity": "blocking",
               "description": "steer item not done yet", "evidence": [], "rule_source": "charter"}
        with patch.object(baton, "kill_codex") as kill:
            baton.apply_monitor_check_result(self.project, "t", dossier,
                                              {"conclusion": "breaking", "summary": "not done", "findings": [gap]}, 1)
        state = self.project.load_state("t")
        self.assertNotIn("supervision_action", state)
        self.assertFalse(kill.called)
        self.assertEqual(state["pending_drift"]["dossier"], dossier)
        self.assertEqual(state["legs"][0]["monitor_check"]["conclusion"], "drifting")
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        signal = dict(gap, category="breaking_signal")
        with patch.object(baton, "kill_codex") as kill:
            baton.apply_monitor_check_result(self.project, "t", dossier,
                                              {"conclusion": "breaking", "summary": "broken", "findings": [signal]}, 1)
        self.assertEqual(self.project.load_state("t").get("supervision_action"), "breaking")
        self.assertTrue(kill.called)

    def test_backoff_is_ten_twenty_forty_sixty_sixty(self):
        self.make_probe()
        values = []
        for _ in range(5):
            leg = self.project.load_state("t")["legs"][0]
            dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
            baton.apply_monitor_check_result(self.project, "t", dossier,
                                              {"conclusion": "on_track", "summary": "ok", "findings": []}, 1)
            values.append(self.project.load_state("t")["legs"][0]["check_interval_minutes"])
        self.assertEqual(values, [20, 40, 60, 60, 60])

    def test_alert_drifting_steer_and_new_leg_reset_interval(self):
        self.make_probe()
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        baton.apply_monitor_check_result(self.project, "t", dossier,
                                          {"conclusion": "on_track", "summary": "ok", "findings": []}, 1)
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        baton.apply_monitor_check_result(self.project, "t", dossier,
                                          {"conclusion": "drifting", "summary": "d", "findings": []}, 1)
        self.assertEqual(self.project.load_state("t")["legs"][0]["check_interval_minutes"], 10)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(status="stopped", reported=True,
                                                                               check_interval_minutes=40))
        self.project.update_state("t", lambda s: s.update(thread_id="thread"))
        with patch.object(baton, "cmd_resume") as resume:
            baton.cmd_steer(self.project, argparse.Namespace(task="t", message="纠偏",
                                                              message_file=None, kind="纠偏"))
        self.assertIsNone(self.project.load_state("t").get("pending_drift"))
        resume.assert_called_once()

    def test_three_file_early_check_and_two_file_no_early_check(self):
        for name in ("one.py", "two.py"):
            (self.root / name).write_text("x = 1\n", encoding="utf-8")
        store, leg = self.running_leg()
        for name in ("one.py", "two.py"):
            (self.root / name).write_text("x = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        state = self.project.load_state("t")
        changed = baton.shortstat(store, leg["start_ref"], state["legs"][0]["probe_ref"])[0]
        self.assertEqual(changed, 2)
        self.assertFalse(baton.monitor_check_due(state, state["legs"][0]))

        (self.root / "three.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "three.py").write_text("x = 2\n", encoding="utf-8")
        state = self.project.load_state("t")
        baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        state = self.project.load_state("t")
        changed = baton.shortstat(store, leg["start_ref"], state["legs"][0]["probe_ref"])[0]
        self.assertEqual(changed, 3)
        state["legs"][0]["next_check_at"] = baton.now() - 1
        self.assertTrue(baton.monitor_check_due(state, state["legs"][0]))

    def test_alert_while_check_running_sets_pending_without_overlap(self):
        self.running_leg()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            check_interval_minutes=40,
            monitor_check={"id": "t-leg-001-c01", "status": "running", "pid": 123},
            alerts=[{"id": "A1", "rule": "deleted_test_file", "category": "breaking",
                     "evidence": "test_api.py", "at": baton.now()}], alert_keys=["deleted_test_file:test_api.py"]))
        with patch.object(subprocess, "Popen") as popen:
            started = baton.maybe_start_monitor_check(self.project, "t", self.project.load_state("t"),
                                                      self.project.load_state("t")["legs"][0], force=True)
        self.assertFalse(started)
        popen.assert_not_called()
        self.assertTrue(self.project.load_state("t")["legs"][0]["check_pending"])
        self.assertEqual(self.project.load_state("t")["legs"][0]["check_interval_minutes"], 10)

    def test_alert_immediately_starts_check_when_none_is_running(self):
        self.make_probe()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(next_check_at=baton.now() + 600,
            alerts=[{"id": "A1", "rule": "deleted_test_file", "category": "breaking",
                     "evidence": "test_api.py", "at": baton.now()}]))
        fake_dir = Path(self.tmp.name) / "check-bin"
        fake_dir.mkdir()
        fake = fake_dir / "codex"
        fake.write_text("#!/usr/bin/env python3\nimport json,sys\nout=sys.argv[sys.argv.index('-o')+1]\njson.dump({'conclusion':'on_track','summary':'ok','findings':[]},open(out,'w'))\n", encoding="utf-8")
        fake = executable_fixture(fake)
        with patch.dict(os.environ, {"PATH": str(fake_dir) + os.pathsep + os.environ.get("PATH", "")}, clear=False):
            started = baton.maybe_start_monitor_check(self.project, "t", self.project.load_state("t"),
                                                      self.project.load_state("t")["legs"][0], force=True)
        self.assertTrue(started)
        self.assertTrue(self.project.load_state("t")["legs"][0].get("check_dossier"))
        pid = self.project.load_state("t")["legs"][0].get("monitor_check", {}).get("pid")
        wait_for_pid(pid)

    def test_on_track_is_recorded_and_wait_does_not_return(self):
        self.make_probe()
        self.make_check({"conclusion": "on_track", "summary": "ok", "findings": []})
        records = [r for r in baton.ledger_records(self.project) if r.get("type") == "check"]
        self.assertEqual(records[-1]["conclusion"], "on_track")
        with patch.object(baton, "time", baton.time), patch.object(baton.time, "sleep", side_effect=RuntimeError("still waiting")):
            with self.assertRaises(RuntimeError):
                baton.cmd_wait(self.project, argparse.Namespace(task="t", tick=None))

    def test_drifting_wait_event_is_one_shot_and_leg_keeps_running(self):
        self.make_probe()
        self.make_check({"conclusion": "drifting", "summary": "偏离", "findings": []})
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_wait(self.project, argparse.Namespace(task="t", tick=None))
        self.assertIn("BATON::EVENT DRIFT", output.getvalue())
        self.assertEqual(self.project.load_state("t")["legs"][0]["status"], "running")
        output = io.StringIO()
        with patch.object(baton, "time", baton.time), patch.object(baton.time, "sleep", side_effect=RuntimeError("still waiting")):
            with contextlib.redirect_stdout(output):
                with self.assertRaises(RuntimeError):
                    baton.cmd_wait(self.project, argparse.Namespace(task="t", tick=None))
        self.assertNotIn("BATON::EVENT DRIFT", output.getvalue())

    def test_steer_clears_pending_drift(self):
        self.make_probe()
        self.make_check({"conclusion": "drifting", "summary": "d", "findings": []})
        self.project.update_state("t", lambda s: (s.update(thread_id="thread"),
                                                   baton.current_leg(s).update(status="stopped", reported=True)))
        with patch.object(baton, "cmd_resume"):
            baton.cmd_steer(self.project, argparse.Namespace(task="t", message="纠偏",
                                                              message_file=None, kind="纠偏"))
        self.assertIsNone(self.project.load_state("t").get("pending_drift"))

    def test_dismiss_and_breaking_status_allow_resume_without_rule(self):
        for status in ("breaking", "drift_unhandled"):
            self.project.update_state("t", lambda s, status=status: s.update(
                thread_id="thread", pending_drift=None, legs=[{"n": 1, "kind": "start", "status": status,
                "executor_status": status, "started_at": baton.now(), "reported": True, "events": "",
                "result": {"status": status}}]))
            with patch.object(baton, "launch_leg") as launch:
                baton.cmd_resume(self.project, argparse.Namespace(task="t", message="继续", message_file=None,
                                                                   kind="回复", verdict=None, decision=None,
                                                                   user_approved_continue=False,
                                                                   _message_archived=False))
            launch.assert_called_once()

    def test_drift_unhandled_timeout_is_reported_by_wait(self):
        self.running_leg()
        self.project.update_state("t", lambda s: s.update(pending_drift={"dossier": "t-leg-001-c01",
            "brief_path": "brief", "finding_ids": ["F1"], "summary": "d", "at": baton.now() - 10,
            "deadline": baton.now() - 1, "event_reported": True}))
        # The runner owns the transition; this assertion verifies the same event mapping used at leg end.
        self.project.update_state("t", lambda s: baton.current_leg(s).update(status="drift_unhandled",
                                                                               executor_status="drift_unhandled",
                                                                               reported=False, ended_at=baton.now()))
        event = baton.leg_event_text(self.project, "t", self.project.load_state("t")["legs"][0])
        self.assertIn("BATON::EVENT DRIFT_UNHANDLED", event)
        self.assertIn("resume", event)

    def test_runner_ends_drift_unhandled_after_deadline(self):
        store, leg = self.running_leg()
        run_dir = Path(self.project.runs_dir("t"))
        prompt = run_dir / "leg-001.prompt.md"
        prompt.write_text("continue", encoding="utf-8")
        Path(leg["events"]).touch()
        Path(self.project.leg_path("t", 1, "stderr.log")).touch()
        last = self.project.leg_path("t", 1, "last.json")
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            prompt=str(prompt), last=last, stderr=self.project.leg_path("t", 1, "stderr.log"),
            cmd=[sys.executable, "-c", "import os,signal,time; time.sleep(0.1); os.kill(os.getpid(), signal.SIGTERM)"],
            pending_drift=True) or s.update(pending_drift={"dossier": "c01", "finding_ids": ["F1"],
            "deadline": baton.now() - 1, "event_reported": True}))
        baton.cmd_run(self.project, argparse.Namespace(task="t", n="1"))
        state = self.project.load_state("t")
        self.assertEqual(state["legs"][0]["status"], "drift_unhandled")
        self.assertTrue(state["legs"][0].get("end_ref"))
        self.assertIn("BATON::EVENT DRIFT_UNHANDLED", baton.leg_event_text(self.project, "t", state["legs"][0]))

    def test_persisted_breaking_reason_wins_over_sigterm_and_verify_is_written(self):
        self.running_leg()
        run_dir = Path(self.project.runs_dir("t"))
        prompt = run_dir / "leg-001.prompt.md"
        prompt.write_text("continue", encoding="utf-8")
        Path(self.project.leg_path("t", 1, "events.jsonl")).touch()
        Path(self.project.leg_path("t", 1, "stderr.log")).touch()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            prompt=str(prompt), last=self.project.leg_path("t", 1, "last.json"),
            stderr=self.project.leg_path("t", 1, "stderr.log"),
            cmd=[sys.executable, "-c", "import os,signal,time; time.sleep(0.1); os.kill(os.getpid(), signal.SIGTERM)"],
            stop_reason="breaking") or s.update(supervision_action="breaking"))
        baton.cmd_run(self.project, argparse.Namespace(task="t", n="1"))
        state = self.project.load_state("t")
        self.assertEqual(state["legs"][0]["exit_code"], 15 if os.name == "nt" else -15)
        self.assertEqual(state["legs"][0]["status"], "breaking")
        verify_path = Path(self.project.leg_path("t", 1, "verify.json"))
        self.assertTrue(verify_path.exists())
        self.assertIsInstance(baton.read_json(str(verify_path)), dict)

    def test_check_backoff_and_conclusions(self):
        self.make_probe()
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        on_track = {"conclusion": "on_track", "summary": "ok", "findings": []}
        baton.apply_monitor_check_result(self.project, "t", dossier, on_track, 1)
        leg = self.project.load_state("t")["legs"][0]
        self.assertEqual(leg["check_interval_minutes"], 20)
        dossier2 = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        baton.apply_monitor_check_result(self.project, "t", dossier2,
                                          {"conclusion": "drifting", "summary": "偏离", "findings": []}, 1)
        state = self.project.load_state("t")
        self.assertEqual(state["pending_drift"]["dossier"], dossier2)
        self.assertEqual(state["legs"][0]["check_interval_minutes"], 10)

    def test_dismiss_partial_and_all(self):
        self.make_probe()
        leg = self.project.load_state("t")["legs"][0]
        dossier = baton.build_check_dossier(self.project, "t", self.project.load_state("t"), leg)
        findings = [{"id": "F1", "category": "breaking_signal", "severity": "blocking", "description": "a",
                     "evidence": [], "rule_source": "r"},
                    {"id": "F2", "category": "missing_record", "severity": "note", "description": "b",
                     "evidence": [], "rule_source": "r"}]
        baton.apply_monitor_check_result(self.project, "t", dossier,
                                          {"conclusion": "drifting", "summary": "d", "findings": findings}, 1)
        baton.cmd_supervised(self.project, argparse.Namespace(task="t", dismiss=["F1"], reason="已处理"))
        self.assertEqual(self.project.load_state("t")["pending_drift"]["finding_ids"], ["F2"])
        baton.cmd_supervised(self.project, argparse.Namespace(task="t", dismiss=["F2"], reason="已处理"))
        self.assertIsNone(self.project.load_state("t").get("pending_drift"))
        self.assertEqual(len([r for r in baton.ledger_records(self.project) if r.get("type") == "dismiss"]), 2)

    def test_consecutive_failures_enable_fallback_and_success_clears(self):
        self.running_leg()
        baton._check_failure_update(self.project, "t", 1, "t-leg-001-c01", "timeout")
        self.assertFalse(self.project.load_state("t").get("supervision_fallback"))
        baton._check_failure_update(self.project, "t", 1, "t-leg-001-c02", "schema")
        self.assertTrue(self.project.load_state("t").get("supervision_fallback"))
        self.make_probe()
        self.make_check({"conclusion": "on_track", "summary": "recovered", "findings": []})
        self.assertFalse(self.project.load_state("t").get("supervision_fallback"))

    def test_fallback_wait_uses_tick_and_monitor_false_keeps_v1_path(self):
        self.configure(supervision={"interval_minutes": 0.001, "max_unsupervised_minutes": 0.001})
        self.running_leg()
        self.project.update_state("t", lambda s: s.update(supervision_fallback=True,
            last_supervision_at=baton.now() - 1, supervision={"leg": 1, "ref": None}))
        st = self.project.load_state("t")
        with patch.object(baton, "refresh_runner_liveness", return_value=st), \
                patch.object(baton, "safe_quota", return_value=None):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                baton.cmd_wait(self.project, argparse.Namespace(task="t", tick=None))
        self.assertIn("BATON::EVENT TICK", output.getvalue())

        self.project.update_state("t", lambda s: s.update(legs=[], supervision_fallback=False,
                                                            config_snapshot=dict(self.project.config)))
        cfg = json.loads(json.dumps(self.project.config))
        cfg["monitor"]["enabled"] = False
        cfg["supervision"]["interval_minutes"] = 0.001
        self.project.update_state("t", lambda s: s.update(config_snapshot=cfg))
        self.running_leg()
        test_file = self.root / "test_api.py"
        test_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        test_file.unlink()
        state = self.project.load_state("t")
        self.assertEqual(baton.run_rule_checks(self.project, "t", state, baton.current_leg(state)), [])
        self.project.update_state("t", lambda s: s.update(last_supervision_at=baton.now() - 1))
        st = self.project.load_state("t")
        with patch.object(baton, "refresh_runner_liveness", return_value=st), \
                patch.object(baton, "safe_quota", return_value=None):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                baton.cmd_wait(self.project, argparse.Namespace(task="t", tick=None))
        self.assertIn("BATON::EVENT TICK", output.getvalue())
        self.assertNotIn("ALERT", output.getvalue())

    def test_fallback_enables_max_unsupervised_pause(self):
        self.configure(supervision={"max_unsupervised_minutes": 0.001}, monitor={"enabled": True})
        self.running_leg()
        run_dir = Path(self.project.runs_dir("t"))
        prompt = run_dir / "leg-001.prompt.md"
        prompt.write_text("continue", encoding="utf-8")
        Path(self.project.leg_path("t", 1, "events.jsonl")).touch()
        Path(self.project.leg_path("t", 1, "stderr.log")).touch()
        self.project.update_state("t", lambda s: s.update(supervision_fallback=True,
            last_supervision_at=baton.now() - 1) or baton.current_leg(s).update(
            prompt=str(prompt), last=self.project.leg_path("t", 1, "last.json"),
            stderr=self.project.leg_path("t", 1, "stderr.log"),
            cmd=[sys.executable, "-c", "import time; time.sleep(2)"]))
        baton.cmd_run(self.project, argparse.Namespace(task="t", n="1"))
        self.assertEqual(self.project.load_state("t")["legs"][0]["status"], "paused_unsupervised")

    def test_lean_mode_has_no_rule_or_check(self):
        cfg = json.loads(json.dumps(self.project.config))
        cfg["mode"] = "lean"
        self.project.update_state("t", lambda s: s.update(config_snapshot=cfg))
        self.running_leg()
        test_file = self.root / "test_api.py"
        test_file.write_text("def test_a():\n assert 1\n", encoding="utf-8")
        test_file.unlink()
        state = self.project.load_state("t")
        self.assertEqual(baton.run_rule_checks(self.project, "t", state, baton.current_leg(state)), [])
        self.assertFalse(baton.monitor_check_due(state, state["legs"][0]))

    def test_real_cli_breaking_check_ends_leg_and_keeps_deletion(self):
        base = Path(self.tmp.name) / "cli"
        root = base / "project"
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / "test_api.py").write_text("def test_a():\n assert 1\n", encoding="utf-8")
        (root / "baton.json").write_text(json.dumps({
            "monitor": {"model": "gpt-test", "enabled": True, "timeout_minutes": 0.2},
            "supervision": {"rule_check_seconds": 0.02, "first_check_minutes": 0.005,
                             "max_check_minutes": 0.01, "unhandled_stop_minutes": 0.01,
                             "test_command": "python3 -c 'print(1)'"},
            "freeze": {"check_seconds": 0.02}}), encoding="utf-8")
        bindir = base / "bin"
        bindir.mkdir(parents=True)
        fake = bindir / "codex"
        fake.write_text("""#!/usr/bin/env python3
import json, pathlib, sys, time
args = sys.argv[1:]
out = args[args.index('-o') + 1]
if '--ignore-user-config' in args:
    manifest = json.loads(pathlib.Path('manifest.json').read_text())
    if manifest.get('type') == 'check':
        json.dump({'conclusion':'breaking','summary':'测试被删除','findings':[]}, open(out, 'w'))
    else:
        json.dump({'summary':'ok','recommendation':'APPROVE','findings':[],'memory_advice':[]}, open(out, 'w'))
else:
    print(json.dumps({'type':'thread.started','thread_id':'fake-thread'}), flush=True)
    time.sleep(0.20)
    target = pathlib.Path('test_api.py')
    if target.exists():
        target.unlink()
    time.sleep(2)
    json.dump({'status':'done','summary':'done','artifact':'','change_size':'minor'}, open(out, 'w'))
""", encoding="utf-8")
        fake = executable_fixture(fake)
        env = os.environ.copy()
        env.update({"HOME": str(base / "home"), "USERPROFILE": str(base / "home"), "BATON_HOME": str(base / "state"),
                    "PATH": str(bindir) + os.pathsep + env.get("PATH", ""),
                    "BATON_DESKTOP_NOTIFY": "0", "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(self.cli(root, env, "init", "--no-statusline").returncode, 0)
        task = root / ".baton" / "tasks" / "t"
        task.mkdir(parents=True)
        (task / "brief.md").write_text("```baton-scope\n" + json.dumps({
            "allow": ["test_api.py"], "deny": [], "acceptance": [], "frozen": [], "verify": "auto"}) +
            "\n```\n# t\n", encoding="utf-8")
        self.assertEqual(self.cli(root, env, "start", "t").returncode, 0)
        state_path = next((base / "state" / "projects").glob("*/tasks/t/state.json"))
        runner_pids = set()
        deadline = time.time() + 15
        while time.time() < deadline:
            state = json.loads(state_path.read_text())
            leg = state["legs"][-1]
            if leg.get("runner_pid"):
                runner_pids.add(leg["runner_pid"])
            if (leg.get("monitor_check") or {}).get("pid"):
                runner_pids.add(leg["monitor_check"]["pid"])
            if leg.get("status") != "running" and (leg.get("monitor_check") or {}).get("status") != "running":
                break
            time.sleep(0.05)
        state = json.loads(state_path.read_text())
        leg = state["legs"][-1]
        self.assertEqual(leg["status"], "breaking")
        self.assertFalse((root / "test_api.py").exists())
        self.assertTrue(leg.get("end_ref", "").endswith("leg-001-end"))
        self.assertFalse(leg["verify"].get("skipped"))
        self.assertEqual(leg["verify"].get("exit_code"), 0)
        event = self.cli(root, env, "wait", "t", "--tick", "0")
        self.assertEqual(event.returncode, 0, event.stderr)
        self.assertIn("BATON::EVENT BREAKING", event.stdout)
        for pid in runner_pids:
            wait_for_pid(pid)

    def test_check_schema_and_cmd_rule_do_not_emit_alerts(self):
        self.assertEqual(baton._validate_monitor_check({"conclusion": "breaking", "summary": "x", "findings": []}), [])
        self.assertTrue(baton._validate_monitor_check({"conclusion": "breaking", "summary": "x"}))
        self.assertNotIn("ALERT", baton.cmd_rule.__code__.co_names)


if __name__ == "__main__":
    unittest.main()
