import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
LAUNCH = ROOT / "council" / "codex" / "scripts" / "launch.sh"
INSTALL = ROOT / "install.sh"

import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class CouncilCodexScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="council-codex-")
        self.base = Path(self.tmp.name)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        self.args_file = self.base / "codex-args"
        fake = self.bin / "codex"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import os, sys\n"
            "with open(os.environ['FAKE_CODEX_ARGS'], 'wb') as stream:\n"
            "    stream.write(b'\\0'.join(item.encode() for item in sys.argv[1:]))\n",
            encoding="utf-8",
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin) + os.pathsep + self.env.get("PATH", ""),
            "FAKE_CODEX_ARGS": str(self.args_file),
            "HOME": str(self.base / "home"),
        })
        self.env.pop("CODEX_THREAD_ID", None)
        self.env.pop("CODEX_SESSION_ID", None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_launch(self, sid=None, **extra):
        env = dict(self.env)
        env.update(extra)
        args = ["bash", str(LAUNCH)]
        if sid is not None:
            args.append(sid)
        return subprocess.run(args, cwd=str(self.base), env=env, capture_output=True, text=True, timeout=10)

    def captured_args(self):
        return self.args_file.read_bytes().split(b"\0")

    def captured_text_args(self):
        return [item.decode() for item in self.captured_args()]

    def test_session_id_order_ignores_claude_variable(self):
        result = self.run_launch(
            "explicit",
            CODEX_THREAD_ID="thread",
            CODEX_SESSION_ID="session",
            CLAUDE_CODE_SESSION_ID="wrong-claude-session",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"explicit", self.captured_args())

        result = self.run_launch(
            CODEX_THREAD_ID="thread",
            CODEX_SESSION_ID="session",
            CLAUDE_CODE_SESSION_ID="wrong-claude-session",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"thread", self.captured_args())
        self.assertNotIn(b"session", self.captured_args())

        result = self.run_launch(CODEX_SESSION_ID="session", CLAUDE_CODE_SESSION_ID="wrong-claude-session")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"session", self.captured_args())
        self.assertNotIn(b"wrong-claude-session", self.captured_args())

    def test_missing_session_id_is_an_error(self):
        result = self.run_launch(CLAUDE_CODE_SESSION_ID="inherited-but-invalid")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no Codex session id", result.stderr)
        self.assertFalse(self.args_file.exists())

    def test_fork_uses_config_model_effort_and_session_prompt(self):
        result = self.run_launch("thread-123")
        self.assertEqual(result.returncode, 0, result.stderr)
        args = self.captured_text_args()
        self.assertEqual(args[:2], ["exec", "fork"])
        self.assertIn("-m", args)
        self.assertEqual(args[args.index("-m") + 1], "gpt-6-astra")
        self.assertIn('model_reasoning_effort="ultra"', args)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertNotIn('sandbox_mode="workspace-write"', args)
        self.assertNotIn("sandbox_workspace_write.network_access=true", args)
        self.assertIn("thread-123", args)
        prompt = args[-1]
        for rule in (
            "INHERITED BACKGROUND ONLY",
            "must NOT continue it",
            "must NOT address the user",
            "must NOT resume the main agent's task",
            "Never invoke the council skill or launch.sh recursively",
            "Your final message must be ONLY the verdict document",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, prompt)
        self.assertLess(prompt.index("INHERITED BACKGROUND ONLY"), prompt.index("Act as chief."))
        self.assertIn("references/chief.md", prompt)
        self.assertIn("scripts/council.sh", prompt)
        self.assertIn("Do not call this launcher", prompt)
        self.assertTrue((self.base / ".council").is_dir())


