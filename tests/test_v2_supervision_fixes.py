import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import argparse
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class SupervisionFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "a.py").write_text("value = 1\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"HOME": str(base / "home"), "BATON_HOME": str(base / "state"),
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
        return store

    def make_probe(self):
        store = self.running_leg()
        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        commit = store.snapshot("probe")
        store.git("update-ref", "refs/baton/t/probe", commit)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            probe_commit=commit, probe_ref="refs/baton/t/probe"))

    @staticmethod
    def finding(fid, category, severity="blocking"):
        return {"id": fid, "category": category, "severity": severity,
                "description": "检查发现", "evidence": [{"location": "evidence.md", "excerpt": "证据"}],
                "rule_source": "测试"}

    def apply(self, findings, conclusion="breaking"):
        self.make_probe()
        state = self.project.load_state("t")
        leg = baton.current_leg(state)
        dossier = baton.build_check_dossier(self.project, "t", state, leg)
        self.assertTrue(dossier)
        value = {"conclusion": conclusion, "summary": "检查", "findings": findings}
        baton.apply_monitor_check_result(self.project, "t", dossier, value, leg["n"])
        return dossier

    def test_acceptance_and_record_gaps_are_notes_and_do_not_stop_leg(self):
        dossier = self.apply([self.finding("F1", "acceptance_gap"),
                              self.finding("F2", "missing_record")])
        state = self.project.load_state("t")
        leg = baton.current_leg(state)
        self.assertEqual(leg["status"], "running")
        self.assertNotIn("supervision_action", state)
        self.assertFalse((Path(self.project.leg_path("t", 1, "stop"))).exists())
        self.assertEqual(leg["monitor_check"]["conclusion"], "on_track")
        self.assertEqual(leg["monitor_check"]["original_conclusion"], "breaking")
        self.assertEqual(leg["monitor_check"]["downgrade"]["finding_ids"], ["F1", "F2"])
        record = [item for item in baton.ledger_records(self.project) if item.get("type") == "check"][-1]
        self.assertEqual(record["conclusion"], "on_track")
        self.assertEqual(record["original_conclusion"], "breaking")
        self.assertEqual(record["downgrade"]["reason"], "轮中检查不把验收缺口或记录缺口当作方向偏离")
        self.assertEqual([item["severity"] for item in record["findings"]], ["note", "note"])
        self.assertFalse((Path(self.project.root) / ".baton" / "reviews" / (dossier + "-brief.md")).exists())

    def test_breaking_signal_still_stops_leg(self):
        self.apply([self.finding("F1", "breaking_signal")])
        state = self.project.load_state("t")
        self.assertEqual(state["supervision_action"], "breaking")
        self.assertTrue(Path(self.project.leg_path("t", 1, "stop")).exists())
        self.assertEqual(baton.current_leg(state)["monitor_check"]["conclusion"], "breaking")

    def test_note_only_acceptance_and_record_gaps_do_not_stop_breaking_leg(self):
        self.apply([self.finding("F1", "acceptance_gap", "note"),
                    self.finding("F2", "missing_record", "note")])
        state = self.project.load_state("t")
        leg = baton.current_leg(state)
        self.assertEqual(leg["monitor_check"]["conclusion"], "on_track")
        self.assertEqual(leg["monitor_check"]["original_conclusion"], "breaking")
        self.assertEqual(leg["monitor_check"]["downgrade"]["finding_ids"], ["F1", "F2"])
        self.assertNotIn("supervision_action", state)
        self.assertFalse(Path(self.project.leg_path("t", 1, "stop")).exists())

    def test_capacity_failure_patterns_and_quota_exclusions(self):
        events = Path(self.tmp.name) / "capacity-events.jsonl"
        stderr = Path(self.tmp.name) / "capacity-stderr.log"
        positive = ("at capacity", "overloaded", "server_is_overloaded",
                    "servers are currently overloaded", "HTTP 529", "status 529",
                    "503 Service Unavailable")
        negative = ("usage limit", "quota exceeded", "insufficient_quota", "额度")
        for sample in positive:
            events.write_text(sample + "\n", encoding="utf-8")
            stderr.write_text("", encoding="utf-8")
            with self.subTest(sample=sample):
                self.assertTrue(baton.is_capacity_failure(str(events), str(stderr), returncode=1))
        for sample in negative:
            events.write_text(sample + "\n", encoding="utf-8")
            with self.subTest(sample=sample):
                self.assertFalse(baton.is_capacity_failure(str(events), str(stderr), returncode=1))

    def test_capacity_detector_reads_non_json_hermes_event_lines(self):
        events = Path(self.tmp.name) / "hermes-events.jsonl"
        stderr = Path(self.tmp.name) / "hermes-stderr.log"
        events.write_text("API call failed after 3 retries: Our servers are currently overloaded. Please try again later.\n",
                          encoding="utf-8")
        stderr.write_text("", encoding="utf-8")
        self.assertTrue(baton.is_capacity_failure(str(events), str(stderr), returncode=1))

    def unfreeze_scope(self, path):
        return {"allow": ["*"], "deny": [], "acceptance": [], "frozen": [],
                "unfreeze": [path], "verify": "none"}

    def unfreeze_cfg(self):
        cfg = json.loads(json.dumps(self.project.config))
        cfg["supervision"]["test_command"] = "python -m unittest discover -s tests"
        return cfg

    def test_unfreeze_is_optional_but_glob_is_rejected(self):
        scope = baton.parse_scope("```baton-scope\n" + json.dumps({
            "allow": [], "deny": [], "acceptance": [], "frozen": []}) + "\n```")
        self.assertEqual(scope["unfreeze"], [])
        with self.assertRaisesRegex(baton.ConfigError, "unfreeze"):
            baton.parse_scope("```baton-scope\n" + json.dumps({
                "allow": [], "deny": [], "acceptance": [], "frozen": [], "unfreeze": ["tests/*.py"]
            }) + "\n```")

    def test_unfreeze_requires_exact_existing_test_file_and_rejects_config(self):
        (self.root / "tests").mkdir()
        (self.root / "tests" / "test_api.py").write_text("def test_api():\n    assert True\n", encoding="utf-8")
        cfg = self.unfreeze_cfg()
        self.assertEqual(baton._validate_unfreeze(self.project, self.unfreeze_scope("tests/test_api.py"), cfg),
                         ["tests/test_api.py"])
        invalid = ["tests", "tests/missing.py", "tests/conftest.py"]
        (self.root / "tests" / "conftest.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "tests" / "README.md").write_text("说明\n", encoding="utf-8")
        invalid.append("tests/README.md")
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(baton.ConfigError):
                baton._validate_unfreeze(self.project, self.unfreeze_scope(path), cfg)

    def test_unfreeze_file_is_not_freeze_violation_but_deleted_test_alert_remains(self):
        test_path = self.root / "tests" / "test_api.py"
        test_path.parent.mkdir()
        test_path.write_text("def test_api():\n    assert True\n", encoding="utf-8")
        scope = self.unfreeze_scope("tests/test_api.py")
        cfg = self.unfreeze_cfg()
        manifest = baton.write_freeze_baseline(self.project, "t", scope, cfg)
        self.assertNotIn("tests/test_api.py", {entry["path"] for entry in manifest["entries"]})
        st = {"task": "t", "scope": scope, "freeze_manifest": manifest, "config_snapshot": cfg}
        test_path.write_text("def test_api():\n    assert 1\n", encoding="utf-8")
        self.assertFalse(baton.check_freeze(self.project, "t", st)["violations"])
        self.project.update_state("t", lambda s: s.update(scope=scope, freeze_manifest=manifest,
                                                           config_snapshot=cfg, legs=[]))
        self.running_leg()
        test_path.unlink()
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("deleted_test_file", {item["rule"] for item in alerts})

    def test_start_unfreeze_requires_user_approval_before_any_state_change(self):
        test_path = self.root / "tests" / "test_api.py"
        test_path.parent.mkdir()
        test_path.write_text("def test_api():\n    assert True\n", encoding="utf-8")
        cfg = self.unfreeze_cfg()
        self.project.update_state("t", lambda s: s.update(config_snapshot=cfg))
        brief = self.root / ".baton" / "tasks" / "t" / "brief.md"
        brief.write_text("```baton-scope\n" + json.dumps(self.unfreeze_scope("tests/test_api.py"))
                          + "\n```\n# t\n", encoding="utf-8")
        before = Path(self.project.state_path("t")).read_bytes()
        with patch.object(baton, "_require_task_confirmation") as confirm, \
                self.assertRaises(SystemExit):
            baton.cmd_start(self.project, argparse.Namespace(task="t", user_approved=False))
        self.assertEqual(Path(self.project.state_path("t")).read_bytes(), before)
        confirm.assert_not_called()

        with patch.object(baton, "_require_task_confirmation") as confirm, \
                patch.object(baton, "launch_leg"):
            baton.cmd_start(self.project, argparse.Namespace(task="t", user_approved=True))
        confirm.assert_called_once_with("t")
        state = self.project.load_state("t")
        self.assertEqual(state["unfreeze"], ["tests/test_api.py"])
        charter = baton._charter_text(self.project, "t", state)
        self.assertIn("tests/test_api.py", charter)

    def test_non_python_test_file_can_unfreeze_and_deleted_test_alert_remains(self):
        test_path = self.root / "tests" / "test_x.sh"
        test_path.parent.mkdir()
        test_path.write_text("#!/bin/sh\ntrue\n", encoding="utf-8")
        scope = self.unfreeze_scope("tests/test_x.sh")
        cfg = self.unfreeze_cfg()
        self.assertEqual(baton._validate_unfreeze(self.project, scope, cfg), ["tests/test_x.sh"])
        manifest = baton.write_freeze_baseline(self.project, "t", scope, cfg)
        self.assertNotIn("tests/test_x.sh", {entry["path"] for entry in manifest["entries"]})
        self.project.update_state("t", lambda s: s.update(scope=scope, freeze_manifest=manifest,
                                                           config_snapshot=cfg, legs=[]))
        self.running_leg()
        test_path.unlink()
        state = self.project.load_state("t")
        alerts = baton.run_rule_checks(self.project, "t", state, baton.current_leg(state))
        self.assertIn("deleted_test_file", {item["rule"] for item in alerts})

    def build_report_evidence(self, append_log=None, status="report"):
        store = self.project.store("t")
        base = store.snapshot("report base")
        log_start = baton.file_size(self.project.p("log.md"))
        if append_log:
            with open(self.project.p("log.md"), "a", encoding="utf-8") as stream:
                stream.write(append_log)
        (self.root / "a.py").write_text("value = 2\n", encoding="utf-8")
        end = store.snapshot("report end")
        leg = {"n": 1, "status": status, "executor_status": status,
               "result": {"status": status, "summary": "executor", "artifact": ""},
               "start_ref": base, "end_ref": end, "events": "", "diff": baton.shortstat(store, base, end),
               "verify": {"skipped": True, "skip_reason": "本任务没有 baton 亲跑的验证"},
               "started_at": baton.now(), "log_size_at_start": log_start}
        self.project.update_state("t", lambda s: s.update(base_ref=base, legs=[leg], status="idle"))
        dossier = baton.build_dossier(self.project, "t", self.project.load_state("t"), leg, status)
        return Path(self.project.state_dir) / "dossiers" / dossier / "evidence.md"

    def test_report_dossier_includes_log_added_since_leg_start(self):
        evidence = self.build_report_evidence("\n## test log entry\nreport log content\n")
        text = evidence.read_text(encoding="utf-8")
        self.assertIn("## log.md 新增", text)
        self.assertIn("report log content", text)

    def test_done_dossier_includes_log_added_since_leg_start(self):
        evidence = self.build_report_evidence("\n## final log entry\nfinal log content\n", status="done")
        self.assertIn("final log content", evidence.read_text(encoding="utf-8"))

    def test_check_dossier_includes_log_added_since_supervision_offset(self):
        self.make_probe()
        offset = baton.file_size(self.project.p("log.md"))
        self.project.update_state("t", lambda s: s.update(supervision={"leg": 1, "log_offset": offset}))
        with open(self.project.p("log.md"), "a", encoding="utf-8") as stream:
            stream.write("\n## midleg log entry\nmidleg log content\n")
        state = self.project.load_state("t")
        dossier = baton.build_check_dossier(self.project, "t", state, baton.current_leg(state))
        evidence = Path(self.project.state_dir) / "dossiers" / dossier / "evidence.md"
        text = evidence.read_text(encoding="utf-8")
        self.assertIn("midleg log content", text)

    def test_log_evidence_reports_no_change_truncation_and_tail(self):
        offset = baton.file_size(self.project.p("log.md"))
        self.assertIn("本轮无新增", "\n".join(baton._log_evidence_lines(self.project, offset)))
        with open(self.project.p("log.md"), "a", encoding="utf-8") as stream:
            stream.write("HEAD" * 3000 + "TAIL" * 3000)
        evidence = "\n".join(baton._log_evidence_lines(self.project, offset))
        self.assertIn("超过 8000 字符", evidence)
        self.assertIn("TAIL", evidence)
        self.assertNotIn("HEAD", evidence)
        Path(self.project.p("log.md")).write_text("short\n", encoding="utf-8")
        truncated = "\n".join(baton._log_evidence_lines(self.project, offset))
        self.assertIn("log.md 被改写或截断", truncated)


if __name__ == "__main__":
    unittest.main()
