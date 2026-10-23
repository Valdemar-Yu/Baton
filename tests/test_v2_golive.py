import contextlib
import io
import json
import os
import re
import stat
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


class GoliveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-golive-")
        self.base = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.base / "home"),
            "BATON_HOME": str(self.base / "state"),
            "BATON_DESKTOP_NOTIFY": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def test_skill_defaults_are_v2_and_init_writes_v2_keys(self):
        cfg = json.loads((ROOT / "skills/baton/config.json").read_text(encoding="utf-8"))
        executor = cfg["executor"]
        self.assertEqual(executor["backend"], "codex")
        self.assertEqual(executor["model"], "gpt-6.1-sol")
        self.assertEqual(executor["reasoning_effort"], "ultra")
        self.assertEqual(executor["toolsets"], ["file", "terminal", "todo", "baton-memory"])
        self.assertTrue(executor["escalation"]["enabled"])
        for key in ("service_tier", "disable_fast_mode", "sandbox", "extra_config"):
            self.assertIn(key, executor)
        root = self.base / "project"
        root.mkdir()
        project = baton.Project(str(root))
        with contextlib.redirect_stdout(io.StringIO()), patch.object(baton, "prepare_hermes_profile"):
            baton.cmd_init(project, Namespace(no_statusline=True, accept_boundary=True,
                                              relink=False, fresh=False, upgrade_profile=False,
                                              migrate=False))
        written = json.loads((root / "baton.json").read_text(encoding="utf-8"))
        self.assertEqual(written["conductor"]["model"], "gpt-6-astra")
        self.assertEqual(written["monitor"]["model"], "gpt-5.6-luna")
        self.assertEqual(written["executor"]["backend"], "codex")
        self.assertEqual(written["executor"]["model"], "gpt-6.1-sol")

    def test_review_reference_example_uses_adopt_reject_and_parses(self):
        text = (ROOT / "skills/baton/references/review.md").read_text(encoding="utf-8")
        self.assertNotIn("`accept` 或 `reject`", text)
        match = re.search(r"```baton-verdict\s*\n\s*(.*?)\n\s*```", text, re.DOTALL)
        self.assertIsNotNone(match)
        value, _ = baton._parse_verdict(
            "```baton-verdict\n" + match.group(1) + "\n```\n",
            "report", "### F1 · out_of_scope · note\n")
        self.assertEqual(value["findings"]["F1"]["decision"], "reject")

    def test_doctor_warns_for_v1_claude_wording_without_failing(self):
        root = self.base / "repo"
        root.mkdir()
        (root / ".baton").mkdir()
        (root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        (root / "CLAUDE.md").write_text("把 model 设为 judge.model，再派子 agent 当裁判。\n", encoding="utf-8")
        project = baton.Project(str(root))
        out = io.StringIO()
        with patch.object(baton, "safe_quota", return_value=None), \
                patch.object(baton.codex_quota, "describe", return_value="quota"), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                baton.cmd_doctor(project, Namespace(monitor_probe=False))
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("WARN", out.getvalue())
        self.assertIn("CLAUDE.md", out.getvalue())
        self.assertIn("不能派子 agent 代替 monitor", out.getvalue())


class InstallScriptTests(unittest.TestCase):
    def run_install(self, home, path=None):
        env = os.environ.copy()
        env.update({"HOME": str(home), "CLAUDE_SKILLS_DIR": str(home / ".claude/skills"),
                    "BATON_BIN_DIR": str(home / ".local/bin"),
                    "PATH": path or env.get("PATH", "")})
        return subprocess.run(["bash", str(ROOT / "install.sh")], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=30)

    def test_repoints_existing_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="baton-install-") as raw:
            home = Path(raw)
            skill = home / ".claude/skills/baton"
            cli = home / ".local/bin/baton"
            skill.parent.mkdir(parents=True)
            cli.parent.mkdir(parents=True)
            old = home / "old-baton"
            old.mkdir()
            old_cli = home / "old-cli"
            old_cli.write_text("old\n", encoding="utf-8")
            skill.symlink_to(old, target_is_directory=True)
            cli.symlink_to(old_cli)
            result = self.run_install(home)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(skill.resolve(), (ROOT / "skills/baton").resolve())
            self.assertEqual(cli.resolve(), (ROOT / "skills/baton/bin/baton").resolve())
            self.assertIn("更新软链接", result.stdout)

    def test_skips_non_symlink_skill_directory(self):
        with tempfile.TemporaryDirectory(prefix="baton-install-") as raw:
            home = Path(raw)
            skill = home / ".claude/skills/baton"
            skill.mkdir(parents=True)
            marker = skill / "keep"
            marker.write_text("keep\n", encoding="utf-8")
            result = self.run_install(home)
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(marker.exists())
            self.assertIn("不是软链接", result.stderr)

    def test_missing_optional_dependencies_only_prints_hints(self):
        with tempfile.TemporaryDirectory(prefix="baton-install-") as raw:
            home = Path(raw)
            result = self.run_install(home, path="/usr/bin:/bin")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("没有找到 Ollama", result.stdout)
            self.assertIn("没有找到 hermes", result.stdout)
            self.assertIn("不会自动执行", result.stdout)


if __name__ == "__main__":
    unittest.main()
