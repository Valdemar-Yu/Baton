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


class LeanModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".baton").mkdir(parents=True)
        self.env = patch.dict(os.environ, {
            "HOME": str(Path(self.tmp.name) / "home"), "USERPROFILE": str(Path(self.tmp.name) / "home"),
            "BATON_HOME": str(Path(self.tmp.name) / "state"),
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def project(self):
        return baton.Project(str(self.root))

    def test_lean_defaults_and_project_keys_win(self):
        self.write("baton.json", {
            "mode": "lean",
            "conductor": {"reasoning_effort": "high"},
            "executor": {
                "model": "custom-model", "reasoning_effort": "xhigh",
                "escalation": {"enabled": True, "model": "custom-escalation"},
            },
            "monitor": {"enabled": True},
            "supervision": {"interval_minutes": 7, "max_unsupervised_minutes": 9},
        })
        cfg = self.project().config
        self.assertEqual(cfg["mode"], "lean")
        self.assertEqual(cfg["executor"]["model"], "custom-model")
        self.assertEqual(cfg["executor"]["reasoning_effort"], "xhigh")
        self.assertTrue(cfg["executor"]["escalation"]["enabled"])
        self.assertEqual(cfg["conductor"]["reasoning_effort"], "high")
        self.assertTrue(cfg["monitor"]["enabled"])
        self.assertEqual(cfg["supervision"]["interval_minutes"], 7)
        self.assertEqual(cfg["supervision"]["max_unsupervised_minutes"], 9)

    def test_lean_defaults_apply_when_project_does_not_override(self):
        self.write("baton.json", {"mode": "lean"})
        cfg = self.project().config
        self.assertEqual(cfg["executor"]["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["executor"]["reasoning_effort"], "medium")
        self.assertEqual(cfg["executor"]["escalation"], {"enabled": False})
        self.assertEqual(cfg["conductor"]["reasoning_effort"], "medium")
        self.assertFalse(cfg["monitor"]["enabled"])
        self.assertEqual(cfg["supervision"]["interval_minutes"], 0)
        self.assertEqual(cfg["supervision"]["max_unsupervised_minutes"], 0)

    def test_invalid_mode_is_config_error(self):
        self.write("baton.json", {"mode": "turbo"})
        with self.assertRaises(baton.ConfigError) as ctx:
            self.project().config
        self.assertIn("mode", str(ctx.exception))

    def test_protocol_text_selects_lean_protocol(self):
        self.write("baton.json", {"mode": "lean"})
        text = baton.protocol_text(self.project(), "task")
        self.assertIn("一轮做到底", text)
        self.assertIn("只有缺权限、缺网络", text)
        self.assertNotIn("大修改写汇报后停下", text)
        self.assertNotIn("{{", text)

    def test_interval_zero_never_emits_tick(self):
        self.assertFalse(baton.supervision_tick_due(3600, 0))
        self.assertFalse(baton.supervision_tick_due(0, 0))
        self.assertTrue(baton.supervision_tick_due(61, 1))

    def test_config_and_event_have_lean_notice(self):
        self.write("baton.json", {"mode": "lean"})
        project = self.project()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_config(project, Namespace(json=False))
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[0].endswith("monitor=gpt-5.6-luna mode=lean"))
        self.assertIn(baton.LEAN_HINT, lines)
        event = baton.leg_event_text(project, "task", {
            "n": 1, "status": "done", "started_at": 0, "ended_at": 1,
        })
        self.assertIn("BATON::EVENT DONE", event)
        self.assertIn(baton.LEAN_HINT, event)


if __name__ == "__main__":
    unittest.main()
