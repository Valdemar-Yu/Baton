from platform_fixtures import wait_windows_pid
from platform_fixtures import executable_fixture
import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


def wait_for_pid(pid, timeout=30):
    if os.name == "nt":
        return wait_windows_pid(pid, timeout)
    if not pid:
        return
    deadline = baton.now() + timeout
    while baton.now() < deadline:
        try:
            waited, _ = os.waitpid(pid, os.WNOHANG)
            if waited == pid:
                return
        except ChildProcessError:
            pass
        if not baton.pid_alive(pid):
            return
        import time
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


class RulingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "project"
        self.root.mkdir()
        (self.root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.env = patch.dict(os.environ, {"HOME": str(base / "home"), "USERPROFILE": str(base / "home"), "BATON_HOME": str(base / "state"),
                                           "BATON_DESKTOP_NOTIFY": "0", "PYTHONDONTWRITEBYTECODE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        (self.root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        self.project = baton.Project(str(self.root))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(self.project, Namespace(no_statusline=True, accept_boundary=True,
                                                   relink=False, fresh=False))
        task_dir = self.root / ".baton" / "tasks" / "t"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": ["a.py"], "deny": [], "acceptance": [],
                                                 "frozen": [], "verify": "none"}) + "\n```\n# t\n",
            encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(task="t", status="idle", legs=[],
                                                           snapshot_store="shadow", scope={
                                                               "allow": ["a.py"], "deny": [], "acceptance": [],
                                                               "frozen": [], "verify": "none"}))

    def make_dossier(self, kind="report", dossier_id=None, findings=None, report_text=""):
        store = baton.SnapStore(self.project, "shadow")
        base = store.snapshot("base")
        (self.root / "a.py").write_text("x = 2\n", encoding="utf-8")
        end = store.snapshot("end")
        leg = {"n": 1, "status": "done" if kind == "final" else kind,
               "executor_status": "done" if kind == "final" else kind,
               "result": {"status": "done" if kind == "final" else kind, "summary": "executor", "artifact": ""},
               "start_ref": base, "end_ref": end, "events": "", "started_at": baton.now(),
               "verify": {"skipped": True, "skip_reason": "本任务没有 baton 亲跑的验证"}}
        if dossier_id:
            leg["dossier"] = {"id": dossier_id, "type": kind}
        self.project.update_state("t", lambda s: s.update(legs=[leg], base_ref=base, status="idle"))
        if report_text:
            artifact = Path(self.tmp.name) / "report.html"
            artifact.write_text(report_text, encoding="utf-8")
            leg["result"]["artifact"] = str(artifact)
        st = self.project.load_state("t")
        dossier = baton.build_dossier(self.project, "t", st, leg, "done" if kind == "final" else kind)
        if dossier_id and dossier != dossier_id:
            raise AssertionError((dossier, dossier_id))
        brief = {"summary": "brief", "recommendation": {
            "report": "APPROVE", "final": "ACCEPT", "violation": "REVERT"}[kind],
            "findings": findings or [], "memory_advice": []}
        text = baton.render_monitor_brief(self.project, "t", dossier, brief)
        manifest = baton.dossier_manifest(self.project, dossier)
        baton.append_brief_ledger(self.project, "t", dossier, manifest["total_sha256"], text,
                                  brief_json=brief)
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            dossier={"id": dossier, "type": kind},
            monitor={"id": dossier, "status": "completed"}))
        return dossier, brief, base, end

    def verdict_file(self, conclusion, findings, body=""):
        path = Path(self.tmp.name) / "verdict.md"
        path.write_text("```baton-verdict\n" + json.dumps({"conclusion": conclusion, "findings": findings},
                                                                  ensure_ascii=False) + "\n```\n" + body,
                        encoding="utf-8")
        return path

    def rule(self, dossier, conclusion, findings, **kw):
        return baton.cmd_rule(self.project, Namespace(task="t", dossier=dossier,
                                                       verdict_file=str(self.verdict_file(conclusion, findings)),
                                                       rerule=kw.get("rerule", False)))

    def test_verdict_parser_requires_exact_findings_and_reason(self):
        brief = {"findings": [{"id": "F1", "category": "out_of_scope", "severity": "note"}]}
        good = "```baton-verdict\n" + json.dumps({"conclusion": "APPROVE", "findings": {
            "F1": {"decision": "reject", "reason": "理由"}}}) + "\n```\n"
        self.assertEqual(baton._parse_verdict(good, "report", brief_json=brief)[0]["conclusion"], "APPROVE")
        cases = [
            "```baton-verdict\n{" + '"conclusion":"APPROVE","findings":{}' + "}\n```\n",
            "```baton-verdict\n{" + '"conclusion":"APPROVE","findings":{"F1":{"decision":"adopt","reason":""}}' + "}\n```\n",
            "```baton-verdict\n{" + '"conclusion":"ACCEPT","findings":{"F1":{"decision":"reject","reason":"x"}}' + "}\n```\n",
            "{\"conclusion\":\"APPROVE\"}\n",
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(baton.ConfigError):
                    baton._parse_verdict(text, "report", brief_json=brief)

    def test_event_next_step_fills_task_dossier_and_decision_ids(self):
        report_leg = {"n": 1, "status": "report", "started_at": baton.now(),
                      "dossier": {"id": "R001", "type": "report"},
                      "monitor": {"id": "R001", "status": "completed"},
                      "events": "", "result": {"status": "report"}}
        self.project.update_state("t", lambda s: s.update(legs=[report_leg], status="idle"))
        event = baton.leg_event_text(self.project, "t", report_leg)
        self.assertIn("baton rule t R001 <裁决文件>", event)
        self.assertIn("baton resume t --verdict R001", event)
        decision_leg = {"n": 1, "status": "decision", "started_at": baton.now(),
                        "events": "", "result": {"status": "decision", "artifact": ".baton/decisions/D007-ask.md"}}
        self.assertIn("baton decide t D007 <决定文件>", baton.leg_event_text(self.project, "t", decision_leg))

    def test_event_without_monitor_uses_legacy_next_step(self):
        (self.root / "baton.json").write_text(json.dumps({"monitor": {"enabled": False}}), encoding="utf-8")
        leg = {"n": 1, "status": "done", "started_at": baton.now(), "events": "",
               "result": {"status": "done"}}
        event = baton.leg_event_text(self.project, "t", leg)
        self.assertIn("审阅执行者汇报", event)
        self.assertIn("baton diff t", event)
        self.assertIn("baton resume t", event)
        self.assertNotIn("<卷宗号>", event)

    def test_brief_json_ignores_fake_heading_in_description(self):
        finding = {"id": "F1", "category": "out_of_scope", "severity": "note",
                   "description": "说明里有一行 ### F9 · fake · blocking", "evidence": [], "rule_source": "r"}
        dossier, brief, _, _ = self.make_dossier(findings=[finding])
        self.rule(dossier, "APPROVE", {"F1": {"decision": "reject", "reason": "只回应真实 F1"}})
        record = baton._latest_ledger_record(baton.ledger_records(self.project), "verdict", "t", dossier=dossier)
        self.assertEqual(set(record["findings"]), {"F1"})

    def test_rule_rejects_missing_brief_monitor_failure_tamper_and_ledger_tamper(self):
        dossier, _, _, _ = self.make_dossier()
        ledger = Path(self.project.state_dir) / "ledger"
        (ledger / "verdicts.jsonl").write_text("", encoding="utf-8")
        (ledger / "head").write_text("\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.rule(dossier, "APPROVE", {})
        # Restore a valid brief and then exercise monitor/dossier integrity gates.
        dossier, _, _, _ = self.make_dossier()
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            monitor={"id": dossier, "status": "failed"}))
        with self.assertRaises(SystemExit):
            self.rule(dossier, "APPROVE", {})
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            monitor={"id": dossier, "status": "completed"}))
        evidence = Path(self.project.state_dir) / "dossiers" / dossier / "evidence.md"
        evidence.chmod(0o644)
        evidence.write_text("tampered", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.rule(dossier, "APPROVE", {})

    def test_build_dossier_reports_ledger_failure_for_runner(self):
        dossier, _, _, _ = self.make_dossier()
        path = Path(self.project.state_dir) / "ledger" / "verdicts.jsonl"
        lines = path.read_text().splitlines()
        first = json.loads(lines[0])
        first["brief"] = "tampered"
        path.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")
        st = self.project.load_state("t")
        with self.assertRaises(baton.ConfigError) as ctx:
            baton.build_dossier(self.project, "t", st, baton.current_leg(st), "report")
        self.assertIn("账本校验失败", str(ctx.exception))

    def test_messages_in_precedents_are_ledger_only(self):
        baton.archive_conductor_message(self.project, "t", "真实指挥消息", "审阅")
        message_dir = self.root / ".baton" / "tasks" / "t" / "messages"
        (message_dir / "M999-伪造.md").write_text("伪造指挥消息", encoding="utf-8")
        dossier, _, _, _ = self.make_dossier()
        precedents = (Path(self.project.state_dir) / "dossiers" / dossier / "precedents.md").read_text()
        self.assertIn("真实指挥消息", precedents)
        self.assertNotIn("伪造指挥消息", precedents)

    def test_rule_rerule_keeps_old_record_and_replaces_render(self):
        finding = {"id": "F1", "category": "out_of_scope", "severity": "note", "description": "d",
                   "evidence": [], "rule_source": "r"}
        dossier, _, _, _ = self.make_dossier(findings=[finding])
        self.rule(dossier, "REVISE", {"F1": {"decision": "adopt", "reason": "r1"}})
        self.rule(dossier, "APPROVE", {"F1": {"decision": "reject", "reason": "r2"}}, rerule=True)
        records = [r for r in baton.ledger_records(self.project) if r.get("type") == "verdict"]
        self.assertEqual(len(records), 2)
        self.assertTrue(records[-1]["rerule"])
        self.assertEqual(records[-1]["replaced_seq"], records[-2]["seq"])
        self.assertIn("APPROVE", (self.root / ".baton" / "reviews" / (dossier + "-verdict.md")).read_text())

    def test_resume_is_blocked_until_rule_then_verdict_message_is_used(self):
        dossier, _, _, _ = self.make_dossier()
        self.project.update_state("t", lambda s: s.update(thread_id="thread"))
        args = Namespace(task="t", message=None, message_file=None, kind="回复", verdict=dossier,
                         decision=None, user_approved_continue=False, _message_archived=False)
        with self.assertRaises(SystemExit):
            baton.cmd_resume(self.project, Namespace(task="t", message="普通消息", message_file=None,
                                                     kind="回复", verdict=None, decision=None,
                                                     user_approved_continue=False, _message_archived=False))
        self.rule(dossier, "APPROVE", {})
        with patch.object(baton, "launch_leg") as launch:
            baton.cmd_resume(self.project, args)
        launch.assert_called_once()
        self.assertIn("# 裁决", launch.call_args.args[3])
        messages = [r for r in baton.ledger_records(self.project) if r.get("type") == "message"]
        self.assertEqual(len(messages), 1)

    def test_violation_conclusion_hints(self):
        dossier, _, _, _ = self.make_dossier(kind="violation")
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            violations=[{"path": "a.py", "type": "改动"}]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.rule(dossier, "REVERT", {})
        self.assertIn("restore-frozen", output.getvalue())
        dossier2, _, _, _ = self.make_dossier(kind="violation")
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            violations=[{"path": "a.py", "type": "改动"}]))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.rule(dossier2, "ADOPT", {}, rerule=True)
        self.assertIn("--adopt a.py", output.getvalue())

    def test_ask_user_violation_blocks_resume(self):
        dossier, _, _, _ = self.make_dossier(kind="violation")
        self.rule(dossier, "ASK_USER", {})
        self.project.update_state("t", lambda s: s.update(thread_id="thread"))
        with self.assertRaises(SystemExit):
            baton.cmd_resume(self.project, Namespace(task="t", message="x", message_file=None, kind="回复",
                                                     verdict=None, decision=None, user_approved_continue=False,
                                                     _message_archived=False))

    def test_revision_count_uses_latest_dossiers_and_original_item(self):
        # Use ledger records directly: R003 itself and two reports explicitly revised from it.
        for dossier, text in (("R003", "原始汇报"), ("R004", "修订自 R003"), ("R005", "修订自 R003")):
            root = Path(self.project.state_dir) / "dossiers" / dossier
            root.mkdir(parents=True)
            (root / "report.html").write_text(text, encoding="utf-8")
            baton.append_ledger(self.project, {"type": "verdict", "task": "t", "dossier": dossier,
                                               "dossier_hash": "h", "brief_seq": 1, "brief_record_hash": "b",
                                               "conclusion": "REVISE", "findings": {}, "verdict": "v",
                                               "rendered": "v"})
        counts = baton._revision_counts(self.project, "t")
        self.assertEqual(counts["R003"], 3)
        baton.append_ledger(self.project, {"type": "verdict", "task": "t", "dossier": "R005",
                                           "dossier_hash": "h", "brief_seq": 1, "brief_record_hash": "b",
                                           "conclusion": "APPROVE", "findings": {}, "verdict": "v",
                                           "rendered": "v", "rerule": True, "replaced_seq": 3})
        self.assertEqual(baton._revision_counts(self.project, "t")["R003"], 2)

    def test_revision_gate_requires_tty_confirmation_after_three(self):
        for dossier, text in (("R003", "原始"), ("R004", "修订自 R003"), ("R005", "修订自 R003")):
            root = Path(self.project.state_dir) / "dossiers" / dossier
            root.mkdir(parents=True)
            (root / "report.html").write_text(text, encoding="utf-8")
            baton.append_ledger(self.project, {"type": "verdict", "task": "t", "dossier": dossier,
                                               "conclusion": "REVISE", "findings": {}, "verdict": "v",
                                               "rendered": "v"})
        self.project.update_state("t", lambda s: s.update(thread_id="thread", legs=[{
            "n": 1, "status": "report", "executor_status": "report", "started_at": baton.now(),
            "dossier": {"id": "R005", "type": "report"}, "monitor": {"id": "R005", "status": "completed"},
            "events": "", "result": {"status": "report"}}]))
        args = Namespace(task="t", message="继续", message_file=None, kind="回复", verdict=None,
                         decision=None, user_approved_continue=False, _message_archived=False)
        with patch.object(baton, "_resume_ruling_gate", return_value=None), self.assertRaises(SystemExit):
            baton.cmd_resume(self.project, args)
        args.user_approved_continue = True
        with patch.object(baton, "_resume_ruling_gate", return_value=None), \
                patch.object(baton, "_require_task_confirmation") as confirm, \
                patch.object(baton, "launch_leg"):
            baton.cmd_resume(self.project, args)
        confirm.assert_called_once_with("t")

    def test_decide_freezes_request_and_decision_message(self):
        self.project.p("decisions")
        request_dir = self.root / ".baton" / "decisions"
        request_dir.mkdir(parents=True, exist_ok=True)
        request = request_dir / "D001-ask.md"
        request.write_text("请决定", encoding="utf-8")
        decision = Path(self.tmp.name) / "decision.md"
        decision.write_text("按选项 A 继续。", encoding="utf-8")
        self.project.update_state("t", lambda s: s.update(legs=[{"n": 1, "status": "decision", "started_at": baton.now(),
            "events": "", "result": {"status": "decision"}}], status="idle"))
        baton.cmd_decide(self.project, Namespace(task="t", decision_id="D001", decision_file=str(decision), rerule=False))
        rec = [r for r in baton.ledger_records(self.project) if r.get("type") == "decision"][-1]
        self.assertTrue(Path(rec["request_path"]).is_file())
        self.assertEqual(Path(rec["request_path"]).read_text(), "请决定")
        self.assertIn("按选项 A", (self.root / ".baton" / "reviews" / "D001-decision.md").read_text())

    def test_close_rejects_diff_error(self):
        finding = {"id": "F1", "category": "out_of_scope", "severity": "note", "description": "d",
                   "evidence": [], "rule_source": "r"}
        dossier, _, _, _ = self.make_dossier(kind="final", findings=[finding])
        self.rule(dossier, "ACCEPT", {"F1": {"decision": "reject", "reason": "理由"}})
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            verify={"skipped": False, "exit_code": 0, "acceptance_failed": []}))
        store = self.project.store("t", self.project.load_state("t"))
        original = store.git
        def fail_diff(*args, **kwargs):
            if args and args[0] == "diff":
                raise RuntimeError("diff failed")
            return original(*args, **kwargs)
        with patch.object(self.project, "store", return_value=store), \
                patch.object(store, "git", side_effect=fail_diff), self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t"))
        self.assertNotEqual(self.project.load_state("t").get("status"), "closed")

    def test_close_success_lists_rejected_findings_and_closed_blocks_resume(self):
        finding = {"id": "F1", "category": "out_of_scope", "severity": "blocking", "description": "d",
                   "evidence": [], "rule_source": "r"}
        report, _, _, _ = self.make_dossier(kind="report")
        dossier, _, _, _ = self.make_dossier(kind="final", findings=[finding])
        self.rule(dossier, "ACCEPT", {"F1": {"decision": "reject", "reason": "保留记录"}})
        self.project.update_state("t", lambda s: baton.current_leg(s).update(
            verify={"skipped": False, "exit_code": 0, "acceptance_failed": []}))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_close(self.project, Namespace(task="t"))
        self.assertIn("已 closed", output.getvalue())
        self.assertIn("F1 · out_of_scope · blocking", output.getvalue())
        root = Path(baton.dossier_path(self.project, dossier))
        self.assertFalse((root / "code").exists())
        self.assertFalse((Path(baton.dossier_path(self.project, report)) / "code").exists())
        self.assertTrue((root / "charter.md").exists())
        self.assertTrue(baton.verify_dossier(self.project, dossier)[0])
        self.assertTrue(baton.verify_ledger(self.project))
        brief, _ = baton._dossier_brief(self.project, "t", dossier)
        self.assertIsNotNone(brief)
        with self.assertRaises(SystemExit):
            baton.cmd_resume(self.project, Namespace(task="t", message="x", message_file=None, kind="回复",
                                                     verdict=None, decision=None, user_approved_continue=False,
                                                     _message_archived=False))

    def test_monitor_rerun_rebuilds_dossier_missing_manifest(self):
        dossier = self.make_dossier(kind="report")[0]
        root = Path(baton.dossier_path(self.project, dossier))
        baton._unseal_dossier(str(root))
        (root / "manifest.json").unlink()
        with patch.object(baton.platform_support, "detached_popen", return_value=Mock(pid=1)),                 contextlib.redirect_stdout(io.StringIO()) as output:
            baton.cmd_monitor(self.project, Namespace(task="t", dossier=dossier, rerun=True, curate=False))
        self.assertIn("已重建卷宗", output.getvalue())
        self.assertTrue(baton.verify_dossier(self.project, dossier)[0])
        self.assertEqual(baton.current_leg(self.project.load_state("t"))["dossier"]["id"], dossier)

    def test_close_abandon_without_final_dossier_records_and_prunes(self):
        report, _, _, _ = self.make_dossier(kind="report")
        with self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t"))
        with self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t", abandon="  "))
        self.assertNotEqual(self.project.load_state("t").get("status"), "closed")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_close(self.project, Namespace(task="t", abandon="以 REPORT 结束"))
        self.assertIn("已按放弃 closed", output.getvalue())
        st = self.project.load_state("t")
        self.assertEqual(st["status"], "closed")
        self.assertTrue(st["closed"]["abandoned"])
        rec = [r for r in baton.ledger_records(self.project) if r.get("type") == "abandon"][-1]
        self.assertEqual((rec["task"], rec["reason"], rec["seq"]), ("t", "以 REPORT 结束", st["closed"]["ledger_seq"]))
        self.assertTrue(baton.verify_ledger(self.project))
        root = Path(baton.dossier_path(self.project, report))
        self.assertFalse((root / "code").exists())
        self.assertTrue(baton.verify_dossier(self.project, report)[0])
        with self.assertRaises(SystemExit):
            baton.cmd_close(self.project, Namespace(task="t", abandon="again"))

    def test_real_cli_end_to_end_report_rule_resume_final_close(self):
        # This uses only a temporary project, HOME/BATON_HOME and a fake codex executable.
        cli_root = Path(self.tmp.name) / "cli-project"
        cli_root.mkdir()
        subprocess.run(["git", "init", "-q", str(cli_root)], check=True)
        env = os.environ.copy()
        env.update({"HOME": str(Path(self.tmp.name) / "cli-home"), "USERPROFILE": str(Path(self.tmp.name) / "cli-home"),
                    "BATON_HOME": str(Path(self.tmp.name) / "cli-state"),
                    "BATON_DESKTOP_NOTIFY": "0"})
        bindir = Path(self.tmp.name) / "cli-bin"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
