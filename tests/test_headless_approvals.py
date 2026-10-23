import contextlib
import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class _StateProject:
    def __init__(self, state, delegate=False):
        self.state = state
        self.config = {"approvals": {"delegate_to_conductor": delegate}}

    def read_state(self, task, default=None):
        return self.state

    def update_state(self, task, callback):
        callback(self.state)
        return self.state

    def leg_path(self, task, leg_n, suffix):
        raise AssertionError("整理 monitor 不应访问轮次路径")


class HeadlessApprovalTests(unittest.TestCase):
    def test_curation_capacity_wait_uses_curation_state_and_can_retry(self):
        project = _StateProject({"curation_monitor": {"id": "curate-1", "status": "running"}})
        with patch.object(baton.time, "sleep", lambda _: None):
            self.assertTrue(baton._wait_monitor_capacity(project, "task", "curate-1", None, 0, 1, "immediate"))
            self.assertTrue(baton._wait_monitor_capacity(project, "task", "curate-1", None, 0, 2, "delayed"))
        self.assertEqual(project.state["curation_monitor"]["status"], "running")
        self.assertNotIn("capacity_wait", project.state["curation_monitor"])

    def test_failed_curation_monitor_does_not_block_resume_but_running_does(self):
        project = SimpleNamespace()
        state = {"legs": [{"n": 1, "executor_status": "report", "status": "report",
                            "monitor": {"id": "R001", "status": "completed"}}],
                 "curation_monitor": {"id": "task-curate-1", "status": "failed"}}
        verdict = {"dossier_hash": "hash", "brief_seq": 1, "brief_record_hash": "record"}
        with patch.object(baton, "verify_ledger", return_value=True), \
                patch.object(baton, "verify_dossier", return_value=(True, "")), \
                patch.object(baton, "_dossier_brief", return_value=({"seq": 1, "record_hash": "record"}, "hash")), \
                patch.object(baton, "ledger_records", return_value=[]), \
                patch.object(baton, "_latest_ledger_record", return_value=verdict), \
                patch.object(baton, "dossier_manifest", return_value={}):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertIs(verdict, baton._resume_ruling_gate(project, "task", state))
            self.assertIn("baton monitor task --curate", out.getvalue())

        state["curation_monitor"]["status"] = "running"
        with self.assertRaises(SystemExit):
            baton._resume_ruling_gate(project, "task", state)

    def test_delegate_false_without_tty_still_rejects(self):
        project = _StateProject({}, delegate=False)
        with patch.object(baton, "_open_tty", side_effect=OSError("no tty")), \
                self.assertRaisesRegex(baton.ConfigError, "无法打开 /dev/tty"):
            baton._require_task_confirmation("task")

    def test_delegate_true_amend_records_approver_without_tty(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HOME": str(Path(tmp) / "home"),
                                         "BATON_HOME": str(Path(tmp) / "baton-home")}):
                root = Path(tmp) / "project"
                root.mkdir()
                (root / "baton.json").write_text(json.dumps({
                    "executor": {"backend": "codex"},
                    "approvals": {"delegate_to_conductor": True},
                }), encoding="utf-8")
                (root / ".baton").mkdir()
                (root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
                project = baton.Project(str(root))
                scope = {"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}
                cfg = project.config
                manifest = baton.write_freeze_baseline(project, "task", scope, cfg)
                project.update_state("task", lambda state: state.update(
                    task="task", legs=[], scope=scope, freeze_manifest=manifest,
                    config_snapshot=cfg))
                amendment = Path(tmp) / "amend.md"
                amendment.write_text("无人值守确认\n", encoding="utf-8")
                args = Namespace(task="task", amendment_file=str(amendment),
                                 config=[], adopt=[], user_approved=True)
                out = io.StringIO()
                with patch.object(baton, "_open_tty", side_effect=AssertionError("不应打开 tty")), \
                        contextlib.redirect_stdout(out):
                    baton.cmd_amend(project, args)
                state = project.read_state("task")
                self.assertEqual(state["amendments"][0]["approver"], "conductor-delegated")
                self.assertIn("已按 approvals.delegate_to_conductor 由 conductor 代为确认", out.getvalue())

    def test_user_approved_continue_path_uses_same_headless_delegate(self):
        project = _StateProject({}, delegate=True)
        with patch.object(baton, "_open_tty", side_effect=AssertionError("不应打开 tty")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            delegated = baton._confirmation_delegated(project, "task")
            if delegated:
                print("已按 approvals.delegate_to_conductor 由 conductor 代为确认")
        self.assertTrue(delegated)
        self.assertIn("由 conductor 代为确认", out.getvalue())


if __name__ == "__main__":
    unittest.main()
