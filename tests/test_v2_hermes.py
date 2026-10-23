from platform_fixtures import directory_link
from platform_fixtures import executable_fixture
import contextlib
import base64
import io
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import shutil
import re
import shlex
import time
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


def _jwt(payload):
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return "header." + body + ".signature"


class HermesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.hermes_home = self.root / "hermes"
        self.state = self.root / "state"
        self.bin = self.root / "bin"
        self.agent = self.root / "agent"
        self.project_root = self.root / "project"
        for path in (self.home, self.hermes_home, self.state, self.bin, self.project_root):
            path.mkdir(parents=True, exist_ok=True)
        (self.agent / "tools").mkdir(parents=True)
        (self.agent / "tools" / "__init__.py").write_text("", encoding="utf-8")
        (self.agent / "tools" / "threat_patterns.py").write_text(
            "import os\ndef scan_for_threats(content, scope='context'):\n"
            "    return ['fake_hit'] if os.environ.get('FAKE_SCAN') else []\n", encoding="utf-8")
        (self.agent / "hermes_cli").mkdir()
        (self.agent / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
        (self.agent / "hermes_cli" / "default_soul.py").write_text(
            "DEFAULT_SOUL_MD = 'You are Hermes Agent, an intelligent AI assistant created by Nous Research.\\n'\n",
            encoding="utf-8")
        self.calls = self.root / "hermes.calls"
        self._write_fake_hermes()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "BATON_HOME": str(self.state), "HERMES_HOME": str(self.hermes_home),
            "HERMES_PYTHON": sys.executable, "HERMES_AGENT_DIR": str(self.agent),
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
            "PYTHONDONTWRITEBYTECODE": "1", "BATON_DESKTOP_NOTIFY": "0",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _write_fake_hermes(self):
        script = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["FAKE_HERMES_CALLS"], "a") as f: f.write(json.dumps(args) + "\n")
if args[:2] == ["profile", "create"]:
    root = os.environ["HERMES_HOME"]
    profile = args[2]
    path = os.path.join(root, "profiles", profile)
    os.makedirs(path, exist_ok=True)
    json.dump({"openai-codex": [{"account_id": "acct"}]}, open(os.path.join(path, "auth.json"), "w"))
    with open(os.path.join(path, "SOUL.md"), "w") as f:
        f.write("You are Hermes Agent, an intelligent AI assistant created by Nous Research.\n")
    with open(os.path.join(path, ".env"), "w") as f:
        f.write("# Per-profile secrets for this Hermes profile.\n# Add secrets here when needed.\n")
    sys.exit(0)
if args[:1] == ["--version"]:
    print("Hermes v0.20.1\nHermes build details\nHermes extra diagnostics")
    sys.exit(0)
if "tools" in args:
    print(os.environ.get("FAKE_HERMES_TOOLS", "file terminal todo web_search"))
    sys.exit(0)
if "chat" in args:
    count_path = os.environ.get("FAKE_HERMES_CHAT_COUNT")
    n = int(open(count_path).read() or "0") if count_path and os.path.exists(count_path) else 0
    if count_path: open(count_path, "w").write(str(n + 1))
    mode = os.environ.get("FAKE_HERMES_MODE", "success")
    if mode == "exit1":
        print("Error: fake hermes failure", file=sys.stderr)
        sys.exit(1)
    if mode in ("invalid_once", "invalid_always") and (mode == "invalid_always" or n == 0):
        print("BATON_RESULT_BEGIN")
        print(json.dumps({"status": "done"}))
        print("BATON_RESULT_END")
        print("session_id: fake-session", file=sys.stderr)
        sys.exit(0)
    if mode == "multi":
        print("BATON_RESULT_BEGIN\n{}\nBATON_RESULT_END")