out = args[args.index('-o') + 1]
if '--ignore-user-config' in args:
    manifest = json.loads(pathlib.Path('manifest.json').read_text())
    if manifest.get('type') == 'final':
        value = {'summary':'final','recommendation':'ACCEPT','findings':[],'memory_advice':[]}
    else:
        value = {'summary':'report','recommendation':'APPROVE','findings':[{'id':'F1','category':'out_of_scope','severity':'note','description':'偏离','evidence':[],'rule_source':'scope'}],'memory_advice':[]}
    pathlib.Path(out).write_text(json.dumps(value, ensure_ascii=False))
else:
    print(json.dumps({'type':'thread.started','thread_id':'fake-thread'}), flush=True)
    if 'resume' not in args:
        report = pathlib.Path('.baton/reports/R003-report.html')
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('<h1>R003</h1>')
        value = {'status':'report','summary':'report','artifact':str(report),'change_size':'minor'}
    else:
        value = {'status':'done','summary':'done','artifact':'','change_size':'minor'}
    pathlib.Path(out).write_text(json.dumps(value, ensure_ascii=False))
""", encoding="utf-8")
        fake = executable_fixture(fake)
        env["PATH"] = str(bindir) + os.pathsep + env.get("PATH", "")
        def run(*args):
            return subprocess.run([sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(cli_root), *args],
                                  env=env, capture_output=True, text=True, timeout=30)
        def diagnostics(label):
            state_path = Path(baton.Project(str(cli_root)).state_path("t"))
            state_tail = state_path.read_text(encoding="utf-8")[-4000:] if state_path.exists() else "<no state.json>"
            run_logs = list((cli_root / ".baton" / "runs" / "t").glob("leg-*.runner.log"))
            log_tail = "\n".join(path.name + ":\n" + path.read_text(encoding="utf-8")[-2000:]
                                  for path in run_logs) if run_logs else "<no runner.log>"
            return f"{label}\nstate.json tail:\n{state_tail}\nrunner.log tail:\n{log_tail}"

        def run_checked(*args):
            result = run(*args)
            if result.returncode != 0:
                self.fail(diagnostics("CLI failed: " + " ".join(args)) +
                          "\nstdout:\n" + result.stdout[-4000:] + "\nstderr:\n" + result.stderr[-4000:])
            return result

        (cli_root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        run_checked("init", "--no-statusline")
        task = cli_root / ".baton" / "tasks" / "t"
        task.mkdir(parents=True, exist_ok=True)
        (task / "brief.md").write_text("```baton-scope\n" + json.dumps({
            "allow": ["a.py"], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}) + "\n```\n# t\n",
            encoding="utf-8")
        (cli_root / "a.py").write_text("x=1\n")
        run_checked("start", "t")
        os.environ.update({"HOME": env["HOME"], "BATON_HOME": env["BATON_HOME"]})
        project = baton.Project(str(cli_root))
        background_pids = set()
        self.addCleanup(lambda: [wait_for_pid(pid, timeout=60) for pid in background_pids])

        def wait_background_state(timeout=60):
            deadline = baton.now() + timeout
            while baton.now() < deadline:
                state = baton.refresh_runner_liveness(project, "t", project.read_state("t", {}) or {})
                leg = baton.current_leg(state) or {}
                if leg.get("runner_pid"):
                    background_pids.add(leg["runner_pid"])
                if (leg.get("monitor") or {}).get("pid"):
                    background_pids.add((leg.get("monitor") or {})["pid"])
                if (state.get("curation_monitor") or {}).get("pid"):
                    background_pids.add((state.get("curation_monitor") or {})["pid"])
                running = (leg.get("status") == "running"
                           or (leg.get("monitor") or {}).get("status") == "running"
                           or (state.get("curation_monitor") or {}).get("status") == "running")
                if not running:
                    return state
                import time
                time.sleep(0.1)
            return project.load_state("t")

        deadline = baton.now() + 30
        while baton.now() < deadline:
            state = project.read_state("t", {}) or {}
            leg = baton.current_leg(state) or {}
            if leg.get("runner_pid"):
                background_pids.add(leg["runner_pid"])
            if (leg.get("monitor") or {}).get("pid"):
                background_pids.add((leg.get("monitor") or {})["pid"])
            if (state.get("curation_monitor") or {}).get("pid"):
                background_pids.add((state.get("curation_monitor") or {})["pid"])
            if leg.get("status") != "running" and leg.get("dossier") and \
                    (leg.get("monitor") or {}).get("status") != "running" and \
                    (state.get("curation_monitor") or {}).get("status") != "running":
                break
            import time
            time.sleep(0.05)
        state = project.load_state("t")
        leg = baton.current_leg(state)
        if "dossier" not in leg:
            log = cli_root / ".baton" / "runs" / "t" / "leg-001.runner.log"
            self.fail("runner state missing dossier: " + json.dumps(state, ensure_ascii=False) + "\n" +
                      (log.read_text() if log.exists() else "no runner log"))
        dossier = leg["dossier"]["id"]
        run_checked("wait", "t", "--tick", "0")
        verdict = Path(self.tmp.name) / "report-verdict.md"
        verdict.write_text("```baton-verdict\n" + json.dumps({"conclusion":"APPROVE","findings":{
            "F1":{"decision":"reject","reason":"按章程拒绝"}}}, ensure_ascii=False) + "\n```\n", encoding="utf-8")
        run_checked("rule", "t", dossier, str(verdict))
        run_checked("resume", "t", "--verdict", dossier)
        deadline = baton.now() + 30
        while baton.now() < deadline:
            state = project.read_state("t", {}) or {}
            leg = baton.current_leg(state) or {}
            if leg.get("runner_pid"):
                background_pids.add(leg["runner_pid"])
            if (leg.get("monitor") or {}).get("pid"):
                background_pids.add((leg.get("monitor") or {})["pid"])
            if (state.get("curation_monitor") or {}).get("pid"):
                background_pids.add((state.get("curation_monitor") or {})["pid"])
            if leg.get("status") != "running" and leg.get("dossier") and \
                    (leg.get("monitor") or {}).get("status") != "running" and \
                    (state.get("curation_monitor") or {}).get("status") != "running":
                break
            import time
            time.sleep(0.05)
        state = project.load_state("t")
        leg = baton.current_leg(state) or {}
        if ((leg.get("monitor") or {}).get("status") == "running"
                or (state.get("curation_monitor") or {}).get("status") == "running"):
            self.fail(diagnostics("monitor did not finish before verdict"))
        for pid in background_pids:
            wait_for_pid(pid)
        wait_background_state()
        final_state = project.load_state("t")
        final_leg = baton.current_leg(final_state)
        final = final_leg["dossier"]["id"]
        if (final_leg.get("monitor") or {}).get("status") == "failed":
            # A failed monitor is handled through the documented rerun path; the
            # test must never bypass a missing final brief.
            run_checked("monitor", "t", final, "--rerun")
            wait_background_state()
        final_verdict = Path(self.tmp.name) / "final-verdict.md"
        final_verdict.write_text("```baton-verdict\n" + json.dumps({"conclusion":"ACCEPT","findings":{}}) +
                                 "\n```\n本任务没有 baton 亲跑的验证。\n", encoding="utf-8")
        run_checked("rule", "t", final, str(final_verdict))
        closed = run_checked("close", "t")
        self.assertIn("F1", closed.stdout)
        for pid in background_pids:
            wait_for_pid(pid)
        # The detached runner/monitor must be gone before TemporaryDirectory cleanup;
        # otherwise an in-flight child can recreate state files after the assertion.
        for _ in range(200):
            state = project.read_state("t", {}) or {}
            leg = baton.current_leg(state) or {}
            pids = [leg.get("runner_pid"), leg.get("codex_pid"), (leg.get("monitor") or {}).get("pid")]
            pids.extend(background_pids)
            if not any(baton.pid_alive(pid) for pid in pids if pid):
                break
            import time
            time.sleep(0.05)
        else:
            self.fail(diagnostics("background process did not exit"))
        for artifact in (cli_root / "a.py", cli_root / "${BATON_HOME}"):
            if artifact.exists():
                artifact.unlink()
            self.assertFalse(artifact.exists(), diagnostics("test artifact remained: " + str(artifact)))


if __name__ == "__main__":
    unittest.main()
