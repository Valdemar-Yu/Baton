from platform_fixtures import executable_fixture
import contextlib
import io
import json
import os
import subprocess
import textwrap
import time
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.state = Path(self.tmp.name) / "state"
        self.env = patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "BATON_HOME": str(self.state)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        (self.root / "baton.json").write_text(json.dumps({"executor": {"backend": "codex"}}), encoding="utf-8")
        (self.root / "tests" / "acceptance").mkdir(parents=True)
        self.project = baton.Project(str(self.root))
        self.scope = {"allow": ["tests"], "deny": [], "acceptance": ["tests/acceptance"],
                      "frozen": [], "verify": "auto"}
        self.cfg = json.loads((Path(baton.SKILL_DIR) / "config.json").read_text())
        self.cfg["supervision"]["test_command"] = "pytest tests/acceptance"

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def baseline(self):
        manifest = baton.write_freeze_baseline(self.project, "t", self.scope, self.cfg)
        self.st = {"scope": self.scope, "freeze_manifest": manifest}
        return manifest

    def test_bytecode_and_binary_files_under_tested_folder_do_not_break_freeze(self):
        self.write("tests/acceptance/test_a.py", "def test_a():\n    assert True\n")
        cache = self.root / "tests" / "acceptance" / "__pycache__"
        cache.mkdir()
        (cache / "test_a.cpython-312.pyc").write_bytes(b"\x9c\x00\xff\xfe bytecode")
        (self.root / "tests" / "acceptance" / "fixture.bin").write_bytes(b"\x9c\xff\x00")
        manifest = self.baseline()
        paths = {entry["path"]: entry for entry in manifest["entries"]}
        self.assertNotIn("tests/acceptance/__pycache__/test_a.cpython-312.pyc", paths)
        self.assertTrue(paths["tests/acceptance/fixture.bin"]["value"].startswith("sha256:"))
        (cache / "test_a.cpython-312.pyc").write_bytes(b"\x9c rebuilt by a test run")
        self.assertEqual(baton.check_freeze(self.project, "t", self.st)["violations"], [])
        (self.root / "tests" / "acceptance" / "fixture.bin").write_bytes(b"\x9c\xff\x01")
        self.assertEqual(baton.check_freeze(self.project, "t", self.st)["violations"][0]["type"], "改动")

    def test_scope_valid_missing_invalid_unknown_and_escape(self):
        valid = "```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [], "frozen": []}) + "\n```"
        self.assertEqual(baton.parse_scope(valid)["verify"], "auto")
        with self.assertRaises(baton.ConfigError):
            baton.parse_scope("# no scope")
        with self.assertRaises(baton.ConfigError):
            baton.parse_scope("```baton-scope\n{bad}\n```")
        with self.assertRaises(baton.ConfigError):
            baton.parse_scope("```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [], "frozen": [], "extra": []}) + "\n```")
        with self.assertRaises(baton.ConfigError):
            baton.parse_scope("```baton-scope\n" + json.dumps({"allow": ["../outside"], "deny": [], "acceptance": [], "frozen": []}) + "\n```")

    def test_new_task_with_no_scope_is_rejected(self):
        (self.root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "t" / "brief.md").write_text("# t\n", encoding="utf-8")
        self.write("baton.json", json.dumps({"supervision": {"test_command": "python -m unittest"}}))
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(baton.ConfigError):
            baton.cmd_start(self.project, Namespace(task="t"))

    def test_start_stores_effective_config_snapshot(self):
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        brief = "```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [],
            "frozen": [], "verify": "none"}) + "\n```\n# t\n"
        (self.root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "t" / "brief.md").write_text(brief, encoding="utf-8")
        with patch.object(baton, "launch_leg"):
            baton.cmd_start(self.project, Namespace(task="t"))
        state = self.project.read_state("t")
        self.assertEqual(state["config_snapshot"]["executor"]["model"], self.project.config["executor"]["model"])
        self.assertTrue(state["config_input_hash"])

    def test_next_leg_warns_when_project_config_changed(self):
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        brief = "```baton-scope\n" + json.dumps({"allow": [], "deny": [], "acceptance": [],
            "frozen": [], "verify": "none"}) + "\n```\n# t\n"
        (self.root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "t" / "brief.md").write_text(brief, encoding="utf-8")
        with patch.object(baton, "launch_leg"):
            baton.cmd_start(self.project, Namespace(task="t"))
        self.write("baton.json", json.dumps({"executor": {"model": "gpt-5.6-luna"}}))
        class FakeProcess:
            pid = 123456
        out = io.StringIO()
        with patch.object(baton, "codex_command", return_value=["codex"]), \
                patch.object(baton.subprocess, "Popen", return_value=FakeProcess()), \
                patch.object(baton, "safe_quota", return_value=None), \
                contextlib.redirect_stdout(out):
            baton.launch_leg(self.project, "t", "resume", "resume")
        self.assertIn("本任务不生效，需 baton amend --config", out.getvalue())

    def test_lock_change_delete_and_new_violate(self):
        self.write("conftest.py", "x = 1\n")
        self.baseline()
        self.write("conftest.py", "x = 2\n")
        self.assertEqual(baton.check_freeze(self.project, "t", self.st)["violations"][0]["type"], "改动")
        (self.root / "conftest.py").unlink()
        self.assertEqual(baton.check_freeze(self.project, "t", self.st)["violations"][0]["type"], "删除")
        self.write("conftest.py", "new\n")
        self.baseline()
        (self.root / "conftest.py").unlink()
        self.write("pytest.ini", "[pytest]\n")
        self.assertTrue(any(x["type"] == "新建" for x in baton.check_freeze(self.project, "t", self.st)["violations"]))

    def test_segment_hashes_and_package_keys(self):
        self.write("pyproject.toml", "[project]\ndependencies=['a']\n\n[tool.pytest.ini_options]\naddopts='-q'\n")
        self.write("package.json", json.dumps({"dependencies": {"a": "1"}, "scripts": {"test": "pytest", "build": "tool"}, "jest": {"x": 1}}))
        manifest = self.baseline()
        self.write("pyproject.toml", "[project]\ndependencies=['b']\n\n[tool.pytest.ini_options]\naddopts='-q'\n")
        self.write("package.json", json.dumps({"dependencies": {"b": "2"}, "scripts": {"test": "pytest", "build": "changed"}, "jest": {"x": 1}}))
        self.assertFalse(baton.check_freeze(self.project, "t", self.st)["violations"])
        self.write("pyproject.toml", "[project]\ndependencies=['b']\n\n[tool.pytest.ini_options]\naddopts='-s'\n")
        self.write("package.json", json.dumps({"dependencies": {"b": "2"}, "scripts": {"test": "python -m pytest"}, "jest": {"x": 1}}))
        violations = baton.check_freeze(self.project, "t", self.st)["violations"]
        self.assertEqual({x["path"] for x in violations}, {"pyproject.toml", "package.json"})
        rules = {entry["path"]: entry["rule"] for entry in manifest["entries"]}
        self.assertEqual(rules["package.json"], "test-keys")
        self.assertEqual(rules["pyproject.toml"], "pytest-section")

    def test_nested_test_configs_use_segment_rules(self):
        self.write("pkg/pyproject.toml", "[project]\ndependencies=['a']\n")
        self.write("pkg/setup.cfg", "[metadata]\nname=pkg\n")
        manifest = self.baseline()
        rules = {entry["path"]: entry["rule"] for entry in manifest["entries"]}
        self.assertEqual(rules["pkg/pyproject.toml"], "pytest-section")
        self.assertEqual(rules["pkg/setup.cfg"], "pytest-section")
        self.write("pkg/pyproject.toml", "[project]\ndependencies=['b']\n")
        self.write("pkg/setup.cfg", "[metadata]\nname=changed\n")
        self.assertFalse(baton.check_freeze(self.project, "t", {"scope": self.scope,
                                                                  "freeze_manifest": manifest})["violations"])

    def test_acceptance_new_allowed_and_new_conftest_forbidden(self):
        self.baseline()
        self.write("tests/acceptance/test_new.py", "def test_new(): pass\n")
        self.assertFalse(baton.check_freeze(self.project, "t", self.st)["violations"])
        self.write("tests/acceptance/conftest.py", "x=1\n")
        self.assertTrue(baton.check_freeze(self.project, "t", self.st)["violations"])

    def test_monitor_only_changed_files_with_parts(self):
        self.write("requirements.txt", "pytest\n")
        self.write("Makefile", "test:\n\tpytest\n")
        self.write("pyproject.toml", "[project]\ndependencies=['a']\n\n[tool.pytest.ini_options]\naddopts='-q'\n")
        self.baseline()
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(task="t", scope=self.scope,
                                                                  freeze_manifest=self.st["freeze_manifest"],
                                                                  legs=[]))
        self.assertEqual(baton.check_freeze(self.project, "t", self.st)["monitor"], [])
        self.write("requirements.txt", "pytest\ncoverage\n")
        self.write("pyproject.toml", "[project]\ndependencies=['b']\n\n[tool.pytest.ini_options]\naddopts='-q'\n")
        result = baton.check_freeze(self.project, "t", self.st)
        self.assertEqual([x["path"] for x in result["monitor"]], ["pyproject.toml", "requirements.txt"])
        self.assertIn("[project]", result["monitor"][0]["parts"])

    def test_restore_frozen_preserves_unlocked_parts_and_backs_up_new(self):
        self.write("pyproject.toml", "[project]\ndependencies=['a']\n\n[tool.pytest.ini_options]\naddopts='-q'\n")
        self.baseline()
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(task="t", scope=self.scope,
                                                                  freeze_manifest=self.st["freeze_manifest"], legs=[]))
        self.write("pyproject.toml", "[project]\ndependencies=['b']\n\n[tool.pytest.ini_options]\naddopts='-s'\n")
        self.write("pytest.ini", "[pytest]\n")
        args = Namespace(task="t", paths=[])
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_restore_frozen(self.project, args)
        text = (self.root / "pyproject.toml").read_text()
        self.assertIn("dependencies=['b']", text)
        self.assertIn("addopts='-q'", text)
        self.assertFalse((self.root / "pytest.ini").exists())
        self.assertTrue(list((Path(baton.freeze_dir(self.project, "t")) / "restore-backup").rglob("pytest.ini")))

    def test_external_hermes_change_is_warn_only(self):
        git_root = Path(self.tmp.name) / "repo"
        git_root.mkdir()
        (git_root / ".git").mkdir()
        project_root = git_root / "project"
        project_root.mkdir()
        (git_root / "AGENTS.md").write_text("one\n")
        project = baton.Project(str(project_root))
        manifest = baton.write_freeze_baseline(project, "t", self.scope, self.cfg)
        (git_root / "AGENTS.md").write_text("two\n")
        result = baton.check_freeze(project, "t", {"scope": self.scope, "freeze_manifest": manifest})
        self.assertFalse(result["violations"])
        self.assertEqual(result["external_warn"], [os.path.realpath(str(git_root / "AGENTS.md"))])

    def test_amend_records_config_and_external_proposal(self):
        self.write("conftest.py", "x=1\n")
        manifest = self.baseline()
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(task="t", legs=[], scope=self.scope,
                                                                  freeze_manifest=manifest,
                                                                  config_snapshot=self.cfg,
                                                                  config_input_hash=baton.config_input_hash(self.project)))
        proposal = Path(self.tmp.name) / "amend.md"
        proposal.write_text("说明：固定测试环境。\n", encoding="utf-8")
        args = Namespace(task="t", amendment_file=str(proposal), config=["freeze.check_seconds=5"],
                         adopt=[], user_approved=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_amend(self.project, args)
        state = self.project.read_state("t")
        self.assertEqual(state["config_snapshot"]["freeze"]["check_seconds"], 5)
        self.assertEqual(len(state["amendments"]), 1)
        self.assertTrue(Path(state["amendments"][0]["path"]).is_file())
        self.assertEqual(state["amendments"][0]["id"], "A001")
        self.assertEqual(state["amendments"][0]["after_leg"], 0)
        self.assertFalse(state["amendments"][0]["relax"])
        self.assertIn("标准在第 0 轮后改过", out.getvalue())

    def test_amend_relax_and_adopt_require_tty_confirmation(self):
        self.write("conftest.py", "x=1\n")
        manifest = self.baseline()
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(task="t", legs=[], scope=self.scope,
                                                                  freeze_manifest=manifest,
                                                                  config_snapshot=self.cfg))
        with self.assertRaises(SystemExit):
            baton.cmd_amend(self.project, Namespace(task="t", amendment_file=None,
                                                    config=["supervision.test_command=pytest"],
                                                    adopt=[], user_approved=False))
        self.write("conftest.py", "x=2\n")
        args = Namespace(task="t", amendment_file=None, config=[],
                         adopt=["conftest.py"], user_approved=True)
        before = Path(self.project.state_path("t")).read_bytes()
        with patch.object(baton, "_open_tty", side_effect=OSError("无控制终端")), \
                self.assertRaisesRegex(baton.ConfigError, r"无法打开 (/dev/tty|CONIN\$)"):
            baton.cmd_amend(self.project, args)
        self.assertEqual(Path(self.project.state_path("t")).read_bytes(), before)
        wrong_tty = Mock()
        wrong_tty.readline.return_value = "wrong-task\n"
        with patch.object(baton, "_open_tty", return_value=wrong_tty), \
                self.assertRaisesRegex(baton.ConfigError, "任务名确认不匹配"):
            baton.cmd_amend(self.project, args)
        wrong_tty.close.assert_called_once()
        self.assertEqual(Path(self.project.state_path("t")).read_bytes(), before)
        correct_tty = Mock()
        correct_tty.readline.return_value = "t\n"
        with patch.object(baton, "_open_tty", return_value=correct_tty), \
                contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_amend(self.project, args)
        correct_tty.close.assert_called_once()
        self.assertFalse(baton.check_freeze(self.project, "t")["violations"])
        self.assertEqual((Path(baton.freeze_dir(self.project, "t")) / "files/conftest.py").read_text(), "x=2\n")

    def test_open_tty_uses_console_devices_on_windows(self):
        opened = []

        def fake_open(path, mode, encoding=None):
            opened.append((path, mode))
            return Mock()

        with patch.object(baton.platform_support.os, "name", "nt"), \
                patch("builtins.open", side_effect=fake_open):
            tty = baton.platform_support.open_tty()
            tty.write("q")
            tty.readline()
            tty.close()
        self.assertEqual(opened, [("CONIN$", "r"), ("CONOUT$", "w")])
        with patch.object(baton.platform_support.os, "name", "posix"), \
                patch("builtins.open", side_effect=fake_open):
            baton.platform_support.open_tty()
        self.assertEqual(opened[-1], ("/dev/tty", "r+"))

    def test_amend_validates_config_and_classifies_relaxations(self):
        manifest = self.baseline()
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.project.update_state("t", lambda state: state.update(task="t", legs=[], scope=self.scope,
                                                                  freeze_manifest=manifest,
                                                                  config_snapshot=self.cfg))
        state_path = Path(self.project.state_path("t"))
        before = state_path.read_bytes()
        for assignment, message in (("conductor.model=gpt-4", "conductor.model"),
                                     ("executor=1", "executor"), ("mode=lean", "不能用 amend"),
                                     ("project.root=x", "不能用 amend")):
            with self.subTest(assignment=assignment), self.assertRaises(baton.ConfigError) as ctx:
                baton.cmd_amend(self.project, Namespace(task="t", amendment_file=None,
                                                        config=[assignment], adopt=[], user_approved=False))
            self.assertIn(message, str(ctx.exception))
            self.assertEqual(state_path.read_bytes(), before)
        with self.assertRaises(SystemExit):
            baton.cmd_amend(self.project, Namespace(task="t", amendment_file=None,
                                                    config=["freeze.check_seconds=120"], adopt=[],
                                                    user_approved=False))
        with self.assertRaises(SystemExit):
            baton.cmd_amend(self.project, Namespace(task="t", amendment_file=None,
                                                    config=["supervision.verify_timeout_minutes=60"], adopt=[],
                                                    user_approved=False))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_amend(self.project, Namespace(task="t", amendment_file=None,
                                                    config=["freeze.check_seconds=10"], adopt=[],
                                                    user_approved=False))
        self.assertEqual(self.project.read_state("t")["config_snapshot"]["freeze"]["check_seconds"], 10)

    def test_runner_dead_with_valid_result_is_collected(self):
        (self.root / ".baton" / "config.json").parent.mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        self.root.joinpath(".baton", "runs", "t").mkdir(parents=True)
        last = self.root / ".baton" / "runs" / "t" / "leg-001.last.json"
        last.write_text(json.dumps({"status": "report", "summary": "done"}), encoding="utf-8")
        events = self.root / ".baton" / "runs" / "t" / "leg-001.events.jsonl"
        events.write_text("", encoding="utf-8")
        st = {"task": "t", "status": "running", "legs": [{
            "n": 1, "status": "running", "started_at": 0, "runner_pid": 999,
            "codex_pid": 998, "last": str(last), "events": str(events),
        }]}
        self.project.update_state("t", lambda state: state.update(st))
        with patch.object(baton, "pid_alive", return_value=False), \
                patch.object(baton.time, "monotonic", side_effect=[0, 4]):
            result = baton.refresh_runner_liveness(self.project, "t", self.project.load_state("t"))
        self.assertEqual(baton.current_leg(result)["status"], "report")
        self.assertIn("未做轮末核对", baton.current_leg(result)["error"])

    def test_junit_counts_and_acceptance_failures(self):
        junit = Path(self.tmp.name) / "junit.xml"
        junit.write_text("""<testsuite tests='4'>
          <testcase file='tests/acceptance/test_a.py' name='test_pass'/>
          <testcase file='tests/acceptance/test_a.py' name='test_skip'><skipped/></testcase>
          <testcase file='tests/acceptance/test_a.py' name='test_xfail'><skipped type='pytest.xfail'/></testcase>
          <testcase file='tests/other/test_b.py' name='test_err'><error/></testcase>
        </testsuite>""", encoding="utf-8")
        counts, tests = baton._parse_junit(str(junit), ["tests/acceptance"])
        self.assertEqual(counts["passed"], 1)
        self.assertEqual(counts["skipped"], 1)
        self.assertEqual(counts["xfailed"], 1)
        self.assertEqual(counts["error"], 1)
        failed = baton._acceptance_failures(tests, ["tests/acceptance"])
        self.assertEqual({item["reason"] for item in failed}, {"skipped", "xfailed"})

    def test_real_pytest_xunit1_and_xunit2_node_ids_match_acceptance_forms(self):
        self.write("tests/acceptance/test_a.py", "")
        cases = """
          <testcase {file}classname="tests.acceptance.test_a" name="test_ok"/>
          <testcase {file}classname="tests.acceptance.test_a.TestK" name="test_method"/>
          <testcase {file}classname="tests.acceptance.test_a.TestK" name="test_param[1]"/>
          <testcase {file}classname="tests.acceptance.test_a" name="test_skip"><skipped message="skip"/></testcase>
          <testcase {file}classname="tests.acceptance.test_a" name="test_xfail"><skipped type="pytest.xfail" message="xfail"/></testcase>
          <testcase {file}classname="tests.acceptance.test_a.TestK" name="test_xpass">
            <failure message="[XPASS(strict)] unexpected pass">traceback mentions xfail only</failure>
          </testcase>
        """
        for label, file_attr in (("xunit1", 'file="tests/acceptance/test_a.py" '), ("xunit2", "")):
            with self.subTest(family=label):
                junit = Path(self.tmp.name) / f"{label}.xml"
                junit.write_text("<testsuite>" + cases.format(file=file_attr) + "</testsuite>",
                                 encoding="utf-8")
                counts, tests = baton._parse_junit(str(junit), ["tests/acceptance"], str(self.root))
                self.assertEqual(counts, {"passed": 3, "failed": 0, "skipped": 1,
                                          "xfailed": 1, "xpassed": 1, "error": 0, "deselected": 0})
                nodeids = {item["nodeid"] for item in tests}
                self.assertIn("tests/acceptance/test_a.py::test_ok", nodeids)
                self.assertIn("tests/acceptance/test_a.py::TestK::test_method", nodeids)
                self.assertIn("tests/acceptance/test_a.py::TestK::test_param[1]", nodeids)
                self.assertEqual(
                    {item["reason"] for item in baton._acceptance_failures(
                        tests, ["tests/acceptance/test_a.py::TestK::test_method"])}, set())
                self.assertEqual(
                    {item["reason"] for item in baton._acceptance_failures(
                        tests, ["tests/acceptance/test_a.py"])}, {"skipped", "xfailed"})

    def test_pytest_acceptance_hint_when_rootdir_prefix_does_not_match(self):
        bindir = Path(self.tmp.name) / "bin-hint"
        bindir.mkdir()
        fake = bindir / "pytest"
        fake.write_text("#!/usr/bin/env python3\n"
                        "import pathlib, sys\n"
                        "j = next(x.split('=', 1)[1] for x in sys.argv if x.startswith('--junitxml='))\n"
                        "pathlib.Path(j).write_text('<testsuite><testcase classname=\"pkg.test_a\" name=\"test_ok\"/></testsuite>')\n",
                        encoding="utf-8")
        fake = executable_fixture(fake)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "pytest"
        st = {"scope": dict(self.scope, acceptance=["tests/acceptance"])}
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}):
            result = baton.run_verification(self.project, "t-hint", st, cfg, leg_n=1)
        self.assertIn("pytest rootdir", result.get("acceptance_hint", ""))

    def test_composite_verification_is_not_treated_as_pytest(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "python3 -c \"print('ok')\" && echo pytest"
        result = baton.run_verification(self.project, "t-composite",
                                        {"scope": self.scope}, cfg, leg_n=1)
        self.assertIsNone(result["counts"])
        self.assertIn("复合命令，未追加 junit", result.get("verification_note", ""))

    def test_verification_timeout_kills_process_group(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "sh -c 'sleep 30 & echo $! > child.pid; wait'"
        if os.name == "nt":
            script = self.root / "sleep-tree.py"
            script.write_text("import subprocess,sys,time,pathlib; p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); pathlib.Path('child.pid').write_text(str(p.pid)); time.sleep(30)", encoding="utf-8")
            cfg["supervision"]["test_command"] = subprocess.list2cmdline([sys.executable, str(script)])
        cfg["supervision"]["verify_timeout_minutes"] = 0.01
        result = baton.run_verification(self.project, "t-timeout",
                                        {"scope": self.scope}, cfg, leg_n=1)
        self.assertTrue(result["timed_out"])
        child_file = self.root / "child.pid"
        self.assertTrue(child_file.exists())
        child_pid = int(child_file.read_text().strip())
        self.assertFalse(baton.pid_alive(child_pid))

    def test_non_pytest_verification_writes_project_and_external_records(self):
        (self.root / ".baton").mkdir(parents=True, exist_ok=True)
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        scope = dict(self.scope, verify="auto")
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "python3 -c \"print('verify-ok')\""
        st = {"scope": scope}
        result = baton.run_verification(self.project, "t", st, cfg, leg_n=1)
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["counts"])
        self.assertIn("verify-ok", result["output_tail"])
        self.assertTrue(Path(self.project.leg_path("t", 1, "verify.json")).is_file())
        self.assertTrue((Path(self.project.state_dir) / "runs/t/leg-001.verify.json").is_file())

    def test_verification_none_records_skip_reason(self):
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "python3 -c 'raise SystemExit(1)'"
        result = baton.run_verification(self.project, "t", {"scope": dict(self.scope, verify="none")}, cfg, leg_n=1)
        self.assertTrue(result["skipped"])
        self.assertIn("没有 baton 亲跑", result["skip_reason"])

    def test_pytest_verification_appends_junit_and_marks_exitfirst(self):
        bindir = Path(self.tmp.name) / "bin"
        bindir.mkdir()
        fake = bindir / "pytest"
        fake.write_text("#!/usr/bin/env python3\n"
                        "import pathlib, sys\n"
                        "j = next(x.split('=', 1)[1] for x in sys.argv if x.startswith('--junitxml='))\n"
                        "pathlib.Path(j).write_text('<testsuite><testcase file=\"tests/acceptance/test_a.py\" name=\"test_a\"/></testsuite>')\n"
                        "print('1 passed, 2 deselected')\n", encoding="utf-8")
        fake = executable_fixture(fake)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["supervision"]["test_command"] = "pytest tests/acceptance -x"
        st = {"scope": self.scope}
        with patch.dict(os.environ, {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}):
            result = baton.run_verification(self.project, "t", st, cfg, leg_n=1)
        self.assertEqual(result["counts"]["passed"], 1)
        self.assertEqual(result["counts"]["deselected"], 2)
        self.assertTrue(result["counts_incomplete"])
        self.assertEqual(result["acceptance_failed"], [])

    def test_fake_codex_cli_leg_reports_violation(self):
        (self.root / ".baton").mkdir()
        (self.root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        (self.root / "baton.json").write_text(json.dumps({
            "freeze": {"check_seconds": 0.1},
            "supervision": {"test_command": ""},
            "monitor": {"enabled": False},
        }), encoding="utf-8")
        self.write("tests/acceptance/test_a.py", "def test_a(): pass\n")
        brief = "```baton-scope\n" + json.dumps({"allow": ["tests"], "deny": [],
            "acceptance": ["tests/acceptance"], "frozen": [], "verify": "none"}) + "\n```\n# task\n"
        (self.root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (self.root / ".baton" / "tasks" / "t" / "brief.md").write_text(brief, encoding="utf-8")
        fake = Path(self.tmp.name) / "fake_codex.py"
        fake.write_text("import json, sys, time\n"
                        "out = sys.argv[sys.argv.index('-o') + 1]\n"
                        "print(json.dumps({'type':'thread.started','thread_id':'fake'}), flush=True)\n"
                        "time.sleep(0.3)\n"
                        "open('tests/acceptance/test_a.py', 'w').write('def test_a():\\n    assert False\\n')\n"
                        "json.dump({'status':'report','summary':'fake','artifact':'','change_size':'minor'}, open(out, 'w'))\n",
                        encoding="utf-8")
        # launch_leg supplies the real output path through the patched command below.
        def command(project, kind, thread, last, cfg=None):
            return [sys.executable, str(fake), "-o", last]
        out = io.StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home), "BATON_HOME": str(self.state),
                                      "BATON_DESKTOP_NOTIFY": "0"}), \
                patch.object(baton, "safe_quota", return_value=None), \
                patch.object(baton, "codex_command", side_effect=command):
            baton.cmd_start(self.project, Namespace(task="t"))
            deadline = time.time() + 30
            runner_pid = None
            while time.time() < deadline:
                state = self.project.read_state("t") or {}
                leg = baton.current_leg(state) or {}
                runner_pid = leg.get("runner_pid") or runner_pid
                if runner_pid and os.name != "nt":
                    try:
                        os.waitpid(runner_pid, os.WNOHANG)
                    except ChildProcessError:
                        pass
                if state.get("status") != "running" and not baton.pid_alive(runner_pid):
                    break
                time.sleep(0.1)
            else:
                if runner_pid and baton.pid_alive(runner_pid):
                    baton.platform_support.stop_tree(runner_pid, (0, 3)) if os.name == "nt" else os.kill(runner_pid, baton.signal.SIGKILL)
                self.fail("fake codex runner did not exit within 30 seconds")
            with contextlib.redirect_stdout(out):
                baton.cmd_wait(self.project, Namespace(task="t", tick=0))
        self.assertIn("BATON::EVENT VIOLATION", out.getvalue())


if __name__ == "__main__":
    unittest.main()
