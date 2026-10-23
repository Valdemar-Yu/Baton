import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "skills" / "baton" / "scripts"))
import baton


class DossierStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="baton-storage-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"BATON_HOME": str(self.base / "state"),
            "HOME": str(self.base / "home"), "USERPROFILE": str(self.base / "home"),
            "CODEX_HOME": str(self.base / "codex"), "BATON_DESKTOP_NOTIFY": "0"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.project = baton.Project(str(self.root))
        self.addCleanup(lambda: baton._unseal_dossier(str(self.base)))
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.root / "source.py").write_text("value = 1\n", encoding="utf-8")
        Path(self.project.dir).mkdir()
        baton.write_json(self.project.p("config.json"), {})
        baton.write_json(str(self.root / "baton.json"), {"executor": {"backend": "codex"},
                                                        "memory": {"backend": "local"}})
        brief = Path(self.project.task_dir("t")) / "brief.md"
        brief.parent.mkdir(parents=True)
        brief.write_text("# task\n", encoding="utf-8")
        self.project.record_id()
        self.project.update_state("t", lambda s: s.update(task="t", status="idle", legs=[],
            snapshot_store="shadow", scope={"allow": ["*"], "deny": [], "acceptance": [],
                                            "frozen": [], "verify": "none"}))

    def make_leg(self, task="t", n=1):
        store = self.project.store(task)
        ref = store.snapshot("storage test")
        leg = {"n": n, "status": "running", "start_ref": ref, "end_ref": ref,
               "probe_ref": ref, "probe_commit": ref, "started_at": baton.now(),
               "events": "", "result": {"status": "done", "summary": "done"},
               "verify": {"skipped": True}, "last_rule_terminal_at": baton.now()}
        self.project.update_state(task, lambda s: s.update(task=task, status="running", legs=[leg],
                                                          base_ref=ref, snapshot_store="shadow"))
        return leg

    def build(self, task="t", kind="done", n=1):
        leg = self.make_leg(task, n)
        st = self.project.load_state(task)
        if kind == "check":
            return baton.build_check_dossier(self.project, task, st, leg)
        if kind == "curation":
            return baton.build_curation_dossier(self.project, task)
        return baton.build_dossier(self.project, task, st, leg, kind)

    def dossier(self, ident):
        return Path(baton.dossier_path(self.project, ident))

    def test_omission_uses_user_ignores_and_keeps_tracked_files(self):
        (self.root / ".git" / "info" / "exclude").write_text("evidence/\n*.cache\n", encoding="utf-8")
        (self.root / "evidence").mkdir()
        (self.root / "evidence" / "raw.bin").write_bytes(b"x" * 4096)
        (self.root / "evidence" / "tracked.bin").write_bytes(b"tracked")
        (self.root / "result.cache").write_bytes(b"cache")
        (self.root / "normal.txt").write_bytes(b"keep")
        subprocess.run(["git", "-C", str(self.root), "add", "-f", "evidence/tracked.bin"], check=True)
        git_index = (self.root / ".git" / "index").read_bytes()
        # Shadow snapshots retain these files; dossier filtering is a separate operation.
        store = self.project.store("t")
        ref = store.snapshot("before ignore change")
        self.assertEqual(store.git("show", ref + ":evidence/raw.bin"), "x" * 4096)
        # A shadow exclusion can leave ignored files in an older snapshot; still omit them.
        with (Path(store.git_dir) / "info" / "exclude").open("a", encoding="utf-8") as stream:
            stream.write("\nevidence/\n*.cache\n")
        target = self.base / "copy"
        omissions = baton._dossier_code_snapshot(store, ref, str(target))
        self.assertEqual(omissions["files"], 2)
        self.assertEqual(omissions["bytes"], 4101)
        self.assertIn("git ignore", omissions["reason"])
        self.assertFalse((target / "evidence" / "raw.bin").exists())
        self.assertFalse((target / "result.cache").exists())
        self.assertEqual((target / "evidence" / "tracked.bin").read_bytes(), b"tracked")
        self.assertEqual((target / "normal.txt").read_bytes(), b"keep")
        self.assertEqual((self.root / ".git" / "index").read_bytes(), git_index)
        self.assertEqual(store.git("show", ref + ":evidence/raw.bin"), "x" * 4096)

    @unittest.skipUnless(os.name == "nt", "Windows ACL behaviour")
    def test_dossier_code_directories_keep_inherited_acl(self):
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
        store = self.project.store("t")
        ref = store.snapshot("subdirectory")
        # TemporaryDirectory is itself owner-only on Windows; use a parent with an ordinary ACL.
        parent = Path(tempfile.gettempdir()) / f"baton-acl-{os.getpid()}-{id(self)}"
        self.addCleanup(shutil.rmtree, parent, True)
        target = parent / "copy"
        baton._dossier_code_snapshot(store, ref, str(target))
        acl = subprocess.run(["icacls", str(target / "pkg")], capture_output=True, text=True,
                             encoding="mbcs").stdout
        self.assertIn("(I)", acl)

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH behaviour")
    def test_dossier_snapshot_records_paths_beyond_max_path(self):
        (self.root / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (self.root / "deep").mkdir()
        (self.root / "deep" / ("f" * 120 + ".txt")).write_text("tmp\n", encoding="utf-8")
        store = self.project.store("t")
        ref = store.snapshot("long path")
        parent = Path(tempfile.gettempdir()) / f"baton-long-{os.getpid()}-{id(self)}"
        self.addCleanup(shutil.rmtree, parent, True)
        target = parent / ("d" * max(1, 200 - len(str(parent))))
        omissions = baton._dossier_code_snapshot(store, ref, str(target))
        self.assertTrue((target / "keep.py").is_file())
        self.assertEqual(omissions["long_paths"]["files"], 1)
        self.assertEqual(omissions["long_paths"]["examples"], ["deep/" + "f" * 120 + ".txt"])

    def test_non_git_dossier_keeps_ignored_files(self):
        shutil.rmtree(self.root / ".git")
        (self.root / "evidence").mkdir()
        (self.root / "evidence" / "raw.bin").write_bytes(b"keep")
        store = self.project.store("t")
        ref = store.snapshot("non-git snapshot")
        (self.root / ".gitignore").write_text("evidence/\n", encoding="utf-8")
        target = self.base / "copy"
        omissions = baton._dossier_code_snapshot(store, ref, str(target))
        self.assertEqual((target / "evidence" / "raw.bin").read_bytes(), b"keep")
        self.assertEqual(omissions["files"], 0)

    def test_omission_in_project_below_git_root(self):
        sub = self.root / "subproject"
        sub.mkdir()
        (sub / "tracked.bin").write_bytes(b"tracked")
        (sub / "untracked.bin").write_bytes(b"ignored")
        subprocess.run(["git", "-C", str(self.root), "add", "subproject/tracked.bin"], check=True)
        (self.root / ".git" / "info" / "exclude").write_text("subproject/*.bin\n", encoding="utf-8")
        store = baton.Project(str(sub)).store()
        ref = store.snapshot("nested project")
        target = self.base / "nested-copy"
        omissions = baton._dossier_code_snapshot(store, ref, str(target))
        self.assertEqual(omissions["files"], 1)
        self.assertEqual(omissions["bytes"], 7)
        self.assertEqual((target / "tracked.bin").read_bytes(), b"tracked")
        self.assertFalse((target / "untracked.bin").exists())

    def test_all_code_dossier_manifests_record_omissions_and_owner(self):
        (self.root / ".git" / "info" / "exclude").write_text("evidence.bin\n", encoding="utf-8")
        (self.root / "evidence.bin").write_bytes(b"omitted")
        for kind in ("report", "done", "violation", "check", "curation"):
            with self.subTest(kind=kind):
                ident = self.build(kind=kind)
                manifest = baton.dossier_manifest(self.project, ident)
                self.assertEqual(manifest["task"], "t")
                self.assertEqual(manifest["leg"], 1)
                self.assertEqual(manifest["code_omissions"]["files"], 1)
                self.assertEqual(manifest["code_omissions"]["bytes"], 7)
                self.assertTrue(baton.verify_dossier(self.project, ident)[0])

    def test_git_ignore_failure_is_loud(self):
        store = self.project.store("t")
        ref = store.snapshot("ignore failure")
        original = subprocess.run
        def fail_ignore(command, **kwargs):
            if "check-ignore" in command:
                return subprocess.CompletedProcess(command, 128, "", "broken ignore query")
            return original(command, **kwargs)
        with patch.object(baton.subprocess, "run", side_effect=fail_ignore):
            with self.assertRaisesRegex(RuntimeError, "broken ignore query"):
                baton._dossier_code_snapshot(store, ref, str(self.base / "copy"))

    def test_repeated_checks_keep_one_copy_per_leg_and_preserve_records(self):
        first = self.build(kind="check")
        old = self.dossier(first)
        records = {p.name: p.read_bytes() for p in old.iterdir() if p.is_file() and p.name != "manifest.json"}
        original_hash = baton.dossier_manifest(self.project, first)["total_sha256"]
        st = self.project.load_state("t")
        second = baton.build_check_dossier(self.project, "t", st, baton.current_leg(st))
        self.assertFalse((old / "code").exists())
        self.assertTrue((self.dossier(second) / "code").exists())
        self.assertEqual({name: (old / name).read_bytes() for name in records}, records)
        self.assertEqual(baton.dossier_manifest(self.project, first)["total_sha256"], original_hash)
        self.assertEqual(baton.verify_dossier(self.project, first), (True, ""))
        self.assertIn("已清理", baton.verify_dossier(self.project, first, require_code=True)[1])
        third = self.build(kind="check", n=2)
        other = self.build(task="other", kind="check")
        self.assertTrue((self.dossier(second) / "code").exists())
        self.assertTrue((self.dossier(third) / "code").exists())
        self.assertTrue((self.dossier(other) / "code").exists())
        (old / "charter.md").chmod(0o644)
        (old / "charter.md").write_text("changed", encoding="utf-8")
        self.assertFalse(baton.verify_dossier(self.project, first)[0])

    def test_gc_dry_run_and_cleanup_preserve_active_tasks_and_ledger(self):
        closed = [self.build(kind="report"), self.build(kind="check"), self.build()]
        self.project.update_state("t", lambda s: s.update(status="closed"))
        active = self.build(task="active")
        self.project.update_state("active", lambda s: s.update(status="idle"))
        baton.append_ledger(self.project, {"type": "close", "task": "t", "dossier": closed[-1]})
        ledger = Path(baton.ledger_dir(self.project)) / "verdicts.jsonl"
        ledger_before = ledger.read_bytes()
        preserved = {ident: {p.name: p.read_bytes() for p in self.dossier(ident).iterdir()
                            if p.is_file()} for ident in closed + [active]}
        expected_bytes = sum(p.stat().st_size for ident in closed
                             for p in (self.dossier(ident) / "code").rglob("*") if p.is_file())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=True))
        self.assertIn(str(expected_bytes) + " 字节", output.getvalue())
        for ident in closed + [active]:
            self.assertTrue((self.dossier(ident) / "code").exists())
            self.assertEqual((self.dossier(ident) / "manifest.json").read_bytes(), preserved[ident]["manifest.json"])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False))
        self.assertIn("已清理 3 份卷宗代码", output.getvalue())
        self.assertIn(str(expected_bytes) + " 字节", output.getvalue())
        self.assertTrue((self.dossier(active) / "code").exists())
        self.assertEqual(ledger.read_bytes(), ledger_before)
        self.assertTrue(baton.verify_ledger(self.project))
        for ident in closed:
            self.assertFalse((self.dossier(ident) / "code").exists())
            for name, content in preserved[ident].items():
                if name != "manifest.json":
                    self.assertEqual((self.dossier(ident) / name).read_bytes(), content)
            self.assertTrue(baton.verify_dossier(self.project, ident)[0])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False))
        self.assertIn("已清理 0 份卷宗代码", output.getvalue())

    def test_gc_prunes_interrupted_dossier_without_manifest(self):
        ident = self.build(kind="check")
        root = self.dossier(ident)
        baton._unseal_dossier(str(root))
        (root / "manifest.json").unlink()
        self.project.update_state("t", lambda s: s.update(status="closed"))
        with contextlib.redirect_stdout(io.StringIO()):
            baton.cmd_gc(self.project, argparse.Namespace(dry_run=False))
        self.assertFalse((root / "code").exists())
        self.assertFalse((root / "manifest.json").exists())

    def test_prune_retries_transient_directory_not_empty(self):
        ident = self.build(kind="check")
        root = self.dossier(ident)
        real_rmtree = shutil.rmtree
        calls = []

        def flaky(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise OSError(145, "directory not empty")
            return real_rmtree(path, *args, **kwargs)

        with patch.object(baton.shutil, "rmtree", flaky), patch.object(baton.time, "sleep"):
            removed = baton._prune_dossier_code(self.project, ident, "closed")
        self.assertEqual(len(calls), 2)
        self.assertTrue(removed["files"] > 0)
        self.assertFalse((root / "code").exists())

    def test_gc_recognizes_old_report_ownership_from_ledger_and_state(self):
        reports = []
        for n in (1, 2):
            artifact = self.root / f"R00{n}-report.html"
            artifact.write_text("report", encoding="utf-8")
            leg = self.make_leg(n=n)
            leg["result"]["artifact"] = str(artifact)
            ident = baton.build_dossier(self.project, "t", self.project.load_state("t"), leg, "report")
            root = self.dossier(ident)
            baton._unseal_dossier(str(root))
            manifest = baton.dossier_manifest(self.project, ident)
            del manifest["task"]
            del manifest["leg"]
            baton.write_json(str(root / "manifest.json"), manifest)
            baton._seal_dossier(str(root))
            reports.append(ident)
        baton.append_ledger(self.project, {"type": "brief", "task": "t", "dossier": reports[0]})
        self.project.update_state("t", lambda s: s.update(status="closed", legs=[{"dossier": {"id": reports[1]}}]))
        with contextlib.redirect_stdout(io.StringIO()):
            removed = baton.prune_closed_task(self.project, "t")
        self.assertEqual(len(removed), 2)

    def test_gc_cli_obeys_baton_home(self):
        ident = self.build()
        self.project.update_state("t", lambda s: s.update(status="closed"))
        command = [sys.executable, str(ROOT / "skills/baton/scripts/baton.py"), "--root", str(self.root), "gc"]
        preview = subprocess.run(command + ["--dry-run"], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(preview.returncode, 0, preview.stderr)
        self.assertTrue((self.dossier(ident) / "code").exists())
        removed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn("已清理 1 份卷宗代码", removed.stdout)
        self.assertFalse((self.dossier(ident) / "code").exists())

    def test_baton_home_default_absolute_and_relative(self):
        with patch.dict(os.environ):
            os.environ.pop("BATON_HOME", None)
            self.assertEqual(Path(self.project.state_dir).parents[1], self.base / "home" / ".baton")
        for home in (str(self.base / "large drive"), "relative-state"):
            with patch.dict(os.environ, BATON_HOME=home):
                self.assertEqual(Path(self.project.state_dir).parents[1], Path(home).resolve())
                self.assertEqual(baton.memory_baton_home(), str(Path(home).resolve()))

    def test_windows_readonly_flags_cover_all_monitor_schemas(self):
        cfg = self.project.config
        for platform in ("nt", "posix"):
            for schema in ("monitor-brief", "monitor-check", "monitor-curation"):
                with self.subTest(platform=platform, schema=schema), \
                        patch.object(baton.platform_support.os, "name", platform), \
                        patch.object(baton.platform_support, "executable", return_value="codex"):
                    command = baton._monitor_codex_command(self.project, "R001", cfg,
                        "out.json", schema_path=schema + ".schema.json")
                self.assertEqual('windows.sandbox="unelevated"' in command, platform == "nt")
                self.assertIn("--ignore-user-config", command)
                self.assertEqual(command[command.index("-s") + 1], "read-only")

    def test_probe_requires_reading_dossier_file(self):
        def fake_run(command, **kwargs):
            if "--ignore-user-config" in command:
                path = Path(kwargs["cwd"]) / "charter.md"
                secret = path.read_text(encoding="utf-8").strip()
                self.assertNotIn(secret, kwargs["input"])
                readable = 'windows.sandbox="unelevated"' in command
                Path(command[command.index("-o") + 1]).write_text(secret if readable else "NONE", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "" if readable else "rejected: blocked by policy")
            init = {"type": "system", "subtype": "init", "tools": ["Read", "Grep", "Glob", "StructuredOutput"],
                    "mcp_servers": [], "permissionMode": "dontAsk"}
            return subprocess.CompletedProcess(command, 0, json.dumps(init), "")
        for config, expected in (([], False), (["-c", 'windows.sandbox="unelevated"'], True)):
            messages = []
            with patch.object(baton.platform_support, "codex_readonly_config", return_value=config), \
                    patch.object(baton.platform_support, "executable", return_value="codex"), \
                    patch.object(baton.subprocess, "run", side_effect=fake_run):
                result = baton.run_monitor_probe(self.project, self.project.config,
                                                 lambda *args: messages.append(args))
            self.assertEqual(result["codex_dossier_read"], expected)
            self.assertEqual(any(level == "FAIL" for level, _ in messages), not expected)
            if not expected:
                self.assertIn("基础设施错误", result["codex_error"])

    def unreadable_result(self):
        return {"conclusion": "drifting", "summary": "无法读取卷宗 charter.md，读取被策略阻止",
                "findings": [{"id": "F1", "category": "missing_record", "severity": "blocking",
                              "description": "无法读取 charter.md", "evidence": []}]}

    def test_unreadable_check_does_not_record_executor_drift(self):
        ident = self.build(kind="check")
        self.assertFalse(baton.apply_monitor_check_result(self.project, "t", ident, self.unreadable_result(), 1))
        st = self.project.load_state("t")
        self.assertFalse(st.get("pending_drift"))
        self.assertEqual(st["legs"][0]["monitor_check"]["status"], "failed")
        self.assertIn("基础设施错误", st["legs"][0]["monitor_check"]["error"])
        self.assertFalse(any(r.get("type") == "check" for r in baton.ledger_records(self.project)))
        self.assertEqual(baton._monitor_read_error({"summary": "ok", "findings": [{"category": "missing_record",
            "description": "executor 没有提供 report.html", "evidence": []}]}), "")

    def test_policy_text_in_successful_read_is_not_infrastructure_failure(self):
        for findings in (None, 1, "invalid", [None]):
            self.assertEqual(baton._monitor_read_error({"summary": None, "findings": findings}), "")
        item = {"type": "command_execution", "status": "completed", "exit_code": 0,
                "aggregated_output": "executor log: rejected: blocked by policy"}
        self.assertEqual(baton._monitor_read_error(None, json.dumps({"type": "item.completed", "item": item})), "")
        item.update(status="failed", exit_code=1)
        self.assertIn("基础设施错误", baton._monitor_read_error(None, json.dumps({"type": "item.completed", "item": item})))

    def test_infrastructure_failure_stops_retry_and_keeps_executor_running(self):
        ident = self.build(kind="check")
        failure = (None, "monitor 基础设施错误：无法读取卷宗", 1, "infrastructure")
        with patch.object(baton, "run_monitor_check_once", return_value=failure) as run:
            self.assertFalse(baton.run_monitor_check_lifecycle(self.project, "t", ident, self.project.config, 1))
            self.assertEqual(run.call_count, 1)
        st = self.project.load_state("t")
        self.assertEqual(st["legs"][0]["status"], "running")
        self.assertFalse(st.get("pending_drift"))
        ident = self.build(kind="report")
        with patch.object(baton, "run_monitor_once", return_value=failure) as run:
            self.assertFalse(baton.run_monitor_lifecycle(self.project, "t", ident, "report", self.project.config, 1, "report"))
            self.assertEqual(run.call_count, 1)
        self.assertEqual(self.project.load_state("t")["legs"][0]["monitor"]["status"], "failed")
        self.assertEqual(baton.ledger_records(self.project), [])

    def test_policy_blocked_monitor_is_infrastructure_failure(self):
        ident = self.build(kind="check")
        def fake_popen(command, **kwargs):
            Path(command[command.index("-o") + 1]).write_text(json.dumps(self.unreadable_result()), encoding="utf-8")
            kwargs["stderr"].write("rejected: blocked by policy\n")
            return SimpleNamespace(pid=123, returncode=0, communicate=lambda *a, **kw: (None, None))
        with patch.object(baton.platform_support, "detached_popen", side_effect=fake_popen), \
                patch.object(baton.platform_support, "executable", return_value="codex"):
            for run in (baton.run_monitor_check_once,
                        lambda p, t, d, cfg, prompt: baton.run_monitor_once(p, t, d, "report", cfg, prompt)):
                value, error, _, kind = run(self.project, "t", ident, self.project.config, "read")
                self.assertIsNone(value)
                self.assertEqual(kind, "infrastructure")
                self.assertIn("blocked by policy", error)


if __name__ == "__main__":
    unittest.main()
