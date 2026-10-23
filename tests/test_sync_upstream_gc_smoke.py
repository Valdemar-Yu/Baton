import contextlib
import io
import os
from argparse import Namespace
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton  # noqa: E402


class SyncUpstreamGcSmokeTests(unittest.TestCase):
    def _write_task(self, project, task, leg, status="idle"):
        task_dir = Path(project.task_dir(task))
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "brief.md").write_text("# " + task + "\n", encoding="utf-8")
        project.update_state(task, lambda state: state.update(
            task=task, status=status, legs=[leg],
            scope={"allow": [], "deny": [], "acceptance": [], "frozen": [], "verify": "none"}))

    def _report_leg(self, number=1):
        return {
            "n": number,
            "status": "report",
            "executor_status": "report",
            "events": "",
            "started_at": baton.now(),
            "result": {"status": "report", "summary": "smoke", "artifact": ""},
        }

    def _add_code_to_dossier(self, project, dossier_id, name="hello.txt", content="hello\n"):
        root = Path(baton.dossier_path(project, dossier_id))
        baton._unseal_dossier(str(root))
        code = root / "code"
        code.mkdir(exist_ok=True)
        (code / name).write_text(content, encoding="utf-8")
        manifest = baton.dossier_manifest(project, dossier_id)
        files = baton._dossier_files(str(root))
        manifest["files"] = files
        manifest["total_sha256"] = baton._dossier_total(files)
        baton.write_json(str(root / "manifest.json"), manifest)
        baton._seal_dossier(str(root))
        return root

    def test_close_and_gc_prune_dossier_code(self):
        with tempfile.TemporaryDirectory(prefix="baton-sync-gc-smoke-") as tmp:
            base = Path(tmp)
            root = base / "project"
            baton_home = base / "baton-home"
            root.mkdir()
            (root / ".baton").mkdir()
            (root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
            with patch.dict(os.environ, {"BATON_HOME": str(baton_home)}, clear=False):
                project = baton.Project(str(root))
                project.record_id()
                leg = self._report_leg()
                self._write_task(project, "t", leg)
                state = project.load_state("t")
                dossier_id = baton.build_dossier(project, "t", state, leg, "report")
                project.update_state("t", lambda current: current["legs"][0].update(
                    dossier={"id": dossier_id, "type": "report"}))
                dossier_root = self._add_code_to_dossier(project, dossier_id)

                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    baton.cmd_close(project, Namespace(task="t", abandon="smoke close"))
                self.assertIn("已按放弃 closed", output.getvalue())
                manifest = baton.dossier_manifest(project, dossier_id)
                self.assertFalse((dossier_root / "code").exists())
                self.assertTrue(manifest.get("code_pruned"))
                self.assertEqual(baton.verify_dossier(project, dossier_id), (True, ""))

                leg2 = self._report_leg()
                self._write_task(project, "t2", leg2, status="closed")
                gc_id = "t2-leg-001"
                gc_root = Path(baton.dossier_path(project, gc_id))
                (gc_root / "code").mkdir(parents=True)
                (gc_root / "code" / "remove.txt").write_text("remove\n", encoding="utf-8")
                files = baton._dossier_files(str(gc_root))
                baton.write_json(str(gc_root / "manifest.json"), {
                    "dossier": gc_id, "task": "t2", "leg": 1, "type": "report",
                    "code_omissions": {"files": 0, "bytes": 0, "reason": "smoke"},
                    "files": files, "total_sha256": baton._dossier_total(files), "created_at": baton.now()})
                command = [sys.executable, str(Path(baton.__file__).resolve()), "--root", str(root), "gc"]
                preview = subprocess.run(command + ["--dry-run"], capture_output=True, text=True,
                                         encoding="utf-8", env=os.environ.copy())
                self.assertEqual(preview.returncode, 0, preview.stderr)
                self.assertTrue((gc_root / "code").exists())
                self.assertIn("将清理 1 份卷宗代码", preview.stdout)
                removed = subprocess.run(command, capture_output=True, text=True,
                                         encoding="utf-8", env=os.environ.copy())
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertIn("已清理 1 份卷宗代码", removed.stdout)
                self.assertFalse((gc_root / "code").exists())


if __name__ == "__main__":
    unittest.main()
