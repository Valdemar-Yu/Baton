import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class ScopeAcceptanceGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-scope-guard-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".baton").mkdir(parents=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.env = patch.dict(os.environ, {
            "HOME": str(Path(self.tmp.name) / "home"),
            "BATON_HOME": str(Path(self.tmp.name) / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = baton.Project(str(self.root))

    def write(self, rel, text="test\n"):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def brief(self, scope):
        task_dir = self.root / ".baton" / "tasks" / "task"
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "brief.md").write_text(
            "```baton-scope\n" + json.dumps(scope) + "\n```\n# task\n", encoding="utf-8")

    def start(self, scope):
        self.brief(scope)
        return baton.cmd_start(self.project, Namespace(task="task"))

    def test_allow_conflict_with_acceptance_is_rejected(self):
        self.write("tests/test_app.py")
        self.brief({"allow": ["tests/test_app.py"], "deny": [], "acceptance": ["tests/test_app.py"],
                    "frozen": [], "verify": "none"})
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="task"))
        self.assertIn("tests/test_app.py", err.getvalue())
        self.assertIn("从 allow 移除", err.getvalue())

    def test_locking_allow_parent_is_rejected(self):
        self.write("tests/test_app.py")
        self.brief({"allow": ["tests/test_app.py"], "deny": [], "acceptance": ["tests"],
                    "frozen": [], "verify": "none"})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="task"))

    def test_allow_conflict_with_frozen_is_rejected(self):
        self.write("tests/test_app.py")
        self.brief({"allow": ["tests/test_app.py"], "deny": [], "acceptance": [],
                    "frozen": ["tests/test_app.py"], "verify": "none"})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            baton.cmd_start(self.project, Namespace(task="task"))

    def test_missing_acceptance_is_rejected_with_scope_guidance(self):
        for entry in ("待通过的 SQL 回归测试", "tests/test_new.py", "tests/test_new.py::test_ok"):
            with self.subTest(entry=entry):
                self.brief({"allow": ["tests"], "deny": [],
                            "acceptance": [entry], "frozen": [], "verify": "none"})
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised, \
                        patch.object(baton, "launch_leg") as launch:
                    baton.cmd_start(self.project, Namespace(task="task"))
                self.assertEqual(raised.exception.code, 1)
                message = err.getvalue()
                self.assertIn(entry, message)
                self.assertIn("未匹配到项目中已存在的测试文件", message)
                self.assertIn("新测试放 allow", message)
                self.assertIn("文字标准写简报正文", message)
                launch.assert_not_called()
                self.assertFalse(Path(baton.freeze_dir(self.project, "task")).exists())

    def test_narrow_acceptance_inside_allow_directory_is_legal(self):
        self.write("tests/acceptance/test_app.py")
        with patch.object(baton, "launch_leg") as launch:
            self.start({"allow": ["tests"], "deny": [],
                        "acceptance": ["tests/acceptance"],
                        "frozen": [], "verify": "none"})
        launch.assert_called_once()

    def test_empty_acceptance_starts_normally(self):
        self.write("src/app.py", "VALUE = 1\n")
        with patch.object(baton, "launch_leg") as launch:
            self.start({"allow": ["src"], "deny": [], "acceptance": [],
                        "frozen": [], "verify": "none"})
        launch.assert_called_once()

    def test_existing_node_id_starts_normally(self):
        self.write("src/app.py", "VALUE = 1\n")
        self.write("tests/test_app.py", "def test_ok(): pass\n")
        with patch.object(baton, "launch_leg") as launch:
            self.start({"allow": ["src"], "deny": [],
                        "acceptance": ["tests/test_app.py::test_ok"],
                        "frozen": [], "verify": "none"})
        launch.assert_called_once()

    def test_existing_acceptance_glob_starts_normally(self):
        self.write("tests/test_app.py")
        with patch.object(baton, "launch_leg") as launch:
            self.start({"allow": ["src/app.py", "tests/test_new.py"], "deny": [],
                        "acceptance": ["tests/test_*.py"], "frozen": [], "verify": "none"})
        launch.assert_called_once()

    def test_normalized_root_and_glob_conflict_rules(self):
        cases = [("./tests/", "tests", True), ("tests/test_app.py::test_ok", "tests/test_app.py", True),
                 (".", "src/app.py", True), ("", "src", True), ("tests/acceptance", "tests", False),
                 ("tests", ".", False), ("tests/test_*.py", "tests/test_*.py", True),
                 ("tests", "tests/*.py", False), ("tests/*", "tests/test_app.py", False)]
        for locked, allowed, expected in cases:
            with self.subTest(locked=locked, allowed=allowed):
                self.assertEqual(baton._scope_covers(locked, allowed), expected)

    def test_existing_task_start_does_not_revalidate_scope(self):
        self.project.update_state("task", lambda state: state.update(
            legs=[{"n": 1, "status": "stopped"}], base_ref="existing-base"))
        with patch.object(baton, "launch_leg") as launch, \
                patch.object(baton, "_validate_start_scope", side_effect=AssertionError("unexpected validation")):
            self.start({"allow": ["tests/test_new.py"], "deny": [],
                        "acceptance": ["tests/test_new.py"], "frozen": [], "verify": "none"})
        launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
