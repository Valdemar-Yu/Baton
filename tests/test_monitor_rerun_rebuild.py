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


class MonitorRerunRebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.case = 0
        for patcher in (
            patch.dict(os.environ, {"HOME": str(self.root / "home"),
                                    "BATON_HOME": str(self.root / "state")}),
            patch.object(baton, "_DETACHED_RUNNERS", []),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(baton.subprocess, "Popen")
        self.popen = patcher.start()
        self.popen.return_value.pid = 123456
        self.addCleanup(patcher.stop)

    def prepare(self, status="report", source="monitor", partial=True):
        self.case += 1
        root = self.root / ("project-%d" % self.case)
        (root / ".baton" / "tasks" / "t").mkdir(parents=True)
        (root / ".baton" / "config.json").write_text("{}", encoding="utf-8")
        (root / ".baton" / "tasks" / "t" / "brief.md").write_text("# t\n", encoding="utf-8")
        report = root / ".baton" / "R002-report.html"
        report.write_text('<h2>记忆候选</h2><pre data-memory-candidates="true">[]</pre>', encoding="utf-8")
        project = baton.Project(str(root))
        leg = {"n": 2, "status": status, "executor_status": status, "events": "",
               "result": {"status": "report", "artifact": str(report)}, "reported": True}
        dossier_id = baton.dossier_id_for(project, "t", leg, status)
        leg["monitor"] = {"id": dossier_id, "status": "failed", "error": "缺少卷宗 manifest"}
        if source == "dossier":
            leg["dossier"] = {"id": dossier_id, "type": baton.dossier_type_for(status)}
            leg["monitor"]["id"] = "other"
        project.update_state("t", lambda st: st.update(task="t", status="idle", legs=[leg]))
        if partial:
            directory = Path(baton.dossier_path(project, dossier_id))
            directory.mkdir(parents=True)
            (directory / "evidence.md").write_text("incomplete", encoding="utf-8")
        return project, Namespace(task="t", dossier=dossier_id, rerun=True, curate=False)

    def invoke(self, project, args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                baton.cmd_monitor(project, args)
            finally:
                self.stderr = err.getvalue()

    def test_rerun_rebuilds_current_missing_or_partial_dossier(self):
        for status in ("report", "done", "violation"):
            for source, partial in (("monitor", False), ("dossier", True)):
                with self.subTest(status=status, source=source, partial=partial):
                    project, args = self.prepare(status, source, partial)
                    original_id = args.dossier
                    if status == "report":
                        project.update_state("t", lambda st: st["legs"][-1].pop("executor_status"))
                    elif status == "violation":
                        project.update_state("t", lambda st: st["legs"][-1].update(status="report"))
                    with patch.object(baton, "build_dossier", wraps=baton.build_dossier) as build:
                        self.invoke(project, args)
                    self.assertEqual(build.call_args.args[-1], status)
                    leg = baton.current_leg(project.load_state("t"))
                    rebuilt = leg["dossier"]["id"]
                    self.assertTrue(Path(baton.dossier_path(project, rebuilt), "manifest.json").is_file())
                    self.assertEqual(baton.verify_dossier(project, rebuilt), (True, ""))
                    self.assertEqual(leg["dossier"]["type"], baton.dossier_type_for(status))
                    self.assertEqual(leg["monitor"]["id"], rebuilt)
                    self.assertEqual(leg["monitor"]["status"], "running")
                    self.assertEqual(leg["monitor"]["pid"], 123456)
                    self.assertFalse(leg["reported"])
                    self.assertEqual(self.popen.call_args.args[0][-2:], ["t", rebuilt])
                    self.assertEqual(self.popen.call_args.kwargs["stdin"], baton.subprocess.DEVNULL)
                    if status == "done" and partial:
                        self.assertNotEqual(rebuilt, original_id)
                    self.popen.reset_mock()

    def test_rebuild_parse_failure_does_not_start_monitor_or_change_state(self):
        project, args = self.prepare()
        before = project.load_state("t")
        report = Path(before["legs"][-1]["result"]["artifact"])
        report.write_text('<h2>记忆候选</h2><pre data-memory-candidates="true">[{</pre>', encoding="utf-8")
        with self.assertRaises(SystemExit) as raised:
            self.invoke(project, args)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("记忆候选 JSON 无法解析", self.stderr)
        self.assertEqual(project.load_state("t"), before)
        self.popen.assert_not_called()

    def test_rebuild_unavailable_does_not_start_monitor(self):
        project, args = self.prepare()
        project.update_state("t", lambda st: st.update(config_snapshot={"mode": "lean"}))
        before = project.load_state("t")
        with self.assertRaises(SystemExit):
            self.invoke(project, args)
        self.assertEqual(project.load_state("t"), before)
        self.popen.assert_not_called()

    def test_existing_manifest_is_not_rebuilt(self):
        project, args = self.prepare()
        st = project.load_state("t")
        dossier_id = baton.build_dossier(project, "t", st, baton.current_leg(st), "report")
        manifest = Path(baton.dossier_path(project, dossier_id), "manifest.json")
        before = manifest.read_bytes()
        with patch.object(baton, "build_dossier") as build:
            self.invoke(project, args)
        build.assert_not_called()
        self.assertEqual(manifest.read_bytes(), before)
        self.popen.assert_called_once()

    def test_without_rerun_or_non_current_id_preserves_behavior(self):
        for rerun, matches in ((False, True), (True, False)):
            for partial in (False, True):
                with self.subTest(rerun=rerun, matches=matches, partial=partial):
                    project, args = self.prepare(partial=partial)
                    args.rerun = rerun
                    if not matches:
                        project.update_state("t", lambda st: st["legs"][-1]["monitor"].update(id="other"))
                    with patch.object(baton, "build_dossier") as build:
                        if partial:
                            self.invoke(project, args)
                            self.popen.assert_called_once()
                        else:
                            with self.assertRaises(SystemExit):
                                self.invoke(project, args)
                            self.assertIn("找不到卷宗", self.stderr)
                            self.popen.assert_not_called()
                    build.assert_not_called()
                    self.popen.reset_mock()

    def test_running_monitor_is_rejected_before_rebuilding(self):
        project, args = self.prepare()
        project.update_state("t", lambda st: st["legs"][-1]["monitor"].update(status="running"))
        with patch.object(baton, "build_dossier") as build, self.assertRaises(SystemExit):
            self.invoke(project, args)
        self.assertIn("monitor 已在运行", self.stderr)
        build.assert_not_called()
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
