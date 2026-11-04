import base64
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class MacHermesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-mac-hermes-")
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.baton_home = self.root / "baton-home"
        self.hermes_home = self.root / "hermes-home"
        self.codex_home = self.root / "codex-home"
        self.claude_config = self.root / "claude-config"
        self.bin = self.root / "bin"
        self.project_root = self.root / "project"
        for path in (self.home, self.baton_home, self.hermes_home, self.codex_home,
                     self.claude_config, self.bin, self.project_root):
            path.mkdir(parents=True, exist_ok=True)
        self.hermes_bin = self.bin / "hermes"
        self.hermes_bin.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "if '--version' in sys.argv:\n"
            "    print('Hermes fake 0.20.1')\n"
            "elif 'tools' in sys.argv:\n"
            "    print('file terminal todo')\n",
            encoding="utf-8")
        self.hermes_bin.chmod(self.hermes_bin.stat().st_mode | stat.S_IXUSR)
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.baton_home),
            "HERMES_HOME": str(self.hermes_home),
            "CODEX_HOME": str(self.codex_home),
            "CLAUDE_CONFIG_DIR": str(self.claude_config),
            "HERMES_BIN": str(self.hermes_bin),
            "HERMES_PYTHON": sys.executable,
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1",
            "BATON_DESKTOP_NOTIFY": "0",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _mac_only(self):
        return os.name == "posix"

    def _project(self, effort="max"):
        (self.project_root / "baton.json").write_text(json.dumps({
            "executor": {
                "backend": "hermes",
                "provider": "openai-codex",
                "hermes_profile": "baton-exec-v2",
                "model": "gpt-6.1-sol",
                "reasoning_effort": effort,
                "service_tier": "priority",
                "disable_fast_mode": False,
                "sandbox_exec": True,
                "escalation": {
                    "enabled": True,
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": effort,
                },
            },
            "memory": {"backend": "local"},
            "monitor": {"enabled": False},
        }), encoding="utf-8")
        return baton.Project(str(self.project_root))

    def _jwt(self, account_id="acct-mac"):
        payload = {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        return "header." + body + ".signature"

    def _doctor_project(self):
        project = self._project()
        cfg = project.config
        profile = Path(baton.hermes_profile_dir(cfg))
        profile.mkdir(parents=True, exist_ok=True)
        account_token = self._jwt()
        (profile / "SOUL.md").write_text(baton.hermes_soul_text(), encoding="utf-8")
        (profile / "config.yaml").write_text(baton.hermes_config_text(cfg), encoding="utf-8")
        (profile / ".env").write_text("# no assignments\n", encoding="utf-8")
        (profile / "auth.json").write_text(json.dumps({
            "credential_pool": {"openai-codex": [{"access_token": account_token}]}
        }), encoding="utf-8")
        root_auth = Path(baton.hermes_root_auth_path(cfg))
        root_auth.write_text("{}", encoding="utf-8")
        Path(project.state_dir).mkdir(parents=True, exist_ok=True)
        (Path(project.state_dir) / "hermes-root-auth.sha256").write_text(
            hashlib.sha256(root_auth.read_bytes()).hexdigest() + "\n", encoding="utf-8")
        (self.codex_home / "auth.json").write_text(json.dumps({
            "tokens": {"access_token": account_token}
        }), encoding="utf-8")
        return project

    def test_claude_config_dir_is_shared_by_base_draft_and_fallback(self):
        if not self._mac_only():
            return
        source = self.claude_config / "CLAUDE.md"
        source.write_text("CUSTOM-CLAUDE-INSTRUCTION\n", encoding="utf-8")
        self.assertEqual(Path(baton.harness_base_source_path()), source)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_harness_base(None, Namespace(init=True, approve=False, force=False))
        base = Path(baton.harness_base_path())
        self.assertIn(str(source), base.read_text(encoding="utf-8").splitlines()[0])
        project = self._project()
        draft = baton.build_harness_draft(project, "mac-task")
        self.assertIn(str(base), draft)
        self.assertIn("CUSTOM-CLAUDE-INSTRUCTION", base.read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(Path(baton.harness_base_source_path()),
                             Path(self.home / ".claude" / "CLAUDE.md"))

    def test_harness_base_status_and_doctor_use_custom_source_path(self):
        if not self._mac_only():
            return
        source = self.claude_config / "CLAUDE.md"
        source.write_text("CUSTOM-DOCTOR-SOURCE\n", encoding="utf-8")
        project = self._project()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_harness_base(None, Namespace(init=True, approve=False, force=False))
        self.assertIn("已生成待审 harness-base.md", output.getvalue())
        info = baton._harness_base_info()
        self.assertEqual(info["source_path"], str(source))
        status = io.StringIO()
        with contextlib.redirect_stdout(status):
            baton.cmd_harness_base(None, Namespace(init=False, approve=False, force=False))
        self.assertIn(str(source), status.getvalue())

    def test_start_resume_and_escalation_build_hermes_argv(self):
        if not self._mac_only():
            return
        project = self._project()
        cfg = project.config
        prompt = "line 1\n" + ("quoted % 中文; " * 6000)
        start = baton.hermes_command(project, "start", None, prompt, cfg=cfg)
        self.assertEqual(start[:3], ["sandbox-exec", "-f", str(ROOT / "skills/baton/templates/hermes-sandbox.sb")])
        self.assertEqual(start[start.index("--provider") + 1], "openai-codex")
        self.assertEqual(start[start.index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(start[start.index("--reasoning") + 1], "max")
        query = next(item for item in start if item.startswith("--query="))
        self.assertEqual(query[8:], prompt)
        resume = baton.hermes_command(project, "resume", "session-1", prompt, cfg=cfg)
        self.assertIn("-r", resume)
        self.assertIn("session-1", resume)
        escalated_cfg = baton._effective_executor_config(cfg, {"escalated": True})
        escalation = baton.hermes_command(project, "resume", "session-1", "escalated", cfg=escalated_cfg)
        self.assertEqual(escalation[escalation.index("--provider") + 1], "openai-codex")
        self.assertEqual(escalation[escalation.index("-m") + 1], "gpt-6.1-sol")
        self.assertEqual(escalation[escalation.index("--reasoning") + 1], "max")

    def test_codex_ultra_is_mapped_and_invalid_effort_is_rejected(self):
        if not self._mac_only():
            return
        project = self._project(effort="ultra")
        command = baton.hermes_command(project, "start", None, "prompt", cfg=project.config)
        self.assertEqual(command[command.index("--reasoning") + 1], "max")
        self.assertEqual(baton.hermes_reasoning_effort("ultra"), "max")
        diagnostic = baton.hermes_effort_diagnostic("executor.reasoning_effort", "bogus")
        self.assertEqual(diagnostic[0], "FAIL")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_command(project, "start", None, "prompt",
                                 cfg=dict(project.config, executor=dict(project.config["executor"],
                                                                         reasoning_effort="bogus")))

    def test_doctor_uses_hermes_provider_without_direct_codex_requirements(self):
        if not self._mac_only():
            return
        project = self._doctor_project()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                baton.cmd_doctor(project, Namespace(monitor_probe=False))
        text = output.getvalue()
        self.assertEqual(raised.exception.code, 0, text)
        self.assertIn("Hermes provider：openai-codex", text)
        self.assertIn("provider=openai-codex", text)
        self.assertNotIn("[FAIL] codex CLI", text)
        self.assertNotIn("models_cache", text)
        self.assertIn("service_tier/fast mode", text)

    def test_doctor_reports_effort_mapping_and_supported_values(self):
        if not self._mac_only():
            return
        project = self._doctor_project()
        cfg = copy.deepcopy(project.config)
        cfg["executor"]["reasoning_effort"] = "ultra"
        cfg["executor"]["escalation"]["reasoning_effort"] = "ultra"
        lines = []
        failed = baton.doctor_hermes(project, cfg, lambda level, message: lines.append((level, message)))
        self.assertFalse(failed)
        warnings = [message for level, message in lines if level == "WARN"]
        self.assertTrue(any("映射为 max" in message for message in warnings))
        self.assertTrue(any("none, minimal, low, medium, high, xhigh, max" in message for message in warnings))

    def test_codex_sqlite_warning_is_windows_only(self):
        if not self._mac_only():
            return
        self.assertEqual(baton.codex_sqlite_home_warning({"executor": {"backend": "codex"}}), "")
        self.assertEqual(baton.codex_sqlite_home_warning({"executor": {"backend": "hermes"}}), "")

    def _dossier_project(self):
        project_root = self.root / "dossier-project"
        project_root.mkdir()
        agent = self.root / "agent"
        (agent / "tools").mkdir(parents=True)
        (agent / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (agent / "tools" / "threat_patterns.py").write_text(
            "def scan_for_threats(content, scope='context'):\n    return []\n", encoding="utf-8")
        (agent / "hermes_cli").mkdir()
        (agent / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (agent / "hermes_cli" / "default_soul.py").write_text(
            "DEFAULT_SOUL_MD = 'You are Hermes Agent, an intelligent AI assistant created by Nous Research.\\n'\n",
            encoding="utf-8")
        os.environ["HERMES_AGENT_DIR"] = str(agent)
        (project_root / "baton.json").write_text(json.dumps({
            "executor": {
                "backend": "hermes",
                "provider": "openai-codex",
                "hermes_profile": "baton-exec-v2",
                "model": "gpt-6.1-sol",
                "reasoning_effort": "max",
                "sandbox_exec": False,
            },
            "monitor": {"enabled": True},
        }), encoding="utf-8")
        project = baton.Project(str(project_root))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(project, Namespace(no_statusline=True, accept_boundary=True,
                                               relink=False, fresh=False))
        task_dir = project.task_dir("check")
        Path(task_dir).mkdir(parents=True, exist_ok=True)
        Path(task_dir, "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": ["*"], "deny": [], "acceptance": [],
                                                 "frozen": [], "verify": "none"}) + "\n```\n# check\n",
            encoding="utf-8")
        project.update_state("check", lambda state: state.update(
            task="check", status="running", legs=[], snapshot_store="shadow",
            scope={"allow": ["*"], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}))
        return project

    def test_check_dossier_falls_back_to_leg_start_for_empty_incremental_diff(self):
        if not self._mac_only():
            return
        project = self._dossier_project()
        store = project.store("check")
        start = store.snapshot("leg start")
        changed = Path(project.root, "changed.py")
        changed.write_text("value = 2\n", encoding="utf-8")
        probe = store.snapshot("probe")
        store.git("update-ref", "refs/baton/check/probe", probe)
        started = baton.now()
        leg = {"n": 2, "status": "running", "started_at": started, "start_ref": start,
               "probe_commit": probe, "probe_ref": "refs/baton/check/probe",
               "check_baseline_ref": probe, "alerts": [], "alert_keys": [],
               "last_rule_terminal_at": started, "events": project.leg_path("check", 2, "events.jsonl")}
        Path(project.runs_dir("check")).mkdir(parents=True, exist_ok=True)
        project.update_state("check", lambda state: state.update(base_ref=start, legs=[leg]))
        state = project.load_state("check")
        dossier = baton.build_check_dossier(project, "check", state, baton.current_leg(state))
        diff = Path(baton.dossier_path(project, dossier), "diff.patch").read_text(encoding="utf-8")
        self.assertIn("changed.py", diff)
        self.assertTrue(diff.strip())

    def test_hermes_usage_reads_task_profile_lineage_and_missing_usage_is_explicit(self):
        if not self._mac_only():
            return
        project = self._dossier_project()
        task_home = self.root / "task-hermes"
        profile = task_home / "profiles" / "baton-exec-v2"
        profile.mkdir(parents=True)
        db = profile / "state.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
        create table sessions (id text primary key, parent_session_id text);
        create table session_model_usage (
          session_id text, model text, input_tokens integer, output_tokens integer,
          cache_read_tokens integer
        );
        """)
        conn.execute("insert into sessions values ('s1', null)")
        conn.execute("insert into sessions values ('s2', 's1')")
        conn.execute("insert into session_model_usage values ('s1', 'm', 100, 40, 10)")
        conn.execute("insert into session_model_usage values ('s2', 'm', 200, 80, 20)")
        conn.commit()
        conn.close()
        started = baton.now()
        leg = {"n": 1, "status": "done", "started_at": started, "ended_at": started + 1,
               "backend": "hermes", "model": "gpt-6.1-sol", "usage": None,
               "thread_id": "s1", "events": project.leg_path("check", 1, "events.jsonl")}
        project.update_state("check", lambda state: state.update(hermes_home=str(task_home),
                                                                   thread_id="s1", legs=[leg]))
        usage = baton.hermes_usage(project, "check", leg)
        self.assertEqual(usage, {"input_tokens": 300, "cached_input_tokens": 30, "output_tokens": 120})
        text = baton.leg_event_text(project, "check", leg)
        self.assertIn("token：Hermes 未提供用量", text)


if __name__ == "__main__":
    unittest.main()
