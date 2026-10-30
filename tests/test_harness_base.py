import contextlib
import hashlib
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


class HarnessBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.home = self.base / "home"
        self.state = self.base / "baton-home"
        self.project_root = self.base / "project"
        self.home.mkdir()
        self.project_root.mkdir()
        self.source = self.home / ".claude" / "CLAUDE.md"
        self.source.parent.mkdir(parents=True)
        self.source.write_text("USER-SOURCE-ONLY\n主会话流程\n", encoding="utf-8")
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

    def _base_args(self, init=False, force=False, approve=False):
        return Namespace(init=init, force=force, approve=approve)

    def _run_base(self, **kwargs):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_harness_base(None, self._base_args(**kwargs))
        return out.getvalue()

    def _approved_custom_base(self):
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_base(None, self._base_args(init=True, force=Path(baton.harness_base_path()).exists()))
        path = Path(baton.harness_base_path())
        path.write_text(
            path.read_text(encoding="utf-8").splitlines()[0] + "\n" +
            baton.HARNESS_PENDING_MARKER + "\n\nEXECUTOR-BASE-RULE\n",
            encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_harness_base(None, self._base_args(approve=True))
        return path

    def test_init_generates_pending_base_and_rejects_repeat(self):
        output = self._run_base(init=True)
        path = Path(baton.harness_base_path())
        text = path.read_text(encoding="utf-8")
        self.assertIn("已生成待审", output)
        self.assertTrue(text.splitlines()[0].startswith("<!-- baton-harness-base source="))
        self.assertEqual(text.splitlines()[1], baton.HARNESS_PENDING_MARKER)
        self.assertIn("USER-SOURCE-ONLY", text)
        with self.assertRaises(SystemExit):
            baton.cmd_harness_base(None, self._base_args(init=True))

    def test_force_init_backs_up_existing_base(self):
        self._run_base(init=True)
        path = Path(baton.harness_base_path())
        original = path.read_text(encoding="utf-8")
        self._run_base(init=True, force=True)
        backups = list(path.parent.glob(path.name + ".bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_approve_removes_marker_and_refreshes_source_hash(self):
        self._run_base(init=True)
        path = Path(baton.harness_base_path())
        path.write_text(path.read_text(encoding="utf-8") + "\nEXECUTOR-ONLY\n", encoding="utf-8")
        self._run_base(approve=True)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn(baton.HARNESS_PENDING_MARKER, text)
        expected = hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.assertIn("source_sha256=" + expected, text.splitlines()[0])
        self.assertIn("EXECUTOR-ONLY", text)

    def test_draft_uses_approved_base_without_user_source(self):
        self._approved_custom_base()
        task = "draft"
        draft = baton.build_harness_draft(self.project, task)
        self.assertIn("### 用户级基底（harness-base.md）", draft)
        self.assertIn("EXECUTOR-BASE-RULE", draft)
        self.assertNotIn("USER-SOURCE-ONLY", draft)
        self.assertNotIn(str(self.source), draft)

    def test_missing_and_pending_base_are_explained_without_content(self):
        missing = baton.build_harness_draft(self.project, "missing")
        self.assertIn("未找到用户级基底", missing)
        self.assertNotIn("USER-SOURCE-ONLY", missing)
        self._run_base(init=True)
        pending = baton.build_harness_draft(self.project, "pending")
        self.assertIn("仍待审定", pending)
        self.assertNotIn("USER-SOURCE-ONLY", pending)

    def test_source_hash_change_warns_in_draft_and_new_stderr(self):
        self._approved_custom_base()
        self.source.write_text("USER-SOURCE-CHANGED\n", encoding="utf-8")
        draft = baton.build_harness_draft(self.project, "stale")
        self.assertIn("自基底审定后已修改", draft)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            baton.cmd_new(self.project, Namespace(task="stale", brief=None))
        self.assertIn("自基底审定后已修改", err.getvalue())

    def test_baton_home_environment_controls_path_and_cli_works_without_project(self):
        self.assertEqual(Path(baton.harness_base_path()), Path(baton.canonical_dir(self.state)) / "harness-base.md")
        result = subprocess.run(
            [sys.executable, str(ROOT / "skills" / "baton" / "scripts" / "baton.py"), "harness", "base", "--init"],
            cwd=str(self.base), env=os.environ.copy(), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(self.state / "harness-base.md"), result.stdout)

    def _doctor_output(self):
        out = io.StringIO()
        patches = [
            patch.object(baton, "doctor_memory", return_value=False),
            patch.object(baton, "safe_quota", return_value=None),
            patch.object(baton.shutil, "which", return_value="/usr/bin/codex"),
            patch.object(baton, "run", return_value="codex 0"),
            patch.object(baton.codex_quota, "describe", return_value="quota"),
            patch.object(baton.codex_quota, "is_low", return_value=False),
        ]
        with contextlib.ExitStack() as stack:
            for item in patches:
                stack.enter_context(item)
            with contextlib.redirect_stdout(out):
                with self.assertRaises(SystemExit) as raised:
                    baton.cmd_doctor(self.project, Namespace(monitor_probe=False))
        self.assertEqual(raised.exception.code, 0)
        return out.getvalue()

    def test_doctor_warns_for_missing_pending_and_stale_and_ok_for_approved(self):
        self.assertIn("[WARN] harness-base：不存在", self._doctor_output())
        self._run_base(init=True)
        self.assertIn("[WARN] harness-base：存在但仍待审定", self._doctor_output())
        self._approved_custom_base()
        self.source.write_text("changed\n", encoding="utf-8")
        self.assertIn("[WARN] harness-base：源文件哈希不一致", self._doctor_output())
        self.source.write_text("USER-SOURCE-ONLY\n主会话流程\n", encoding="utf-8")
        self._run_base(approve=True)
        self.assertIn("[OK] harness-base：已审定且源文件哈希一致", self._doctor_output())


if __name__ == "__main__":
    unittest.main()
