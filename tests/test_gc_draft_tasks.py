import contextlib
import io
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


class GcDraftTaskTests(unittest.TestCase):
    def test_gc_skips_draft_without_state_and_cleans_closed_task(self):
        with tempfile.TemporaryDirectory(prefix="baton-gc-draft-") as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            (root / ".baton").mkdir()
            (root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
            draft = root / ".baton" / "tasks" / "draft"
            draft.mkdir(parents=True)
            (draft / "brief.md").write_text("# draft\n", encoding="utf-8")
            with patch.dict(os.environ, {"BATON_HOME": str(Path(tmp) / "home")}, clear=False):
                project = baton.Project(str(root))
                project.record_id()
                closed = root / ".baton" / "tasks" / "closed"
                closed.mkdir(parents=True)
                (closed / "brief.md").write_text("# closed\n", encoding="utf-8")
                project.update_state("closed", lambda s: s.update(task="closed", status="closed", legs=[]))
                ident = "closed-leg-001"
                dossier = Path(baton.dossier_path(project, ident))
                (dossier / "code").mkdir(parents=True)
                (dossier / "code" / "x.txt").write_text("x\n", encoding="utf-8")
                files = baton._dossier_files(str(dossier))
                baton.write_json(str(dossier / "manifest.json"), {
                    "dossier": ident, "task": "closed", "files": files,
                    "total_sha256": baton._dossier_total(files)})
                for dry_run in (True, False):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        baton.cmd_gc(project, Namespace(dry_run=dry_run))
                    self.assertIn("跳过 1 个 draft 任务", output.getvalue())
                self.assertFalse((dossier / "code").exists())


if __name__ == "__main__":
    unittest.main()
