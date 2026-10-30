import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402
from baton_memory import server as memory_server  # noqa: E402


class ExecutorHarnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.project_root = self.base / "project"
        self.project_root.mkdir()
        self.home = self.base / "home"
        self.state = self.base / "state"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.state), "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        (self.project_root / "baton.json").write_text(json.dumps({
            "executor": {"backend": "codex"},
            "supervision": {"test_command": "python3 -m unittest"},
        }), encoding="utf-8")
        self.project = baton.Project(str(self.project_root))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(self.project, Namespace(no_statusline=True, accept_boundary=True,
                                                   relink=False, fresh=False, migrate=False,
                                                   upgrade_profile=False))

    def _new(self, task="t"):
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_new(self.project, Namespace(task=task, brief=None))
        brief = self.project_root / ".baton" / "tasks" / task / "brief.md"
        brief.write_text("```baton-scope\n" + json.dumps({
            "allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"
        }) + "\n```\n# task\n", encoding="utf-8")
        return task

    def test_new_writes_reviewable_harness(self):
        self._new()
        path = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        self.assertTrue(path.exists())
        self.assertTrue(path.read_text(encoding="utf-8").startswith(baton.HARNESS_PENDING_MARKER))
        self.assertIn("## 项目约定", path.read_text(encoding="utf-8"))

    def test_start_rejects_unreviewed_harness(self):
        self._new()
        with self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="t"))

    def test_harness_approve_removes_marker_and_records_hash(self):
        self._new()
        path = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        reviewed = "# Reviewed harness\n\n长期约定\n"
        path.write_text(baton.HARNESS_PENDING_MARKER + "\n" + reviewed, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        self.assertEqual(path.read_text(encoding="utf-8"), "\n" + reviewed)
        state = self.project.load_state("t")
        digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        self.assertEqual(state["harness_sha256"], digest)
        self.assertEqual(state["harness_approval"]["sha256"], digest)
        self.assertTrue(state["harness_approved_at"])
        records = baton.ledger_records(self.project)
        self.assertEqual(records[-1]["type"], "harness_approval")
        self.assertEqual(records[-1]["harness_sha256"], digest)

    def test_harness_approve_rejects_empty_after_marker(self):
        self._new()
        path = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        path.write_text(baton.HARNESS_PENDING_MARKER + "\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        self.assertIn(baton.HARNESS_PENDING_MARKER, path.read_text(encoding="utf-8"))
        self.assertEqual(baton.ledger_records(self.project), [])

    def test_start_rejects_marker_removed_without_approval_record(self):
        self._new()
        path = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        path.write_text(path.read_text(encoding="utf-8").replace(baton.HARNESS_PENDING_MARKER, ""), encoding="utf-8")
        with self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="t"))

    def test_start_rejects_harness_changed_after_approval(self):
        self._new()
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        path = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="t"))

    def test_approved_harness_hash_allows_start(self):
        self._new()
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        with patch.object(baton, "launch_leg") as launch:
            baton.cmd_start(self.project, Namespace(task="t"))
        launch.assert_called_once()

    def test_root_resolver_rejects_implicit_ancestor(self):
        child = self.project_root / "child"
        child.mkdir()
        with self.assertRaises(SystemExit):
            baton.resolve_cli_project_root(str(child), "start")
        self.assertEqual(baton.resolve_cli_project_root(str(child), "start", str(self.project_root)),
                         baton.canonical_dir(self.project_root))
        self.assertEqual(baton.resolve_cli_project_root(str(child), "init"), baton.canonical_dir(child))

    def test_legacy_start_without_harness_warns_and_does_not_render_context(self):
        task_dir = self.project_root / ".baton" / "tasks" / "legacy"
        task_dir.mkdir(parents=True)
        (task_dir / "brief.md").write_text(
            "```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [],
                                                   "frozen": [], "verify": "none"}) +
            "\n```\n# legacy\n", encoding="utf-8")
        with patch.object(baton, "launch_leg") as launch, contextlib.redirect_stdout(io.StringIO()) as out:
            baton.cmd_start(self.project, Namespace(task="legacy"))
        launch.assert_called_once()
        self.assertIn("没有 harness.md", out.getvalue())
        self.assertFalse((self.project_root / ".hermes.md").exists())
        self.assertFalse((self.project_root / "AGENTS.override.md").exists())

    def test_codex_command_uses_boundary_and_resume_cwd_contract(self):
        cfg = self.project.config
        start = baton.codex_command(self.project, "start", None, "/tmp/last.json", cfg=cfg, isolated=True)
        resume = baton.codex_command(self.project, "resume", "thread", "/tmp/last.json", cfg=cfg, isolated=True)
        self.assertIn('project_root_markers=["baton.json"]', start)
        self.assertIn('project_root_markers=["baton.json"]', resume)
        self.assertIn("-C", start)
        self.assertNotIn("-C", resume)

    def test_codex_context_is_task_local_and_restorable(self):
        self._new()
        harness = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        harness.write_text(harness.read_text(encoding="utf-8").replace(baton.HARNESS_PENDING_MARKER, ""), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        old = self.project_root / "AGENTS.override.md"
        old.write_text("user override\n", encoding="utf-8")
        baton.prepare_task_codex_context(self.project, "t")
        self.assertIn("Baton harness", old.read_text(encoding="utf-8"))
        self.assertTrue((self.state / "projects" / self.project.project_id / "tasks" / "t" / "codex").exists())
        st = self.project.load_state("t")
        baton.restore_task_codex_context(self.project, "t", st)
        self.assertEqual(old.read_text(encoding="utf-8"), "user override\n")

    def test_hermes_task_home_shares_auth_and_sets_runtime_env(self):
        cfg = self.project.config
        cfg["executor"]["backend"] = "hermes"
        global_profile = Path(baton.hermes_profile_dir(cfg))
        global_profile.mkdir(parents=True, exist_ok=True)
        auth = global_profile / "auth.json"
        auth.write_text("{}", encoding="utf-8")
        harness = self.project_root / ".baton" / "tasks" / "t"
        harness.mkdir(parents=True, exist_ok=True)
        (harness / "harness.md").write_text("approved\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        task_home = Path(baton.hermes_task_home(self.project, "t", cfg))
        self.project.update_state("t", lambda s: s.update(launch_root=str(self.project_root), hermes_home=str(task_home)))
        with patch.object(baton, "_scan_soul_with_hermes", return_value=[]):
            home = Path(baton.prepare_task_hermes_profile(self.project, "t", cfg))
        self.assertEqual(home, Path(baton.canonical_dir(Path(self.project.state_dir) / "tasks" / "t" / "hermes")))
        task_auth = home / "profiles" / cfg["executor"]["hermes_profile"] / "auth.json"
        self.assertTrue(task_auth.is_symlink())
        env = baton.hermes_executor_env(self.project, "t", 1, 1.0, cfg, self.project.load_state("t"))
        self.assertEqual(env["HERMES_HOME"], str(home))
        self.assertEqual(env["BATON_PROJECT_ROOT"], str(self.project.root))
        self.assertEqual(env["TERMINAL_CWD"], str(self.project.root))

    def test_hermes_task_home_inside_baton_home_is_reallowed_after_deny(self):
        cfg = self.project.config
        cfg["executor"]["backend"] = "hermes"
        cfg["executor"]["sandbox_exec"] = True
        task_home = Path(baton.hermes_task_home(self.project, "t", cfg))
        defines = baton.hermes_sandbox_defines(self.project, cfg, task="t")
        self.assertEqual(Path(defines["HERMES_HOME"]), task_home)
        self.assertTrue(str(task_home).startswith(str(Path(defines["BATON_HOME"]) / "projects")))

        command = baton.hermes_command(self.project, "start", None, "prompt", cfg=cfg, task="t")
        template = str(ROOT / "skills/baton/templates/hermes-sandbox.sb")
        self.assertEqual(command[:3], ["sandbox-exec", "-f", template])
        hermes_index = command.index(baton.hermes_executable(cfg))
        for key, value in (("HERMES_HOME", defines["HERMES_HOME"]), ("BATON_HOME", defines["BATON_HOME"])):
            self.assertLess(command.index("-D", 3), hermes_index)
            self.assertIn(f"{key}={value}", command)
        rules = (ROOT / "skills/baton/templates/hermes-sandbox.sb").read_text(encoding="utf-8")
        deny = rules.index('(deny file-write* (subpath (param "BATON_HOME")))')
        allow = rules.index('(allow file-write* (subpath (param "PROFILE_DIR")))')
        soul_deny = rules.index('(deny file-write* (literal (param "PROFILE_SOUL")))')
        self.assertLess(deny, allow)
        self.assertLess(allow, soul_deny)

        task_write = task_home / "sessions" / "state.db"
        other_write = Path(defines["BATON_HOME"]) / "projects" / "other" / "write"
        task_write.parent.mkdir(parents=True, exist_ok=True)
        other_write.parent.mkdir(parents=True, exist_ok=True)
        sandbox = ["sandbox-exec", "-f", template]
        for key, value in defines.items():
            sandbox += ["-D", f"{key}={value}"]
        for path, allowed in ((task_write, True), (other_write, False)):
            path.unlink(missing_ok=True)
            result = subprocess.run(sandbox + ["/bin/sh", "-c", "echo x > \"$1\"", "sh", str(path)],
                                    capture_output=True, text=True)
            if result.returncode != 0 and "sandbox_apply" in (result.stderr or ""):
                self.skipTest("Codex 沙箱禁止嵌套 sandbox-exec")
            self.assertEqual(result.returncode == 0, allowed,
                             f"{path} 写入结果不符合预期：{result.stderr or result.stdout}")
            self.assertEqual(path.exists(), allowed)

    def test_monitor_task_home_define_follows_baton_home_deny(self):
        cfg = self.project.config
        cfg["monitor"].update({"backend": "hermes", "provider": "deepseek", "model": "deepseek-v4-pro"})
        command = baton._monitor_hermes_command(self.project, "D001", cfg, "high", "review")
        template = str(ROOT / "skills/baton/templates/hermes-monitor-sandbox.sb")
        self.assertEqual(command[:3], ["sandbox-exec", "-f", template])
        hermes_index = command.index(baton.hermes_executable(cfg))
        self.assertLess(command.index("-D", 3), hermes_index)
        self.assertIn(f"HERMES_HOME={baton.canonical_dir(baton.hermes_monitor_home(self.project, cfg))}", command)
        self.assertNotIn("BATON_HOME=", command)
        rules = (ROOT / "skills/baton/templates/hermes-monitor-sandbox.sb").read_text(encoding="utf-8")
        deny = rules.index('(deny file-write*)')
        allow = rules.index('(allow file-write*\n    (subpath (param "PROFILE_DIR"))')
        soul_deny = rules.index('(deny file-write* (literal (param "PROFILE_SOUL")))')
        self.assertLess(deny, allow)
        self.assertLess(allow, soul_deny)

    def test_hermes_config_passes_project_root_to_memory_mcp(self):
        with patch.object(baton, "memory_installed", return_value=True), \
             patch.object(baton, "memory_venv_python", return_value="/tmp/baton-memory-python"):
            text = baton.hermes_config_text(self.project.config, project=self.project)
        self.assertIn('BATON_PROJECT_ROOT: "%s"' % self.project.root, text)

    def test_legacy_task_keeps_global_codex_home_and_command_context(self):
        global_home = self.home / ".codex"
        global_home.mkdir()
        (global_home / "config.toml").write_text("model = 'legacy'\n", encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_HOME": str(global_home)}, clear=False):
            env = baton.codex_executor_env(self.project, "legacy", {"launch_root": str(self.project_root)})
            self.assertEqual(env.get("CODEX_HOME"), str(global_home))
            self.assertEqual(env["BATON_PROJECT_ROOT"], str(self.project.root))
            self.assertEqual(env["TERMINAL_CWD"], str(self.project.root))
            command = baton.codex_command(self.project, "resume", "thread", "/tmp/last.json", cfg=self.project.config)
            self.assertNotIn("project_root_markers=[\"baton.json\"]", command)

    def test_legacy_hermes_env_keeps_global_home_but_sets_project_scope(self):
        cfg = dict(self.project.config)
        cfg["executor"] = dict(cfg["executor"])
        cfg["executor"]["backend"] = "hermes"
        with patch.dict(os.environ, {"HERMES_HOME": str(self.home / ".hermes")}, clear=False):
            env = baton.hermes_executor_env(self.project, "legacy", 1, 1.0, cfg,
                                            {"launch_root": str(self.project_root)})
        self.assertEqual(env["HERMES_HOME"], str(self.home / ".hermes"))
        self.assertEqual(env["BATON_PROJECT_ROOT"], str(self.project.root))
        self.assertEqual(env["TERMINAL_CWD"], str(self.project.root))

    def test_isolated_resume_uses_recorded_startup_cwd(self):
        home = baton.codex_task_home(self.project, "t")
        st = {"codex_home": home, "launch_root": str(self.project_root / "startup")}
        (self.project_root / "startup").mkdir()
        env = baton.codex_executor_env(self.project, "t", st)
        self.assertEqual(env["CODEX_HOME"], home)
        self.assertEqual(env["TERMINAL_CWD"], baton.canonical_dir(self.project_root / "startup"))

    def test_unreviewed_or_empty_harness_never_writes_context_files(self):
        self._new()
        harness = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        harness.write_text("\n", encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.prepare_task_codex_context(self.project, "t")
        self.assertFalse((self.project_root / "AGENTS.override.md").exists())
        harness.write_text(baton.HARNESS_PENDING_MARKER + "\n", encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.render_task_hermes_context(self.project, "t", {"launch_root": str(self.project_root)})
        self.assertFalse((self.project_root / ".hermes.md").exists())

    def test_codex_home_inherits_config_but_excludes_global_agents(self):
        source = self.home / ".codex"
        source.mkdir()
        (source / "config.toml").write_text("model = 'x'\n", encoding="utf-8")
        (source / "AGENTS.md").write_text("global\n", encoding="utf-8")
        (source / "AGENTS.override.md").write_text("override\n", encoding="utf-8")
        with patch.dict(os.environ, {"CODEX_HOME": str(source)}, clear=False):
            target, inherited, excluded = baton.prepare_task_codex_home(self.project, "t")
        self.assertTrue(Path(target, "config.toml").exists())
        self.assertFalse(Path(target, "AGENTS.md").exists())
        self.assertFalse(Path(target, "AGENTS.override.md").exists())
        self.assertIn("config.toml", inherited)
        self.assertEqual(excluded, ["AGENTS.md", "AGENTS.override.md"])

    def test_context_chain_follows_git_root_to_startup_directory(self):
        root = self.base / "repo"
        nested = root / "sub"
        nested.mkdir(parents=True)
        (root / "AGENTS.md").write_text("root\n", encoding="utf-8")
        (nested / "AGENTS.md").write_text("sub\n", encoding="utf-8")
        project = baton.Project(str(nested))
        with patch.object(baton, "git_repo_top", return_value=str(root)):
            self.assertEqual(baton.hermes_context_files(project),
                             [str(baton.canonical_dir(root / "AGENTS.md")),
                              str(baton.canonical_dir(nested / "AGENTS.md"))])

    def test_three_level_context_chain_and_lowercase_agents(self):
        root = self.base / "repo3"
        nested = root / "one" / "two"
        nested.mkdir(parents=True)
        for directory, name in ((root, "AGENTS.md"), (root / "one", "agents.md"), (nested, "AGENTS.md")):
            (directory / name).write_text(str(directory), encoding="utf-8")
        project = baton.Project(str(nested))
        with patch.object(baton, "git_repo_top", return_value=str(root)):
            paths = baton.hermes_context_files(project)
        self.assertEqual(paths, [str(baton.canonical_dir(root / "AGENTS.md")),
                                 str(baton.canonical_dir(root / "one" / "agents.md")),
                                 str(baton.canonical_dir(nested / "AGENTS.md"))])

    def test_context_chain_uses_only_one_agents_file_per_level(self):
        root = self.base / "repo4"
        nested = root / "one"
        nested.mkdir(parents=True)
        for directory in (root, nested):
            (directory / "AGENTS.md").write_text("upper\n", encoding="utf-8")
            (directory / "agents.md").write_text("lower\n", encoding="utf-8")
        project = baton.Project(str(nested))
        with patch.object(baton, "git_repo_top", return_value=str(root)):
            paths = baton.hermes_context_files(project)
        self.assertEqual(paths, [str(baton.canonical_dir(root / "AGENTS.md")),
                                 str(baton.canonical_dir(nested / "AGENTS.md"))])

    def test_pinned_memory_failure_is_written_to_harness(self):
        from baton_memory.ledger import MemoryLedger
        with patch.object(MemoryLedger, "latest", side_effect=RuntimeError("boom")):
            text = baton.build_harness_draft(self.project, "t")
        self.assertIn("记忆台账 pinned 条目读取失败", text)
        self.assertIn("boom", text)

    def test_hermes_context_backup_exclude_and_restore(self):
        self._new()
        harness = self.project_root / ".baton" / "tasks" / "t" / "harness.md"
        harness.write_text("approved\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="t"))
        original = self.project_root / ".hermes.md"
        original.write_text("user context\n", encoding="utf-8")
        with patch.object(baton, "git_repo_top", return_value=str(self.project_root)):
            subprocess.run(["git", "init", "-q", str(self.project_root)], check=True)
            st = {"launch_root": str(self.project_root), "hermes_home": baton.hermes_task_home(self.project, "t")}
            path = baton.render_task_hermes_context(self.project, "t", st)
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "approved\n")
            state = self.project.load_state("t")
            self.assertIn(".hermes.md", (self.project_root / ".git" / "info" / "exclude").read_text())
            baton.restore_task_hermes_context(self.project, "t", state)
        self.assertEqual(original.read_text(encoding="utf-8"), "user context\n")

    def test_monitor_profile_is_separate_and_shares_auth(self):
        cfg = self.project.config
        cfg["monitor"].update({"backend": "hermes", "provider": "deepseek", "model": "deepseek-v4-pro"})
        executor_auth = Path(baton.hermes_profile_auth_path(cfg))
        executor_auth.parent.mkdir(parents=True, exist_ok=True)
        executor_auth.write_text("{}", encoding="utf-8")
        home = Path(baton.prepare_monitor_hermes_profile(self.project, cfg))
        self.assertNotEqual(home, Path(baton.hermes_home(cfg)))
        monitor_profile = Path(baton.hermes_profile_dir(cfg, home=str(home), profile="baton-monitor-v2"))
        self.assertTrue((monitor_profile / "auth.json").is_symlink())
        command = baton._monitor_hermes_command(self.project, "D001", cfg, "high", "review")
        self.assertIn("baton-monitor-v2", command)
        self.assertNotIn((cfg.get("executor") or {}).get("hermes_profile", "baton-exec-v2"), command)

    def test_same_startup_directory_rejects_before_overwriting_first_context(self):
        self._new("first")
        self._new("second")
        first_harness = self.project_root / ".baton" / "tasks" / "first" / "harness.md"
        second_harness = self.project_root / ".baton" / "tasks" / "second" / "harness.md"
        first_harness.write_text("approved\n", encoding="utf-8")
        second_harness.write_text("approved\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_approve(self.project, Namespace(task="first"))
            baton.cmd_harness_approve(self.project, Namespace(task="second"))
        first_context = self.project_root / ".hermes.md"
        first_context.write_text("first context\n", encoding="utf-8")
        self.project.update_state("first", lambda s: s.update(legs=[{"n": 1, "status": "running"}],
                                                                launch_root=str(self.project_root)))
        with self.assertRaises(SystemExit):
            baton.launch_leg(self.project, "second", "start", "prompt")
        self.assertEqual(first_context.read_text(encoding="utf-8"), "first context\n")
        self.assertFalse((self.project_root / "AGENTS.override.md").exists())

    def test_generated_context_names_are_ignored_by_freeze_walk(self):
        (self.project_root / ".hermes.md").write_text("generated\n", encoding="utf-8")
        (self.project_root / "AGENTS.override.md").write_text("generated\n", encoding="utf-8")
        files = set(baton._walk_code_files(self.project))
        self.assertNotIn(".hermes.md", files)
        self.assertNotIn("AGENTS.override.md", files)

    def test_snapshot_and_diff_exclude_generated_context_files(self):
        subprocess.run(["git", "init", "-q", str(self.project_root)], check=True)
        (self.project_root / "a.py").write_text("x = 1\n", encoding="utf-8")
        store = baton.SnapStore(self.project, "shadow")
        base = store.snapshot("base")
        (self.project_root / ".hermes.md").write_text("generated\n", encoding="utf-8")
        (self.project_root / "AGENTS.override.md").write_text("generated\n", encoding="utf-8")
        end = store.snapshot("end")
        entries = baton.diff_tree_entries(store, base, end)
        self.assertFalse(any(path in {".hermes.md", "AGENTS.override.md"} for _, _, _, path in entries))
        diff = store.git("diff", "--name-only", base, end, "--", ".", *baton.code_excludes(store), check=False)
        self.assertNotIn(".hermes.md", diff)
        self.assertNotIn("AGENTS.override.md", diff)

    def test_memory_server_prefers_project_root_env(self):
        other = self.base / "other"
        other.mkdir()
        with patch.dict(os.environ, {"BATON_PROJECT_ROOT": str(self.project_root)}, clear=False):
            self.assertEqual(memory_server._find_project(str(other)), str(self.project_root.resolve()))


if __name__ == "__main__":
    unittest.main()
