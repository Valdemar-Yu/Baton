import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class BatonJsonTests(unittest.TestCase):
    def project(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        (root / ".baton").mkdir()
        return tmp, root, baton.Project(str(root))

    def write_json(self, path, data):
        path.write_text(json.dumps(data), encoding="utf-8")

    def test_merge_order_and_quota_is_skill_only(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / ".baton" / "config.json", {
            "judge": {"model": "sonnet"},
            "executor": {"model": "layer2", "network_access": True},
            "supervision": {"test_command": "old"},
            "quota": {"warn_remaining_percent": 99},
        })
        self.write_json(root / "baton.json", {
            "judge": {"council_for_major_decisions": False},
            "executor": {"model": "layer3"},
            "supervision": {"test_command": "new"},
            "quota": {"warn_remaining_percent": 1},
        })
        cfg = project.config
        self.assertEqual(cfg["judge"]["model"], "sonnet")
        self.assertFalse(cfg["judge"]["council_for_major_decisions"])
        self.assertEqual(cfg["executor"]["model"], "layer3")
        self.assertTrue(cfg["executor"]["network_access"])
        self.assertEqual(cfg["supervision"]["test_command"], "new")
        self.assertEqual(cfg["quota"]["warn_remaining_percent"], 5)

    def test_missing_baton_json_uses_defaults(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        cfg = project.config
        self.assertEqual(cfg["judge"]["model"], "opus")
        self.assertEqual(cfg["executor"]["model"], "gpt-6.1-sol")

    def test_invalid_baton_json_is_an_error(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        (root / "baton.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(baton.ConfigError) as ctx:
            project.config
        self.assertIn("baton.json", str(ctx.exception))
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_invalid_judge_model_is_an_error(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "baton.json", {"judge": {"model": "unknown"}})
        with self.assertRaises(baton.ConfigError) as ctx:
            project.config
        self.assertIn("judge.model", str(ctx.exception))

    def test_init_creates_baton_json_without_overwriting(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        baton.cmd_init(project, Namespace(no_statusline=True))
        path = root / "baton.json"
        self.assertTrue(path.exists())
        first = path.read_text(encoding="utf-8")
        data = json.loads(first)
        self.assertEqual(data["judge"]["model"], "opus")
        path.write_text(json.dumps({"judge": {"model": "haiku"}}), encoding="utf-8")
        baton.cmd_init(project, Namespace(no_statusline=True))
        self.assertEqual(path.read_text(encoding="utf-8"), json.dumps({"judge": {"model": "haiku"}}))


if __name__ == "__main__":
    unittest.main()
