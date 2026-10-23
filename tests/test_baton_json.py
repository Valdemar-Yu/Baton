import contextlib
import hashlib
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


class BatonJsonTests(unittest.TestCase):
    def setUp(self):
        env_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(env_tmp.cleanup)
        self.env_root = Path(env_tmp.name)
        env = patch.dict(os.environ, {
            "HOME": str(self.env_root / "home"),
            "BATON_HOME": str(self.env_root / "state"),
        })
        env.start()
        self.addCleanup(env.stop)

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
        self.assertNotIn("judge", cfg)
        self.assertEqual(cfg["conductor"]["model"], "sonnet")
        self.assertFalse(cfg["conductor"]["council_for_major_decisions"])
        self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["conductor"]["model"], "sonnet")
        self.assertFalse(cfg["conductor"]["council_for_major_decisions"])
        self.assertEqual(cfg["executor"]["model"], "layer3")
        self.assertTrue(cfg["executor"]["network_access"])
        self.assertEqual(cfg["supervision"]["test_command"], "new")
        self.assertEqual(cfg["quota"]["warn_remaining_percent"], 5)

    def test_missing_baton_json_uses_defaults(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        cfg = project.config
        self.assertEqual(cfg["conductor"]["model"], "gpt-6-astra")
        self.assertTrue(cfg["conductor"]["council_for_major_decisions"])
        self.assertEqual(cfg["executor"]["backend"], "codex")
        self.assertEqual(cfg["executor"]["model"], "gpt-6.1-sol")
        self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-luna")

    def test_invalid_baton_json_is_an_error(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        (root / "baton.json").write_text("{bad", encoding="utf-8")
        with self.assertRaises(baton.ConfigError) as ctx:
            project.config
        self.assertIn("baton.json", str(ctx.exception))
        self.assertIn("不是合法 JSON", str(ctx.exception))

    def test_invalid_legacy_judge_model_is_a_conductor_error(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "baton.json", {"judge": {"model": "unknown"}})
        with self.assertRaises(baton.ConfigError) as ctx:
            project.config
        self.assertIn("conductor.model", str(ctx.exception))

    def test_init_creates_baton_json_without_overwriting(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / ".baton" / "config.json", {"executor": {"backend": "codex"}})
        baton.cmd_init(project, Namespace(no_statusline=True))
        path = root / "baton.json"
        self.assertTrue(path.exists())
        first = path.read_text(encoding="utf-8")
        data = json.loads(first)
        self.assertEqual(data["conductor"], {"model": "gpt-6-astra", "council_for_major_decisions": True})
        self.assertNotIn("judge", data)
        self.assertEqual(data["monitor"]["model"], "gpt-5.6-luna")
        self.assertEqual(data["executor"]["backend"], "codex")
        path.write_text(json.dumps({"judge": {"model": "haiku"}}), encoding="utf-8")
        baton.cmd_init(project, Namespace(no_statusline=True))
        self.assertEqual(path.read_text(encoding="utf-8"), json.dumps({"judge": {"model": "haiku"}}))

    def test_legacy_judge_migrates_from_either_project_layer(self):
        for filename in ("baton.json", ".baton/config.json"):
            with self.subTest(filename=filename):
                tmp, root, project = self.project()
                self.addCleanup(tmp.cleanup)
                self.write_json(root / filename, {
                    "judge": {"model": "haiku", "council_for_major_decisions": False},
                })
                cfg = project.config
                self.assertEqual(cfg["conductor"], {"model": "haiku", "council_for_major_decisions": False, "reasoning_effort": "ultra", "service_tier": "priority", "disable_fast_mode": False})
                self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-luna")
                self.assertNotIn("judge", cfg)
                self.assertNotIn("conductor", json.loads((root / filename).read_text()))

    def test_explicit_conductor_in_either_layer_prevents_migration(self):
        for filename in ("baton.json", ".baton/config.json"):
            with self.subTest(filename=filename):
                tmp, root, project = self.project()
                self.addCleanup(tmp.cleanup)
                self.write_json(root / ".baton/config.json", {
                    "judge": {"model": "haiku", "council_for_major_decisions": False},
                })
                self.write_json(root / "baton.json", {"judge": {"model": "fable"}})
                raw = json.loads((root / filename).read_text())
                raw["conductor"] = {"model": "sonnet", "council_for_major_decisions": True}
                self.write_json(root / filename, raw)
                cfg = project.config
                self.assertEqual(cfg["conductor"], {"model": "sonnet", "council_for_major_decisions": True, "reasoning_effort": "ultra", "service_tier": "priority", "disable_fast_mode": False})
                self.assertEqual(cfg["monitor"]["model"], "fable")
                self.assertNotIn("judge", cfg)

    def test_early_v2_judge_is_renamed_to_monitor_with_notice(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "baton.json", {
            "conductor": {"model": "sonnet"},
            "judge": {"model": "claude-monitor", "timeout_minutes": 12},
        })
        cfg = project.config
        self.assertEqual(cfg["conductor"]["model"], "sonnet")
        self.assertEqual(cfg["monitor"]["model"], "claude-monitor")
        self.assertEqual(cfg["monitor"]["timeout_minutes"], 12)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_config(project, Namespace(json=False))
        self.assertIn("judge 节已按 v2 早期草案改名为 monitor", out.getvalue())

    def test_explicit_monitor_overrides_early_judge_alias(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "baton.json", {
            "conductor": {"model": "sonnet"},
            "judge": {"model": "claude-monitor"},
            "monitor": {"model": "gpt-5.6-sol", "timeout_minutes": 7},
        })
        cfg = project.config
        self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-sol")
        self.assertEqual(cfg["monitor"]["timeout_minutes"], 7)

    def test_partial_explicit_conductor_uses_defaults_and_merge_order(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / ".baton/config.json", {
            "conductor": {"model": "sonnet", "council_for_major_decisions": False},
            "judge": {"model": "haiku"},
        })
        self.write_json(root / "baton.json", {"conductor": {"model": "fable"}})
        self.assertEqual(project.config["conductor"], {"model": "fable", "council_for_major_decisions": False, "reasoning_effort": "ultra", "service_tier": "priority", "disable_fast_mode": False})
        self.assertEqual(project.config["monitor"]["model"], "haiku")
        self.write_json(root / ".baton/config.json", {"judge": {"model": "haiku", "council_for_major_decisions": False}})
        self.write_json(root / "baton.json", {"conductor": {}})
        self.assertEqual(project.config["conductor"], {"model": "gpt-6-astra", "council_for_major_decisions": True, "reasoning_effort": "ultra", "service_tier": "priority", "disable_fast_mode": False})
        self.assertEqual(project.config["monitor"]["model"], "haiku")

    def test_invalid_role_models_and_sections_raise_config_error(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        for role in ("conductor", "monitor"):
            invalid_values = ("unknown", None, [], {}) if role == "conductor" else ("", None, [], {})
            for value in invalid_values:
                with self.subTest(role=role, model=value):
                    self.write_json(root / "baton.json", {role: {"model": value}})
                    with self.assertRaises(baton.ConfigError) as ctx:
                        project.config
                    self.assertIn(role + ".model", str(ctx.exception))
            for section in (None, [], "opus"):
                with self.subTest(role=role, section=section):
                    self.write_json(root / "baton.json", {role: section})
                    with self.assertRaises(baton.ConfigError) as ctx:
                        project.config
                    self.assertIn(role, str(ctx.exception))

    def test_all_supported_models_are_accepted(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        for model in ("opus", "sonnet", "haiku", "fable"):
            with self.subTest(model=model):
                self.write_json(root / "baton.json", {
                    "conductor": {"model": model}, "monitor": {"model": model},
                })
                self.assertEqual(project.config["conductor"]["model"], model)
                self.assertEqual(project.config["monitor"]["model"], model)

    def test_migration_ignores_new_skill_defaults(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        defaults = json.loads((Path(baton.SKILL_DIR) / "config.json").read_text())
        defaults["conductor"] = {"model": "fable", "council_for_major_decisions": False}
        defaults["executor"]["backend"] = "hermes"
        skill = self.env_root / "skill"
        skill.mkdir()
        self.write_json(skill / "config.json", defaults)
        self.write_json(root / "baton.json", {"judge": {"model": "haiku"}})
        with patch.object(baton, "SKILL_DIR", str(skill)):
            self.assertEqual(project.config["conductor"], {"model": "haiku", "council_for_major_decisions": False})
            self.assertEqual(project.config["executor"]["backend"], "codex")
            self.assertEqual(project.config["monitor"]["model"], "gpt-5.6-luna")
            self.assertTrue(project.needs_backend_migration)
            self.write_json(root / "baton.json", {"executor": {"backend": "codex"}})
            self.assertEqual(project.config["executor"]["backend"], "codex")
            self.assertFalse(project.needs_backend_migration)

    def test_explicit_backend_in_either_project_layer_suppresses_notice(self):
        for filename in ("baton.json", ".baton/config.json"):
            with self.subTest(filename=filename):
                tmp, root, project = self.project()
                self.addCleanup(tmp.cleanup)
                self.write_json(root / filename, {"executor": {"backend": "codex"}})
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    baton.cmd_config(project, Namespace(json=False))
                self.assertEqual(project.config["executor"]["backend"], "codex")
                self.assertFalse(project.needs_backend_migration)
                self.assertNotIn("迁移提示", out.getvalue())

    def test_config_summary_and_json_include_roles_and_project(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / "baton.json", {"judge": {"model": "sonnet"}})
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            baton.cmd_config(project, Namespace(json=False))
        summary = out.getvalue()
        self.assertTrue(summary.startswith("BATON::CONFIG judge=sonnet executor=gpt-6.1-sol effort=ultra conductor=sonnet backend=codex project_id="))
        for expected in ("conductor.model：sonnet", "monitor.model：gpt-5.6-luna", "monitor.reasoning_effort：max",
                         "monitor.final_reasoning_effort：max", "monitor.timeout_minutes：40", "executor.backend：codex",
                         os.path.realpath(str(root)), project.project_id, project.state_dir,
                         "迁移提示", "Hermes", "第 4 阶段", "baton init --migrate"):
            self.assertIn(expected, summary)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            baton.cmd_config(project, Namespace(json=True))
        marker, payload = out.getvalue().split("\n", 1)
        self.assertTrue(marker.startswith("BATON::CONFIG judge=sonnet executor=gpt-6.1-sol effort=ultra conductor=sonnet backend=codex project_id="))
        cfg = json.loads(payload)
        self.assertEqual(cfg["conductor"]["model"], "sonnet")
        self.assertNotIn("judge", cfg)
        self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-luna")
        self.assertEqual(cfg["executor"]["backend"], "codex")
        # the project root is canonical: the real path, with the on-disk letter case
        self.assertEqual(cfg["project"], {"root": os.path.realpath(str(root)), "id": project.project_id,
                                          "state_dir": project.state_dir})
        self.assertIn("迁移提示", err.getvalue())
        self.assertFalse(Path(project.state_dir).exists())

    def test_project_identity_realpath_and_home_fallback(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        real = os.path.realpath(str(root))
        expected = os.path.basename(real) + "-" + hashlib.sha1(os.fsencode(real)).hexdigest()[:8]
        self.assertEqual(project.project_id, expected)
        self.assertEqual(project.state_dir, str(self.env_root / "state" / "projects" / expected))
        alias = self.env_root / "alias"
        alias.symlink_to(root, target_is_directory=True)
        self.assertEqual(baton.Project(str(alias)).project_id, expected)
        with patch.dict(os.environ):
            os.environ.pop("BATON_HOME", None)
            self.assertEqual(project.state_dir, str(self.env_root / "home" / ".baton" / "projects" / expected))

    def test_init_carries_legacy_roles_into_new_file(self):
        tmp, root, project = self.project()
        self.addCleanup(tmp.cleanup)
        self.write_json(root / ".baton/config.json", {
            "judge": {"model": "haiku", "council_for_major_decisions": False},
            "executor": {"backend": "codex"},
        })
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_init(project, Namespace(no_statusline=True))
        cfg = json.loads((root / "baton.json").read_text())
        self.assertEqual(cfg["conductor"], {"model": "haiku", "council_for_major_decisions": False})
        self.assertEqual(cfg["monitor"]["model"], "gpt-5.6-luna")
        self.assertNotIn("judge", cfg)
        self.assertEqual(cfg["executor"]["backend"], "codex")
        self.assertFalse(project.needs_backend_migration)


if __name__ == "__main__":
    unittest.main()
