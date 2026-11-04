"""Native Windows Hermes executor and Codex-home regression tests."""
import contextlib
import copy
import ctypes
from ctypes import wintypes
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from argparse import Namespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "baton" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT / "tests"))
import baton  # noqa: E402
from platform_fixtures import executable_fixture  # noqa: E402


@unittest.skipUnless(os.name == "nt", "native Windows only")
class WindowsHermesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.state = self.root / "state"
        self.codex_home = self.root / "codex-home"
        self.claude_home = self.root / "claude-home"
        self.hermes_home = self.root / "hermes-home"
        self.bin = self.root / "bin"
        for path in (self.home, self.state, self.codex_home, self.claude_home, self.hermes_home, self.bin):
            path.mkdir(parents=True, exist_ok=True)
        self.hermes_exe = self._fake_hermes("hermes.exe")
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "BATON_HOME": str(self.state), "CODEX_HOME": str(self.codex_home),
            "HERMES_HOME": str(self.hermes_home), "CLAUDE_CONFIG_DIR": str(self.claude_home),
            "HERMES_BIN": str(self.hermes_exe), "PYTHONDONTWRITEBYTECODE": "1",
            "BATON_DESKTOP_NOTIFY": "0",
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)

    def _fake_hermes(self, name):
        script = self.root / "fake-hermes.py"
        script.write_text(
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('Hermes fake 0.0')\n"
            "elif 'tools' in args:\n"
            "    print('file terminal todo')\n"
            "elif 'chat' in args:\n"
            "    print('BATON_RESULT_BEGIN')\n"
            "    print('{}')\n"
            "    print('BATON_RESULT_END')\n",
            encoding="utf-8")
        built = executable_fixture(script)
        target = built.with_name(name)
        if target != built:
            built.replace(target)
        return target

    def _project(self, executor=None, monitor=None):
        root = self.root / "project"
        root.mkdir(exist_ok=True)
        config = {"executor": dict(executor or {}), "monitor": dict(monitor or {})}
        (root / "baton.json").write_text(json.dumps(config), encoding="utf-8")
        return baton.Project(str(root))

    def _hermes_project(self):
        return self._project({
            "backend": "hermes", "sandbox_exec": False, "hermes_bin": str(self.hermes_exe),
        })

    def _exclusive_file(self, path):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        return kernel, handle

    def test_hermes_sandbox_requires_explicit_opt_out(self):
        project = self._project({"backend": "hermes"})
        with self.assertRaisesRegex(baton.ConfigError, "sandbox_exec.*false"):
            baton.require_supported_executor(project.config)
        project = self._hermes_project()
        baton.require_supported_executor(project.config)
        self.assertEqual(project.config["executor"]["backend"], "hermes")
        default = baton.Project(str(self.root / "default-project"))
        self.assertEqual(default.config["executor"]["backend"], "codex")

    def test_monitor_hermes_is_rejected_on_windows(self):
        project = self._project(monitor={"backend": "hermes", "provider": "deepseek"})
        with self.assertRaisesRegex(baton.ConfigError, "monitor.backend"):
            baton._monitor_hermes_command(project, "R001", project.config, None, "prompt")

    def test_hermes_command_uses_query_file_and_long_resume_flag(self):
        project = self._hermes_project()
        task = "demo"
        Path(project.task_dir(task)).mkdir(parents=True)
        prompt = 'line 1\n"quoted" 100% 中文\nline 3'
        command = baton.hermes_command(project, "start", None, prompt,
                                       cfg=project.config, task=task)
        self.assertIn("--query-file", command)
        self.assertFalse(any(str(item).startswith("--query=") for item in command))
        path = command[command.index("--query-file") + 1]
        self.assertEqual(Path(path).read_bytes().decode("utf-8"), prompt)
        resume = baton.hermes_command(project, "resume", "session-1", prompt,
                                      cfg=project.config, task=task)
        self.assertIn("--resume", resume)
        self.assertIn("session-1", resume)
        self.assertNotIn("-r", resume)

    def test_hermes_cmd_launcher_is_rejected_and_config_shows_bin(self):
        cmd = self.root / "hermes.cmd"
        cmd.write_text("@echo off\n", encoding="utf-8")
        project = self._project({"backend": "hermes", "sandbox_exec": False, "hermes_bin": str(cmd)})
        with mock.patch.dict(os.environ, {"HERMES_BIN": ""}):
            with self.assertRaisesRegex(baton.ConfigError, "hermes.exe"):
                baton.hermes_executable(project.config)
        project = self._hermes_project()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_config(project, Namespace(json=False))
        self.assertIn("executor.hermes_bin", out.getvalue())
        self.assertIn(str(self.hermes_exe), out.getvalue())

    def test_claude_config_dir_controls_harness_base(self):
        source = self.claude_home / "CLAUDE.md"
        source.write_text("user instruction from custom config\n", encoding="utf-8")
        self.assertEqual(Path(baton.harness_base_source_path()), source)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            baton.cmd_harness_base(None, Namespace(init=True, approve=False, force=False))
        base = Path(baton.harness_base_path())
        self.assertTrue(base.is_file())
        self.assertIn("user instruction from custom config", base.read_text(encoding="utf-8"))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(Path(baton.harness_base_source_path()),
                             Path(os.path.expanduser("~/.claude/CLAUDE.md")))

    def test_doctor_accepts_installer_wrapper(self):
        wrapper = self.bin / "baton.cmd"
        wrapper.write_text(
            '@echo off\r\npython "' + str(SCRIPTS / "baton.py").replace("\\", "/") + '" %*\r\n',
            encoding="utf-8")
        level, message = baton.baton_path_check()
        self.assertEqual(level, "OK", message)

    def test_monitor_probe_command_includes_provider(self):
        project = self._project(monitor={"provider": "deepseek", "model": "deepseek-v4-pro"})
        with mock.patch.object(baton.platform_support, "executable", side_effect=lambda name: name + ".exe"):
            command = baton._monitor_codex_probe_command(project.config, str(self.root / "dossier"),
                                                         str(self.root / "out.json"), "deepseek-v4-pro")
        self.assertIn('model_provider="deepseek"', command)
        self.assertIn("deepseek-v4-pro", command)
        self.assertTrue(any("env_key=\"BATON_MONITOR_API_KEY\"" in item for item in command))

    def test_prepare_task_codex_home_skips_locked_runtime_files(self):
        source = self.codex_home
        (source / "config.toml").write_text("model = \"test\"\n", encoding="utf-8")
        runtime = [".sqlite-maintenance.lock", "thread_history_1.sqlite",
                   "state_5.sqlite-wal", "state_5.sqlite-shm", "scratch.tmp", ".DS_Store"]
        for name in runtime:
            (source / name).write_text("runtime", encoding="utf-8")
        handles = []
        try:
            for name in (".sqlite-maintenance.lock", "thread_history_1.sqlite"):
                handles.append(self._exclusive_file(source / name))
            target = self.root / "task-codex-home"
            project = self._project({"backend": "hermes", "sandbox_exec": False})
            cfg = {"executor": {"backend": "hermes", "sandbox_exec": False}}
            baton.prepare_task_codex_home(project, "demo", target=str(target), cfg=cfg)
        finally:
            for kernel, handle in handles:
                kernel.CloseHandle(handle)
        self.assertTrue((target / "config.toml").exists())
        for name in runtime:
            self.assertFalse((target / name).exists(), name)

    def test_auth_json_is_never_copied(self):
        (self.codex_home / "auth.json").write_text('{"token": "secret"}', encoding="utf-8")
        target = self.root / "auth-task-home"
        project = self._project({"backend": "codex"})
        with mock.patch.object(baton.os, "symlink", side_effect=OSError(1314, "no privilege")):
            with self.assertRaisesRegex(baton.ConfigError, "auth.json"):
                baton.prepare_task_codex_home(project, "demo", target=str(target),
                                              cfg=project.config)
        self.assertFalse((target / "auth.json").exists())
        target2 = self.root / "hermes-auth-task-home"
        hermes_cfg = {"executor": {"backend": "hermes", "sandbox_exec": False}}
        with mock.patch.object(baton.os, "symlink", side_effect=OSError(1314, "no privilege")):
            baton.prepare_task_codex_home(project, "demo", target=str(target2), cfg=hermes_cfg)
        self.assertFalse((target2 / "auth.json").exists())
        deepseek_cfg = copy.deepcopy(baton.Project(str(self.root / "default-project")).config)
        deepseek_cfg["executor"]["extra_config"] = ['model_provider="deepseek"']
        target3 = self.root / "deepseek-auth-task-home"
        with mock.patch.object(baton.os, "symlink", side_effect=OSError(1314, "no privilege")):
            baton.prepare_task_codex_home(project, "demo", target=str(target3), cfg=deepseek_cfg)
        self.assertFalse((target3 / "auth.json").exists())

    def test_codex_command_adds_windows_unelevated_sandbox(self):
        project = self._project({"backend": "codex"})
        with mock.patch.object(baton.platform_support, "executable", side_effect=lambda name: name + ".exe"):
            command = baton.codex_command(project, "start", None, str(self.root / "last.json"),
                                          cfg=project.config)
            self.assertIn('windows.sandbox="unelevated"', command)
            cfg = copy.deepcopy(project.config)
            cfg["executor"]["extra_config"] = ['windows.sandbox="elevated"']
            command = baton.codex_command(project, "start", None, str(self.root / "last.json"), cfg=cfg)
        values = [item for item in command if "windows.sandbox" in item]
        self.assertEqual(values, ['windows.sandbox="elevated"'])


if __name__ == "__main__":
    unittest.main()
