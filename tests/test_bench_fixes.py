import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class BenchFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"HOME": str(base / "home"), "BATON_HOME": str(base / "state"),
                                           "BATON_DESKTOP_NOTIFY": "0", "PYTHONDONTWRITEBYTECODE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        self.project = baton.Project(str(self.root))
        (self.root / ".baton").mkdir()
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(self.project, Namespace(no_statusline=True, accept_boundary=True,
                                                   relink=False, fresh=False, migrate=False, upgrade_profile=False))
        self.project.record_id()
        (self.root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "t" / "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": ["a.py"], "deny": [], "acceptance": [],
                                                   "frozen": [], "verify": "none"}) + "\n```\n# t\n",
            encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(task="t", status="idle", legs=[], snapshot_store="shadow",
                                                           scope={"allow": ["a.py"], "deny": [], "acceptance": [],
                                                                  "frozen": [], "verify": "none"}))

    def final_accept(self, verify):
        store = baton.SnapStore(self.project, "shadow")
        base = store.snapshot("base")
        (self.root / "a.py").write_text("x = 2\n", encoding="utf-8")
        end = store.snapshot("end")
        leg = {"n": 1, "status": "done", "executor_status": "done", "started_at": baton.now(),
               "events": "", "start_ref": base, "end_ref": end,
               "result": {"status": "done", "summary": "done", "artifact": ""}, "verify": verify}
        self.project.update_state("t", lambda s: s.update(legs=[leg], base_ref=base, status="idle"))
        dossier = baton.build_dossier(self.project, "t", self.project.load_state("t"), leg, "done")
        brief = {"summary": "final", "recommendation": "ACCEPT", "findings": [], "memory_advice": []}
        text = baton.render_monitor_brief(self.project, "t", dossier, brief)
        manifest = baton.dossier_manifest(self.project, dossier)
        baton.append_brief_ledger(self.project, "t", dossier, manifest["total_sha256"], text, brief_json=brief)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            dossier={"id": dossier, "type": "final"}, monitor={"id": dossier, "status": "completed"}))
        verdict = {"conclusion": "ACCEPT", "findings": {}, "dossier_hash": manifest["total_sha256"],
                   "brief_seq": baton.ledger_records(self.project)[-1]["seq"],
                   "brief_record_hash": baton.ledger_records(self.project)[-1]["record_hash"],
                   "rendered": "ACCEPT"}
        # Use the real binding fields produced by cmd_rule's ledger record shape.
        baton.append_ledger(self.project, {"type": "verdict", "task": "t", "dossier": dossier,
                                           **verdict, "verdict": "ACCEPT"})
        return dossier

    def test_waive_close_records_reason_and_items(self):
        dossier = self.final_accept({"skipped": False, "exit_code": 1,
                                     "acceptance_failed": [{"nodeid": "tests/test_x.py", "reason": "failed"}]})
        with patch.object(baton, "_require_task_confirmation", return_value="user"), \
             contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_close(self.project, Namespace(task="t", abandon=None, waive="环境缺少依赖",
                                                    user_approved=True))
        self.assertEqual(self.project.load_state("t")["status"], "closed")
        close = [r for r in baton.ledger_records(self.project) if r.get("type") == "close"][-1]
        self.assertEqual(close["waive_reason"], "环境缺少依赖")
        self.assertEqual(close["waive_approver"], "user")
        self.assertEqual(close["waived"]["exit_code"], 1)
        self.assertTrue(close["waived"]["acceptance_failed"])

    def test_waive_requires_approval_and_is_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t", abandon="放弃", waive="豁免", user_approved=True))
        with self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t", abandon=None, waive="豁免", user_approved=False))

    def test_close_gate_error_lists_both_exits(self):
        dossier = self.final_accept({"skipped": False, "exit_code": 1, "acceptance_failed": []})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t", abandon=None, waive=None, user_approved=False))
        self.assertIn("--waive", err.getvalue())
        self.assertIn("--abandon", err.getvalue())

    def test_accept_idle_hint_and_accept_rule_hint(self):
        self.assertIn("baton close t", baton._rule_conclusion_hint("t", "ACCEPT", "R001", {}))
        leg = {"n": 1, "status": "done", "started_at": baton.now(), "reported": True,
               "events": "", "result": {"status": "done"}}
        self.project.update_state("t", lambda s: s.update(legs=[leg], status="idle"))
        # The IDLE helper is exercised through a final dossier in the close fixture.
        dossier = self.final_accept({"skipped": True, "skip_reason": "本任务没有 baton 亲跑的验证"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out), patch.object(baton, "reap_detached_runners"):
            baton.cmd_wait(self.project, Namespace(task="t", tick=0))
        self.assertIn(f"已最终 ACCEPT（{dossier}）", out.getvalue())
        self.assertIn("--waive", out.getvalue())

    def test_node_ids_freeze_file_and_parameter_brackets_do_not_become_glob(self):
        test_file = self.root / "tests" / "test_app.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_ok(): pass\n", encoding="utf-8")
        scope = {"allow": [], "deny": [], "acceptance": ["tests/test_app.py::test_ok[param[x]]",
                                                               "tests/test_app.py::test_ok[param[y]]"],
                 "frozen": [], "verify": "none"}
        manifest = baton.write_freeze_baseline(self.project, "t", scope, self.project.config)
        self.assertEqual([e["path"] for e in manifest["entries"]], ["tests/test_app.py"])
        test_file.write_text("def test_ok(): return 1\n", encoding="utf-8")
        check = baton.check_freeze(self.project, "t", self.project.load_state("t"))
        self.assertTrue(any(v["path"] == "tests/test_app.py" for v in check["violations"]))

    def test_quota_classification_and_event_text(self):
        events = Path(self.tmp.name) / "events"
        stderr = Path(self.tmp.name) / "stderr"
        events.write_text(json.dumps({"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}) + "\n")
        stderr.write_text("")
        self.assertEqual(baton.capacity_failure_kind(str(events), str(stderr), returncode=1), "quota")
        self.project.update_state("t", lambda s: s.update(legs=[{"n": 1, "status": "failed", "started_at": baton.now(),
                                                                    "events": "", "result": {}, "failure_kind": "quota",
                                                                    "monitor": {"id": "t-final", "status": "failed",
                                                                                "failure_kind": "quota", "error": "额度耗尽"}}], status="idle"))
        event = baton.leg_event_text(self.project, "t", self.project.load_state("t")["legs"][0])
        self.assertIn("基础设施问题，不是方法失败", event)
        self.assertIn("baton monitor t t-final --rerun", event)

        self.project.update_state("t", lambda s: s.update(legs=[{"n": 1, "status": "running", "started_at": baton.now(),
                                                                    "events": "", "result": {},
                                                                    "monitor_check": {"id": "c03", "status": "failed",
                                                                                      "failure_kind": "quota",
                                                                                      "error": "额度耗尽"}}], status="idle"))
        check_event = baton.leg_event_text(self.project, "t", self.project.load_state("t")["legs"][0])
        self.assertIn("baton monitor t c03 --rerun", check_event)

    def test_capacity_classifier_ignores_command_output(self):
        events = Path(self.tmp.name) / "events"
        stderr = Path(self.tmp.name) / "stderr"
        events.write_text("assistant summary: Selected model is at capacity\n", encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        self.assertIsNone(baton.capacity_failure_kind(str(events), str(stderr), returncode=1))
        events.write_text("Error: Selected model is at capacity\n", encoding="utf-8")
        self.assertEqual(baton.capacity_failure_kind(str(events), str(stderr), returncode=1), "capacity")
        command_output = {"type": "item.completed", "item": {
            "type": "command_execution", "status": "completed",
            "aggregated_output": "usage limit and at capacity appear in inspected source"
        }}
        ordinary_failure = {"type": "turn.failed", "error": {"message": "invalid parameter"}}
        events.write_text("\n".join(json.dumps(item) for item in (command_output, ordinary_failure)) + "\n")
        stderr.write_text("")
        self.assertIsNone(baton.capacity_failure_kind(str(events), str(stderr), returncode=1))

        capacity_failure = {"type": "turn.failed", "error": {"message": "Selected model is at capacity"}}
        events.write_text("\n".join(json.dumps(item) for item in (command_output, capacity_failure)) + "\n")
        self.assertEqual(baton.capacity_failure_kind(str(events), str(stderr), returncode=1), "capacity")

        events.write_text("\n".join(json.dumps(item) for item in (
            command_output, {"type": "turn.failed", "error": {"message": "You've hit your usage limit"}}
        )) + "\n")
        self.assertEqual(baton.capacity_failure_kind(str(events), str(stderr), returncode=1), "quota")

    def test_quota_reset_uses_windows_and_check_quota_is_terminal(self):
        epoch = 1793774165
        quota = {"windows": [
            {"remaining": 35, "resets_at": epoch},
            {"remaining": 0, "resets_at": epoch + 3600},
        ]}
        with patch.object(baton, "safe_quota", return_value=quota):
            expected = baton.stamp(epoch + 3600, fmt="%m-%d %H:%M")
            self.assertEqual(baton._quota_reset_text(), expected)
            self.assertIn(expected, baton._monitor_capacity_error("额度耗尽"))
        with patch.object(baton, "safe_quota", return_value={
                "windows": [{"remaining": 0, "resets_at": "2099-01-02T03:04:05Z"}]}):
            self.assertEqual(baton._quota_reset_text(), "2099-01-02T03:04:05Z")
        dossier_root = Path(baton.dossier_path(self.project, "c03"))
        dossier_root.mkdir(parents=True)
        (dossier_root / "MONITOR_PROMPT.md").write_text("prompt\n", encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(legs=[{"n": 1, "status": "running",
                                                                    "monitor_check": {}}]))
        with patch.object(baton, "verify_dossier", return_value=(True, "")), \
             patch.object(baton, "run_monitor_check_once",
                          return_value=(None, "轮中检查额度耗尽", 1, "quota")):
            self.assertFalse(baton.run_monitor_check_lifecycle(self.project, "t", "c03",
                                                               self.project.config, 1))
        check = baton.current_leg(self.project.load_state("t"))["monitor_check"]
        self.assertEqual(check["failure_kind"], "quota")

    def test_prompt_rule_is_rendered(self):
        dossier = self.final_accept({"skipped": False, "exit_code": 0, "acceptance_failed": []})
        prompt = (Path(baton.dossier_path(self.project, dossier)) / "MONITOR_PROMPT.md").read_text(encoding="utf-8")
        self.assertIn("self_report_mismatch", prompt)
        self.assertIn("只是措辞不精确", prompt)
        self.assertIn("blocking", prompt)
        self.assertNotIn("{{TASK}}", prompt)


if __name__ == "__main__":
    unittest.main()
