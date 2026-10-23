import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class SyncUpstreamRegressionTests(unittest.TestCase):
    def test_provider_monitor_keeps_windows_readonly_config(self):
        project = SimpleNamespace(state_dir=tempfile.mkdtemp(prefix="baton-dossier-"))
        cfg = {"monitor": {"model": "deepseek-v4-pro", "provider": "deepseek"}}
        with patch.object(baton.platform_support, "executable", return_value="codex.exe"), \
                patch.object(baton.platform_support, "codex_readonly_config",
                             return_value=['-c', 'windows.sandbox="unelevated"']) as readonly:
            command = baton._monitor_codex_command(project, "R001", cfg, "out.json")
        readonly.assert_called_once_with()
        self.assertIn('model_provider="deepseek"', command)
        self.assertIn('windows.sandbox="unelevated"', command)

    def test_open_tty_routes_by_platform(self):
        posix_tty = object()
        with patch.object(baton.os, "name", "posix"), \
                patch.object(baton, "_TerminalPrompt", return_value=posix_tty) as terminal:
            self.assertIs(baton._open_tty(), posix_tty)
        terminal.assert_called_once_with()

        windows_tty = object()
        with patch.object(baton.os, "name", "nt"), \
                patch.object(baton.platform_support, "open_tty", return_value=windows_tty) as platform_tty:
            self.assertIs(baton._open_tty(), windows_tty)
        platform_tty.assert_called_once_with()

    def test_shortstat_diff_is_ignored_by_memory_path_fallback(self):
        state = {"legs": [{"n": 1, "diff": "4 files changed, 195 insertions(+), 96 deletions(-)"}]}
        self.assertEqual(baton._memory_diff_paths(None, "task", state), [])

        with tempfile.TemporaryDirectory(prefix="baton-memory-") as tmp:
            root = Path(tmp) / "project"
            task_dir = root / ".baton" / "tasks" / "task"
            task_dir.mkdir(parents=True)
            (task_dir / "brief.md").write_text("目标\n", encoding="utf-8")
            project = baton.Project(str(root))
            cfg = baton.deep_merge(project.config, {"memory": {"backend": "local"}})
            text, _ = baton.memory_prompt_for_leg(project, "task", "指令", st=state, cfg=cfg)
            self.assertEqual(text, "指令")


if __name__ == "__main__":
    unittest.main()
