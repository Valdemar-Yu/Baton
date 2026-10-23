from platform_fixtures import wait_windows_pid
from platform_fixtures import executable_fixture
import contextlib
import io
import json
import os
import signal
import stat
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


def wait_for_pid(pid, timeout=30):
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


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.state = root / "state"
        self.project_root = root / "project"
        self.project_root.mkdir()
        subprocess.run(["git", "init", "-q", str(self.project_root)], check=True)
        (self.project_root / "a.py").write_text("x = 1\n", encoding="utf-8")
        (self.project_root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.project_root / ".baton" / "tasks" / "t" / "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": ["a.py"], "deny": [], "acceptance": [],
                                                    "frozen": [], "verify": "none"}) + "\n```\n# t\n",
            encoding="utf-8")
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "BATON_HOME": str(self.state),
                                           "PYTHONDONTWRITEBYTECODE": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = baton.Project(str(self.project_root))
        self.project.record_id()
        self.project.update_state("t", lambda s: s.update(task="t", snapshot_store="shadow", scope={
            "allow": ["a.py"], "deny": [], "acceptance": [], "frozen": [], "verify": "none"},
            legs=[], status="idle"))

    def make_dossier(self, status="report"):
        store = baton.SnapStore(self.project, "shadow")
        base = store.snapshot("base")
        self.project.update_state("t", lambda s: s.update(base_ref=base))
        (self.project_root / "a.py").write_text("x = 2\n", encoding="utf-8")
        end = store.snapshot("end")
        leg = {"n": 1, "status": status, "executor_status": status,
               "result": {"status": status, "summary": "executor", "artifact": ""},
               "start_ref": base, "end_ref": end, "events": "", "diff": baton.shortstat(store, base, end),
               "verify": {"skipped": True, "skip_reason": "本任务没有 baton 亲跑的验证"},
               "started_at": baton.now()}
        self.project.update_state("t", lambda s: s.update(legs=[leg], status="idle"))
        st = self.project.load_state("t")
        dossier = baton.build_dossier(self.project, "t", st, leg, status)
        self.assertTrue(dossier)
        return dossier

    def fake_codex(self, behavior="valid"):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir(exist_ok=True)
        script = bindir / "codex"
        script.write_text("""#!/usr/bin/env python3
import json, os, sys, time
args = sys.argv[1:]
if '--ignore-user-config' not in args:
    sys.exit(2)
out = args[args.index('-o') + 1]
behavior = os.environ.get('BATON_MONITOR_BEHAVIOR', 'valid')
if behavior == 'sleep':
    time.sleep(10)
elif behavior == 'capacity':
    print(json.dumps({'type':'turn.failed','error':{'message':'at capacity'}}), flush=True)
    sys.exit(1)
elif behavior == 'capacity-once':
    marker = os.environ.get('BATON_MONITOR_MARKER')
    first = not marker or not os.path.exists(marker)
    if marker:
        open(marker, 'a').close()
    if first:
        print(json.dumps({'type':'turn.failed','error':{'message':'at capacity'}}), flush=True)
        sys.exit(1)
    json.dump({'summary':'monitor ok','recommendation':'APPROVE','findings':[],'memory_advice':[]}, open(out, 'w'))
elif behavior == 'invalid':
    json.dump({'summary':'bad'}, open(out, 'w'))
else:
    json.dump({'summary':'monitor ok','recommendation':'APPROVE','findings':[],'memory_advice':[]}, open(out, 'w'))
""", encoding="utf-8")
        script = executable_fixture(script)
        self.env_path = patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}, clear=False)
        self.env_path.start()
        self.addCleanup(self.env_path.stop)

    def cfg(self, **monitor):
        cfg = json.loads(json.dumps(self.project.config))
        cfg["monitor"].update({"enabled": True, "timeout_minutes": 0.05, **monitor})
        return cfg

    def test_dossier_ids_and_final_increment(self):
        self.assertEqual(baton.dossier_id_for(self.project, "t", {"n": 1, "result": {"artifact": ".baton/reports/R007-x.html"}}, "report"), "R007")
        self.assertEqual(baton.dossier_id_for(self.project, "t", {"n": 2, "result": {"artifact": ""}}, "violation"), "t-leg-002-violation")
        first = baton.dossier_id_for(self.project, "t", {"n": 1, "result": {}}, "done")
        Path(self.project.state_dir, "dossiers", first).mkdir(parents=True)
        self.assertEqual(baton.dossier_id_for(self.project, "t", {"n": 2, "result": {}}, "done"), "t-final-2")

    def test_dossier_files_permissions_manifest_and_scope(self):
        dossier = self.make_dossier()
        root = Path(self.project.state_dir) / "dossiers" / dossier
        self.assertTrue((root / "MONITOR_PROMPT.md").exists())
        self.assertTrue((root / "diff.patch").exists())
        self.assertTrue((root / "code" / "a.py").exists())
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((root / "evidence.md").stat().st_mode), 0o444)
        self.assertEqual(baton.verify_dossier(self.project, dossier), (True, ""))
        (root / "evidence.md").chmod(0o644)
        (root / "evidence.md").write_text("tampered", encoding="utf-8")
        self.assertFalse(baton.verify_dossier(self.project, dossier)[0])

    def test_prompt_does_not_include_conductor_message_and_schema_validation(self):
        dossier = self.make_dossier()
        prompt = (Path(self.project.state_dir) / "dossiers" / dossier / "MONITOR_PROMPT.md").read_text()
        self.assertNotIn("secret conductor message", prompt)
        valid = {"summary": "ok", "recommendation": "APPROVE", "findings": [], "memory_advice": []}
        self.assertEqual(baton._validate_monitor_brief(valid, "report"), [])
        self.assertTrue(baton._validate_monitor_brief(dict(valid, summary="x" * 601), "report"))

    def test_codex_and_claude_commands_and_effort(self):
        dossier = self.make_dossier()
        cfg = self.cfg(reasoning_effort=None, final_reasoning_effort="high")
        codex = baton._monitor_codex_command(self.project, dossier, cfg, str(Path(self.project.state_dir) / "out.json"), None)
        self.assertIn("--ignore-user-config", codex)
        self.assertIn("--ignore-rules", codex)
        self.assertIn("--ephemeral", codex)
        self.assertIn("-s", codex)
        self.assertNotIn("model_reasoning_effort=\"None\"", codex)
        self.assertFalse(any(item.startswith("service_tier=") for item in codex))
        fast = baton._monitor_codex_command(self.project, dossier, self.cfg(service_tier="priority"),
                                            str(Path(self.project.state_dir) / "out.json"), "ultra")
        self.assertIn('service_tier="priority"', fast)
        self.assertIn('model_reasoning_effort="ultra"', fast)
        self.assertEqual(baton._monitor_effort(cfg, "final"), "high")
        claude_cfg = self.cfg(model="haiku", reasoning_effort="medium")
        claude = baton._monitor_claude_command(self.project, dossier, claude_cfg, "medium")
        schema_arg = claude[claude.index("--json-schema") + 1]
        self.assertEqual(json.loads(schema_arg), json.loads((ROOT / "skills/baton/templates/monitor-brief.schema.json").read_text()))
        self.assertIn("--safe-mode", claude)
        self.assertIn("--strict-mcp-config", claude)
        self.assertIn("--permission-mode", claude)
        self.assertIn("dontAsk", claude)
        self.assertIn("--json-schema", claude)
        stream = baton._monitor_claude_command(self.project, dossier, claude_cfg, stream=True)
        self.assertIn("--verbose", stream)

    def test_provider_monitor_uses_hermes_key_prompt_schema_and_lenient_json(self):
        dossier = self.make_dossier()
        cfg = self.cfg(model="deepseek-v4-pro", provider="deepseek", reasoning_effort="high")
        out = str(Path(self.project.state_dir) / "out.json")
        command = baton._monitor_codex_command(self.project, dossier, cfg, out, "high")
        self.assertIn('model_provider="deepseek"', command)
        provider_arg = next(item for item in command if item.startswith("model_providers.deepseek="))
        self.assertIn('base_url="https://api.deepseek.com/v1"', provider_arg)
        self.assertIn('wire_api="responses"', provider_arg)
        self.assertIn(f'env_key="{baton.MONITOR_KEY_ENV}"', provider_arg)
        with self.assertRaises(baton.ConfigError):
            baton.monitor_provider_env(cfg)
        auth = Path(baton.hermes_profile_auth_path(cfg))
        auth.parent.mkdir(parents=True, exist_ok=True)
        auth.write_text(json.dumps({"credential_pool": {"deepseek": [{"id": "d", "access_token": "sk-test"}]}}),
                        encoding="utf-8")
        self.assertEqual(baton.monitor_provider_env(cfg), {baton.MONITOR_KEY_ENV: "sk-test"})
        schema = ROOT / "skills/baton/templates/monitor-brief.schema.json"
        prompt = baton.monitor_prompt_for(cfg, "brief please", str(schema))
        self.assertIn('"recommendation"', prompt)
        self.assertIn("at most 600 characters", prompt)
        check_schema = ROOT / "skills/baton/templates/monitor-check.schema.json"
        self.assertIn("at most 300 characters", baton.monitor_prompt_for(cfg, "check", str(check_schema)))
        self.assertEqual(baton.monitor_prompt_for(self.cfg(), "brief please", str(schema)), "brief please")
        self.assertEqual(baton.monitor_provider(self.cfg()), "")
        Path(out).write_text('```json\n{"summary": "ok"}\n```\n', encoding="utf-8")
        self.assertEqual(baton.read_monitor_json(out), {"summary": "ok"})
        Path(out).write_text("no json here", encoding="utf-8")
        self.assertIsNone(baton.read_monitor_json(out))

    def test_hermes_monitor_is_sandboxed_read_only_and_parses_stdout(self):
        dossier = self.make_dossier()
        cfg = self.cfg(model="deepseek-v4-pro", provider="deepseek", backend="hermes", reasoning_effort="high")
        self.assertEqual(baton.monitor_backend(cfg), "hermes")
        self.assertEqual(baton.monitor_backend(self.cfg()), "codex")
        command = baton._monitor_hermes_command(self.project, dossier, cfg, "high", "review")
        self.assertEqual(command[:3], ["sandbox-exec", "-f", str(ROOT / "skills/baton/templates/hermes-monitor-sandbox.sb")])
        defines = [command[i + 1] for i, item in enumerate(command) if item == "-D"]
        self.assertFalse(any(item.startswith(("PROJECT=", "BATON_HOME=")) for item in defines))
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("--provider") + 1], "deepseek")
        self.assertEqual(command[command.index("-m") + 1], "deepseek-v4-pro")
        self.assertEqual(command[command.index("--reasoning") + 1], "high")
        self.assertEqual(command[command.index("-t") + 1], "file,terminal")
        self.assertIn("--query=review", command)
        sandbox = (ROOT / "skills/baton/templates/hermes-monitor-sandbox.sb").read_text()
        self.assertNotIn('"PROJECT"', sandbox)
        with self.assertRaises(baton.ConfigError):
            baton._monitor_hermes_command(self.project, dossier, self.cfg(backend="hermes"), None, "review")
        out = Path(self.project.state_dir) / "hermes.out"
        out.write_text('{"summary": "ok"}\n\nsession_id: 20261020_000000_000000\n', encoding="utf-8")
        self.assertEqual(baton.read_monitor_json(str(out)), {"summary": "ok"})

    def test_monitor_probe_uses_isolated_flags_and_records_state(self):
        bindir = Path(self.tmp.name) / "probe-bin"
        bindir.mkdir()
        codex = bindir / "codex"
        codex.write_text("#!/usr/bin/env python3\nimport json,os,sys\nout=sys.argv[sys.argv.index('-o')+1]; mode=os.environ.get('BATON_PROBE_MODE','none')\nif mode == 'fail':\n print(json.dumps({'type':'turn.failed','error':{'message':'probe capacity'}})); sys.exit(1)\nopen(out,'w',encoding='utf-8').write({'visible':'小标题','first':'# Global','none':'NONE'}[mode] + '\\n' + open('charter.md',encoding='utf-8').read())\n", encoding="utf-8")
        codex = executable_fixture(codex)
        claude = bindir / "claude"
        claude.write_text("#!/usr/bin/env python3\nimport json,sys\nschema=sys.argv[sys.argv.index('--json-schema')+1]\njson.loads(schema)\nprint(json.dumps({'type':'system','subtype':'init','tools':['Glob','Grep','Read','StructuredOutput'],'mcp_servers':[],'permissionMode':'dontAsk'}))\n", encoding="utf-8")
        claude = executable_fixture(claude)
        valid_claude_command = baton._monitor_claude_command(self.project, "monitor-probe", self.cfg(model="haiku"), stream=True)
        bad_claude_command = list(valid_claude_command)
        bad_claude_command[bad_claude_command.index("--json-schema") + 1] = str(ROOT / "skills/baton/templates/monitor-brief.schema.json")
        bad = subprocess.run([str(claude)] + bad_claude_command[1:], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)
        codex_home = Path(self.tmp.name) / "codex-home"
        codex_home.mkdir()
        (codex_home / "AGENTS.md").write_text("# Global\n<!-- migrated-from: old -->\n## 小标题\n", encoding="utf-8")
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), "CODEX_HOME": str(codex_home), "BATON_PROBE_MODE": "visible"}, clear=False):
            messages = []
            result = baton.run_monitor_probe(self.project, self.cfg(), lambda level, msg: messages.append((level, msg)))
        self.assertTrue(result["claude_ok"])
        self.assertTrue(result["codex_dossier_read"])
        self.assertTrue(result["codex_global_instructions"])
        self.assertTrue(Path(result["codex_events"]).exists())
        self.assertTrue(Path(result["claude_stream"]).exists())
        self.assertTrue(any("Claude monitor 探针" in msg for _, msg in messages))

        for mode in ("first", "none"):
            with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), "CODEX_HOME": str(codex_home), "BATON_PROBE_MODE": mode}, clear=False):
                result = baton.run_monitor_probe(self.project, self.cfg(), lambda level, msg: None)
            self.assertFalse(result["codex_global_instructions"])
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""), "CODEX_HOME": str(codex_home), "BATON_PROBE_MODE": "fail"}, clear=False):
            result = baton.run_monitor_probe(self.project, self.cfg(), lambda level, msg: None)
        self.assertIsNone(result["codex_global_instructions"])
        self.assertIn("probe capacity", result["codex_error"])

    def test_invalid_schema_retries_once_then_succeeds(self):
        dossier = self.make_dossier()
        self.fake_codex()
        counter = Path(self.tmp.name) / "counter"
        # The fake reads this marker to produce invalid output once.
        os.environ["BATON_MONITOR_BEHAVIOR"] = "invalid"
        cfg = self.cfg()
        with patch.object(baton, "run_monitor_once", side_effect=[(None, "summary 缺失", 1, None),
                                                                      ({"summary": "ok", "recommendation": "APPROVE", "findings": [], "memory_advice": []}, "", 2, None)]):
            self.assertTrue(baton.run_monitor_lifecycle(self.project, "t", dossier, "report", cfg, 1, "report"))
        self.assertTrue((self.project_root / ".baton" / "reviews" / (dossier + "-brief.md")).exists())

    def test_codex_monitor_writes_attempt_streams_and_capacity_retry(self):
        dossier = self.make_dossier()
        self.fake_codex("capacity")
        os.environ["BATON_MONITOR_BEHAVIOR"] = "capacity"
        cfg = self.cfg()
        cfg["executor"]["capacity_retry"] = {"immediate": 0, "delayed": 0, "delayed_minutes": 0.001}
        leg = self.project.load_state("t")["legs"][0]
        value = baton.run_monitor_once(self.project, "t", dossier, "report", cfg,
                                       (Path(self.project.state_dir) / "dossiers" / dossier / "MONITOR_PROMPT.md").read_text(), 1)
        self.assertEqual(value[3], "capacity")
        out = Path(self.project.state_dir) / "dossiers" / (dossier + ".out")
        self.assertTrue(list(out.glob("attempt-*.events.jsonl")))
        self.assertTrue(list(out.glob("attempt-*.stderr.log")))

    def test_monitor_capacity_retry_is_separate_from_schema_retry(self):
        dossier = self.make_dossier()
        self.fake_codex()
        marker = Path(self.tmp.name) / "monitor-marker"
        os.environ.update(BATON_MONITOR_BEHAVIOR="capacity-once", BATON_MONITOR_MARKER=str(marker))
        cfg = self.cfg()
        cfg["executor"]["capacity_retry"] = {"immediate": 1, "delayed": 0, "delayed_minutes": 0.001}
        self.assertTrue(baton.run_monitor_lifecycle(self.project, "t", dossier, "report", cfg, 1, "report"))
        out = Path(self.project.state_dir) / "dossiers" / (dossier + ".out")
        self.assertEqual(len(list(out.glob("attempt-*.events.jsonl"))), 2)
        self.assertEqual(len(list(out.glob("brief-*.json"))), 1)

    def test_monitor_timeout_is_failed_without_schema_retry(self):
        dossier = self.make_dossier()
        self.fake_codex()
        os.environ["BATON_MONITOR_BEHAVIOR"] = "sleep"
        cfg = self.cfg(timeout_minutes=0.001)
        self.assertFalse(baton.run_monitor_lifecycle(self.project, "t", dossier, "report", cfg, 1, "report"))
        monitor = self.project.load_state("t")["legs"][0]["monitor"]
        self.assertIn("超时", monitor["error"])
        self.assertEqual(monitor["attempt"], 1)

    def test_monitor_failure_does_not_write_brief_or_ledger(self):
        dossier = self.make_dossier()
        cfg = self.cfg()
        with patch.object(baton, "run_monitor_once", return_value=(None, "bad output", 1, None)):
            self.assertFalse(baton.run_monitor_lifecycle(self.project, "t", dossier, "report", cfg, 1, "report"))
        self.assertFalse((self.project_root / ".baton" / "reviews" / (dossier + "-brief.md")).exists())
        self.assertFalse((Path(self.project.state_dir) / "ledger" / "verdicts.jsonl").exists())

    def test_ledger_hash_chain_detects_tampering(self):
        a = baton.append_brief_ledger(self.project, "t", "R001", "dossier-hash", "brief")
        baton.append_brief_ledger(self.project, "t", "R002", "dossier-hash-2", "brief-2")
        self.assertTrue(baton.verify_ledger(self.project))
        path = Path(self.project.state_dir) / "ledger" / "verdicts.jsonl"
        value = json.loads(path.read_text().splitlines()[0])
        value["brief"] = "tampered"
        path.write_text(json.dumps(value) + "\n" + "\n".join(path.read_text().splitlines()[1:]) + "\n")
        self.assertFalse(baton.verify_ledger(self.project))

    def test_rerun_requires_flag_and_records_rerun(self):
        dossier = self.make_dossier()
        review = Path(self.project_root / ".baton" / "reviews")
        review.mkdir(parents=True)
        (review / (dossier + "-brief.md")).write_text("old", encoding="utf-8")
        with self.assertRaises(SystemExit):
            baton.cmd_monitor(self.project, type("Args", (), {"task": "t", "dossier": dossier, "rerun": False})())

    def test_rerun_starts_background_monitor_and_appends_ledger_flag(self):
        dossier = self.make_dossier()
        (self.project_root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        review = self.project_root / ".baton" / "reviews"
        review.mkdir(parents=True, exist_ok=True)
        (review / (dossier + "-brief.md")).write_text("old", encoding="utf-8")
        self.fake_codex()
        baton.cmd_monitor(self.project, type("Args", (), {"task": "t", "dossier": dossier, "rerun": True})())
        monitor_pid = (self.project.load_state("t")["legs"][0].get("monitor") or {}).get("pid")
        deadline = time.time() + 30
        while time.time() < deadline:
            baton.refresh_runner_liveness(self.project, "t", self.project.load_state("t"))
            monitor = (self.project.load_state("t")["legs"][0].get("monitor") or {})
            if monitor.get("status") != "running":
                break
            time.sleep(0.05)
        monitor = self.project.load_state("t")["legs"][0]["monitor"]
        self.assertEqual(monitor.get("status"), "completed", monitor)
        wait_for_pid(monitor_pid)
        self.assertNotIn("error", monitor)
        record = json.loads((Path(self.project.state_dir) / "ledger" / "verdicts.jsonl").read_text().splitlines()[-1])
        self.assertTrue(record.get("rerun"))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_wait(self.project, type("Args", (), {"task": "t", "tick": 0})())
        self.assertIn("BATON::EVENT REPORT", output.getvalue())
        self.assertNotIn("BATON::EVENT IDLE", output.getvalue())

    def test_monitor_startup_grace_uses_live_pid_without_marker(self):
        self.project.update_state("t", lambda s: s.update(legs=[{
            "n": 1, "status": "report", "started_at": baton.now(), "events": "", 
            "monitor": {"id": "R001", "status": "running", "pid": 123,
                         "marker": "_monitor_run t R001", "started_at": baton.now() - 20}}]))
        with patch.object(baton, "pid_alive", side_effect=[False, True]):
            state = baton.refresh_runner_liveness(self.project, "t", self.project.load_state("t"))
        self.assertEqual((baton.current_leg(state).get("monitor") or {}).get("status"), "running")

    def test_resume_and_steer_reject_during_monitor_and_archive_messages(self):
        self.project.update_state("t", lambda s: s.update(legs=[{"n": 1, "status": "report", "started_at": baton.now(),
            "monitor": {"id": "R001", "status": "running"}, "events": "", "result": {"status": "report"}}], status="idle"))
        with self.assertRaises(SystemExit):
            baton.cmd_resume(self.project, type("Args", (), {"task": "t", "message": "wait", "message_file": None, "kind": "回复"})())
        with self.assertRaises(SystemExit):
            baton.cmd_steer(self.project, type("Args", (), {"task": "t", "message": "steer", "message_file": None})())
        path = baton.archive_conductor_message(self.project, "t", "message text", "回复")
        self.assertEqual(Path(path).read_text(), "message text")

    def test_lean_disables_monitor(self):
        cfg = self.cfg()
        cfg["mode"] = "lean"
        self.assertFalse(baton.monitor_enabled(cfg))


if __name__ == "__main__":
    unittest.main()