print("BATON_RESULT_BEGIN")
print(json.dumps({"status":"done","summary":"fake","artifact":"","change_size":"none","memory_used":[]}))
print("BATON_RESULT_END")
print("session_id: fake-session", file=sys.stderr)
'''
        path = self.bin / "hermes"
        path.write_text(script, encoding="utf-8")
        path = executable_fixture(path)
        os.environ["FAKE_HERMES_CALLS"] = str(self.calls)

    def _project(self, backend=True):
        (self.project_root / "baton.json").write_text(json.dumps({
            "executor": {"backend": "hermes" if backend else "codex", "sandbox_exec": False},
            "monitor": {"enabled": False},
        }), encoding="utf-8")
        return baton.Project(str(self.project_root))

    def _args(self):
        return Namespace(no_statusline=True, accept_boundary=False, relink=False, fresh=False)

    def _init(self):
        project = self._project()
        baton.cmd_init(project, self._args())
        return baton.Project(str(self.project_root))

    def _run_cli_mode(self, mode):
        self._project()
        os.environ["FAKE_HERMES_MODE"] = mode
        chat_count = self.root / "hermes.chat.count"
        os.environ["FAKE_HERMES_CHAT_COUNT"] = str(chat_count)
        cli = [sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(self.project_root)]
        for args in (("init", "--no-statusline"), ("new", "hermes")):
            result = subprocess.run(cli + list(args), env=os.environ.copy(), capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        brief = self.project_root / ".baton" / "tasks" / "hermes" / "brief.md"
        brief.write_text("```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}) + "\n```\n# hermes\n", encoding="utf-8")
        result = subprocess.run(cli + ["start", "hermes"], env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        state_path = next((self.state / "projects").iterdir()) / "tasks" / "hermes" / "state.json"
        state = {}
        for _ in range(200):
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state.get("status") != "running":
                    break
            time.sleep(0.05)
        return state, [json.loads(line) for line in self.calls.read_text().splitlines() if json.loads(line) and "chat" in json.loads(line)]

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_init_creates_profile_without_clone_and_files_readonly(self):
        project = self._init()
        profile = Path(baton.hermes_profile_dir(project.config))
        self.assertTrue((profile / "SOUL.md").exists())
        self.assertTrue((profile / "config.yaml").exists())
        self.assertEqual((profile / "SOUL.md").read_text(), baton.hermes_soul_text())
        self.assertEqual(stat.S_IMODE((profile / "SOUL.md").stat().st_mode), 0o444)
        self.assertEqual(stat.S_IMODE((profile / "config.yaml").stat().st_mode), 0o444)
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        create = next(args for args in calls if args[:2] == ["profile", "create"])
        self.assertNotIn("--clone", create)
        self.assertNotIn("--clone-from", create)
        self.assertNotIn("--clone-all", create)

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_existing_profile_is_skipped_and_mismatch_warns_without_overwrite(self):
        project = self._init()
        before = len(self.calls.read_text().splitlines())
        soul = Path(baton.hermes_profile_dir(project.config)) / "SOUL.md"
        soul.chmod(0o644)
        soul.write_text("old\n", encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_init(project, self._args())
        self.assertEqual(len(self.calls.read_text().splitlines()), before)
        self.assertEqual(soul.read_text(), "old\n")
        self.assertEqual(stat.S_IMODE(soul.stat().st_mode), 0o644)
        self.assertIn("未覆盖", out.getvalue())

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_existing_hermes_default_soul_is_replaced_even_when_readonly(self):
        project = self._init()
        soul = Path(baton.hermes_profile_dir(project.config)) / "SOUL.md"
        soul.chmod(0o644)
        soul.write_text("You are Hermes Agent, an intelligent AI assistant created by Nous Research.\n", encoding="utf-8")
        soul.chmod(0o444)
        baton.cmd_init(project, self._args())
        self.assertEqual(soul.read_text(), baton.hermes_soul_text())
        self.assertEqual(stat.S_IMODE(soul.stat().st_mode), 0o444)

    @unittest.skipIf(os.name == "nt", "Hermes profile initialization unsupported on Windows")
    def test_scan_hit_refuses_soul_write(self):
        project = self._project()
        os.environ["FAKE_SCAN"] = "1"
        with self.assertRaises(baton.ConfigError) as ctx:
            baton.cmd_init(project, self._args())
        self.assertIn("注入扫描命中", str(ctx.exception))
        soul = self.hermes_home / "profiles" / "baton-exec-v2" / "SOUL.md"
        self.assertTrue(soul.read_text().startswith("You are Hermes Agent, an intelligent AI assistant created by Nous Research"))

    def test_context_precedence_and_agents_chain(self):
        project = self._project()
        (self.project_root / "HERMES.md").write_text("h", encoding="utf-8")
        (self.project_root / "AGENTS.md").write_text("a", encoding="utf-8")
        self.assertEqual(baton.hermes_context_files(project), [str(baton.canonical_dir(self.project_root / "HERMES.md"))])
        (self.project_root / "HERMES.md").unlink()
        self.assertEqual(baton.hermes_context_files(project), [str(baton.canonical_dir(self.project_root / "AGENTS.md"))])

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_auth_hash_and_doctor_checks(self):
        self.hermes_home.mkdir(exist_ok=True)
        (self.hermes_home / "auth.json").write_text('{"root":"x"}', encoding="utf-8")
        project = self._init()
        baseline = Path(project.state_dir) / "hermes-root-auth.sha256"
        self.assertTrue(baseline.exists())
        lines = []
        failed = baton.doctor_hermes(project, project.config, lambda level, msg: lines.append((level, msg)))
        self.assertFalse(failed)
        self.assertTrue(any("sandbox-exec" in msg for _, msg in lines))
        version_lines = [msg for _, msg in lines if msg.startswith("Hermes 版本：")]
        self.assertEqual(version_lines, ["Hermes 版本：Hermes v0.20.1"])

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_account_ids_use_jwt_and_preflight_rejects_mismatch(self):
        project = self._init()
        cfg = project.config
        profile = Path(baton.hermes_profile_dir(cfg))
        account_claim = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-same"}}
        (profile / "auth.json").write_text(json.dumps({
            "credential_pool": {"openai-codex": [{
                "access_token": _jwt({"sub": "no-account-claim"}),
                "id_token": _jwt(account_claim),
            }]}
        }), encoding="utf-8")
        codex = self.home / ".codex"
        codex.mkdir()
        (codex / "auth.json").write_text(json.dumps({
            "tokens": {"account_id": "acct-same", "access_token": _jwt(account_claim)}
        }), encoding="utf-8")
        self.assertEqual(baton.hermes_account_ids(cfg), {
            "profile": "acct-same", "codex": "acct-same", "mismatch": False})
        baton.hermes_preflight(project, cfg)
        lines = []
        self.assertFalse(baton.doctor_hermes(project, cfg, lambda level, msg: lines.append((level, msg))))
        self.assertTrue(any(level == "OK" and "账号 id 哈希" in msg for level, msg in lines))

        (profile / "auth.json").write_text(json.dumps({
            "credential_pool": {"openai-codex": [{"id_token": _jwt({
                "https://api.openai.com/auth": {"chatgpt_account_id": "acct-other"}
            })}]}
        }), encoding="utf-8")
        self.assertTrue(baton.hermes_account_ids(cfg)["mismatch"])
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        lines = []
        self.assertTrue(baton.doctor_hermes(project, cfg, lambda level, msg: lines.append((level, msg))))
        self.assertTrue(any(level == "FAIL" and "账号 id 哈希" in msg for level, msg in lines))

    def test_config_and_memory_compatibility(self):
        cfg = json.loads((ROOT / "skills/baton/config.json").read_text())
        cfg["executor"]["backend"] = "bad"
        with self.assertRaises(baton.ConfigError):
            baton.validate_config(cfg)
        old = {"status": "done", "summary": "x", "artifact": "", "change_size": "none"}
        self.assertTrue(baton.valid_executor_result(old))
        self.assertEqual(baton.normalize_executor_result(old)["memory_used"], [])

    def test_hermes_command_ephemeral_and_result_parsing(self):
        project = self._project()
        cfg = project.config
        cfg["executor"]["sandbox_exec"] = True
        self.assertIn((ROOT / "skills/baton/templates/leg-result.schema.json").read_text().strip(), baton.hermes_soul_text())
        cmd = baton.hermes_command(project, "resume", "s1", "say -p literally", cfg)
        self.assertEqual(cmd.count("--query=say -p literally"), 1)
        self.assertIn("-r", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "file,terminal,todo")
        self.assertIn("-D", cmd)
        prompt = baton.hermes_ephemeral_system_prompt(project, "task", {"config_snapshot": cfg, "scope": {}})
        self.assertIn("language=中文", prompt)
        text = "noise\nBATON_RESULT_BEGIN\n{}\nBATON_RESULT_END\n".format(json.dumps({"status":"blocked","summary":"bad","artifact":"","change_size":"none","memory_used":[]}))
        value, error = baton.parse_hermes_result(text)
        self.assertEqual(value["status"], "blocked")
        self.assertFalse(error)
        self.assertIsNone(baton.parse_hermes_result("BATON_RESULT_BEGIN\n{}\nBATON_RESULT_END\n")[0])

    def test_capacity_reads_raw_hermes_stdout(self):
        out = self.root / "stdout"
        err = self.root / "stderr"
        out.write_text("Selected model is at capacity\n", encoding="utf-8")
        err.write_text("", encoding="utf-8")
        self.assertTrue(baton.is_capacity_failure(str(out), str(err), returncode=1))

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_runner_corrects_one_invalid_result_and_resumes(self):
        state, calls = self._run_cli_mode("invalid_once")
        self.assertEqual(state["legs"][-1]["status"], "done")
        self.assertEqual(len(calls), 2)
        self.assertIn("-r", calls[1])
        query = next(item for item in calls[1] if item.startswith("--query="))
        self.assertIn("缺少字段", query)

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_runner_marks_two_invalid_results_failed(self):
        state, calls = self._run_cli_mode("invalid_always")
        self.assertEqual(state["legs"][-1]["status"], "failed")
        self.assertEqual(len(calls), 2)

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_runner_nonzero_hermes_exit_does_not_resume(self):
        state, calls = self._run_cli_mode("exit1")
        self.assertEqual(state["legs"][-1]["status"], "failed")
        self.assertEqual(len(calls), 1)

    def test_last_result_pair_and_session_id(self):
        first = {"status": "blocked", "summary": "old", "artifact": "", "change_size": "none", "memory_used": []}
        last = {"status": "done", "summary": "new", "artifact": "", "change_size": "none", "memory_used": []}
        stdout = "BATON_RESULT_BEGIN\n" + json.dumps(first) + "\nBATON_RESULT_END\n" + "BATON_RESULT_BEGIN\n" + json.dumps(last) + "\nBATON_RESULT_END\n"
        value, error = baton.parse_hermes_result(stdout)
        self.assertEqual(value["summary"], "new")
        self.assertFalse(error)
        self.assertEqual(baton.hermes_session_id("session_id: old\nnoise\nsession_id: newest\n"), "newest")

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_preflight_rejects_env_auth_count_and_missing_web_search(self):
        project = self._init()
        cfg = project.config
        profile = Path(baton.hermes_profile_dir(cfg))
        (profile / ".env").write_text("# Per-profile secrets for this Hermes profile.\n\n# comment\n", encoding="utf-8")
        baton.hermes_preflight(project, cfg)
        (profile / ".env").write_text("HERMES_IGNORE_RULES=1\n", encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        (profile / ".env").write_text("", encoding="utf-8")
        (profile / "auth.json").write_text('{"openai-codex": []}', encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        (profile / "auth.json").write_text('{"openai-codex": [{"id": "a"}, {"id": "b"}]}', encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        (profile / "auth.json").write_text('{"openai-codex": [{"id": "a"}]}', encoding="utf-8")
        cfg["executor"]["network_access"] = True
        os.environ["FAKE_HERMES_TOOLS"] = "file terminal todo"
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)

    def test_preflight_counts_pool_credentials_of_the_executor_provider(self):
        project = self._init()
        cfg = project.config
        profile = Path(baton.hermes_profile_dir(cfg))
        (profile / ".env").write_text("", encoding="utf-8")
        mirrored = {"providers": {"openai-codex": {"tokens": {"refresh_token": "r"},
                                                   "last_auth_error": {"provider": "openai-codex"}}},
                    "credential_pool": {"openai-codex": [{"id": "a", "auth_type": "oauth"}]}}
        (profile / "auth.json").write_text(json.dumps(mirrored), encoding="utf-8")
        baton.hermes_preflight(project, cfg)
        mirrored["credential_pool"]["openai-codex"].append({"id": "b", "auth_type": "oauth"})
        (profile / "auth.json").write_text(json.dumps(mirrored), encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        cfg["executor"]["provider"] = "deepseek"
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)
        mirrored["credential_pool"]["deepseek"] = [{"id": "d", "auth_type": "api_key"}]
        (profile / "auth.json").write_text(json.dumps(mirrored), encoding="utf-8")
        baton.hermes_preflight(project, cfg)
        mirrored["credential_pool"]["deepseek"].append({"id": "e", "auth_type": "api_key"})
        (profile / "auth.json").write_text(json.dumps(mirrored), encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.hermes_preflight(project, cfg)

    def test_preset_use_switches_executor_and_monitor_and_task_snapshot(self):
        project = self._init()
        path = Path(project.baton_config_path)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["presets"] = {
            "codex": {"executor": {"backend": "hermes", "provider": "openai-codex", "model": "gpt-6.1-sol",
                                   "reasoning_effort": "max", "service_tier": "priority",
                                   "disable_fast_mode": False, "escalation": {"enabled": False}},
                      "monitor": {"model": "gpt-6.1-sol", "reasoning_effort": "ultra", "service_tier": "priority"}},
            "deepseek": {"executor": {"backend": "hermes", "provider": "deepseek", "model": "deepseek-v4-flash",
                                      "reasoning_effort": "max"},
                         "monitor": {"backend": "hermes", "provider": "deepseek", "model": "deepseek-v4-pro",
                                     "reasoning_effort": "high"}},
            "broken": {"monitor": {"model": ""}},
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        project = baton.Project(str(self.project_root))
        baton.cmd_new(project, Namespace(task="t", template=None, brief=None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_preset(project, Namespace(action="use", name="deepseek", task=["t"]))
        self.assertIn("还没启动", out.getvalue())
        os.makedirs(baton.freeze_dir(project, "t"), exist_ok=True)
        project.update_state("t", lambda state: state.update(task="t", legs=[], config_snapshot=project.config))
        baton.cmd_preset(project, Namespace(action="use", name="deepseek", task=["t"]))
        cfg = project.config
        self.assertEqual((cfg["executor"]["provider"], cfg["executor"]["model"]), ("deepseek", "deepseek-v4-flash"))
        self.assertEqual((cfg["monitor"]["backend"], cfg["monitor"]["provider"]), ("hermes", "deepseek"))
        self.assertIn("provider: deepseek", (Path(baton.hermes_profile_dir(cfg)) / "config.yaml").read_text())
        snapshot = baton.config_for_state(project, project.load_state("t"))
        self.assertEqual(snapshot["monitor"]["model"], "deepseek-v4-pro")
        baton.cmd_preset(project, Namespace(action="use", name="codex", task=["t"]))
        cfg = project.config
        self.assertEqual(cfg["executor"]["model"], "gpt-6.1-sol")
        self.assertIsNone(cfg["monitor"].get("provider"))
        self.assertIsNone(cfg["monitor"].get("backend"))
        self.assertEqual(baton.monitor_backend(cfg), "codex")
        snapshot = baton.config_for_state(project, project.load_state("t"))
        self.assertEqual((snapshot["monitor"]["model"], snapshot["monitor"].get("provider")), ("gpt-6.1-sol", None))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_preset(project, Namespace(action="list", name=None, task=[]))
        self.assertIn("模式：standard", out.getvalue())
        self.assertIn("* codex", out.getvalue())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["preset"], "codex")
        self.assertIn("  deepseek", out.getvalue())
        before = path.read_text(encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.cmd_preset(project, Namespace(action="use", name="broken", task=[]))
        self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_state_db_terminal_calls_follow_compression_chain(self):
        project = self._project(backend=False)
        baton.cmd_init(project, self._args())
        cfg = project.config
        db = Path(baton.hermes_profile_dir(cfg)) / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("""
        create table sessions (id text primary key, parent_session_id text);
        create table messages (id integer primary key, session_id text, role text, content text,
          tool_calls text, tool_name text, tool_call_id text, timestamp real);
        """)
        conn.execute("insert into sessions values ('s1', null)")
        conn.execute("insert into sessions values ('s2', 's1')")
        tc = json.dumps([{"id":"c1","function":{"name":"terminal","arguments":json.dumps({"command":"git push"})}}])
        conn.execute("insert into messages values (1,'s2','assistant','',?,?,?,1)", (tc, None, None))
        conn.execute("insert into messages values (2,'s2','tool',?,NULL,?,?,2)", (json.dumps({"exit_code": 0}), "terminal", "c1"))
        conn.commit(); conn.close()
        calls = baton.hermes_terminal_calls(project, "task", {"backend":"hermes", "thread_id":"s1"})
        self.assertEqual(calls[0]["command"], "git push")
        self.assertEqual(calls[0]["exit_code"], 0)

    def test_external_action_detection(self):
        actions = baton.external_action_findings(["refs/remotes/a"], ["refs/remotes/b"], [{"command":"ssh host", "exit_code":0}])
        self.assertEqual({item["type"] for item in actions}, {"remote_refs_changed", "external_command"})

    @unittest.skipIf(os.name == "nt", "Hermes execution/profile backend unsupported on Windows; no Hermes installed")
    def test_real_cli_hermes_executor_end_to_end(self):
        project = self._project()
        cli = [sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(self.project_root)]
        env = os.environ.copy()
        self.assertEqual(subprocess.run(cli + ["init", "--no-statusline"], env=env, capture_output=True, text=True).returncode, 0)
        self.assertEqual(subprocess.run(cli + ["new", "e2e"], env=env, capture_output=True, text=True).returncode, 0)
        brief = self.project_root / ".baton" / "tasks" / "e2e" / "brief.md"
        brief.write_text("```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}) + "\n```\n# e2e\n", encoding="utf-8")
        self.assertEqual(subprocess.run(cli + ["start", "e2e"], env=env, capture_output=True, text=True).returncode, 0)
        state_path = next((self.state / "projects").iterdir()) / "tasks" / "e2e" / "state.json"
        for _ in range(200):
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state.get("status") != "running":
                    break
            import time
            time.sleep(0.05)
        self.assertEqual(state["legs"][-1]["status"], "done")
        self.assertEqual(state["legs"][-1]["result"]["memory_used"], [])

    def test_sandbox_syntax_skips_when_nested_sandbox_is_denied(self):
        shutil_which = shutil.which("sandbox-exec")
        if not shutil_which:
            self.skipTest("sandbox-exec 不存在")
        rule = ROOT / "skills/baton/templates/hermes-sandbox.sb"
        values = {"PROJECT": str(self.project_root), "PROFILE_DIR": str(self.hermes_home / "profile"), "CACHE_HOME": str(self.home / "Library/Caches"),
                  "TMP_DIR": "/private/tmp", "VAR_TMP_DIR": "/private/var/tmp", "TMP_FOLDERS_DIR": "/private/var/folders",
                  "USER_CACHE": str(self.home / ".cache"), "BATON_TASKS": str(self.project_root / ".baton/tasks"),
                  "BATON_REVIEWS": str(self.project_root / ".baton/reviews"), "PROFILE_SOUL": str(self.hermes_home / "SOUL.md"),
                  "PROFILE_CONFIG": str(self.hermes_home / "config.yaml"), "PROFILE_ENV": str(self.hermes_home / ".env"),
                  "BATON_HOME": str(self.home / ".baton"), "CLAUDE_HOME": str(self.home / ".claude")}
        command = [shutil_which, "-f", str(rule)]
        for key, value in values.items(): command += ["-D", f"{key}={value}"]
        command += ["/usr/bin/true"]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            if re.search(r"sandbox_apply", result.stderr or "", re.I):
                self.skipTest("Codex 沙箱禁止嵌套 sandbox-exec")
            self.fail("sandbox-exec 规则语法失败：" + (result.stderr or result.stdout))

    def test_sandbox_write_matrix(self):
        shutil_which = shutil.which("sandbox-exec")
        if not shutil_which:
            self.skipTest("sandbox-exec 不存在")
        sandbox_parent = self.root / "sandbox-allowed"
        sandbox_parent.mkdir()
        other_root = self.root / "sandbox-other"
        other_root.mkdir()
        hermes_root = self.root / "hermes-root"
        hermes_root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(other_root, ignore_errors=True))
        temp_dirs = {
            "TMP_DIR": sandbox_parent / "tmp",
            "VAR_TMP_DIR": sandbox_parent / "var-tmp",
            "TMP_FOLDERS_DIR": sandbox_parent,
        }
        for directory in temp_dirs.values():
            directory.mkdir(parents=True, exist_ok=True)
        project_root = sandbox_parent / "project"
        project_root.mkdir()
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_root)}):
            project = baton.Project(str(project_root))
            cfg = self._project().config
            profile = Path(baton.hermes_profile_dir(cfg))
            profile.mkdir(parents=True, exist_ok=True)
            baton_home = Path(baton.hermes_sandbox_defines(project, cfg)["BATON_HOME"])
            claude_home = Path(baton.hermes_sandbox_defines(project, cfg)["CLAUDE_HOME"])
            for directory in (hermes_root, baton_home, claude_home, project_root / ".baton/tasks", project_root / ".baton/reviews"):
                directory.mkdir(parents=True, exist_ok=True)
            files = {"soul": profile / "SOUL.md", "config": profile / "config.yaml", "env": profile / ".env"}
            for path in files.values(): path.write_text("old\n", encoding="utf-8")
            values = baton.hermes_sandbox_defines(project, cfg)
            values.update({key: baton.canonical_dir(value) for key, value in temp_dirs.items()})
        command_base = [shutil_which, "-f", str(ROOT / "skills/baton/templates/hermes-sandbox.sb")]
        for key, value in values.items(): command_base += ["-D", f"{key}={value}"]
        probes = {
            "project": (project_root / "write.ok", True), "profile_log": (profile / "logs/x", True),
            "profile_journal": (profile / "state.db-journal", True), "other": (other_root / "write.ok", False),
            "hermes_root": (hermes_root / "config.yaml", False),
            "tasks": (project_root / ".baton/tasks/write", False), "reviews": (project_root / ".baton/reviews/write", False),
            "soul": (files["soul"], False), "config": (files["config"], False), "env": (files["env"], False),
            "baton": (baton_home / "write", False), "claude": (claude_home / "write", False),
        }
        for name, (path, allowed) in probes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(command_base + ["/bin/sh", "-c", "echo x > " + shlex.quote(str(path))], capture_output=True, text=True)
            if result.returncode != 0 and "sandbox_apply" in (result.stderr or ""):
                self.skipTest("Codex 沙箱禁止嵌套 sandbox-exec")
            self.assertEqual(result.returncode == 0, allowed, name + " 写入结果不符合预期：" + (result.stderr or result.stdout))

    def test_sandbox_defines_resolve_symlinked_project_paths(self):
        actual = self.root / "real-project"
        actual.mkdir()
        link = self.root / "project-link"
        directory_link(link, actual)
        project = baton.Project(str(link))
        cfg = self._project().config
        defines = baton.hermes_sandbox_defines(project, cfg)
        self.assertEqual(defines["PROJECT"], os.path.realpath(actual))
        self.assertEqual(defines["BATON_TASKS"], os.path.realpath(actual / ".baton/tasks"))


if __name__ == "__main__":
    unittest.main()
