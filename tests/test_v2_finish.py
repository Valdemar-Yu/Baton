import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402
import statusline  # noqa: E402


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-finish-")
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.home = Path(self.tmp.name) / "baton-home"
        self.old_env = dict(os.environ)
        os.environ["BATON_HOME"] = str(self.home)
        os.environ["HOME"] = str(Path(self.tmp.name) / "home")
        (self.root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        self.project = baton.Project(str(self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(self.project, Namespace(no_statusline=True, accept_boundary=True,
                                                    relink=False, fresh=False, upgrade_profile=False,
                                                    migrate=False))
        self.project = baton.Project(str(self.root))
        self.project.update_state("t", lambda s: s.update(task="t", status="idle", legs=[]))

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_env)
        self.tmp.cleanup()

    def add_verdict(self, conclusion):
        baton.append_ledger(self.project, {"type": "verdict", "task": "t",
                                           "dossier": "R%03d" % (len(baton.ledger_records(self.project)) + 1),
                                           "conclusion": conclusion})

    def test_revise_trigger_and_approve_reset(self):
        self.add_verdict("REVISE")
        self.add_verdict("REVISE")
        st = self.project.load_state("t")
        decision = baton.escalation_decision(self.project, "t", st)
        self.assertTrue(decision["escalated"])
        self.assertIn("REVISE", decision["reason"])
        self.add_verdict("APPROVE")
        self.assertFalse(baton.escalation_decision(self.project, "t", st)["escalated"])

    def test_escalation_config_validation(self):
        path = self.root / "baton.json"
        path.write_text(json.dumps({"executor": {"escalation": {"after_revise": 0}}}), encoding="utf-8")
        with self.assertRaises(baton.ConfigError) as ctx:
            baton.Project(str(self.root)).config
        self.assertIn("executor.escalation.after_revise", str(ctx.exception))

    def test_method_stuck_classification_excludes_external_failures(self):
        self.assertTrue(baton.method_stuck_failure({"status": "failed", "failure_kind": "method_stuck"}))
        for kind, summary in (("capacity", "Selected model is at capacity"),
                              ("network", "network unavailable"),
                              ("permission", "缺少权限"),
                              ("user_input", "需要用户输入")):
            self.assertFalse(baton.method_stuck_failure({"status": "blocked", "failure_kind": kind,
                                                          "result": {"summary": summary}}))
        self.project.update_state("t", lambda s: s.update(legs=[
            {"status": "failed", "failure_kind": "method_stuck", "result": {"summary": "实现卡住"}},
            {"status": "blocked", "failure_kind": "method_stuck", "result": {"summary": "方法卡住"}},
        ]))
        self.assertTrue(baton.escalation_decision(self.project, "t")["escalated"])

    def test_capacity_and_network_are_neutral_for_streak(self):
        self.project.update_state("t", lambda s: s.update(legs=[
            {"status": "failed", "failure_kind": "method_stuck"},
            {"status": "failed", "failure_kind": "capacity"},
            {"status": "failed", "failure_kind": "network"},
        ]))
        self.assertEqual(baton.escalation_counters(self.project, "t")["method_stuck_streak"], 1)

    def test_force_escalate_and_lean_disable(self):
        forced = baton.escalation_decision(self.project, "t", force=True)
        self.assertTrue(forced["escalated"])
        (self.root / "baton.json").write_text(json.dumps({"mode": "lean"}), encoding="utf-8")
        lean = baton.Project(str(self.root))
        self.assertFalse(lean.config["executor"]["escalation"]["enabled"])
        self.assertFalse(baton.escalation_decision(lean, "t")["escalated"])

    def test_max_per_task_and_no_second_automatic_upgrade_after_failure(self):
        self.project.update_state("t", lambda s: s.update(escalation={"auto_count": 3}))
        self.add_verdict("REVISE")
        self.add_verdict("REVISE")
        self.assertFalse(baton.escalation_decision(self.project, "t")["escalated"])
        self.project.update_state("t", lambda s: s.update(escalation={"auto_count": 1, "auto_suppressed": True}))
        decision = baton.escalation_decision(self.project, "t")
        self.assertFalse(decision["escalated"])
        self.assertIn("上次升级", decision["reason"])

    def test_codex_and_hermes_escalation_commands_keep_session(self):
        cfg = json.loads(json.dumps(self.project.config))
        cfg["executor"]["model"] = "gpt-6.1-sol"
        cfg["executor"]["reasoning_effort"] = "xhigh"
        codex = baton.codex_command(self.project, "resume", "thread-1", "/tmp/last.json", cfg=cfg)
        self.assertEqual(codex[codex.index("-m") + 1], "gpt-6.1-sol")
        self.assertIn("resume", codex)
        cfg["executor"]["backend"] = "hermes"
        hermes = baton.hermes_command(self.project, "resume", "thread-1", "prompt", cfg=cfg)
        self.assertIn("-r", hermes)
        self.assertEqual(hermes[hermes.index("-m") + 1], "gpt-6.1-sol")

    def test_migrate_backup_diff_and_idempotence(self):
        raw = {"judge": {"model": "sonnet", "council_for_major_decisions": False},
               "executor": {"model": "gpt-6.1-sol", "service_tier": "priority",
                             "disable_fast_mode": True, "sandbox": "workspace-write",
                             "network_access": True},
               "supervision": {"test_command": "python -m unittest"}}
        path = self.root / "baton.json"
        path.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        project = baton.Project(str(self.root))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(baton.migrate_baton_config(project))
        migrated = json.loads(path.read_text())
        self.assertEqual(migrated["conductor"]["model"], "sonnet")
        self.assertEqual(migrated["executor"]["backend"], "codex" if os.name == "nt" else "hermes")
        self.assertEqual(migrated["executor"]["model"], "gpt-5.6-luna")
        for key in ("service_tier", "disable_fast_mode", "sandbox"):
            if os.name == "nt":
                self.assertEqual(migrated["executor"].get(key), raw["executor"].get(key))
            else:
                self.assertNotIn(key, migrated["executor"])
        self.assertTrue((self.root / "baton.json.v1.bak").exists())
        self.assertIn("配置迁移差异", out.getvalue())
        second = io.StringIO()
        with contextlib.redirect_stdout(second):
            self.assertFalse(baton.migrate_baton_config(project))
        self.assertIn("无需迁移", second.getvalue())

    def test_templates_and_references_include_v2_instructions(self):
        report = (ROOT / "skills/baton/templates/report.html").read_text()
        monitor = (ROOT / "skills/baton/templates/monitor-prompt.md").read_text()
        self.assertIn("申辩", report)
        self.assertIn("请重点核对", report)
        self.assertIn("逐条核对申辩", monitor)
        self.assertIn("简报失败", (ROOT / "skills/baton/references/monitor.md").read_text())
        self.assertIn("baton rule", (ROOT / "skills/baton/references/review.md").read_text())
        self.assertIn("baton decide", (ROOT / "skills/baton/references/decision.md").read_text())

    def test_statusline_running_round_shows_model_monitor_and_capacity_wait(self):
        project_id = statusline._project_id(str(self.root))
        tasks = self.home / "projects" / project_id / "tasks" / "t"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "state.json").write_text(json.dumps({"legs": [{
            "n": 2, "status": "running", "model": "gpt-6.1-sol", "escalated": True,
            "monitor": {"status": "running"},
            "capacity_wait": {"next_retry_at": time.time() + 120}
        }]}), encoding="utf-8")
        info = statusline.runtime_line({"workspace": {"project_dir": str(self.root)}})
        self.assertIn("t·第2轮·gpt-6.1-sol（升级）", info)
        self.assertIn("monitor 运行中", info)
        self.assertIn("容量重试等待", info)


if __name__ == "__main__":
    unittest.main()
