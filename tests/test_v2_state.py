import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class V2StateTests(unittest.TestCase):
    def setUp(self):
        self.env_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.env_tmp.cleanup)
        env_root = Path(self.env_tmp.name)
        self.home = env_root / "home"
        self.baton_home = env_root / "baton-home"
        self.env = patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.baton_home),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def init_git(self, path):
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-q", str(path)], check=True)

    def test_locator_uses_nearest_baton_json(self):
        root = Path(self.env_tmp.name) / "repo"
        self.init_git(root)
        self.write_json(root / "baton.json", {"judge": {"model": "opus"}})
        project = root / "one" / "two"
        project.mkdir(parents=True)
        self.write_json(root / "one" / "baton.json", {"judge": {"model": "sonnet"}})
        start = project / "three"
        start.mkdir()
        self.assertEqual(baton.find_project_dir(str(start)), os.path.realpath(str(root / "one")))

    def test_locator_does_not_cross_git_top(self):
        outer = Path(self.env_tmp.name) / "outer"
        self.write_json(outer / "baton.json", {"judge": {"model": "opus"}})
        repo = outer / "repo"
        self.init_git(repo)
        start = repo / "child"
        start.mkdir()
        self.assertEqual(baton.find_project_dir(str(start)), os.path.realpath(str(start)))

    def test_locator_without_git_only_checks_start(self):
        parent = Path(self.env_tmp.name) / "parent"
        self.write_json(parent / "baton.json", {"judge": {"model": "opus"}})
        start = parent / "child"
        start.mkdir()
        self.assertEqual(baton.find_project_dir(str(start)), os.path.realpath(str(start)))
        self.assertEqual(baton.find_project_dir(str(parent)), os.path.realpath(str(parent)))

    def test_init_boundary_requires_acceptance_before_creating_files(self):
        root = Path(self.env_tmp.name) / "repo"
        self.init_git(root)
        project_root = root / "project"
        project_root.mkdir()
        self.write_json(project_root / "baton.json", {"judge": {"model": "opus"}})
        project = baton.Project(str(project_root))
        with self.assertRaises(SystemExit) as ctx:
            baton.cmd_init(project, Namespace(no_statusline=True, accept_boundary=False))
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse((project_root / ".baton").exists())
        self.assertFalse(Path(project.state_dir).exists())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_init(project, Namespace(no_statusline=True, accept_boundary=True))
        self.assertIn("项目目录：", out.getvalue())
        self.assertIn("git 顶层：", out.getvalue())
        self.assertTrue((project_root / ".baton" / "config.json").exists())
        self.assertTrue(Path(project.state_dir).is_dir())

    def test_external_state_is_used_and_legacy_state_imported_once(self):
        root = Path(self.env_tmp.name) / "project"
        (root / ".baton" / "tasks" / "legacy").mkdir(parents=True)
        original = {
            "task": "legacy", "status": "idle", "legs": [],
            "base_ref": "refs/baton/legacy/base",
        }
        legacy_path = root / ".baton" / "tasks" / "legacy" / "state.json"
        self.write_json(legacy_path, original)
        project = baton.Project(str(root))
        state = project.load_state("legacy")
        self.assertEqual({k: v for k, v in state.items() if k != "imported_from_v1"}, original)
        self.assertIsInstance(state["imported_from_v1"], str)
        self.assertEqual(json.loads(legacy_path.read_text()), original)
        self.assertEqual(Path(project.state_path("legacy")).parent,
                         self.baton_home / "projects" / project.project_id / "tasks" / "legacy")
        self.assertTrue(Path(project.state_path("legacy")).exists())
        project.update_state("legacy", lambda s: s.update(status="running"))
        self.assertEqual(project.read_state("legacy")["status"], "running")
        self.assertTrue(Path(project.state_lock_path("legacy")).exists())
        self.assertFalse((root / ".baton" / "tasks" / "legacy" / "state.lock").exists())

    def test_new_state_does_not_write_project_task_state(self):
        root = Path(self.env_tmp.name) / "project"
        (root / ".baton").mkdir(parents=True)
        project = baton.Project(str(root))
        project.update_state("new", lambda s: s.update(task="new", status="draft"))
        self.assertEqual(project.read_state("new")["status"], "draft")
        self.assertTrue(Path(project.state_path("new")).exists())
        self.assertFalse((root / ".baton" / "tasks" / "new" / "state.json").exists())
        self.assertFalse((root / ".baton" / "tasks" / "new" / "state.lock").exists())

    def test_tasks_unions_human_and_external_task_directories(self):
        root = Path(self.env_tmp.name) / "project"
        (root / ".baton" / "tasks" / "human").mkdir(parents=True)
        project = baton.Project(str(root))
        (Path(project.state_dir) / "tasks" / "external").mkdir(parents=True)
        self.assertEqual(project.tasks(), ["external", "human"])

    def test_launch_lock_is_project_external(self):
        root = Path(self.env_tmp.name) / "project"
        (root / ".baton" / "tasks" / "task").mkdir(parents=True)
        (root / ".baton" / "tasks" / "task" / "brief.md").write_text("filled", encoding="utf-8")
        (root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        project = baton.Project(str(root))

        class FakeProcess:
            pid = 123456

        fake_quota = patch.object(baton, "safe_quota", return_value=None)
        fake_describe = patch.object(baton.codex_quota, "describe", return_value="quota ok")
        fake_popen = patch.object(baton.subprocess, "Popen", return_value=FakeProcess())
        with fake_quota, fake_describe, fake_popen, patch.object(baton, "is_git", return_value=False), contextlib.redirect_stdout(io.StringIO()):
            baton.launch_leg(project, "task", "start", "prompt")
        self.assertTrue(Path(project.launch_lock_path()).exists())
        self.assertFalse((root / ".baton" / "launch.lock").exists())
        self.assertTrue(Path(project.state_path("task")).exists())

    def test_main_reports_uninitialized_when_no_project_is_found(self):
        root = Path(self.env_tmp.name) / "plain"
        root.mkdir()
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        env["BATON_HOME"] = str(self.baton_home)
        p = subprocess.run([
            sys.executable, str(ROOT / "skills/baton/scripts/baton.py"),
            "--root", str(root), "list",
        ], env=env, text=True, capture_output=True)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("还没有初始化", p.stderr)


if __name__ == "__main__":
    unittest.main()