class CouncilCodexRoundTests(unittest.TestCase):
    def test_round_uses_path_councilors_and_persists_opinions(self):
        with tempfile.TemporaryDirectory(prefix="council-round-") as raw:
            base = Path(raw)
            scripts = base / "skill" / "scripts"
            scripts.mkdir(parents=True)
            runner = scripts / "council.sh"
            shutil.copyfile(LAUNCH.with_name("council.sh"), runner)
            names = ["council-codex-fixture", "council-cc-fixture"]
            (scripts.parent / "config.json").write_text(json.dumps({
                "councilors": names,
            }), encoding="utf-8")
            fake_bin = base / "bin"
            fake_bin.mkdir()
            for name in names:
                executable = fake_bin / name
                executable.write_text(
                    "#!/usr/bin/env python3\n"
                    "import json, os, sys\n"
                    "from pathlib import Path\n"
                    "name = Path(sys.argv[0]).name\n"
                    "query = sys.stdin.read()\n"
                    "(Path(os.environ['COUNCIL_CAPTURE']) / (name + '.json')).write_text(\n"
                    "    json.dumps({'args': sys.argv[1:], 'stdin': query}))\n"
                    "print(name + ' opinion')\n",
                    encoding="utf-8",
                )
                executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
            scratch = base / "tmp"
            scratch.mkdir()
            query = base / "main-20261022-120000.md"
            query.write_text("Review this isolated fixture.\n", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "PATH": str(fake_bin) + os.pathsep + env.get("PATH", ""),
                "HOME": str(base / "home"),
                "COUNCILOR_LIB": str(base / "no-backend-library"),
                "COUNCIL_CAPTURE": str(base),
                "TMPDIR": str(scratch),
            })
            result = subprocess.run(
                ["bash", str(runner), str(query), str(scratch / "out")],
                cwd=str(base), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for name in names:
                with self.subTest(councilor=name):
                    self.assertIn("RESULT " + name + " ok ", result.stdout)
                    opinion = base / ".council" / (name.removeprefix("council-") + "-20261022-120000.md")
                    self.assertEqual(opinion.read_text(encoding="utf-8"), name + " opinion\n")
            codex = json.loads((base / (names[0] + ".json")).read_text())
            self.assertEqual(codex["args"], [])
            self.assertIn("You are an external councilor", codex["stdin"])
            self.assertIn("READ-ONLY sandbox", codex["stdin"])
            self.assertTrue(codex["stdin"].endswith(query.read_text()))
            claude = json.loads((base / (names[1] + ".json")).read_text())
            self.assertIn("--append-system-prompt", claude["args"])
            self.assertEqual(claude["args"][0], "-p")
            self.assertEqual(claude["stdin"], query.read_text())


class CouncilCodexInstallTests(unittest.TestCase):
    def test_install_does_not_overwrite_existing_codex_skill(self):
        with tempfile.TemporaryDirectory(prefix="council-install-") as raw:
            base = Path(raw)
            codex_home = base / "codex"
            dst = codex_home / "skills" / "council"
            dst.mkdir(parents=True)
            marker = dst / "keep-me"
            marker.write_text("existing\n", encoding="utf-8")
            (dst / "config.json").write_text(json.dumps({
                "councilors": ["council-cc-opus", "council-codex-sol"],
            }), encoding="utf-8")
            lib = base / "council-def.sh"
            lib.write_text(
                "council-cc-opus() { :; }\n"
                "council-codex-sol() { :; }\n",
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update({"HOME": str(base / "home"), "CODEX_HOME": str(codex_home), "COUNCILOR_LIB": str(lib)})
            result = subprocess.run(
                ["bash", str(INSTALL), "--council-codex"],
                cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing\n")
            self.assertFalse((dst / "SKILL.md").exists())
            self.assertIn("保留原配置", result.stdout)


class CouncilCodexDoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="council-doctor-")
        self.base = Path(self.tmp.name)
        self.project_root = self.base / "project"
        (self.project_root / ".baton").mkdir(parents=True)
        self.claude_skills = self.base / "claude-skills"
        self.codex_home = self.base / "codex-home"
        self.lib = self.base / "council-def.sh"
        self.lib.write_text(
            "council-cc-opus() { :; }\n"
            "council-codex-sol() { :; }\n",
            encoding="utf-8",
        )
        self.fake_bin = self.base / "bin"
        self.fake_bin.mkdir()
        codex = self.fake_bin / "codex"
        codex.write_text("#!/bin/sh\necho fake-codex\n", encoding="utf-8")
        codex.chmod(codex.stat().st_mode | stat.S_IXUSR)
        self.env = patch.dict(os.environ, {
            "HOME": str(self.base / "home"),
            "BATON_HOME": str(self.base / "state"),
            "CLAUDE_SKILLS_DIR": str(self.claude_skills),
            "CODEX_HOME": str(self.codex_home),
            "COUNCILOR_LIB": str(self.lib),
            "PATH": str(self.fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def make_skill(self, root):
        skill = root / "council"
        (skill / "scripts").mkdir(parents=True)
        (skill / "scripts" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (skill / "config.json").write_text(json.dumps({
            "councilors": ["council-cc-opus", "council-codex-sol"],
        }), encoding="utf-8")

    def run_doctor_for(self, model):
        (self.project_root / "baton.json").write_text(json.dumps({"conductor": {"model": model}}), encoding="utf-8")
        project = baton.Project(str(self.project_root))
        out = io.StringIO()
        with patch.object(baton, "safe_quota", return_value=None), \
                patch.object(baton.codex_quota, "describe", return_value="quota"), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit) as ctx:
                baton.cmd_doctor(project, Namespace(monitor_probe=False))
        self.assertEqual(ctx.exception.code, 0)
        return out.getvalue()

    def test_doctor_uses_claude_skill_for_claude_conductor(self):
        self.make_skill(self.claude_skills)
        output = self.run_doctor_for("opus")
        self.assertIn("council skill：已安装", output)

    def test_doctor_uses_codex_skill_for_codex_conductor(self):
        self.make_skill(self.codex_home / "skills")
        output = self.run_doctor_for("gpt-6-astra")
        self.assertIn("council skill：已安装", output)


if __name__ == "__main__":
    unittest.main()
